#!/usr/bin/env python3
"""
Download email attachments from Gmail by sender, keywords,
and file extension.

Usage:
    python download_email_attachments.py --from raiffeisenbank.rs -k <your-account-number> -e xml
    python download_email_attachments.py -k "monthly statement" -e xml,pdf
    python download_email_attachments.py --from billing@example.com -k invoice -e pdf

Each parameter is resolved in order: CLI arg > environment variable > interactive prompt.

Environment variables:
    EMAIL_FROM              sender to filter on (Gmail 'from:' syntax)
    EMAIL_KEYWORDS          keywords to search for in the email
    EMAIL_FILE_EXTENSIONS   comma-separated list of file extensions
    EMAIL_OUTPUT_DIR        directory to save downloaded files into

First run:
1. Go to https://console.cloud.google.com/
2. Create a project (or pick an existing one)
3. Enable Gmail API: APIs & Services -> Enable APIs -> Gmail API
4. Create OAuth 2.0 credentials:
   APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
   - Application type: Desktop app
   - Download the JSON file and rename it to credentials.json
5. Place credentials.json next to this script
6. Run: python download_email_attachments.py
7. Authorize in the browser — the script will save the token to token.json

Subsequent runs will reuse the saved token.json.
"""

import argparse
import base64
import datetime
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Scopes: Gmail read-only access
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def resolve(cli_value, env_name, prompt_text):
    """Resolve a parameter: CLI arg > env var > interactive prompt."""
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value
    try:
        value = input(prompt_text).strip()
    except EOFError:
        value = ""
    if not value:
        print("❌ Required parameter not provided.", file=sys.stderr)
        sys.exit(1)
    return value


def parse_extensions(raw):
    """Parse a comma-separated list of extensions.

    Strips whitespace and leading dots, lowercases.
    Example: 'xml, PDF, .txt' -> ['xml', 'pdf', 'txt']
    """
    return [e.strip().lstrip(".").lower() for e in raw.split(",") if e.strip()]


def matches_extension(filename, extensions):
    """True if filename ends with any of the given extensions (case-insensitive)."""
    lowered = filename.lower()
    return any(lowered.endswith("." + ext) for ext in extensions)


def build_query(from_addr, keywords, extensions):
    """Build a Gmail search query.

    Uses the keywords as plain search terms so Gmail performs a broad
    keyword match.  Adds a 'filename:' hint for each extension so Gmail
    pre-filters on the server side.
    """
    parts = [f"from:{from_addr}", keywords, "has:attachment"]
    if extensions:
        clauses = " OR ".join(f"filename:{ext}" for ext in extensions)
        parts.append(f"({clauses})" if len(extensions) > 1 else clauses)
    return " ".join(parts)


def get_gmail_service():
    """Authorize and build a Gmail API client."""
    creds = None
    token_path = Path("token.json")
    creds_path = Path("credentials.json")

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                print("❌ credentials.json not found!")
                print(
                    "   Download it from Google Cloud Console (see instructions at the top of this script)"
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
        print("✅ Token saved to token.json")

    return build("gmail", "v1", credentials=creds)


def search_messages(service, query):
    """Search for all messages matching the query."""
    messages = []
    page_token = None

    while True:
        params = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token

        result = service.users().messages().list(**params).execute()
        batch = result.get("messages", [])
        messages.extend(batch)

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    print(f"📧 Messages found: {len(messages)}")
    return messages


def download_attachments(service, messages, output_dir, extensions):
    """Download attachments whose extensions are in the list."""
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    skipped = 0

    for i, msg_meta in enumerate(messages, 1):
        msg_id = msg_meta["id"]
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()

        # Extract email date for filename prefix
        internal_ts = int(msg["internalDate"]) / 1000
        email_date = datetime.datetime.fromtimestamp(internal_ts, tz=datetime.UTC).strftime(
            "%Y-%m-%d"
        )

        # Look for matching attachments in message parts
        parts = msg["payload"].get("parts", [])
        for part in parts:
            filename = part.get("filename", "")
            if not filename or not matches_extension(filename, extensions):
                continue

            att_id = part["body"].get("attachmentId")
            if not att_id:
                continue

            # Prefix filename with email date to avoid collisions
            safe_name = filename.replace("/", "_").replace("\\", "_")
            safe_name = f"{email_date}_{safe_name}"
            out_path = output_dir / safe_name

            if out_path.exists():
                skipped += 1
                continue

            # Download the attachment
            att = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=msg_id, id=att_id)
                .execute()
            )

            data = base64.urlsafe_b64decode(att["data"])
            out_path.write_bytes(data)
            downloaded += 1

            print(f"  [{i}/{len(messages)}] 📥 {safe_name}")

    print(f"\n✅ Downloaded: {downloaded} files")
    if skipped:
        print(f"⏭️  Skipped (already present): {skipped}")
    print(f"📁 Directory: {output_dir.resolve()}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download email attachments from Gmail by sender, "
        "subject substring, and file extension. "
        "Each parameter: CLI arg > env var > interactive prompt.",
    )
    parser.add_argument(
        "--from",
        "-f",
        dest="from_",
        help="Sender to filter on, Gmail 'from:' syntax (env: EMAIL_FROM).",
    )
    parser.add_argument(
        "--keywords",
        "-k",
        help="Keywords to search for in the email (env: EMAIL_KEYWORDS).",
    )
    parser.add_argument(
        "--file-extensions",
        "-e",
        help="Comma-separated list of file extensions to download, "
        "e.g. 'xml', 'pdf', 'xml,pdf' (env: EMAIL_FILE_EXTENSIONS).",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        help="Directory to save downloaded files into (env: EMAIL_OUTPUT_DIR).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    from_addr = resolve(args.from_, "EMAIL_FROM", "Enter sender (from): ")
    keywords = resolve(args.keywords, "EMAIL_KEYWORDS", "Enter search keywords: ")
    ext_raw = resolve(
        args.file_extensions,
        "EMAIL_FILE_EXTENSIONS",
        "Enter file extensions (comma-separated, e.g. xml,pdf): ",
    )
    output_dir = Path(resolve(args.output_dir, "EMAIL_OUTPUT_DIR", "Enter output directory: "))

    extensions = parse_extensions(ext_raw)
    if not extensions:
        print("❌ No valid file extensions provided.", file=sys.stderr)
        sys.exit(1)

    query = build_query(from_addr, keywords, extensions)

    print("📧 Gmail Attachment Downloader")
    print(f"   From:       {from_addr}")
    print(f"   Keywords:   {keywords}")
    print(f"   Extensions: {', '.join(extensions)}")
    print(f"   Output:     {output_dir}")
    print(f"   Query:      {query}\n")

    service = get_gmail_service()
    messages = search_messages(service, query)

    if not messages:
        print("No messages found.")
        return

    download_attachments(service, messages, output_dir, extensions)

    # Summary: count files in the output dir matching the same extensions
    matched = [
        f for f in output_dir.iterdir() if f.is_file() and matches_extension(f.name, extensions)
    ]
    print(f"\n📊 Total matching files in directory: {len(matched)}")


if __name__ == "__main__":
    main()
