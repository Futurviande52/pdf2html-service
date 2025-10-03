from __future__ import annotations

import base64
import io
import re
import unicodedata
import zipfile

import fitz  # PyMuPDF
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="pdf2html-service", version="1.0.0")


def nfkc(text: str) -> str:
    return (
        unicodedata.normalize("NFKC", text or "")
        .replace("\u00AD", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )


class Options(BaseModel):
    ocr: bool = True
    mode: str = "both"
    injectLinks: bool = True
    promoteHeadings: bool = True
    graphEngine: str = "heuristic"
    locale: str = "fr-FR"
    returnZipB64: bool = True


class Payload(BaseModel):
    request_id: str
    filename: str | None = None
    pdf_b64: str | None = None
    pdf_url: str | None = None
    options: Options = Options()


@app.get("/health")
def health() -> dict[str, object]:
    return {"ok": True, "service": "pdf2html", "version": "1.0.0"}


@app.post("/pdf2html")
def pdf2html(payload: Payload) -> dict[str, object]:
    pdf_bytes = _load_pdf_bytes(payload)

    document = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[dict[str, object]] = []
    all_text: list[str] = []
    link_count = 0
    table_count = 0

    for page in document:
        width = page.rect.width
        height = page.rect.height

        links = _extract_links(page)
        link_count += len(links)

        tables = _extract_tables(page)
        table_count += len(tables)

        spans = _extract_spans(page, all_text)

        pages.append(
            {
                "size": [width, height],
                "spans": spans,
                "links": links,
                "tables": tables,
            }
        )

    html_semantic = _build_semantic_html(pages)
    html_fidelity = _build_fidelity_html(pages)

    semantic_text = nfkc(re.sub(r"<[^>]+>", "", html_semantic))
    truth_text = nfkc("".join(all_text))
    char_diff = 0 if semantic_text == truth_text else abs(len(semantic_text) - len(truth_text)) or 1

    zip_b64 = _build_zip_bundle(html_semantic, html_fidelity)

    return {
        "html_semantic": html_semantic,
        "css_semantic": ":root{--text:#111} article{line-height:1.45} table{border-collapse:collapse} td{border:1px solid #ddd;padding:.25em .5em}",
        "html_fidelity": html_fidelity,
        "css_fidelity": ".page{box-shadow:0 0 0 1px #eee;margin:12px 0}.page span{white-space:pre}",
        "metrics": {
            "pages": len(pages),
            "chars": len(semantic_text),
            "links": link_count,
            "tables": table_count,
        },
        "qa": {
            "char_diff": char_diff,
            "status": "ok" if char_diff == 0 else "needs_review",
        },
        "graph": {"edges": []},
        "zip_b64": zip_b64,
    }


def _load_pdf_bytes(payload: Payload) -> bytes:
    try:
        if payload.pdf_b64:
            return base64.b64decode(payload.pdf_b64)
        if payload.pdf_url:
            response = requests.get(payload.pdf_url, timeout=60)
            response.raise_for_status()
            return response.content
        raise HTTPException(400, "Provide 'pdf_b64' or 'pdf_url'")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Cannot read PDF: {exc}") from exc


def _extract_links(page: fitz.Page) -> list[dict[str, object]]:
    links: list[dict[str, object]] = []
    try:
        for link in page.get_links() or []:
            if link.get("uri") and link.get("from"):
                rect = link["from"]
                links.append(
                    {
                        "href": link["uri"],
                        "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    }
                )
    except Exception:  # noqa: BLE001
        return links
    return links


def _extract_tables(page: fitz.Page) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    try:
        table_finder = page.find_tables()
        for table in table_finder.tables:
            tables.append(table.extract())
    except Exception:  # noqa: BLE001
        return tables
    return tables


def _extract_spans(page: fitz.Page, all_text: list[str]) -> list[dict[str, object]]:
    spans: list[dict[str, object]] = []
    page_dict = page.get_text("dict")
    for block in page_dict.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = nfkc(span.get("text", ""))
                if not text:
                    continue
                all_text.append(text)
                color_value = span.get("color", (0, 0, 0))
                color = _format_color(color_value)
                x0, y0, x1, y1 = span.get("bbox", [0, 0, 0, 0])
                spans.append(
                    {
                        "text": text,
                        "font": span.get("font", ""),
                        "size": float(span.get("size", 12)),
                        "color": color,
                        "bbox": [x0, y0, x1, y1],
                    }
                )
    return spans


def _format_color(value: object) -> str:
    if isinstance(value, (list, tuple)):
        red = int(round(value[0] * 255))
        green = int(round(value[1] * 255))
        blue = int(round(value[2] * 255))
        return f"#{red:02X}{green:02X}{blue:02X}"
    return "#000000"


def _build_semantic_html(pages: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    buffer.write("<article>")
    for page in pages:
        for span in page["spans"]:
            text = (
                span["text"]
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            buffer.write(
                f'<p><span style="color:{span["color"]};font-size:{span["size"]}px">{text}</span></p>'
            )
        for table in page["tables"]:
            buffer.write("<table>")
            for row in table:
                escaped_cells = [
                    (cell or "")
                    .replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    for cell in row
                ]
                buffer.write("<tr>" + "".join(f"<td>{cell}</td>" for cell in escaped_cells) + "</tr>")
            buffer.write("</table>")
    buffer.write("</article>")
    return buffer.getvalue()


def _build_fidelity_html(pages: list[dict[str, object]]) -> str:
    buffer = io.StringIO()
    buffer.write('<div class="pdf">')
    for page in pages:
        width, height = page["size"]
        buffer.write(
            '<section class="page" style="position:relative;width:100%;padding-top:{:.2f}%">'.format(
                height / width * 100
            )
        )
        for span in page["spans"]:
            x0, y0, x1, y1 = span["bbox"]
            style = (
                "position:absolute;"
                f"left:{x0 / width * 100:.3f}%;"
                f"top:{y0 / height * 100:.3f}%;"
                f"width:{(x1 - x0) / width * 100:.3f}%;"
                f"height:{(y1 - y0) / height * 100:.3f}%;"
                f"color:{span['color']};"
                f"font-size:{span['size']}px"
            )
            text = (
                span["text"]
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            buffer.write(f'<span style="{style}">{text}</span>')
        buffer.write("</section>")
    buffer.write("</div>")
    return buffer.getvalue()


def _build_zip_bundle(html_semantic: str, html_fidelity: str) -> str | None:
    try:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("semantic.html", html_semantic)
            archive.writestr("fidelity.html", html_fidelity)
        return base64.b64encode(buffer.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return None

