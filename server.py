#!/usr/bin/env python3
"""PDFairy local processing server. Files live only for the duration of a request."""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import fitz
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
MAX_BODY = 200 * 1024 * 1024
MAX_FILE = 50 * 1024 * 1024
MAX_PAGES = 100
MAX_PIXELS = 80_000_000


class UserError(Exception):
    pass


class Upload:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self.file = io.BytesIO(data)


class MultipartForm(dict):
    def getfirst(self, name, default=""):
        value = self.get(name, default)
        if isinstance(value, list):
            return value[0] if value else default
        return value


def parse_multipart(content_type: str, body: bytes) -> MultipartForm:
    raw = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.is_multipart():
        raise UserError("The multipart upload is invalid.")
    form = MultipartForm()
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        value = Upload(Path(filename).name, payload) if filename is not None else payload.decode(part.get_content_charset() or "utf-8", "replace")
        if name in form:
            if not isinstance(form[name], list):
                form[name] = [form[name]]
            form[name].append(value)
        else:
            form[name] = value
    return form


def safe_stem(name: str) -> str:
    stem = Path(name or "file").stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
    return (stem or "file")[:80]


def get_files(form) -> list[tuple[str, bytes]]:
    if "files" not in form:
        raise UserError("No file was received.")
    values = form["files"] if isinstance(form["files"], list) else [form["files"]]
    result = []
    for item in values:
        name = Path(item.filename or "file").name
        data = item.file.read(MAX_FILE + 1)
        if not data:
            raise UserError(f"{name} is empty.")
        if len(data) > MAX_FILE:
            raise UserError(f"{name} is larger than the 50 MB limit.")
        result.append((name, data))
    return result


def get_one_file(form) -> tuple[str, bytes]:
    files = get_files(form)
    if len(files) != 1:
        raise UserError("This tool accepts exactly one file. Use Merge PDF to combine files.")
    return files[0]


def open_image(name: str, data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise UserError(f"{name} exceeds the 80-megapixel image limit.")
        image.load()
        return image
    except UserError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise UserError(f"{name} is damaged or too large.") from exc


def text_from_upload(name: str, data: bytes) -> str:
    if b"\x00" in data:
        raise UserError(f"{name} is not a plain-text UTF-8 file.")
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise UserError(f"{name} is not a valid UTF-8 text file.") from exc


def as_int(value: str, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise UserError(f"{label} must be a whole number.") from exc


def require_pdf(name: str, data: bytes) -> None:
    if not data.startswith(b"%PDF-"):
        raise UserError(f"{name} is not a valid PDF file.")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            raise UserError(f"{name} is password protected. Unlock it before using this tool.")
        if doc.page_count > MAX_PAGES:
            raise UserError(f"{name} has more than the {MAX_PAGES}-page limit.")
        doc.close()
    except UserError:
        raise
    except Exception as exc:
        raise UserError(f"{name} is damaged or unsupported.") from exc


def parse_pages(spec: str, total: int) -> list[int]:
    if not spec.strip():
        raise UserError("Enter the pages to extract, such as 1, 3-5.")
    pages = []
    for token in spec.replace("–", "-").split(","):
        token = token.strip()
        if re.fullmatch(r"\d+", token):
            pages.append(int(token))
        elif re.fullmatch(r"\d+\s*-\s*\d+", token):
            a, b = map(int, re.split(r"\s*-\s*", token))
            if a > b:
                raise UserError(f"Invalid descending page range: {token}.")
            pages.extend(range(a, b + 1))
        else:
            raise UserError(f"Invalid page value: {token or '(blank)'}.")
    pages = list(dict.fromkeys(pages))
    if any(p < 1 or p > total for p in pages):
        raise UserError(f"Page numbers must be between 1 and {total}.")
    return [p - 1 for p in pages]


def add_page_numbers(data: bytes) -> bytes:
    doc = fitz.open(stream=data, filetype="pdf")
    for i, page in enumerate(doc):
        rect = page.rect
        page.insert_textbox(fitz.Rect(0, rect.height - 28, rect.width, rect.height - 8), str(i + 1), fontsize=9, align=1, color=(0.25, 0.2, 0.22))
    out = doc.tobytes(garbage=4, deflate=True)
    doc.close()
    return out


def make_text_pdf(text: str) -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    width, height = A4
    y = height - 58
    for raw in text.splitlines() or [""]:
        words, line = raw.split(), ""
        lines = []
        for word in words:
            trial = f"{line} {word}".strip()
            if c.stringWidth(trial, "Helvetica", 11) > width - 100:
                lines.append(line); line = word
            else:
                line = trial
        lines.append(line)
        for value in lines:
            if y < 48:
                c.showPage(); y = height - 58
            c.drawString(50, y, value); y -= 16
        y -= 3
    c.save()
    return out.getvalue()


def render_archive(data: bytes, fmt: str, stem: str) -> tuple[bytes, str, str]:
    doc = fitz.open(stream=data, filetype="pdf")
    outputs = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
        img = pix.tobytes("png" if fmt == "png" else "jpeg", jpg_quality=90)
        outputs.append((f"{stem}-page-{i+1}.{fmt}", img))
    doc.close()
    if len(outputs) == 1:
        return outputs[0][1], outputs[0][0], f"image/{'jpeg' if fmt == 'jpg' else 'png'}"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in outputs:
            zf.writestr(name, content)
    return out.getvalue(), f"{stem}-pages.zip", "application/zip"


def flatten_pdf(data: bytes, quality: int, scale: float) -> bytes:
    source = fitz.open(stream=data, filetype="pdf")
    target = fitz.open()
    for page in source:
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = pix.tobytes("jpeg", jpg_quality=quality)
        new = target.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=image)
    out = target.tobytes(garbage=4, deflate=True)
    source.close(); target.close()
    return out


class Handler(SimpleHTTPRequestHandler):
    server_version = "PDFairy/2.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/health":
            return self.send_json({"status": "ok", "version": 2})
        return super().do_GET()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise UserError("Request is empty or exceeds the 200 MB total limit.")
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise UserError("Expected a multipart file upload.")
            form = parse_multipart(content_type, self.rfile.read(length))
            routes = {
                "/api/convert": self.convert,
                "/api/compress": self.compress,
                "/api/merge": self.merge,
                "/api/split": self.split,
                "/api/sign": self.sign,
                "/api/render": self.render_pdf,
                "/api/edit": self.edit_pdf,
            }
            path = urlparse(self.path).path
            if path not in routes:
                return self.send_json({"error": "Unknown API route."}, 404)
            routes[path](form)
        except UserError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.log_error("processing error: %s", exc)
            self.send_json({"error": "The file could not be processed. It may be damaged or unsupported."}, 422)

    def field(self, form, name, default=""):
        return form.getfirst(name, default)

    def convert(self, form):
        name, data = get_one_file(form)
        fmt = self.field(form, "format").lower()
        suffix, stem = Path(name).suffix.lower(), safe_stem(name)
        if suffix in {".jpg", ".jpeg", ".png", ".webp"} and fmt == "pdf":
            image = open_image(name, data)
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, "white");
                if "A" in image.getbands(): background.paste(image, mask=image.getchannel("A"))
                else: background.paste(image.convert("RGB"))
                image = background
            out = io.BytesIO(); image.save(out, "PDF", resolution=96)
            return self.send_file(out.getvalue(), f"{stem}-pdfairy.pdf", "application/pdf")
        if suffix == ".txt" and fmt == "pdf":
            return self.send_file(make_text_pdf(text_from_upload(name, data)), f"{stem}-pdfairy.pdf", "application/pdf")
        if suffix == ".pdf":
            require_pdf(name, data)
            doc = fitz.open(stream=data, filetype="pdf")
            if fmt == "txt":
                text = "\n\n".join(page.get_text() for page in doc); doc.close()
                return self.send_file(text.encode("utf-8"), f"{stem}-pdfairy.txt", "text/plain; charset=utf-8")
            if fmt == "docx":
                from docx import Document
                document = Document()
                for i, page in enumerate(doc):
                    if i: document.add_page_break()
                    for line in page.get_text().splitlines(): document.add_paragraph(line)
                doc.close(); out = io.BytesIO(); document.save(out)
                return self.send_file(out.getvalue(), f"{stem}-pdfairy.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            if fmt in {"png", "jpg"}:
                doc.close(); content, out_name, mime = render_archive(data, fmt, f"{stem}-pdfairy")
                return self.send_file(content, out_name, mime)
            doc.close()
        if suffix in {".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"} and fmt == "pdf":
            office = shutil.which("soffice") or shutil.which("libreoffice")
            if not office: raise UserError("LibreOffice is required for this conversion. See README.md.")
            with tempfile.TemporaryDirectory(prefix="pdfairy-") as temp:
                src = Path(temp) / f"input{suffix}"; src.write_bytes(data)
                run = subprocess.run([office, "--headless", "--convert-to", "pdf", "--outdir", temp, str(src)], capture_output=True, timeout=90)
                out_path = src.with_suffix(".pdf")
                if run.returncode or not out_path.exists(): raise UserError("LibreOffice could not convert this document.")
                return self.send_file(out_path.read_bytes(), f"{stem}-pdfairy.pdf", "application/pdf")
        raise UserError("That input and output format combination is not supported.")

    def compress(self, form):
        name, data = get_one_file(form); require_pdf(name, data)
        target_raw = self.field(form, "target_bytes")
        target = as_int(target_raw, "Target size") if target_raw else None
        if target is not None and target <= 0: raise UserError("Target size must be greater than zero.")
        quality = float(self.field(form, "quality", ".65")); preserve = self.field(form, "preserve", "true") == "true"
        doc = fitz.open(stream=data, filetype="pdf"); optimized = doc.tobytes(garbage=4, deflate=True, clean=True); doc.close()
        if target and len(data) <= target: out = data
        elif target and len(optimized) <= target: out = optimized
        elif target:
            out = None
            scales = [1.45, 1.2, 1.0] if preserve else [1.2, .95, .75, .55]
            for scale in scales:
                for q in [80, 65, 50, 38, 28]:
                    candidate = flatten_pdf(data, q, scale)
                    if len(candidate) <= target:
                        out = candidate; break
                if out: break
            if out is None: raise UserError("That target is too small for this PDF. Choose a larger target or turn off quality preservation.")
        elif quality >= .8: out = optimized
        else: out = flatten_pdf(data, 62 if quality >= .6 else 38, 1.2 if quality >= .6 else .85)
        if not target and len(out) >= len(data): out = data
        return self.send_file(out, f"{safe_stem(name)}-compressed.pdf", "application/pdf")

    def merge(self, form):
        files = get_files(form)
        if len(files) < 2: raise UserError("Choose at least two PDFs to merge.")
        writer = PdfWriter()
        for name, data in files:
            require_pdf(name, data)
            reader = PdfReader(io.BytesIO(data))
            for page in reader.pages: writer.add_page(page)
        out = io.BytesIO(); writer.write(out); content = out.getvalue()
        if self.field(form, "page_numbers") == "true": content = add_page_numbers(content)
        return self.send_file(content, "pdfairy-merged.pdf", "application/pdf")

    def split(self, form):
        name, data = get_one_file(form); require_pdf(name, data); reader = PdfReader(io.BytesIO(data)); stem = safe_stem(name)
        if self.field(form, "mode") == "every":
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(reader.pages):
                    writer = PdfWriter(); writer.add_page(page); one = io.BytesIO(); writer.write(one); zf.writestr(f"{stem}-page-{i+1}.pdf", one.getvalue())
            return self.send_file(out.getvalue(), f"{stem}-split.zip", "application/zip")
        pages = parse_pages(self.field(form, "pages"), len(reader.pages)); writer = PdfWriter()
        for p in pages: writer.add_page(reader.pages[p])
        out = io.BytesIO(); writer.write(out)
        return self.send_file(out.getvalue(), f"{stem}-pages.pdf", "application/pdf")

    def sign(self, form):
        name, data = get_one_file(form); require_pdf(name, data); signature = self.field(form, "signature").strip()[:80]
        if not signature: raise UserError("Type your signature first.")
        if not signature.isascii(): raise UserError("Typed signatures currently support Latin characters only. Use a Latin-script signature.")
        doc = fitz.open(stream=data, filetype="pdf"); page_no = as_int(self.field(form, "page", "1"), "Page number") - 1
        if page_no < 0 or page_no >= doc.page_count: raise UserError(f"Page number must be between 1 and {doc.page_count}.")
        page = doc[page_no]; pos = self.field(form, "position", "bottom-right")
        if pos not in {"bottom-right", "bottom-left", "top-right", "top-left"}: raise UserError("Choose a valid signature position.")
        right = pos.endswith("right"); bottom = pos.startswith("bottom")
        x = page.rect.width - 190 if right else 45; y = page.rect.height - 55 if bottom else 65
        if fitz.get_text_length(signature, fontname="tiro", fontsize=22) > 175: raise UserError("This signature is too long to fit. Use 20 characters or fewer.")
        page.insert_text((x, y), signature, fontsize=22, fontname="Times-Italic", color=(.35, .12, .22))
        out = doc.tobytes(garbage=4, deflate=True); doc.close()
        return self.send_file(out, f"{safe_stem(name)}-signed.pdf", "application/pdf")

    def render_pdf(self, form):
        name, data = get_one_file(form); require_pdf(name, data); doc = fitz.open(stream=data, filetype="pdf")
        pages = []
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(1.25, 1.25), alpha=False)
            encoded = base64.b64encode(pix.tobytes("jpeg", jpg_quality=78)).decode("ascii")
            pages.append({"width": pix.width, "height": pix.height, "image": f"data:image/jpeg;base64,{encoded}"})
        doc.close(); self.send_json({"pages": pages})

    def edit_pdf(self, form):
        name, data = get_one_file(form); require_pdf(name, data)
        try: operations = json.loads(self.field(form, "operations", "[]"))
        except json.JSONDecodeError as exc: raise UserError("The edit data is invalid.") from exc
        if not isinstance(operations, list) or len(operations) > 500: raise UserError("Too many or invalid edits.")
        doc = fitz.open(stream=data, filetype="pdf")
        for op in operations:
            try:
                if not isinstance(op, dict) or op.get("type") not in {"text", "sign", "redact"}: raise UserError("Each edit must be text, sign or redact.")
                page_no = as_int(op["page"], "Edit page")
                if page_no < 0 or page_no >= doc.page_count: raise UserError("Edit page is outside this PDF.")
                page = doc[page_no]; typ = op["type"]
                x = float(op["x"]); y = float(op["y"])
                if not (0 <= x <= 1 and 0 <= y <= 1): raise UserError("Edit coordinates must be inside the page.")
                if typ in {"text", "sign"}:
                    text = str(op.get("text", "")).strip()
                    if not text: raise UserError("Text edits cannot be empty.")
                    if not text.isascii(): raise UserError("Typed editor text currently supports Latin characters only.")
                if typ == "redact":
                    w = float(op["w"]); h = float(op["h"])
                    if not (.002 <= w <= 1-x and .002 <= h <= 1-y): raise UserError("Redaction must stay inside the page.")
                    page.add_redact_annot(fitz.Rect(x*page.rect.width, y*page.rect.height, (x+w)*page.rect.width, (y+h)*page.rect.height), fill=(0,0,0))
            except (KeyError, ValueError, TypeError, IndexError) as exc: raise UserError("One of the edits is invalid.") from exc
        for page in doc: page.apply_redactions()
        for op in operations:
            if op.get("type") in {"text", "sign"}:
                page = doc[int(op["page"])]; x=float(op["x"])*page.rect.width; y=float(op["y"])*page.rect.height
                text = str(op.get("text", ""))[:500]
                page.insert_text((x,y),text,fontsize=18 if op["type"]=="sign" else 12,fontname="Times-Italic" if op["type"]=="sign" else "helv",color=(.25,.08,.16) if op["type"]=="sign" else (0,0,0))
        out = doc.tobytes(garbage=4, deflate=True, clean=True); doc.close()
        return self.send_file(out, f"{safe_stem(name)}-edited.pdf", "application/pdf")

    def send_json(self, value, status=200):
        data = json.dumps(value).encode("utf-8"); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def send_file(self, data: bytes, name: str, mime: str):
        if not data: raise UserError("Processing produced an empty output.")
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(data))); self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"'); self.send_header("X-Output-Name", ascii_name); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)


def main():
    host = os.getenv("PDFAIRY_HOST", "127.0.0.1"); port = int(os.getenv("PDFAIRY_PORT", "8080"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"PDFairy is ready at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
