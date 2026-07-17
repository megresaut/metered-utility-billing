"""
Eversource Bill PDF Parser

Parses a downloaded Eversource PDF and extracts:
- amount
- amount_cents
- due_date
- statement_date
- period_start
- period_end

Usage:
    python eversource_pdf_parser.py path/to/eversource-bill.pdf
"""

import re
import sys
from pathlib import Path
from typing import Dict, Optional
from pypdf import PdfReader


def parse_eversource_pdf(pdf_path: str) -> Dict[str, Optional[str]]:
    info: Dict[str, Optional[str]] = {
        "amount": None,
        "amount_cents": None,
        "due_date": None,
        "statement_date": None,
        "period_start": None,
        "period_end": None,
    }

    pdf_path = str(pdf_path)
    reader = PdfReader(pdf_path)

    # ---- Extract text from all pages ----
    texts = []
    for page in reader.pages:
        texts.append(page.extract_text() or "")
    raw = "\n".join(texts)

    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", raw)
    text = re.sub(r"\s*\n\s*", " ", text)

    # --------------------------------------------------
    # Statement Date
    # Handles broken text like "Sta tement Da te: 01/08/26"
    # --------------------------------------------------
    m = re.search(
        r"Sta\s*tement\s+Da\s*te:.*?(\d{2}/\d{2}/\d{2})",
        text,
        re.IGNORECASE,
    )
    if m:
        info["statement_date"] = m.group(1)

    # --------------------------------------------------
    # Service Period
    # Example: "Service from 12/08/25 - 01/08/26"
    # --------------------------------------------------
    m = re.search(
        r"Ser\s*vice\s+from\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})",
        text,
        re.IGNORECASE,
    )
    if m:
        info["period_start"] = m.group(1)
        info["period_end"] = m.group(2)

    # --------------------------------------------------
    # Amount + Due Date
    #
    # pypdf splits words across lines in Eversource PDFs (kerning artifact),
    # so after our whitespace collapse we see things like:
    #   "Amount no w due b y 06/05/26"  (i.e. "now" → "no w", "by" → "b y")
    #
    # Layouts we've seen:
    #   A) Autopay bill: "$49.72 Payment will be sent to bank ... on 02/01/26"
    #   B) Autopay reversed: "for processing on 03/21/26 ... bank $2,259.61"
    #   C) Manual-pay header: "$130.88 Amount now due by 06/05/26"
    #   D) Manual-pay stub:   "by 06/05/26 Amount now due $130.88"
    #
    # All regexes below tolerate split "no\s*w" and "b\s*y".
    # --------------------------------------------------
    amount_raw = None
    due_raw = None

    # A) Autopay forward
    m = re.search(
        r"\$([0-9,]+\.[0-9]{2})\s+Payment will be sent to bank.*?on\s+(\d{2}/\d{2}/\d{2})",
        text,
        re.IGNORECASE,
    )
    if m:
        amount_raw, due_raw = m.group(1), m.group(2)

    # B) Autopay reversed
    if not amount_raw:
        m = re.search(
            r"for processing on\s+(\d{2}/\d{2}/\d{2})\s+Payment will be sent to bank\s+\$([0-9,]+\.[0-9]{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            amount_raw, due_raw = m.group(2), m.group(1)

    # C) Manual-pay: "$AMT Amount now due by DATE"
    if not amount_raw:
        m = re.search(
            r"\$([0-9,]+\.[0-9]{2})\s+Amount\s+no\s*w\s+due\s+b\s*y\s+(\d{2}/\d{2}/\d{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            amount_raw, due_raw = m.group(1), m.group(2)

    # D) Manual-pay reversed (mailing stub): "by DATE Amount now due $AMT"
    if not amount_raw:
        m = re.search(
            r"b\s*y\s+(\d{2}/\d{2}/\d{2})\s+Amount\s+no\s*w\s+due\s+\$([0-9,]+\.[0-9]{2})",
            text,
            re.IGNORECASE,
        )
        if m:
            amount_raw, due_raw = m.group(2), m.group(1)

    # Standalone fallbacks
    if not amount_raw:
        amt_m = re.search(
            r"\$([0-9,]+\.[0-9]{2})\s+T\s*otal\s+Amount\s+Due",
            text,
            re.IGNORECASE,
        )
        if amt_m:
            amount_raw = amt_m.group(1)
    if not amount_raw:
        amt_m = re.search(
            r"T\s*otal\s+Current\s+Charges\s+\$([0-9,]+\.[0-9]{2})",
            text,
            re.IGNORECASE,
        )
        if not amt_m:
            amt_m = re.search(
                r"\$([0-9,]+\.[0-9]{2})\s+T\s*otal\s+Current\s+Charges",
                text,
                re.IGNORECASE,
            )
        if amt_m:
            amount_raw = amt_m.group(1)
    if not due_raw:
        due_m = re.search(
            r"for processing on\s+(\d{2}/\d{2}/\d{2})",
            text,
            re.IGNORECASE,
        )
        if not due_m:
            due_m = re.search(
                r"Amount\s+no\s*w\s+due\s+b\s*y\s+(\d{2}/\d{2}/\d{2})",
                text,
                re.IGNORECASE,
            )
        if due_m:
            due_raw = due_m.group(1)

    if amount_raw:
        clean = amount_raw.replace(",", "")
        info["amount"] = clean
        info["due_date"] = due_raw
        try:
            info["amount_cents"] = int(round(float(clean) * 100))
        except Exception:
            info["amount_cents"] = None

    return info


# ---------------- CLI ----------------

def main(argv=None):
    if not argv or len(argv) != 1:
        print("Usage: python eversource_pdf_parser.py <path-to-pdf>")
        sys.exit(1)

    pdf_path = Path(argv[0])
    if not pdf_path.exists():
        print(f"ERROR: file not found: {pdf_path}")
        sys.exit(1)

    result = parse_eversource_pdf(str(pdf_path))

    for k, v in result.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main(sys.argv[1:])
