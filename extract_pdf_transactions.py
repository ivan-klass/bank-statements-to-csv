#!/usr/bin/env python3
"""
Extract structured transaction data from bank PDF statements.

Reads one or more PDF files (Serbian bank statements, e.g. Raiffeisen)
and outputs JSON with account metadata and transaction rows.

Usage:
    python extract_pdf_transactions.py statement.PDF
    python extract_pdf_transactions.py ./email_attachments/ -o ./extracted/
    python extract_pdf_transactions.py statement.PDF -v

Output is one JSON object per PDF. When --output-dir is given, each PDF
produces a .json file in that directory; otherwise output goes to stdout.
"""

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pdfplumber

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StatementMetadata:
    bank_name: str | None = None
    account_number: str | None = None
    statement_number: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    currency: str | None = None
    account_holder: str | None = None
    opening_balance: float | None = None
    closing_balance: float | None = None


@dataclass
class Transaction:
    posting_date: str | None = None
    value_date: str | None = None
    card_number: str | None = None
    description: str | None = None
    amount_ref: float | None = None
    currency_ref: str | None = None
    amount_orig: float | None = None
    currency_orig: str | None = None
    debit: float | None = None
    credit: float | None = None
    balance: float | None = None


@dataclass
class StatementResult:
    source_file: str
    metadata: StatementMetadata
    transactions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    pages_processed: int = 0


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{2})[./](\d{2})[./](\d{4})")


def parse_date(text: str) -> str | None:
    """Parse DD.MM.YYYY into ISO YYYY-MM-DD."""
    if not text:
        return None
    m = _DATE_RE.search(text.strip())
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def parse_amount(text: str) -> float | None:
    """Parse a numeric amount, auto-detecting format.

    Handles both international (3,500.00) and Serbian (3.500,00) formats
    by checking which separator appears last.
    """
    if not text:
        return None
    text = re.sub(r"[A-Za-z\s]", "", text.strip())
    if not text:
        return None

    last_dot = text.rfind(".")
    last_comma = text.rfind(",")

    if last_dot > last_comma:
        # International: comma=thousands, dot=decimal (3,500.00)
        text = text.replace(",", "")
    elif last_comma > last_dot:
        # Serbian: dot=thousands, comma=decimal (3.500,00)
        text = text.replace(".", "").replace(",", ".")

    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Transaction line regexes (tried in order, first match wins)
# ---------------------------------------------------------------------------

# Format A: Card + two currency amounts (foreign-currency statements)
# 03.02.2026 03.02.2026 1354 CLAUDE.AI SUBSCRIPTION USA 85.78 EUR 100.00 USD 85.78 0.00 556.07
_TXN_CARD_2CUR = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d+)\s+"
    r"(.+?)\s+"
    r"([\d,.]+)\s+([A-Z]{3})\s+"
    r"([\d,.]+)\s+([A-Z]{3})\s+"
    r"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s*$"
)

# Format B: Card + one currency amount (dinar statements, ref_valuti is 0.00)
# 02.03.2026 04.03.2026 1354 HLEB I KIFLE 66 - METR SRB 0.00 385.00 RSD 385.00 0.00 58,950.45
_TXN_CARD_1CUR = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d+)\s+"
    r"(.+?)\s+"
    r"([\d,.]+)\s+"
    r"([\d,.]+)\s+([A-Z]{3})\s+"
    r"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s*$"
)

# Format C: Bank transfer — no card number, no currency codes
# 04.03.2026 04.03.2026 SMART ATM - 000653, 0.00 12,000.00 0.00 41,665.92
_TXN_TRANSFER = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4})\s+"
    r"(\d{2}\.\d{2}\.\d{4})\s+"
    r"(.+?)\s+"
    r"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s*$"
)

# Header line that precedes transaction data on each page
_HEADER_RE = re.compile(r"Datum prijema", re.IGNORECASE)

# Summary/closing balance line
_STANJE_RE = re.compile(r"^STANJE\s+([\d,.]+)\s*$")

# Footer boilerplate to skip
_FOOTER_RE = re.compile(
    r"^Za dinarske transakcije|^Za devizne transakcije|"
    r"^Raiffeisen banke|^Poštovani korisniče|"
    r"^prodajni/kupovni|^važi na dan|"
    r"^Nerealizovani|^\d+ \d+ \d+\.$"
)


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------


def extract_metadata(text: str) -> StatementMetadata:
    """Extract statement metadata from the first-page text using regex."""
    meta = StatementMetadata()

    if not text:
        return meta

    # Account number (18-digit Serbian format, with or without dashes)
    m = re.search(r"\b(\d{3})-?(\d{13})-?(\d{2})\b", text)
    if m:
        meta.account_number = f"{m.group(1)}{m.group(2)}{m.group(3)}"

    # Statement number: "Izvod broj: 2"
    m = re.search(r"izvod\s+br(?:oj)?\.?\s*:?\s*(\d+)", text, re.IGNORECASE)
    if m:
        meta.statement_number = m.group(1)

    # Date range: "Od DD.MM.YYYY do DD.MM.YYYY"
    m = re.search(
        r"[Oo]d\s+(\d{2}\.\d{2}\.\d{4})\s+do\s+(\d{2}\.\d{2}\.\d{4})",
        text,
    )
    if m:
        meta.period_start = parse_date(m.group(1))
        meta.period_end = parse_date(m.group(2))

    # Currency: "Valuta: EUR"
    m = re.search(r"[Vv]aluta:\s*([A-Z]{3})", text)
    if m:
        meta.currency = m.group(1)

    # Bank name
    for pattern in [
        r"(Raiffeisen\s+banka?\b[^,\n]*)",
        r"(OTP\s+banka?\b[^,\n]*)",
        r"(Banca\s+Intesa\b[^,\n]*)",
        r"(Unicredit\s+Bank\b[^,\n]*)",
        r"(Erste\s+Bank\b[^,\n]*)",
        r"(Komercijalna\s+banka?\b[^,\n]*)",
    ]:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            meta.bank_name = m.group(1).strip()
            break

    # Opening balance: "Prethodno stanje: 641.85"
    m = re.search(
        r"(?:prethodno|po[čc]etno)\s+stanje:\s*([\d,.]+)",
        text,
        re.IGNORECASE,
    )
    if m:
        meta.opening_balance = parse_amount(m.group(1))

    # Account holder: may be on its own line or sharing a line with bank name
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Skip lines with bank/metadata keywords
        if re.search(
            r"raiffeisen|banka|đorđa|stanojevića|beograd|tel:|fax:|"
            r"internet|http|izvod|valuta:|strana:|račun|prethodno|"
            r"stanje|datum|NEDIĆ|REON:|PAK:|za\s+dinarske|za\s+devizne|"
            r"kupovni\s+kurs|prodajni",
            line,
            re.IGNORECASE,
        ):
            # Check if name is before bank name on the same line
            for bank_kw in [
                "Raiffeisen",
                "OTP",
                "Banca Intesa",
                "Unicredit",
                "Erste",
                "Komercijalna",
            ]:
                idx = line.find(bank_kw)
                if idx > 0:
                    candidate = line[:idx].strip()
                    if candidate and re.match(r"^[A-ZČĆŽŠĐ][A-ZČĆŽŠĐa-zčćžšđ ]+$", candidate):
                        meta.account_holder = candidate
            continue
        # Standalone name line: ALL CAPS, no digits, reasonable length
        if re.match(r"^[A-ZČĆŽŠĐ][A-ZČĆŽŠĐa-zčćžšđ ]+$", line) and len(line) < 50:
            meta.account_holder = line
            break
        if meta.account_holder:
            break

    return meta


# ---------------------------------------------------------------------------
# Transaction extraction from text
# ---------------------------------------------------------------------------


def _try_parse_transaction(line: str) -> Transaction | None:
    """Try to parse a line as a transaction using multiple format regexes."""
    # Format A: card + two currency pairs (foreign-currency statements)
    m = _TXN_CARD_2CUR.match(line)
    if m:
        return Transaction(
            posting_date=parse_date(m.group(1)),
            value_date=parse_date(m.group(2)),
            card_number=m.group(3),
            description=m.group(4).strip(),
            amount_ref=parse_amount(m.group(5)),
            currency_ref=m.group(6),
            amount_orig=parse_amount(m.group(7)),
            currency_orig=m.group(8),
            debit=parse_amount(m.group(9)),
            credit=parse_amount(m.group(10)),
            balance=parse_amount(m.group(11)),
        )

    # Format B: card + one currency (dinar statements)
    m = _TXN_CARD_1CUR.match(line)
    if m:
        return Transaction(
            posting_date=parse_date(m.group(1)),
            value_date=parse_date(m.group(2)),
            card_number=m.group(3),
            description=m.group(4).strip(),
            amount_ref=parse_amount(m.group(5)),
            amount_orig=parse_amount(m.group(6)),
            currency_orig=m.group(7),
            debit=parse_amount(m.group(8)),
            credit=parse_amount(m.group(9)),
            balance=parse_amount(m.group(10)),
        )

    # Format C: bank transfer — no card, no currencies
    m = _TXN_TRANSFER.match(line)
    if m:
        return Transaction(
            posting_date=parse_date(m.group(1)),
            value_date=parse_date(m.group(2)),
            description=m.group(3).strip(),
            amount_ref=parse_amount(m.group(4)),
            debit=parse_amount(m.group(5)),
            credit=parse_amount(m.group(6)),
            balance=parse_amount(m.group(7)),
        )

    return None


def extract_transactions_from_text(text: str, warnings: list, page_num: int) -> list[Transaction]:
    """Parse transactions from page text using regex line matching."""
    transactions: list[Transaction] = []
    in_data_section = False

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        # Detect the header row to know we're in the data section
        if _HEADER_RE.search(line):
            in_data_section = True
            continue

        # Skip the second header line ("Datum transakcije izvršenja ...")
        if in_data_section and not transactions and re.match(r"^Datum transakcije", line):
            continue

        if not in_data_section:
            continue

        # Stop at footer
        if _FOOTER_RE.match(line):
            break

        # Try matching a transaction line (multiple formats)
        txn = _try_parse_transaction(line)
        if txn:
            transactions.append(txn)
            log.debug(
                "Transaction: %s %s debit=%s credit=%s",
                txn.posting_date,
                txn.description,
                txn.debit,
                txn.credit,
            )
            continue

        # Summary line
        m = _STANJE_RE.match(line)
        if m:
            log.debug("Closing balance: %s", m.group(1))
            continue

        # Continuation line: append to last transaction's description
        if transactions:
            # Strip trailing "Kurs:" (exchange rate label, not description)
            clean = re.sub(r"\s*Kurs:\s*$", "", line).strip()
            if not clean:
                continue
            prev = transactions[-1]
            prev.description = f"{prev.description} {clean}"
            log.debug("  continuation: %s", clean)
            continue

    return transactions


# ---------------------------------------------------------------------------
# PDF processing
# ---------------------------------------------------------------------------


def process_pdf(file_path: Path) -> StatementResult:
    """Process a single PDF file and extract metadata + transactions."""
    result = StatementResult(
        source_file=file_path.name,
        metadata=StatementMetadata(),
    )

    all_transactions: list[Transaction] = []

    with pdfplumber.open(file_path) as pdf:
        result.pages_processed = len(pdf.pages)

        for page_num, page in enumerate(pdf.pages, 1):
            page_text = page.extract_text() or ""

            if page_num == 1:
                result.metadata = extract_metadata(page_text)

            txns = extract_transactions_from_text(
                page_text,
                result.warnings,
                page_num,
            )
            all_transactions.extend(txns)

            if not txns and page_text.strip() and _HEADER_RE.search(page_text):
                result.warnings.append(
                    f"Page {page_num}: found header but extracted 0 transactions"
                )

    # Set closing balance from last transaction
    if all_transactions and result.metadata.closing_balance is None:
        result.metadata.closing_balance = all_transactions[-1].balance

    result.transactions = all_transactions
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def result_to_dict(result: StatementResult) -> dict:
    """Convert a StatementResult to a JSON-serializable dict."""
    d = asdict(result)

    txns = result.transactions
    debits = [t.debit for t in txns if t.debit]
    credits = [t.credit for t in txns if t.credit]

    d["summary"] = {
        "total_transactions": len(txns),
        "total_debits": len(debits),
        "total_credits": len(credits),
        "sum_debits": round(sum(debits), 2),
        "sum_credits": round(sum(credits), 2),
        "pages_processed": result.pages_processed,
        "extraction_warnings": result.warnings,
    }
    d.pop("warnings", None)

    return d


def write_json(data: dict, output_path: Path | None = None):
    """Write JSON to a file or stdout."""
    text = json.dumps(data, indent=2, ensure_ascii=False)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        log.info("Wrote %s", output_path)
    else:
        print(text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def collect_pdf_files(input_path: Path) -> list[Path]:
    """Resolve input path to a list of PDF file paths."""
    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            print(f"Error: {input_path} is not a PDF file.", file=sys.stderr)
            sys.exit(1)
        return [input_path]

    if input_path.is_dir():
        pdfs = sorted(p for p in input_path.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        if not pdfs:
            print(f"Error: no PDF files found in {input_path}", file=sys.stderr)
            sys.exit(1)
        return pdfs

    print(f"Error: {input_path} does not exist.", file=sys.stderr)
    sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract structured transaction data from bank PDF statements. "
        "Reads one or more PDF files and outputs JSON with metadata "
        "and transactions.",
    )
    parser.add_argument(
        "input",
        help="Path to a PDF file or directory containing PDF files.",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        help="Directory to write JSON output files. If omitted, prints to stdout. "
        "One .json file is created per input PDF.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging to stderr.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("pdfminer").setLevel(logging.WARNING)

    input_path = Path(args.input)
    pdf_files = collect_pdf_files(input_path)
    output_dir = Path(args.output_dir) if args.output_dir else None

    print(f"Found {len(pdf_files)} PDF file(s) to process.", file=sys.stderr)

    errors = 0
    for pdf_file in pdf_files:
        print(f"Processing: {pdf_file.name} ...", file=sys.stderr)
        try:
            result = process_pdf(pdf_file)
            data = result_to_dict(result)

            if output_dir:
                out_path = output_dir / (pdf_file.stem + ".json")
                write_json(data, out_path)
                n = data["summary"]["total_transactions"]
                print(f"  -> {out_path} ({n} transactions)", file=sys.stderr)
            else:
                write_json(data)

        except Exception as exc:
            errors += 1
            print(f"  Error processing {pdf_file.name}: {exc}", file=sys.stderr)
            log.debug("Traceback:", exc_info=True)

    if errors:
        print(f"\n{errors} file(s) failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
