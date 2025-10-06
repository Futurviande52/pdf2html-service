# app.py
from __future__ import annotations

import base64, io, re, zipfile, logging
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple, Literal

import fitz  # PyMuPDF
import requests
from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, HttpUrl

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="pdf2html-service", version="1.1.0")

# -----------------------------
# Payload / Options
# -----------------------------
class Pdf2HtmlOptions(BaseModel):
    mode: Literal["semantic", "fidelity", "both"] = "both"
    injectLinks: bool = True
    promoteHeadings: bool = True
    returnZipB64: bool = False

class Pdf2HtmlIn(BaseModel):
    request_id: Optional[str] = None
    filename: Optional[str] = None
    pdf_b64: Optional[str] = None
    pdf_url: Optional[HttpUrl] = None
    options: Optional[Pdf2HtmlOptions] = Pdf2HtmlOptions()

# -----------------------------
# Health
# -----------------------------
@app.get("/health")
def health():
    return {"ok": True}

# -----------------------------
# Utils extraction/rendu
# -----------------------------
HTTP_RE = re.compile(r'(https?://[^\s<>\)]+)')

def rgb_int_to_hex(rgb_int: int) -> str:
    # Couleur texte PyMuPDF sur 24 bits 0xRRGGBB
    return f"#{rgb_int:06x}"

def is_bold(font_name: str) -> bool:
    n = font_name.lower()
    return "bold" in n or "semibold" in n or "black" in n

def is_italic(font_name: str) -> bool:
    n = font_name.lower()
    return "italic" in n or "oblique" in n or "ital" in n

def pct(a: float, total: float) -> float:
    return round(100.0 * a / total, 4) if total else 0.0

def cluster_heading_sizes(sizes: List[float]) -> Dict[str, float]:
    """Heuristique pour détecter h1/h2/h3 à partir des tailles de police."""
    if not sizes:
        return {}
    norms = [round(s * 2) / 2.0 for s in sizes]  # agrégation au 0.5 pt
    cnt = Counter(norms)
    # On prend les plus grandes tailles rencontrées comme candidats titres
    top = sorted({s for s in norms}, reverse=True)[:6]
    mapping = {}
    if len(top) >= 1: mapping["h1"] = top[0]
    if len(top) >= 2: mapping["h2"] = top[1]
    if len(top) >= 3: mapping["h3"] = top[2]
    return mapping

def wrap_links(txt: str) -> str:
    return HTTP_RE.sub(lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>', txt)

def span_to_html(span: Dict[str, Any], inject_links: bool) -> str:
    text = span.get("text", "")
    if not text.strip():
        return text
    color = rgb_int_to_hex(span.get("color", 0))
    font = span.get("font", "")
    bold = is_bold(font)
    italic = is_italic(font)

    style_bits = []
    if color not in ("#000000", "#000001"):
        style_bits.append(f"color:{color}")
    style = f' style="{";".join(style_bits)}"' if style_bits else ""

    out = text
    if inject_links:
        out = wrap_links(out)
    if italic:
        out = f"<em>{out}</em>"
    if bold:
        out = f"<strong>{out}</strong>"
    if style:
        out = f"<span{style}>{out}</span>"
    return out

def line_is_list_item(line_text: str) -> Tuple[bool, bool]:
    s = line_text.strip()
    if not s:
        return False, False
    # Puces
    if s[:1] in {"•", "·", "◦", "-", "–", "*"}:
        return True, False
    # Listes ordonnées : "1. ", "1) ", "a) "
    if re.match(r'^(\d+[\.\)]|[a-zA-Z][\.\)])\s+', s):
        return True, True
    return False, False

# -----------------------------
# Build HTML sémantique
# -----------------------------
def build_semantic(doc: fitz.Document, options: Pdf2HtmlOptions) -> Tuple[str, str, List[Dict[str, Any]]]:
    promote_headings = bool(options.promoteHeadings)
    inject_links = bool(options.injectLinks)

    geom: List[Dict[str, Any]] = []
    all_sizes: List[float] = []
    page_infos = []

    # 1) Collecte spans + tailles
    for pno in range(len(doc)):
        page = doc.load_page(pno)
        w, h = page.rect.width, page.rect.height
        d = page.get_text("dict")
        page_infos.append((w, h, d))
        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    size = float(span.get("size", 12.0))
                    all_sizes.append(size)
                    x0, y0, x1, y1 = span.get("bbox", line.get("bbox", [0, 0, 0, 0]))
                    geom.append({
                        "page": pno + 1,
                        "text": span.get("text", ""),
                        "font": span.get("font", ""),
                        "size": size,
                        "color": rgb_int_to_hex(span.get("color", 0)),
                        "bold": is_bold(span.get("font","")),
                        "italic": is_italic(span.get("font","")),
                        "bbox_pct": [pct(x0, w), pct(y0, h), pct(x1, w), pct(y1, h)],
                    })

    hmap = cluster_heading_sizes(all_sizes) if promote_headings else {}

    # 2) Construction HTML
    body_bits: List[str] = []
    for pno, (w, h, d) in enumerate(page_infos, start=1):
        body_bits.append(f'<section data-page="{pno}">')
        open_list: Optional[str] = None  # 'ul'|'ol'|None

        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_text = "".join(s.get("text", "") for s in spans).strip()

                # Tag : titre ou paragraphe
                tag = "p"
                if promote_headings:
                    max_size = max(float(s.get("size", 12.0)) for s in spans)
                    if "h1" in hmap and max_size >= hmap["h1"]:
                        tag = "h1"
                    elif "h2" in hmap and max_size >= hmap["h2"]:
                        tag = "h2"
                    elif "h3" in hmap and max_size >= hmap["h3"]:
                        tag = "h3"

                # Listes ?
                is_li, is_ord = line_is_list_item(line_text)
                if is_li and not tag.startswith("h"):
                    desired = "ol" if is_ord else "ul"
                    if open_list and open_list != desired:
                        body_bits.append(f"</{open_list}>")
                        open_list = None
                    if not open_list:
                        body_bits.append(f"<{desired}>")
                        open_list = desired
                    body_bits.append("<li>" + "".join(span_to_html(s, inject_links) for s in spans) + "</li>")
                    continue
                else:
                    if open_list:
                        body_bits.append(f"</{open_list}>")
                        open_list = None

                # Ligne normale
                body_bits.append(f"<{tag}>" + "".join(span_to_html(s, inject_links) for s in spans) + f"</{tag}>")

        if open_list:
            body_bits.append(f"</{open_list}>")
        body_bits.append("</section>")

    css = """
:root{--base:16px;--lh:1.5;}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Inter,Arial,sans-serif;line-height:var(--lh);margin:0;padding:2rem;background:#fff;color:#111;}
section{margin:0 auto;max-width:860px;padding:1.2rem 1rem;border-bottom:1px solid #eee;}
h1,h2,h3{line-height:1.2;margin:1.2rem 0 .6rem 0;font-weight:700}
h1{font-size:1.75rem}
h2{font-size:1.4rem}
h3{font-size:1.2rem}
p{margin:.5rem 0}
ul,ol{margin:.6rem 0 .6rem 1.2rem}
a{text-decoration:underline;word-break:break-word}
""".strip()

    html = "<article>\n" + "\n".join(body_bits) + "\n</article>"
    return html, css, geom

# -----------------------------
# Build HTML fidélité (positionné)
# -----------------------------
def build_fidelity(doc: fitz.Document) -> Tuple[str, str]:
    pages_bits: List[str] = []
    for pno in range(len(doc)):
        page = doc.load_page(pno)
        w, h = page.rect.width, page.rect.height
        d = page.get_text("dict")
        bits = [f'<div class="page" data-page="{pno+1}" style="width:{w}px;height:{h}px">']
        for block in d.get("blocks", []):
            if block.get("type", 0) != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    x0, y0, x1, y1 = span.get("bbox", line.get("bbox", [0,0,0,0]))
                    wpx = max(0.0, x1 - x0)
                    font = span.get("font","")
                    size_pt = float(span.get("size", 12.0))
                    color = rgb_int_to_hex(span.get("color", 0))
                    fw = "700" if is_bold(font) else "400"
                    fs = "italic" if is_italic(font) else "normal"
                    txt = span.get("text","").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
                    style = f'left:{x0}px;top:{y0}px;width:{wpx}px;font-size:{size_pt}px;font-weight:{fw};font-style:{fs};color:{color};'
                    bits.append(f'<span class="s" style="{style}">{txt}</span>')
        bits.append("</div>")
        pages_bits.append("\n".join(bits))

    css = """
body{background:#f6f7fb;margin:0;padding:12px}
.page{position:relative;margin:16px auto;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.06),0 1px 2px rgba(0,0,0,.04)}
.page .s{position:absolute;white-space:pre;line-height:1}
""".strip()
    html = "<div class='doc'>\n" + "\n".join(pages_bits) + "\n</div>"
    return html, css

def make_zip_b64(files: Dict[str, str]) -> str:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return base64.b64encode(mem.getvalue()).decode("ascii")

# -----------------------------
# Chargement binaire PDF
# -----------------------------
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

# -----------------------------
# Endpoint principal
# -----------------------------
@app.post("/pdf2html")
def pdf2html(payload: Pdf2HtmlIn = Body(...)) -> Any:
    blob = _load_pdf_bytes(payload)
    doc = fitz.open(stream=blob, filetype="pdf")
    opts = payload.options or Pdf2HtmlOptions()

    out: Dict[str, Any] = {
        "request_id": payload.request_id,
        "filename": payload.filename or "upload.pdf",
        "metrics": {"pages": len(doc)},
    }

    if opts.mode in ("semantic", "both"):
        html_sem, css_sem, geom = build_semantic(doc, opts)
        out["html_semantic"] = f"<!doctype html><html><head><meta charset='utf-8'><style>{css_sem}</style></head><body>{html_sem}</body></html>"
        out["css_semantic"] = css_sem
        out["geom"] = geom

    if opts.mode in ("fidelity", "both"):
        html_fid, css_fid = build_fidelity(doc)
        out["html_fidelity"] = f"<!doctype html><html><head><meta charset='utf-8'><style>{css_fid}</style></head><body>{html_fid}</body></html>"
        out["css_fidelity"] = css_fid

    if bool(opts.returnZipB64):
        files = {}
        if "html_semantic" in out: files["semantic.html"] = out["html_semantic"]
        if "css_semantic" in out: files["semantic.css"] = out["css_semantic"]
        if "html_fidelity" in out: files["fidelity.html"] = out["html_fidelity"]
        if "css_fidelity" in out: files["fidelity.css"] = out["css_fidelity"]
        out["zip_b64"] = make_zip_b64(files)

    doc.close()
    return out

# -----------------------------
# Handler 422 pour debug
# -----------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = (await request.body()).decode("utf-8", errors="ignore")
    logger.error("422 payload=%s errors=%s", body[:1000], exc.errors())
    return JSONResponse(status_code=422, content={"detail": exc.errors()})
