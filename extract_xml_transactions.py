#!/usr/bin/env python3
"""
Extract structured transaction data from bank XML statements.

Reads one or more XML files (Serbian bank statements, Raiffeisen
TransakcioniRacunPrivredaIzvod format) and outputs JSON with account
metadata and transaction rows.

Usage:
    python extract_xml_transactions.py statement.XML
    python extract_xml_transactions.py ./email_attachments/biz/ -o ./extracted/
    python extract_xml_transactions.py statement.XML -v

Output is one JSON object per XML. When --output-dir is given, each XML
produces a .json file in that directory; otherwise output goes to stdout.
"""

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StatementMetadata:
    bank_name: str | None = None
    account_number: str | None = None
    statement_number: str | None = None
    statement_date: str | None = None
    statement_type: str | None = None
    currency: str | None = None
    account_holder: str | None = None
    account_holder_address: str | None = None
    account_holder_city: str | None = None
    tax_id: str | None = None
    account_type: str | None = None
    opening_balance: float | None = None
    closing_balance: float | None = None
    total_debits: float | None = None
    total_credits: float | None = None


@dataclass
class Transaction:
    value_date: str | None = None
    counterparty: str | None = None
    counterparty_place: str | None = None
    counterparty_account: str | None = None
    description: str | None = None
    payment_code: str | None = None
    payment_code_description: str | None = None
    debit: float | None = None
    credit: float | None = None
    balance: float | None = None
    reference: str | None = None
    complaint_number: str | None = None
    explanation: str | None = None
    model_debit_credit: str | None = None
    ref_debit_credit: str | None = None
    model_user: str | None = None
    ref_user: str | None = None
    your_order_number: str | None = None


@dataclass
class StatementResult:
    source_file: str
    metadata: StatementMetadata
    transactions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"(\d{2})[./](\d{2})[./](\d{4})")


def parse_date(text: str | None) -> str | None:
    """Parse DD.MM.YYYY into ISO YYYY-MM-DD."""
    if not text:
        return None
    m = _DATE_RE.search(text.strip())
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def parse_amount(text: str | None) -> float | None:
    """Parse a numeric amount from XML (dot = decimal separator, no thousands)."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    # XML uses simple numbers like "1753.14" or "166807". If a Serbian-style
    # value ever appears ("1.753,14"), normalize it.
    last_dot = text.rfind(".")
    last_comma = text.rfind(",")
    if last_comma > last_dot:
        text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _s(text: str | None) -> str | None:
    """Strip and normalize an XML attribute string; empty → None."""
    if text is None:
        return None
    text = text.strip()
    return text or None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _infer_currency(account_type: str | None, header_attrib: dict) -> str | None:
    """Infer currency from the header. Serbian dinar statements say so explicitly."""
    dinar_flag = header_attrib.get("KomitentDinarskoKnjigovodstvo", "")
    if dinar_flag and "Dinars" in dinar_flag:
        return "RSD"
    if account_type and "dinars" in account_type.lower():
        return "RSD"
    return None


def extract_metadata(header: ET.Element) -> StatementMetadata:
    """Extract statement metadata from the Zaglavlje element."""
    a = header.attrib
    meta = StatementMetadata(
        bank_name="Raiffeisen banka a.d. Beograd",
        account_number=_s(a.get("Partija")),
        statement_number=_s(a.get("BrojIzvoda")),
        statement_date=parse_date(a.get("DatumIzvoda")),
        statement_type=_s(a.get("VrstaIzvoda")),
        account_holder=_s(a.get("KomitentNaziv")),
        account_holder_address=_s(a.get("KomitentAdresa")),
        account_holder_city=_s(a.get("KomitentMesto")),
        tax_id=_s(a.get("MaticniBroj")),
        account_type=_s(a.get("TipRacuna")),
        opening_balance=parse_amount(a.get("PrethodnoStanje")),
        closing_balance=parse_amount(a.get("NovoStanje")),
        total_debits=parse_amount(a.get("DugovniPromet")),
        total_credits=parse_amount(a.get("PotrazniPromet")),
    )
    meta.currency = _infer_currency(meta.account_type, a)
    return meta


def extract_transaction(elem: ET.Element) -> Transaction:
    """Extract a single Stavke element into a Transaction."""
    a = elem.attrib
    return Transaction(
        value_date=parse_date(a.get("DatumValute")),
        counterparty=_s(a.get("NalogKorisnik")),
        counterparty_place=_s(a.get("Mesto")),
        counterparty_account=_s(a.get("BrojRacunaPrimaocaPosiljaoca")),
        description=_s(a.get("Opis")),
        payment_code=_s(a.get("SifraPlacanja")),
        payment_code_description=_s(a.get("SifraPlacanjaOpis")),
        debit=parse_amount(a.get("Duguje")),
        credit=parse_amount(a.get("Potrazuje")),
        reference=_s(a.get("Referenca")),
        complaint_number=_s(a.get("BrojZaReklamaciju")),
        explanation=_s(a.get("Objasnjenje")),
        model_debit_credit=_s(a.get("ModelZaduzenjaOdobrenja")),
        ref_debit_credit=_s(a.get("PozivNaBrojZaduzenjaOdobrenja")),
        model_user=_s(a.get("ModelKorisnika")),
        ref_user=_s(a.get("PozivNaBrojKorisnika")),
        your_order_number=_s(a.get("VasBrojNaloga")),
    )


def compute_running_balance(transactions: list, opening: float | None) -> None:
    """Fill each transaction's balance from opening balance + debits/credits."""
    if opening is None:
        return
    running = opening
    for txn in transactions:
        debit = txn.debit or 0.0
        credit = txn.credit or 0.0
        running = running - debit + credit
        txn.balance = round(running, 2)


# ---------------------------------------------------------------------------
# XML processing
# ---------------------------------------------------------------------------


def process_xml(file_path: Path) -> StatementResult:
    """Process a single XML file and extract metadata + transactions."""
    result = StatementResult(
        source_file=file_path.name,
        metadata=StatementMetadata(),
    )

    tree = ET.parse(file_path)
    root = tree.getroot()

    header = root.find("Zaglavlje")
    if header is None:
        result.warnings.append("No Zaglavlje (header) element found")
    else:
        result.metadata = extract_metadata(header)

    transactions = [extract_transaction(e) for e in root.findall("Stavke")]
    compute_running_balance(transactions, result.metadata.opening_balance)
    result.transactions = transactions

    # Sanity check: does running balance land on closing balance?
    expected = result.metadata.closing_balance
    if transactions and expected is not None:
        actual = transactions[-1].balance
        if actual is not None and abs(actual - expected) > 0.01:
            result.warnings.append(
                f"Running balance {actual} does not match closing balance {expected}"
            )

    # Cross-check totals if provided by the header.
    if transactions:
        sum_debits = round(sum(t.debit or 0.0 for t in transactions), 2)
        sum_credits = round(sum(t.credit or 0.0 for t in transactions), 2)
        if (
            result.metadata.total_debits is not None
            and abs(sum_debits - result.metadata.total_debits) > 0.01
        ):
            result.warnings.append(
                f"Sum of debits {sum_debits} != header DugovniPromet {result.metadata.total_debits}"
            )
        if (
            result.metadata.total_credits is not None
            and abs(sum_credits - result.metadata.total_credits) > 0.01
        ):
            result.warnings.append(
                f"Sum of credits {sum_credits} != header PotrazniPromet "
                f"{result.metadata.total_credits}"
            )

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


def collect_xml_files(input_path: Path) -> list[Path]:
    """Resolve input path to a list of XML file paths."""
    if input_path.is_file():
        if input_path.suffix.lower() != ".xml":
            print(f"Error: {input_path} is not an XML file.", file=sys.stderr)
            sys.exit(1)
        return [input_path]

    if input_path.is_dir():
        xmls = sorted(p for p in input_path.iterdir() if p.is_file() and p.suffix.lower() == ".xml")
        if not xmls:
            print(f"Error: no XML files found in {input_path}", file=sys.stderr)
            sys.exit(1)
        return xmls

    print(f"Error: {input_path} does not exist.", file=sys.stderr)
    sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract structured transaction data from bank XML statements. "
        "Reads one or more XML files and outputs JSON with metadata "
        "and transactions.",
    )
    parser.add_argument(
        "input",
        help="Path to an XML file or directory containing XML files.",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        help="Directory to write JSON output files. If omitted, prints to stdout. "
        "One .json file is created per input XML.",
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

    input_path = Path(args.input)
    xml_files = collect_xml_files(input_path)
    output_dir = Path(args.output_dir) if args.output_dir else None

    print(f"Found {len(xml_files)} XML file(s) to process.", file=sys.stderr)

    errors = 0
    for xml_file in xml_files:
        print(f"Processing: {xml_file.name} ...", file=sys.stderr)
        try:
            result = process_xml(xml_file)
            data = result_to_dict(result)

            if output_dir:
                out_path = output_dir / (xml_file.stem + ".json")
                write_json(data, out_path)
                n = data["summary"]["total_transactions"]
                print(f"  -> {out_path} ({n} transactions)", file=sys.stderr)
            else:
                write_json(data)

        except Exception as exc:
            errors += 1
            print(f"  Error processing {xml_file.name}: {exc}", file=sys.stderr)
            log.debug("Traceback:", exc_info=True)

    if errors:
        print(f"\n{errors} file(s) failed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
