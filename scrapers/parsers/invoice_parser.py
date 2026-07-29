import os
import sys
import json
import pdfplumber
from openai import OpenAI
from dotenv import load_dotenv

# AI bill extraction via OpenRouter (OpenAI-compatible API). The model is
# configurable so we can trade cost/quality without a code change; default is a
# cheap, JSON-reliable model. To use vision on scanned PDFs later, swap to a
# multimodal model and send the PDF instead of extracted text.
load_dotenv()

MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")
client = OpenAI(
    base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    api_key=os.getenv("OPENROUTER_API_KEY"),
)


# =========================================================
# Clean JSON extractor (removes ```json … ``` wrappers)
# =========================================================
def extract_json(text: str):
    text = text.strip()

    # Remove leading ```json or ``` blocks
    if text.startswith("```"):
        first_line_end = text.find("\n")
        if first_line_end != -1:
            text = text[first_line_end+1:]

    if text.endswith("```"):
        text = text[:-3]

    return text.strip()


# =========================================================
# PDF → text (utility bills are digital PDFs with a text layer)
# =========================================================
def pdf_to_text(pdf_path: str) -> str:
    parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
    return "\n\n".join(parts).strip()


# =========================================================
# Main Parsing
# =========================================================
def parse_invoice(pdf_path: str):

    text = pdf_to_text(pdf_path)
    if not text:
        # No text layer (likely a scanned image). Cheap text extraction can't
        # handle this — surface a clear error rather than a bad guess.
        return {"error": "no_text_extracted", "raw_output": ""}

    # =====================================================
    # RAW extraction prompt — no matching, Go handles that
    # =====================================================
    system_prompt = """
You are an invoice parser for a property management company.

This PDF is a vendor invoice/bill/receipt sent TO our company (the customer).
Your job is to identify WHO sent it (the vendor) and WHICH PROPERTY it is for.

CRITICAL DISTINCTIONS:
- "vendor_raw": The COMPANY that provided the service and is billing us. This is the sender/payee, NOT the customer. Look for: company name, "From:", letterhead, "Company Information", "Bill From", "Remit To". Examples: "Vermont Pest Control", "ABC Plumbing LLC".
- "address_raw": The PROPERTY/SERVICE ADDRESS where work was performed. This is OUR property, NOT the vendor's mailing address. Look for: "Service Address", "Customer Address", "Bill To", "Property", "Job Site", "Ship To", "Customer Information" address. Examples: "51 Willowmere Circle, Riverside, CT 06878".

Do NOT confuse:
- Vendor's PO Box / corporate HQ address with the service/property address
- Customer name with vendor name — the vendor is the one billing, the customer is us

Return ONLY valid JSON:

{
  "vendor_raw": "company name that sent this invoice",
  "address_raw": "property/service address where work was done, or customer address — NOT the vendor's address",
  "invoice_number": "... or null",
  "amount_cents": 12345,
  "statement_date": "YYYY-MM-DD or null",
  "due_date": "YYYY-MM-DD or null",
  "service_start": "YYYY-MM-DD or null — start of the service/billing period this bill covers",
  "service_end": "YYYY-MM-DD or null — end of the service/billing period this bill covers",
  "line_items": [
    {"description": "...", "amount_cents": 1234}
  ],
  "parse_confidence": 85
}

line_items: each service line or charge. Return [] if none distinguishable.
parse_confidence (0-100): how confident you are in ALL extracted fields.

No commentary. No markdown. No backticks.
"""

    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1024,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Invoice text:\n\n" + text},
        ],
    )

    raw_text = resp.choices[0].message.content or ""
    clean_text = extract_json(raw_text)

    # Parse JSON returned by the model
    try:
        data = json.loads(clean_text)
    except Exception as e:
        print("JSON parse error:", e, file=sys.stderr)
        print("RAW MODEL OUTPUT:\n", raw_text, file=sys.stderr)
        return {"error": "json_parse_failure", "raw_output": raw_text}

    # Ensure line_items and parse_confidence have defaults
    if "line_items" not in data:
        data["line_items"] = []
    if "parse_confidence" not in data:
        data["parse_confidence"] = 0

    return data


# =========================================================
# CLI ENTRYPOINT
# =========================================================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("ERROR: Must provide PDF path as first argument", file=sys.stderr)
        sys.exit(1)

    pdf_path = sys.argv[1]
    result = parse_invoice(pdf_path)

    # Print structured JSON to stdout (Go reads this)
    print(json.dumps(result))
