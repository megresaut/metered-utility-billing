import os
import tempfile
from fastapi import FastAPI, HTTPException, UploadFile, File
from pydantic import BaseModel

from parsers.invoice_parser import parse_invoice
from parsers.checklist_parser import parse_checklist

app = FastAPI(title="RA AVM Parser Service")


@app.get("/health")
def health():
    return {"status": "ok", "service": "ra-avm-parser"}


class ParseInvoiceRequest(BaseModel):
    pdf_path: str

class ParseChecklistRequest(BaseModel):
    docx_path: str

@app.post("/parse/invoice")
def parse_invoice_endpoint(req: ParseInvoiceRequest):
    pdf_path = req.pdf_path

    # 1️⃣ Validate path
    if not pdf_path:
        raise HTTPException(status_code=400, detail="pdf_path is required")

    if not os.path.exists(pdf_path):
        raise HTTPException(
            status_code=404,
            detail=f"PDF not found at path: {pdf_path}",
        )

    # 2️⃣ Run parser
    try:
        result = parse_invoice(pdf_path)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"parser error: {str(e)}",
        )

    # 3️⃣ Return JSON directly
    return result

@app.post("/parse/checklist")
def parse_checklist_endpoint(req: ParseChecklistRequest):
    docx_path = req.docx_path

    if not docx_path:
        raise HTTPException(status_code=400, detail="docx_path is required")

    if not os.path.exists(docx_path):
        raise HTTPException(
            status_code=404,
            detail=f"DOCX not found at path: {docx_path}",
        )

    try:
        result = parse_checklist(docx_path)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"checklist parser error: {str(e)}",
        )

    return result


@app.post("/parse/checklist/upload")
def parse_checklist_upload_endpoint(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="only .docx files are supported")

    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp.write(file.file.read())
            tmp_path = tmp.name

        result = parse_checklist(tmp_path)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"checklist parser error: {str(e)}",
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return result
