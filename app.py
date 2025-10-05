# app.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, HttpUrl
import base64, requests, html as html_mod
import fitz  # PyMuPDF
import logging

logger = logging.getLogger("uvicorn.error")

app = FastAPI()

class Pdf2HtmlIn(BaseModel):
    request_id: str | None = None
    filename: str | None = None
    pdf_b64: str | None = None
    pdf_url: HttpUrl | None = None   # <= URL http(s) valide sinon 422

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
    pages = 0
    for i, page in enumerate(doc, start=1):
        pages = i
        text = page.get_text("text")  # texte brut; pas d'invention
        html_fragments.append(
            f"<section data-page='{i}'><pre>{html_mod.escape(text)}</pre></section>"
        )
    html_fragments.append("</body></html>")
    doc.close()
    return {
        "request_id": payload.request_id,
        "html_semantic": "\n".join(html_fragments),
        "metrics": {"pages": pages},
    }

# Handler propre pour voir *pourquoi* tu prends un 422
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = (await request.body()).decode("utf-8", errors="ignore")
    logger.error("422 payload=%s errors=%s", body[:1000], exc.errors())  # log côté serveur
    return JSONResponse(status_code=422, content={"detail": exc.errors()})
