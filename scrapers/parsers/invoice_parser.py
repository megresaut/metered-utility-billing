import os
import sys
import json
import base64
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


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
# Main Parsing
# =========================================================
def parse_invoice(pdf_path: str):

    # Read + base64 encode PDF
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

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

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": system_prompt},
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                ],
            }
        ],
    )


    raw_text = resp.content[0].text
    clean_text = extract_json(raw_text)

    # Parse JSON returned by Claude
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
