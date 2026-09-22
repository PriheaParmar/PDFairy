#!/usr/bin/env python3
"""PDFairy local processing server. Files live only for the duration of a request."""
from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from email import policy
from email.parser import BytesParser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape as xml_escape

import pymupdf as fitz
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
VERSION = "2.0"
ENVIRONMENT = os.getenv("PDFAIRY_ENV", "development").strip().lower() or "development"
PUBLIC_URL = (os.getenv("PDFAIRY_PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")
if PUBLIC_URL and (urlparse(PUBLIC_URL).scheme not in {"http", "https"} or not urlparse(PUBLIC_URL).netloc):
    raise RuntimeError("PDFAIRY_PUBLIC_URL must be an absolute http(s) URL.")
MAX_BODY = int(os.getenv("PDFAIRY_MAX_BODY_MB", "80")) * 1024 * 1024
MAX_FILE = int(os.getenv("PDFAIRY_MAX_FILE_MB", "50")) * 1024 * 1024
MAX_OUTPUT = int(os.getenv("PDFAIRY_MAX_OUTPUT_MB", "100")) * 1024 * 1024
MAX_PAGES = int(os.getenv("PDFAIRY_MAX_PAGES", "100"))
MAX_FILES = int(os.getenv("PDFAIRY_MAX_FILES", "20"))
MAX_CONCURRENT = int(os.getenv("PDFAIRY_MAX_CONCURRENT", "2"))
MAX_PIXELS = 80_000_000
REQUEST_TIMEOUT = int(os.getenv("PDFAIRY_REQUEST_TIMEOUT", "120"))
OFFICE_TIMEOUT = int(os.getenv("PDFAIRY_OFFICE_TIMEOUT", "90"))
OFFICE_ENABLED = os.getenv("PDFAIRY_ENABLE_OFFICE", "false").lower() == "true"
LOG_REQUESTS = os.getenv("PDFAIRY_LOG_REQUESTS", "false").lower() == "true"
TRUSTED_PROXIES = {value.strip() for value in os.getenv("PDFAIRY_TRUSTED_PROXIES", "").split(",") if value.strip()}
RENDER_PROXY_MODE = os.getenv("PDFAIRY_PROXY_MODE", "").strip().lower() == "render"
IS_RENDER_WEB = (
    os.getenv("RENDER", "").strip().lower() == "true"
    and os.getenv("RENDER_SERVICE_TYPE", "").strip().lower() == "web"
)
TEMP_ROOT = Path(os.getenv("PDFAIRY_TEMP_DIR") or tempfile.gettempdir()).expanduser().resolve()
TEMP_ROOT.mkdir(parents=True, exist_ok=True)
ACTIVE_PROCESSES: set[subprocess.Popen] = set()
ACTIVE_PROCESSES_LOCK = threading.Lock()
STATIC_FILES = {
    "/": "index.html", "/index.html": "index.html", "/styles.css": "styles.css",
    "/app.js": "app.js", "/favicon.svg": "favicon.svg",
    "/pdfairy-fairy-logo.png": "pdfairy-fairy-logo.png",
    "/pdfairy-favicon.png": "pdfairy-favicon.png", "/robots.txt": "robots.txt",
    "/privacy.html": "privacy.html", "/terms.html": "terms.html",
}
OOXML_MARKERS = {
    ".docx": "word/document.xml", ".pptx": "ppt/presentation.xml", ".xlsx": "xl/workbook.xml",
}
LEGACY_OFFICE = {".doc", ".ppt", ".xls"}
OFFICE_SUFFIXES = set(OOXML_MARKERS) | LEGACY_OFFICE


def office_executable() -> str | None:
    configured = os.getenv("PDFAIRY_LIBREOFFICE_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return str(path.resolve()) if path.is_file() else None
    return shutil.which("soffice") or shutil.which("libreoffice")


def office_available() -> bool:
    return OFFICE_ENABLED and office_executable() is not None


def log_event(event: str, **fields) -> None:
    if LOG_REQUESTS:
        logging.getLogger("pdfairy").info(json.dumps({"event": event, **fields}, separators=(",", ":")))


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
    parts = list(message.iter_parts())
    if len(parts) > MAX_FILES + 20:
        raise UserError("The upload contains too many fields or files.")
    for part in parts:
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        value = Upload(clean_filename(filename), payload) if filename is not None else payload.decode(part.get_content_charset() or "utf-8", "replace")[:10_000]
        if name in form:
            if not isinstance(form[name], list):
                form[name] = [form[name]]
            form[name].append(value)
        else:
            form[name] = value
    return form


def clean_filename(name: str | None) -> str:
    value = unicodedata.normalize("NFC", (name or "file").replace("\\", "/").rsplit("/", 1)[-1])
    value = "".join(char for char in value if char >= " " and char not in "\x7f")
    value = value.strip(" .") or "file"
    suffix = Path(value).suffix[:16]
    stem_limit = max(1, 180 - len(suffix))
    return f"{Path(value).stem[:stem_limit]}{suffix}"


def safe_stem(name: str) -> str:
    stem = Path(name or "file").stem
    stem = re.sub(r"[^\w.-]+", "-", stem, flags=re.UNICODE).strip("-.")
    return (stem or "file")[:80]


def get_files(form) -> list[tuple[str, bytes]]:
    if "files" not in form:
        raise UserError("No file was received.")
    values = form["files"] if isinstance(form["files"], list) else [form["files"]]
    if len(values) > MAX_FILES:
        raise UserError(f"Choose no more than {MAX_FILES} files at once.")
    result = []
    for item in values:
        if not isinstance(item, Upload):
            raise UserError("The file upload is malformed.")
        name = clean_filename(item.filename)
        data = item.file.read(MAX_FILE + 1)
        if not data:
            raise UserError(f"{name} is empty.")
        if len(data) > MAX_FILE:
            raise UserError(f"{name} is larger than the 50 MB limit.")
        result.append((name, data))
    return result


def require_extension(name: str, allowed: set[str]) -> str:
    suffix = Path(name).suffix.lower()
    if not suffix or suffix not in allowed:
        readable = ", ".join(sorted(value.lstrip(".").upper() for value in allowed))
        raise UserError(f"Choose a supported file type: {readable}.")
    return suffix


def require_office(name: str, data: bytes) -> str:
    suffix = require_extension(name, set(OOXML_MARKERS) | LEGACY_OFFICE)
    if suffix in OOXML_MARKERS:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 10_000 or sum(item.file_size for item in entries) > 150 * 1024 * 1024:
                    raise UserError("This Office document expands beyond the safe processing limit.")
                names = {item.filename for item in entries}
                if "[Content_Types].xml" not in names or OOXML_MARKERS[suffix] not in names:
                    raise UserError("The Office document does not match its file extension.")
        except (zipfile.BadZipFile, OSError) as exc:
            raise UserError("The Office document is damaged or does not match its extension.") from exc
    elif not data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise UserError("The legacy Office document does not match its file extension.")
    return suffix


def get_one_file(form) -> tuple[str, bytes]:
    files = get_files(form)
    if len(files) != 1:
        raise UserError("This tool accepts exactly one file. Use Merge PDF to combine files.")
    return files[0]


def open_image(name: str, data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        suffix = require_extension(name, {".jpg", ".jpeg", ".png", ".webp"})
        expected = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP"}[suffix]
        if image.format != expected:
            raise UserError("The image contents do not match its file extension.")
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


def as_float(value: str, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise UserError(f"{label} must be a number.") from exc


def require_pdf(name: str, data: bytes) -> None:
    require_extension(name, {".pdf"})
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


def require_raster_safe(page, scale: float) -> None:
    width = max(1, int(page.rect.width * scale))
    height = max(1, int(page.rect.height * scale))
    if width * height > MAX_PIXELS:
        raise UserError("A PDF page is too large to render safely.")


def cleanup_stale_tempdirs(max_age_seconds: int = 24 * 60 * 60) -> None:
    cutoff = time.time() - max_age_seconds
    for path in TEMP_ROOT.glob("pdfairy-office-*"):
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            pass


def terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def terminate_active_processes() -> None:
    with ACTIVE_PROCESSES_LOCK:
        processes = list(ACTIVE_PROCESSES)
    for process in processes:
        terminate_process_tree(process)


def validate_generated_pdf(data: bytes) -> None:
    if not data or len(data) > MAX_OUTPUT or not data.startswith(b"%PDF-"):
        raise UserError("LibreOffice produced an invalid or oversized PDF.")
    try:
        document = fitz.open(stream=data, filetype="pdf")
        if document.needs_pass or not 1 <= document.page_count <= MAX_PAGES:
            raise UserError("LibreOffice produced an invalid or oversized PDF.")
        document.close()
    except UserError:
        raise
    except Exception as exc:
        raise UserError("LibreOffice produced an invalid PDF.") from exc


def convert_office_document(data: bytes, suffix: str) -> bytes:
    executable = office_executable() if OFFICE_ENABLED else None
    if not executable:
        raise UserError("Office conversion is unavailable on this server. PDF tools remain available.")
    if suffix not in OFFICE_SUFFIXES:
        raise UserError("Choose a supported Office document.")
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="pdfairy-office-", dir=TEMP_ROOT) as temp:
            work = Path(temp)
            input_dir, output_dir, profile_dir = work / "input", work / "output", work / "profile"
            input_dir.mkdir(); output_dir.mkdir(); profile_dir.mkdir()
            source = input_dir / f"input{suffix}"
            source.write_bytes(data)
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            try:
                process = subprocess.Popen(
                    [
                        executable, f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                        "--headless", "--nologo", "--nodefault", "--nolockcheck", "--norestore",
                        "--convert-to", "pdf", "--outdir", str(output_dir), str(source),
                    ],
                    cwd=str(work), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    shell=False, start_new_session=os.name != "nt", creationflags=creationflags,
                )
            except OSError as exc:
                raise UserError("Office conversion could not start.") from exc
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.add(process)
            try:
                process.communicate(timeout=OFFICE_TIMEOUT)
            except subprocess.TimeoutExpired as exc:
                terminate_process_tree(process)
                raise UserError("Office conversion timed out. Try a smaller document.") from exc
            finally:
                with ACTIVE_PROCESSES_LOCK:
                    ACTIVE_PROCESSES.discard(process)
            if process.returncode:
                raise UserError("LibreOffice could not convert this document.")
            generated = [path for path in output_dir.iterdir() if path.is_file()]
            expected = output_dir / "input.pdf"
            if generated != [expected]:
                raise UserError("LibreOffice produced unexpected output.")
            content = expected.read_bytes()
            validate_generated_pdf(content)
            return content
    finally:
        if process is not None:
            with ACTIVE_PROCESSES_LOCK:
                ACTIVE_PROCESSES.discard(process)
            terminate_process_tree(process)


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
    output_size = 0
    for i, page in enumerate(doc):
        require_raster_safe(page, 1.7)
        pix = page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
        img = pix.tobytes("png" if fmt == "png" else "jpeg", jpg_quality=90)
        output_size += len(img)
        if output_size > MAX_OUTPUT:
            doc.close()
            raise UserError("The rendered pages exceed the output limit. Try a smaller PDF.")
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
    image_bytes = 0
    for page in source:
        require_raster_safe(page, scale)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        image = pix.tobytes("jpeg", jpg_quality=quality)
        image_bytes += len(image)
        if image_bytes > MAX_OUTPUT:
            source.close(); target.close()
            raise UserError("The compressed output exceeds the output limit.")
        new = target.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=image)
    out = target.tobytes(garbage=4, deflate=True)
    source.close(); target.close()
    return out


class Handler(SimpleHTTPRequestHandler):
    server_version = "PDFairy/2.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    work_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def setup(self):
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT)

    def send_response(self, code, message=None):
        self._response_status = code
        super().send_response(code, message)

    def _secure_request(self) -> bool:
        if callable(getattr(self.connection, "cipher", None)):
            return True
        trusted_peer = self.client_address[0] in TRUSTED_PROXIES
        trusted_render_edge = (
            RENDER_PROXY_MODE
            and IS_RENDER_WEB
            and bool(self.headers.get("CF-Ray"))
            and bool(self.headers.get("CF-Connecting-IP"))
        )
        if trusted_peer or trusted_render_edge:
            return self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower() == "https"
        return False

    def end_headers(self):
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if ENVIRONMENT in {"production", "staging"} and self._secure_request():
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        request_id = getattr(self, "_request_id", None)
        if request_id:
            self.send_header("X-Request-ID", request_id)
        if not any(header.lower().startswith(b"cache-control:") for header in getattr(self, "_headers_buffer", [])):
            path = urlparse(self.path).path
            cache = "no-cache" if path.endswith((".html", ".txt", ".xml")) else "public, max-age=86400"
            self.send_header("Cache-Control", cache)
        super().end_headers()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in {"/health", "/api/health"}:
            return self.send_json({
                "status": "ok", "version": VERSION, "environment": ENVIRONMENT,
                "office_enabled": OFFICE_ENABLED, "office_available": office_available(),
            })
        if path in {"/", "/index.html"}:
            return self.send_index(head_only=False)
        if path == "/robots.txt":
            return self.send_robots(head_only=False)
        if path == "/sitemap.xml":
            return self.send_sitemap(head_only=False)
        if path not in STATIC_FILES:
            return self.send_json({"error": "Not found."}, 404)
        self.path = "/" + STATIC_FILES[path]
        return super().do_GET()

    def do_HEAD(self):
        path = unquote(urlparse(self.path).path)
        if path in {"/health", "/api/health"}:
            return self.send_json({}, head_only=True)
        if path in {"/", "/index.html"}:
            return self.send_index(head_only=True)
        if path == "/robots.txt":
            return self.send_robots(head_only=True)
        if path == "/sitemap.xml":
            return self.send_sitemap(head_only=True)
        if path not in STATIC_FILES:
            return self.send_json({"error": "Not found."}, 404, head_only=True)
        self.path = "/" + STATIC_FILES[path]
        return super().do_HEAD()

    def send_text(self, data: bytes, mime: str, status=200, head_only=False):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def send_index(self, head_only=False):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        if PUBLIC_URL:
            metadata = (
                f'<link rel="canonical" href="{PUBLIC_URL}/">\n'
                f'  <meta property="og:url" content="{PUBLIC_URL}/">\n'
                f'  <meta property="og:image" content="{PUBLIC_URL}/pdfairy-fairy-logo.png">\n'
                f'  <meta name="twitter:card" content="summary_large_image">\n'
                f'  <meta name="twitter:image" content="{PUBLIC_URL}/pdfairy-fairy-logo.png">\n  '
            )
            html = html.replace("</head>", metadata + "</head>")
        self.send_text(html.encode("utf-8"), "text/html; charset=utf-8", head_only=head_only)

    def send_robots(self, head_only=False):
        text = (ROOT / "robots.txt").read_text(encoding="utf-8").rstrip() + "\n"
        if PUBLIC_URL:
            text += f"Sitemap: {PUBLIC_URL}/sitemap.xml\n"
        self.send_text(text.encode("utf-8"), "text/plain; charset=utf-8", head_only=head_only)

    def send_sitemap(self, head_only=False):
        if not PUBLIC_URL:
            return self.send_json({"error": "Sitemap is unavailable until PDFAIRY_PUBLIC_URL is configured."}, 404, head_only)
        pages = ["/", "/privacy.html", "/terms.html"]
        urls = "".join(f"<url><loc>{xml_escape(PUBLIC_URL + page)}</loc></url>" for page in pages)
        content = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
        self.send_text(content.encode("utf-8"), "application/xml; charset=utf-8", head_only=head_only)

    def do_OPTIONS(self):
        self.close_connection = True
        self.send_json({"error": "Cross-origin requests are not enabled."}, 405)

    def _method_not_allowed(self):
        self.close_connection = True
        self.send_json({"error": "This HTTP method is not supported."}, 405)

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        configured = {value.strip().rstrip("/") for value in os.getenv("PDFAIRY_ALLOWED_ORIGINS", "").split(",") if value.strip()}
        if PUBLIC_URL:
            configured.add(PUBLIC_URL)
        host = self.headers.get("Host", "")
        local = {f"http://{host}", f"https://{host}"} if host else set()
        return origin.rstrip("/") in configured | local

    def do_POST(self):
        started = time.monotonic()
        body_consumed = False
        self._request_id = uuid.uuid4().hex
        self._response_status = 500
        file_count = total_bytes = 0
        error_category = "none"
        if not self.work_slots.acquire(blocking=False):
            self.close_connection = True
            self.send_json({"error": "PDFairy is busy. Wait a moment and try again."}, 503)
            log_event("request", request_id=self._request_id, endpoint=urlparse(self.path).path, status=503,
                      duration_ms=round((time.monotonic() - started) * 1000), file_count=0,
                      aggregate_bytes=0, error_category="capacity")
            return
        try:
            if not self._origin_allowed():
                return self.send_json({"error": "This upload must come from the PDFairy page."}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise UserError("The request size is invalid.") from exc
            if length <= 0 or length > MAX_BODY:
                raise UserError(f"Request is empty or exceeds the {MAX_BODY // 1024 // 1024} MB total limit.")
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                raise UserError("Expected a multipart file upload.")
            body = self.rfile.read(length)
            body_consumed = True
            form = parse_multipart(content_type, body)
            uploads = form.get("files", [])
            uploads = uploads if isinstance(uploads, list) else [uploads]
            file_count = sum(isinstance(item, Upload) for item in uploads)
            total_bytes = sum(len(item.file.getbuffer()) for item in uploads if isinstance(item, Upload))
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
            error_category = "validation"
            self.send_json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            error_category = "client_disconnect"
        except Exception as exc:
            error_category = "processing"
            self.log_error("processing error (%s)", type(exc).__name__)
            self.send_json({"error": "The file could not be processed. It may be damaged or unsupported."}, 422)
        finally:
            if not body_consumed:
                self.close_connection = True
            self.work_slots.release()
            log_event(
                "request", request_id=self._request_id, endpoint=urlparse(self.path).path,
                status=self._response_status, duration_ms=round((time.monotonic() - started) * 1000),
                file_count=file_count, aggregate_bytes=total_bytes, error_category=error_category,
            )

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
            require_extension(name, {".txt"})
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
            require_office(name, data)
            return self.send_file(convert_office_document(data, suffix), f"{stem}-pdfairy.pdf", "application/pdf")
        raise UserError("That input and output format combination is not supported.")

    def compress(self, form):
        name, data = get_one_file(form); require_pdf(name, data)
        target_raw = self.field(form, "target_bytes")
        target = as_int(target_raw, "Target size") if target_raw else None
        if target is not None and target <= 0: raise UserError("Target size must be greater than zero.")
        quality = as_float(self.field(form, "quality", ".65"), "Compression level")
        if not .1 <= quality <= 1:
            raise UserError("Compression level must be between 0.1 and 1.")
        preserve = self.field(form, "preserve", "true") == "true"
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
        preview = self.field(form, "preview") == "true"
        matrix = fitz.Matrix(.65, .65) if preview else fitz.Matrix(1.1, 1.1)
        pages = []
        encoded_size = 0
        for page in doc:
            require_raster_safe(page, .65 if preview else 1.1)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            encoded = base64.b64encode(pix.tobytes("jpeg", jpg_quality=68 if preview else 78)).decode("ascii")
            encoded_size += len(encoded)
            if encoded_size > MAX_OUTPUT:
                doc.close()
                raise UserError("The rendered preview exceeds the output limit. Try a smaller PDF.")
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

    def send_json(self, value, status=200, head_only=False):
        data = json.dumps(value).encode("utf-8"); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def send_file(self, data: bytes, name: str, mime: str):
        if not data: raise UserError("Processing produced an empty output.")
        if len(data) > MAX_OUTPUT: raise UserError(f"Processing output exceeds the {MAX_OUTPUT // 1024 // 1024} MB limit.")
        ascii_name = re.sub(r"[^A-Za-z0-9._-]", "-", name) or "pdfairy-output"
        encoded_name = quote(name, safe="")
        self.send_response(200); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(data))); self.send_header("Content-Disposition", f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'); self.send_header("X-Output-Name", ascii_name); self.send_header("X-Output-Name-Encoded", encoded_name); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)

    def log_message(self, format, *args):
        if LOG_REQUESTS:
            super().log_message(format, *args)


class PDFairyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16


def main():
    host = os.getenv("PDFAIRY_HOST", "127.0.0.1")
    port = int(os.getenv("PDFAIRY_PORT") or os.getenv("PORT", "8080"))
    cleanup_stale_tempdirs()
    server = PDFairyServer((host, port), Handler)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stopping = threading.Event()

    def stop_server(_signum=None, _frame=None):
        if stopping.is_set():
            return
        stopping.set()
        terminate_active_processes()
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop_server)
        signal.signal(signal.SIGINT, stop_server)
    shown_url = PUBLIC_URL or f"http://{host}:{port}"
    print(f"PDFairy is ready at {shown_url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_server()
    finally:
        terminate_active_processes()
        server.server_close()


if __name__ == "__main__": main()
