# pdf2html-service
Minimal FastAPI service (PyMuPDF) to convert PDF → HTML.

## Local
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload
# http://127.0.0.1:8000/health

## Render (no Docker)
Build: pip install -r requirements.txt
Start: uvicorn app:app --host 0.0.0.0 --port $PORT
Health: /health

## Test
curl -X POST "https://<your-service>.onrender.com/pdf2html" \
  -H "Content-Type: application/json" \
  -d '{"pdf_url":"https://example.com/sample.pdf","request_id":"demo-1"}'
