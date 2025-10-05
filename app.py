# app.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
import base64, requests, html as html_mod
import fitz  # PyMuPDF

app = FastAPI()

class Pdf2HtmlIn(BaseModel):
    request_id: str | None = None
    filename: str | None = None
    pdf_b64: str | None = None
    pdf_url: HttpUrl | None = None

@app.get("/health")
def health():
    return {"ok": True}

def _load_pdf_bytes(body: Pdf2HtmlIn) -> bytes:
    if body.pdf_b64:
        try:
            return base64.b64decode(body.pdf_b64, validate=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64: {e}")
    if body.pdf_url:
        r = requests.get(str(body.pdf_url), timeout=60)
        r.raise_for_status()
        return r.content
    raise HTTPException(status_code=400, detail="Provide pdf_b64 or pdf_url")

@app.post("/pdf2html")
def pdf2html(payload: Pdf2HtmlIn):
    blob = _load_pdf_bytes(payload)
    doc = fitz.open(stream=blob, filetype="pdf")
    html_fragments = ["<html><head><meta charset='utf-8'></head><body>"]
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        html_fragments.append(
            f"<section data-page='{i}'><pre>{html_mod.escape(text)}</pre></section>"
        )
    html_fragments.append("</body></html>")
    doc.close()
    return {
        "request_id": payload.request_id,
        "html_semantic": "\n".join(html_fragments),
        "metrics": {"pages": i if 'i' in locals() else 0},
    }
