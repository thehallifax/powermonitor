#!/usr/bin/env python3
"""Synergy PDF bill importer.

The importer is intentionally source-specific: it extracts retailer bill text
and hands structured bill data to DataEngine for persistence. It never writes
SQLite directly.
"""

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.data_engine import DataEngine
from engine.import_framework import (
    ImportPreview,
    ImportResult,
    ImportSource,
    ImportValidationIssue,
)
from engine.models import HouseholdContext

DB_PATH = PROJECT_ROOT / "data" / "power.sqlite"
SOURCE = ImportSource(
    source_id="synergy_bill_pdf",
    display_name="Synergy PDF bill",
    supported_extensions=(".pdf",),
    description="Synergy retailer bill PDF used for actual bill reconciliation.",
)
PDF_EXTRACTION_ERROR = "PDF text extraction failed; install pypdf/pdfminer or provide a text-readable PDF."

BUCKET_ALIASES = {
    "anytime": "anytime",
    "peak": "peak",
    "off peak": "off_peak",
    "off-peak": "off_peak",
    "super off peak": "super_off_peak",
    "super off-peak": "super_off_peak",
    "overnight": "overnight",
    "peak export": "peak_export",
    "off peak export": "off_peak_export",
    "off-peak export": "off_peak_export",
}
IMPORT_BUCKETS = {"anytime", "peak", "off_peak", "super_off_peak", "overnight"}
EXPORT_BUCKETS = {"peak_export", "off_peak_export"}


@dataclass
class SynergyBillLineItem:
    description: str
    category: str
    bucket: Optional[str] = None
    plan_name: Optional[str] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    kwh: Optional[float] = None
    days: Optional[int] = None
    unit_rate: Optional[float] = None
    amount: Optional[float] = None
    raw_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "category": self.category,
            "bucket": self.bucket,
            "plan_name": self.plan_name,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "kwh": self.kwh,
            "days": self.days,
            "unit_rate": self.unit_rate,
            "amount": self.amount,
            "raw_text": self.raw_text,
        }


@dataclass
class SynergyBill:
    retailer: str = "Synergy"
    account_number: Optional[str] = None
    invoice_number: Optional[str] = None
    issue_date: Optional[str] = None
    due_date: Optional[str] = None
    bill_total: Optional[float] = None
    total_amount_owing: Optional[float] = None
    opening_balance: Optional[float] = None
    supply_address: Optional[str] = None
    nmi: Optional[str] = None
    next_scheduled_read_date: Optional[str] = None
    meter_number: Optional[str] = None
    periods: list[dict[str, Any]] = field(default_factory=list)
    line_items: list[SynergyBillLineItem] = field(default_factory=list)
    gst: Optional[float] = None
    warnings: list[str] = field(default_factory=list)
    confidence: str = "medium"

    def as_dict(self, redacted: bool = False) -> dict[str, Any]:
        account_number = mask_identifier(self.account_number) if redacted else self.account_number
        nmi = mask_identifier(self.nmi) if redacted else self.nmi
        result = {
            "retailer": self.retailer,
            "account_number": account_number,
            "invoice_number": self.invoice_number,
            "issue_date": self.issue_date,
            "due_date": self.due_date,
            "bill_total": self.bill_total,
            "total_amount_owing": self.total_amount_owing,
            "opening_balance": self.opening_balance,
            "supply_address": None if redacted else self.supply_address,
            "nmi": nmi,
            "next_scheduled_read_date": self.next_scheduled_read_date,
            "meter_number": self.meter_number,
            "periods": self.periods,
            "line_items": [item.as_dict() for item in self.line_items],
            "gst": self.gst,
            "warnings": self.warnings,
            "confidence": self.confidence,
            "totals": bill_totals(self),
        }
        return result


def mask_identifier(value: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D+", "", str(value or ""))
    if not digits:
        return None
    return f"•••{digits[-3:]}"


def identifier_hash(value: Optional[str]) -> Optional[str]:
    normalized = re.sub(r"\s+", "", str(value or ""))
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def parse_date(value: str) -> Optional[str]:
    raw = str(value or "").strip().replace(".", "")
    formats = ("%d/%m/%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d")
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_money(value: str) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    negative = "-" in text or text.startswith("(") or "cr" in text.lower() or "credit" in text.lower()
    cleaned = re.sub(r"[^0-9.]", "", text)
    if not cleaned:
        return None
    amount = float(cleaned)
    return -amount if negative else amount


def parse_number(value: str) -> Optional[float]:
    if value is None:
        return None
    cleaned = re.sub(r"[^0-9.]", "", str(value))
    return float(cleaned) if cleaned else None


def first_match(patterns: list[str], text: str, flags=re.IGNORECASE) -> Optional[str]:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1).strip()
    return None


def parse_period_line(line: str) -> Optional[dict[str, str]]:
    match = re.search(
        r"(?P<plan>.+?)\s+(?P<start>\d{1,2}[ /][A-Za-z]{3,9}[ /]\d{4}|\d{1,2}/\d{1,2}/\d{4})\s*(?:to|-|–)\s*(?P<end>\d{1,2}[ /][A-Za-z]{3,9}[ /]\d{4}|\d{1,2}/\d{1,2}/\d{4})",
        line,
        re.IGNORECASE,
    )
    if not match:
        return None
    plan = re.sub(r"^(plan|tariff|supply period)\s*[:\-]?\s*", "", match.group("plan"), flags=re.IGNORECASE).strip(" :-")
    start = parse_date(match.group("start"))
    end = parse_date(match.group("end"))
    if not start or not end:
        return None
    return {"plan_name": plan or "Synergy plan", "start_date": start, "end_date": end}


def canonical_bucket(description: str) -> Optional[str]:
    lowered = re.sub(r"\s+", " ", description.lower())
    for label in sorted(BUCKET_ALIASES, key=len, reverse=True):
        if label in lowered:
            return BUCKET_ALIASES[label]
    return None


def parse_line_item(line: str, current_period: Optional[dict[str, str]]) -> Optional[SynergyBillLineItem]:
    compact = re.sub(r"\s+", " ", line).strip()
    if not compact:
        return None
    bucket = canonical_bucket(compact)
    money_match = re.search(r"(-?\$?\(?\d[\d,]*\.\d{2}\)?\s*(?:cr|credit)?)\s*$", compact, re.IGNORECASE)
    amount = parse_money(money_match.group(1)) if money_match else None
    rate_match = re.search(r"(?:@|x)\s*\$?(\d+\.\d+)", compact, re.IGNORECASE)
    unit_rate = parse_number(rate_match.group(1)) if rate_match else None
    kwh_match = re.search(r"([\d,]+(?:\.\d+)?)\s*kWh\b", compact, re.IGNORECASE)
    kwh = parse_number(kwh_match.group(1)) if kwh_match else None
    day_match = re.search(r"(\d+)\s*days?\b", compact, re.IGNORECASE)
    days = int(day_match.group(1)) if day_match else None

    category = None
    if bucket in IMPORT_BUCKETS:
        category = "import_usage"
    elif bucket in EXPORT_BUCKETS or "buyback" in compact.lower() or "export" in compact.lower():
        category = "export_credit"
    elif "supply" in compact.lower() and days is not None:
        category = "supply_charge"
    elif re.search(r"\bgst\b", compact, re.IGNORECASE):
        category = "gst"
    elif amount is not None and ("balance" in compact.lower() or "credit" in compact.lower()):
        category = "balance"

    if category is None:
        return None

    return SynergyBillLineItem(
        description=compact.split("$")[0].strip(" -:") or compact,
        category=category,
        bucket=bucket,
        plan_name=(current_period or {}).get("plan_name"),
        period_start=(current_period or {}).get("start_date"),
        period_end=(current_period or {}).get("end_date"),
        kwh=kwh,
        days=days,
        unit_rate=unit_rate,
        amount=amount,
        raw_text=compact,
    )


def parse_synergy_bill_text(text: str) -> SynergyBill:
    text = normalize_text(text)
    bill = SynergyBill()
    if "synergy" not in text.lower():
        bill.warnings.append("The bill text does not clearly identify Synergy.")

    bill.account_number = first_match([
        r"Account\s+number\s*[:#]?\s*([A-Z0-9 \-]+)",
        r"Account\s*[:#]\s*([A-Z0-9 \-]+)",
    ], text)
    bill.invoice_number = first_match([
        r"Invoice\s+number\s*[:#]?\s*([A-Z0-9 \-]+)",
        r"Tax\s+invoice\s*[:#]?\s*([A-Z0-9 \-]+)",
    ], text)
    bill.issue_date = parse_date(first_match([r"Date\s+of\s+issue\s*[:#]?\s*([0-9A-Za-z/ ]{8,20})", r"Issue\s+date\s*[:#]?\s*([0-9A-Za-z/ ]{8,20})"], text) or "")
    bill.due_date = parse_date(first_match([r"(?:Payment\s+due|Direct\s+debit\s+date|Due\s+date)\s*[:#]?\s*([0-9A-Za-z/ ]{8,20})"], text) or "")
    bill.bill_total = parse_money(first_match([r"Total\s+bill\s+amount\s*[:#]?\s*(-?\$?[\d,]+\.\d{2})", r"Bill\s+total\s*[:#]?\s*(-?\$?[\d,]+\.\d{2})"], text) or "")
    bill.total_amount_owing = parse_money(first_match([r"Total\s+amount\s+owing\s*[:#]?\s*(-?\$?[\d,]+\.\d{2})", r"Amount\s+owing\s*[:#]?\s*(-?\$?[\d,]+\.\d{2})"], text) or "")
    bill.opening_balance = parse_money(first_match([r"Opening\s+balance\s*[:#]?\s*(-?\$?[\d,]+\.\d{2}(?:\s*CR)?)", r"Previous\s+balance\s*[:#]?\s*(-?\$?[\d,]+\.\d{2}(?:\s*CR)?)"], text) or "")
    bill.supply_address = first_match([r"Supply\s+address\s*[:#]?\s*(.+)", r"Supply\s+at\s*[:#]?\s*(.+)"], text)
    bill.nmi = first_match([r"\bNMI\s*[:#]?\s*([0-9A-Z ]{8,20})"], text)
    bill.next_scheduled_read_date = parse_date(first_match([r"Next\s+scheduled\s+read\s+date\s*[:#]?\s*([0-9A-Za-z/ ]{8,20})", r"Next\s+read\s+date\s*[:#]?\s*([0-9A-Za-z/ ]{8,20})"], text) or "")
    bill.meter_number = first_match([r"Meter\s+number\s*[:#]?\s*([0-9A-Z \-]+)"], text)

    current_period = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        period = parse_period_line(line)
        if period and any(marker in line.lower() for marker in ("home", "ev", "plan", "tariff")):
            current_period = period
            bill.periods.append(period)
            continue
        item = parse_line_item(line, current_period)
        if item:
            bill.line_items.append(item)
            if item.category == "gst" and item.amount is not None:
                bill.gst = item.amount

    if bill.bill_total is None:
        bill.bill_total = bill.total_amount_owing
    if bill.total_amount_owing is None:
        bill.total_amount_owing = bill.bill_total
    if not bill.invoice_number:
        bill.warnings.append("Invoice number was not found; duplicate detection will be limited.")
    if not bill.line_items:
        bill.warnings.append("No tariff line items were found.")
    if len(bill.periods) > 1:
        bill.warnings.append("Multiple plan periods detected in this invoice.")
    bill.confidence = "high" if bill.invoice_number and bill.issue_date and bill.bill_total is not None and bill.line_items else "medium"
    if not bill.invoice_number or bill.bill_total is None:
        bill.confidence = "low"
    return bill


def bill_totals(bill: SynergyBill) -> dict[str, Any]:
    buckets: dict[str, dict[str, float]] = {}
    export_credits = 0.0
    supply_charge = 0.0
    for item in bill.line_items:
        if item.bucket:
            bucket = buckets.setdefault(item.bucket, {"kwh": 0.0, "amount": 0.0, "unit_rate": item.unit_rate or 0.0})
            bucket["kwh"] += float(item.kwh or 0)
            bucket["amount"] += float(item.amount or 0)
            if item.unit_rate is not None:
                bucket["unit_rate"] = item.unit_rate
        if item.category == "export_credit":
            export_credits += float(item.amount or 0)
        if item.category == "supply_charge":
            supply_charge += float(item.amount or 0)
    return {
        "usage_by_bucket": buckets,
        "export_credits": export_credits,
        "supply_charge": supply_charge,
        "gst": bill.gst,
        "bill_total": bill.bill_total,
    }


def extract_text_with_pdftotext(path: Path) -> str:
    completed = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def extract_text_with_pypdf(path: Path) -> str:
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text_with_pdfminer(path: Path) -> str:
    from pdfminer.high_level import extract_text  # type: ignore

    return extract_text(str(path))


def extract_text_from_pdf(path: Path) -> str:
    failures: list[str] = []
    if shutil.which("pdftotext"):
        try:
            text = extract_text_with_pdftotext(path)
            if text.strip():
                return text
            failures.append("pdftotext returned no readable text")
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"pdftotext failed: {exc.__class__.__name__}")
    else:
        failures.append("pdftotext is unavailable")

    for label, extractor in (
        ("pypdf", extract_text_with_pypdf),
        ("pdfminer", extract_text_with_pdfminer),
    ):
        try:
            text = extractor(path)
            if text.strip():
                return text
            failures.append(f"{label} returned no readable text")
        except ImportError:
            failures.append(f"{label} is not installed")
        except Exception as exc:
            failures.append(f"{label} failed: {exc.__class__.__name__}")

    detail = "; ".join(failures)
    raise ValueError(f"{PDF_EXTRACTION_ERROR} Details: {detail}")


class SynergyBillImporter:
    source = SOURCE

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path

    def detect(self, file_path: Path) -> bool:
        return file_path.suffix.lower() == ".pdf"

    def parse(self, file_path: Path) -> SynergyBill:
        text = extract_text_from_pdf(file_path)
        if not text.strip():
            raise ValueError("The Synergy PDF did not contain readable text.")
        return parse_synergy_bill_text(text)

    def validate(self, parsed_data: SynergyBill) -> list[ImportValidationIssue]:
        issues = [
            ImportValidationIssue("warning", warning)
            for warning in parsed_data.warnings
        ]
        if parsed_data.bill_total is None:
            issues.append(ImportValidationIssue("error", "Bill total was not found."))
        if not parsed_data.invoice_number:
            issues.append(ImportValidationIssue("error", "Invoice number was not found."))
        return issues

    def preview(self, parsed_data: SynergyBill) -> ImportPreview:
        totals = bill_totals(parsed_data)
        samples = [
            {
                "invoice_number": parsed_data.invoice_number,
                "issue_date": parsed_data.issue_date,
                "bill_total": parsed_data.bill_total,
                "account_number": mask_identifier(parsed_data.account_number),
                "nmi": mask_identifier(parsed_data.nmi),
                "billing_periods": parsed_data.periods,
                "usage_by_bucket": totals["usage_by_bucket"],
                "export_credits": totals["export_credits"],
                "gst": parsed_data.gst,
                "confidence": parsed_data.confidence,
            }
        ]
        return ImportPreview(
            source=self.source,
            total_records=1,
            valid_records=0 if any(issue.severity == "error" for issue in self.validate(parsed_data)) else 1,
            date_start=parsed_data.periods[0]["start_date"] if parsed_data.periods else None,
            date_end=parsed_data.periods[-1]["end_date"] if parsed_data.periods else None,
            total_energy_kwh=sum(float(values.get("kwh") or 0) for values in totals["usage_by_bucket"].values()),
            issues=self.validate(parsed_data),
            sample_records=samples,
        )

    def commit(
        self,
        parsed_data: SynergyBill,
        household_context: HouseholdContext,
        job_id: Optional[str] = None,
    ) -> ImportResult:
        engine = DataEngine(db_path=self.db_path, household_context=household_context)
        result = engine.import_synergy_bill(parsed_data.as_dict(), import_job_id=job_id)
        status = "completed" if result.get("status") in {"inserted", "duplicate"} else "failed"
        message = (
            "Synergy bill already imported; no duplicate was created."
            if result.get("status") == "duplicate"
            else "Synergy bill imported for actual bill reconciliation."
            if status == "completed"
            else "Synergy bill import failed."
        )
        return ImportResult(
            job_id=job_id or "",
            source=self.source,
            status=status,
            processed_records=1,
            inserted_records=1 if result.get("status") == "inserted" else 0,
            updated_records=0,
            message=message,
        )


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python3 collector/import_synergy_bill.py bill.pdf")
        raise SystemExit(1)
    importer = SynergyBillImporter()
    parsed = importer.parse(Path(sys.argv[1]))
    print(json.dumps(parsed.as_dict(redacted=True), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
