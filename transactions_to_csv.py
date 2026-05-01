#!/usr/bin/env python3
"""
Merge transactions from extracted JSON files into a single flat CSV.

Supports two input formats:
  - pdf (default): output of extract_pdf_transactions.py
  - xml:           output of extract_xml_transactions.py

Usage:
    python transactions_to_csv.py                          # reads extracted_transactions/, writes to stdout
    python transactions_to_csv.py ./extracted_transactions/ -o transactions.csv
    python transactions_to_csv.py ./extracted_transactions/biz/ -f xml -o biz.csv
"""

import argparse
import contextlib
import csv
import json
import sys
from pathlib import Path

METADATA_COLUMNS = ["source_file", "account_number", "statement_number", "currency"]

PDF_TRANSACTION_COLUMNS = [
    "posting_date",
    "value_date",
    "card_number",
    "description",
    "amount_ref",
    "currency_ref",
    "amount_orig",
    "currency_orig",
    "debit",
    "credit",
    "balance",
]
PDF_NUMERIC_COLUMNS = {"amount_ref", "amount_orig", "debit", "credit", "balance"}

XML_TRANSACTION_COLUMNS = [
    "value_date",
    "counterparty",
    "counterparty_account",
    "description",
    "debit",
    "credit",
    "balance",
]
XML_NUMERIC_COLUMNS = {"debit", "credit", "balance"}

FORMATS = {
    "pdf": (PDF_TRANSACTION_COLUMNS, PDF_NUMERIC_COLUMNS),
    "xml": (XML_TRANSACTION_COLUMNS, XML_NUMERIC_COLUMNS),
}


def iter_transactions(input_dir: Path, txn_columns: list, numeric_columns: set):
    """Yield flat dicts (metadata + transaction fields) from all JSON files."""
    for json_path in sorted(input_dir.glob("*.json")):
        with open(json_path) as f:
            data = json.load(f)

        meta = data.get("metadata", {})
        prefix = {
            "source_file": data.get("source_file", json_path.name),
            "account_number": meta.get("account_number"),
            "statement_number": meta.get("statement_number"),
            "currency": meta.get("currency"),
        }

        for txn in data.get("transactions", []):
            row = {**prefix, **{k: txn.get(k) for k in txn_columns}}
            for col in numeric_columns:
                v = row[col]
                if isinstance(v, int | float):
                    row[col] = str(v).replace(".", ",")
            yield row


def main():
    parser = argparse.ArgumentParser(description="Merge JSON transactions into a flat CSV")
    parser.add_argument(
        "input_dir",
        nargs="?",
        default="extracted_transactions",
        help="Directory containing extracted JSON files (default: extracted_transactions)",
    )
    parser.add_argument("-o", "--output", default=None, help="Output CSV path (default: stdout)")
    parser.add_argument(
        "-f",
        "--format",
        choices=sorted(FORMATS),
        default="pdf",
        help="Input JSON format (default: pdf)",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.is_dir():
        print(f"Error: {input_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    txn_columns, numeric_columns = FORMATS[args.format]
    all_columns = METADATA_COLUMNS + txn_columns

    with contextlib.ExitStack() as stack:
        out = stack.enter_context(open(args.output, "w", newline="")) if args.output else sys.stdout
        writer = csv.DictWriter(out, fieldnames=all_columns)
        writer.writeheader()
        count = 0
        for row in iter_transactions(input_dir, txn_columns, numeric_columns):
            writer.writerow(row)
            count += 1
        print(f"Wrote {count} transactions", file=sys.stderr)


if __name__ == "__main__":
    main()
