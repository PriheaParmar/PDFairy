#!/usr/bin/env python3
"""Dependency-light PDFairy API integration tests. Start server.py before running."""
import io
import json
import shutil
import sys
import uuid
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen import canvas

BASE = "http://127.0.0.1:8080"


def sample_pdf(pages=2):
    out = io.BytesIO(); c = canvas.Canvas(out)
    for i in range(pages):
        c.drawString(72, 720, f"PDFairy test page {i + 1}"); c.showPage()
    c.save(); return out.getvalue()


def sample_png():
    out = io.BytesIO(); Image.new("RGBA", (180, 120), (220, 90, 140, 150)).save(out, "PNG"); return out.getvalue()


def post(path, files, fields=None):
    boundary = "----PDFairy" + uuid.uuid4().hex
    body = io.BytesIO()
    for key, value in (fields or {}).items():
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    for name, data, mime in files:
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n".encode()); body.write(data); body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = Request(BASE + path, data=body.getvalue(), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urlopen(req, timeout=60) as response:
        return response.read(), dict(response.headers)


def check(name, fn):
    try: fn(); print("PASS", name); return True
    except Exception as exc: print("FAIL", name, "—", exc); return False


PDF1, PDF2 = sample_pdf(2), sample_pdf(1)


def main():
    tests = []
    tests.append(check("health", lambda: json.loads(urlopen(BASE + "/api/health").read())["status"] == "ok" or (_ for _ in ()).throw(AssertionError())))
    def merge():
        data,_=post("/api/merge",[("one.pdf",PDF1,"application/pdf"),("two.pdf",PDF2,"application/pdf")],{"page_numbers":"true"}); assert data.startswith(b"%PDF-"); assert len(PdfReader(io.BytesIO(data)).pages)==3
    tests.append(check("merge PDFs and preserve all pages",merge))
    def split():
        data,_=post("/api/split",[("one.pdf",PDF1,"application/pdf")],{"mode":"extract","pages":"2"}); assert len(PdfReader(io.BytesIO(data)).pages)==1
    tests.append(check("extract PDF page",split))
    def split_zip():
        data,_=post("/api/split",[("one.pdf",PDF1,"application/pdf")],{"mode":"every","pages":""}); assert len(zipfile.ZipFile(io.BytesIO(data)).namelist())==2
    tests.append(check("split every page to ZIP",split_zip))
    def sign():
        data,_=post("/api/sign",[("one.pdf",PDF1,"application/pdf")],{"signature":"Priya","page":"1","position":"bottom-right"}); assert b"Priya" in PdfReader(io.BytesIO(data)).pages[0].extract_text().encode()
    tests.append(check("typed PDF signature persists",sign))
    def redact_edit():
        ops=json.dumps([{"type":"redact","page":0,"x":.05,"y":.05,"w":.3,"h":.12},{"type":"text","page":0,"x":.2,"y":.4,"text":"Added by PDFairy"}]); data,_=post("/api/edit",[("one.pdf",PDF1,"application/pdf")],{"operations":ops}); assert data.startswith(b"%PDF-"); assert "Added by PDFairy" in PdfReader(io.BytesIO(data)).pages[0].extract_text()
    tests.append(check("edit and export PDF",redact_edit))
    def render():
        data,_=post("/api/render",[("one.pdf",PDF1,"application/pdf")]); pages=json.loads(data)["pages"]; assert len(pages)==2 and pages[0]["image"].startswith("data:image/jpeg;base64,")
    tests.append(check("render real PDF pages",render))
    def txt_pdf():
        data,_=post("/api/convert",[("notes.txt",b"Hello PDFairy","text/plain")],{"format":"pdf"}); assert data.startswith(b"%PDF-")
    tests.append(check("TXT to valid PDF",txt_pdf))
    def image_pdf():
        data,_=post("/api/convert",[("image.png",sample_png(),"image/png")],{"format":"pdf"}); assert data.startswith(b"%PDF-")
    tests.append(check("PNG to valid PDF",image_pdf))
    def pdf_text():
        data,_=post("/api/convert",[("one.pdf",PDF1,"application/pdf")],{"format":"txt"}); assert b"PDFairy test page 1" in data
    tests.append(check("PDF text extraction",pdf_text))
    def pdf_docx():
        data,_=post("/api/convert",[("one.pdf",PDF1,"application/pdf")],{"format":"docx"}); assert zipfile.is_zipfile(io.BytesIO(data))
    tests.append(check("PDF to valid DOCX container",pdf_docx))
    def pdf_images():
        data,_=post("/api/convert",[("one.pdf",PDF1,"application/pdf")],{"format":"png"}); assert zipfile.is_zipfile(io.BytesIO(data)); assert len(zipfile.ZipFile(io.BytesIO(data)).namelist())==2
    tests.append(check("PDF pages to PNG ZIP",pdf_images))
    def compress():
        data,_=post("/api/compress",[("one.pdf",PDF1,"application/pdf")],{"quality":".85","preserve":"true"}); assert data.startswith(b"%PDF-")
    tests.append(check("PDF compression output is valid",compress))
    if shutil.which("soffice") or shutil.which("libreoffice"):
        def office_pdf():
            from docx import Document
            source=io.BytesIO(); document=Document(); document.add_heading("PDFairy Office test",0); document.save(source)
            data,_=post("/api/convert",[("document.docx",source.getvalue(),"application/vnd.openxmlformats-officedocument.wordprocessingml.document")],{"format":"pdf"}); assert data.startswith(b"%PDF-") and len(PdfReader(io.BytesIO(data)).pages)>=1
        tests.append(check("DOCX to valid PDF through LibreOffice",office_pdf))
    def reject_bad():
        try: post("/api/merge",[("fake.pdf",b"not pdf","application/pdf"),("two.pdf",PDF2,"application/pdf")]); raise AssertionError("invalid PDF accepted")
        except HTTPError as exc: assert exc.code==400 and b"not a valid PDF" in exc.read()
    tests.append(check("invalid PDF is rejected clearly",reject_bad))
    passed=sum(tests); print(f"\n{passed}/{len(tests)} integration tests passed")
    return 0 if passed==len(tests) else 1


if __name__=="__main__": sys.exit(main())
