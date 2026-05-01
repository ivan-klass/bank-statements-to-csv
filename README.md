# bank-statements-to-csv

Three-step pipeline that produces `transactions.csv` from Raiffeisen (Serbia) bank statement PDFs delivered by email.

```
Gmail  ──►  email_attachments/*.pdf  ──►  extracted_transactions/*.json  ──►  transactions.csv
        (1)                          (2)                                  (3)
```

## 0. One-time setup

Install [uv](https://docs.astral.sh/uv/) if you don't already have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Create the virtualenv and install dependencies:

```bash
uv sync
```

This creates `.venv/` and installs everything pinned in `uv.lock`. Run scripts with `uv run`, e.g. `uv run python download_email_attachments.py ...`, or activate the venv with `source .venv/bin/activate`.

Get Gmail API credentials (needed only for step 1):

1. Go to https://console.cloud.google.com/
2. Create a project (or pick an existing one).
3. Enable the **Gmail API** under *APIs & Services → Enable APIs*.
4. Create OAuth 2.0 credentials: *APIs & Services → Credentials → Create Credentials → OAuth client ID*, application type **Desktop app**.
5. Download the JSON and save it as `credentials.json` in the repo root.

On the first run a browser window will open to authorize access; the token is cached in `token.json` for subsequent runs.

## 1. Download PDF statements from Gmail

```bash
uv run python download_email_attachments.py \
    --from raiffeisenonline@raiffeisenbank.rs \
    --keywords "izvod tekucem <your account number>" \
    --file-extensions pdf \
    --output-dir email_attachments
```

Each argument can also be supplied via env var (`EMAIL_FROM`, `EMAIL_KEYWORDS`, `EMAIL_FILE_EXTENSIONS`, `EMAIL_OUTPUT_DIR`) or the script will prompt for it interactively. Files are saved with a `YYYY-MM-DD_` email date prefix; already-downloaded files are skipped.

## 2. Extract transactions from each PDF

```bash
uv run python extract_pdf_transactions.py email_attachments/ --output-dir extracted_transactions/
```

Parses every PDF in the input directory and writes one JSON file per statement, containing account metadata and a list of transactions. Add `-v` for verbose logging.

## 3. Merge JSON files into a flat CSV

```bash
uv run python transactions_to_csv.py extracted_transactions/ -o transactions.csv
```

Produces `transactions.csv` with one row per transaction plus source metadata (account number, statement number, currency). Numeric columns use a comma decimal separator for friendlier viewing in Serbian-locale spreadsheets.

## Business accounts (XML statements)

Raiffeisen Serbia business accounts ship statements as XML alongside PDF. XML is preferred when available — it's structured, so extraction is exact rather than regex-based. The pipeline is the same three steps, just with a different keyword filter, the XML extractor, and `-f xml` on the final step:

```bash
uv run python download_email_attachments.py \
    --from raiffeisenonline@raiffeisenbank.rs \
    --keywords "Izvod po dinarskom <your business account number>" \
    --file-extensions xml \
    --output-dir email_attachments/biz

uv run python extract_xml_transactions.py email_attachments/biz/ \
    --output-dir extracted_transactions/biz/

uv run python transactions_to_csv.py extracted_transactions/biz/ -f xml -o transactions_biz.csv
```

## Files in this repo

- `download_email_attachments.py` — Gmail downloader (step 1)
- `extract_pdf_transactions.py` — PDF → JSON parser (step 2, personal accounts)
- `extract_xml_transactions.py` — XML → JSON parser (step 2, business accounts)
- `transactions_to_csv.py` — JSON → CSV merger (step 3, `-f pdf|xml`)
- `pyproject.toml`, `uv.lock` — Python dependencies (managed by `uv`)
- `credentials.json`, `token.json` — Gmail OAuth (gitignored)
- `email_attachments/`, `extracted_transactions/`, `transactions.csv` — pipeline outputs (gitignored)

## Troubleshooting

**Gmail token expired or auth errors in step 1.** Delete `token.json` and re-run the script — a browser window will open to re-authorize, and a fresh token will be cached.

## Development

[ruff](https://docs.astral.sh/ruff/) is used for linting and formatting. It is installed as a dev dependency by `uv sync`.

```bash
uv run ruff check .          # lint
uv run ruff check --fix .    # lint and apply auto-fixes
uv run ruff format .         # format
uv run ruff format --check . # check formatting without writing
```

Configuration lives in the `[tool.ruff]` sections of `pyproject.toml`.
