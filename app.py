from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import base64, io, unicodedata, zipfile, requests, re
import fitz  # PyMuPDF

app = FastAPI(title="pdf2html-service", version="1.0.0")

def nfkc(s: str) -> str:
    return (
        unicodedata.normalize("NFKC", s or "")
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
def health():
    return {"ok": True, "service": "pdf2html", "version": "1.0.0"}

@app.post("/pdf2html")
def pdf2html(p: Payload):
    # 1) Lire les octets du PDF (base64 ou URL)
    try:
        if p.pdf_b64:
            pdf_bytes = base64.b64decode(p.pdf_b64)
        elif p.pdf_url:
            r = requests.get(p.pdf_url, timeout=60); r.raise_for_status()
            pdf_bytes = r.content
        else:
            raise HTTPException(400, "Provide 'pdf_b64' or 'pdf_url'")
    except Exception as e:
        raise HTTPException(400, f"Cannot read PDF: {e}")

    # 2) Parse PDF avec PyMuPDF
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages, all_text = [], []
    link_count, table_count = 0, 0
    for pg in doc:
        w, h = pg.rect.width, pg.rect.height
        links=[]
        try:
            for lk in pg.get_links() or []:
                if lk.get("uri") and lk.get("from"):
                    r = lk["from"]
                    links.append({"href": lk["uri"], "bbox":[r.x0,r.y0,r.x1,r.y1]})
        except: pass
        link_count += len(links)
        tables=[]
        try:
            tf = pg.find_tables()
            for t in tf.tables: tables.append(t.extract())
        except: pass
        table_count += len(tables)
        spans=[]
        d = pg.get_text("dict")
        for b in d.get("blocks", []):
            if b.get("type",0)!=0: continue
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    txt = nfkc(sp.get("text", ""))
                    if not txt:
                        continue
                    txt = nfkc(sp.get("text","")); if not txt: continue
                    all_text.append(txt)
                    col = sp.get("color",(0,0,0))
                    if isinstance(col,(list,tuple)):
                        r=int(round(col[0]*255)); g=int(round(col[1]*255)); b=int(round(col[2]*255))
                        color=f"#{r:02X}{g:02X}{b:02X}"
                    else: color="#000000"
                    x0,y0,x1,y1 = sp.get("bbox",[0,0,0,0])
                    spans.append({"text":txt,"font":sp.get("font",""),"size":float(sp.get("size",12)),
                                  "color":color,"bbox":[x0,y0,x1,y1]})
        pages.append({"size":[w,h],"spans":spans,"links":links,"tables":tables})

    # 3) Constructions HTML (simple)
    def build_semantic(pages):
        out=io.StringIO(); out.write("<article>")
        for p in pages:
            for s in p["spans"]:
                t=s["text"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
                out.write(f'<p><span style="color:{s["color"]};font-size:{s["size"]}px">{t}</span></p>')
            for t in p["tables"]:
                out.write("<table>")
                for row in t:
                    out.write("<tr>"+"".join(f"<td>{(c or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')}</td>" for c in row)+"</tr>")
                out.write("</table>")
        out.write("</article>")
        return out.getvalue()
        out.write("</article>"); return out.getvalue()

    def build_fidelity(pages):
        out=io.StringIO(); out.write('<div class="pdf">')
        for p in pages:
            w,h=p["size"]; out.write(f'<section class="page" style="position:relative;width:100%;padding-top:{h/w*100:.2f}%">')
            for s in p["spans"]:
                x0,y0,x1,y1=s["bbox"]
                style=f'position:absolute;left:{x0/w*100:.3f}%;top:{y0/h*100:.3f}%;width:{(x1-x0)/w*100:.3f}%;height:{(y1-y0)/h*100:.3f}%;color:{s["color"]};font-size:{s["size"]}px'
                t=s["text"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
                out.write(f'<span style="{style}">{t}</span>')
            out.write("</section>")
        out.write("</div>")
        return out.getvalue()
        out.write("</div>"); return out.getvalue()

    html_sem = build_semantic(pages)
    html_fid = build_fidelity(pages)

    # 4) QA: char_diff (NFKC)
    inner = nfkc(re.sub(r"<[^>]+>","", html_sem))
    truth = nfkc("".join(all_text))
    char_diff = 0 if inner==truth else abs(len(inner)-len(truth)) or 1

    # 5) ZIP optionnel
    zip_b64=None
    try:
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
            z.writestr("semantic.html",html_sem)
            z.writestr("fidelity.html",html_fid)
        zip_b64 = base64.b64encode(buf.getvalue()).decode()
    except: pass

    return {
        "html_semantic": html_sem,
        "css_semantic": ":root{--text:#111} article{line-height:1.45} table{border-collapse:collapse} td{border:1px solid #ddd;padding:.25em .5em}",
        "html_fidelity": html_fid,
        "css_fidelity": ".page{box-shadow:0 0 0 1px #eee;margin:12px 0}.page span{white-space:pre}",
        "metrics": {"pages": len(pages), "chars": len(inner), "links": link_count, "tables": table_count},
        "qa": {"char_diff": char_diff, "status": "ok" if char_diff==0 else "needs_review"},
        "graph": {"edges": []},
        "zip_b64": zip_b64
    }

