#!/usr/bin/env python3
"""Release-readiness checks, including a mocked LibreOffice boundary."""
from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
from test_api import BASE


def valid_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 720, "Mocked LibreOffice output")
    document.showPage()
    document.save()
    return output.getvalue()


def expect_http(code, request):
    try:
        urlopen(request, timeout=10)
    except HTTPError as exc:
        assert exc.code == code, (exc.code, exc.read())
        return exc.read()
    raise AssertionError(f"expected HTTP {code}")


def check(name, function):
    try:
        function()
        print("PASS", name)
        return True
    except Exception as exc:
        print("FAIL", name, "-", exc)
        return False


class FakeProcess:
    next_pid = 7000

    def __init__(self, args, *, returncode=0, output=None, extra=False, timeout=False, **_kwargs):
        self.args = args
        self.returncode = None
        self._final_code = returncode
        self._output = output
        self._extra = extra
        self._timeout = timeout
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def communicate(self, timeout=None):
        if self._timeout:
            raise subprocess.TimeoutExpired(self.args, timeout)
        output_dir = Path(self.args[self.args.index("--outdir") + 1])
        if self._output is not None:
            (output_dir / "input.pdf").write_bytes(self._output)
        if self._extra:
            (output_dir / "unexpected.txt").write_text("unexpected", encoding="utf-8")
        self.returncode = self._final_code
        return b"", b""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = -9 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self):
        self.returncode = -9


@contextmanager
def office_environment(factory):
    original_temp = server.TEMP_ROOT
    with tempfile.TemporaryDirectory() as directory:
        server.TEMP_ROOT = Path(directory)
        try:
            with patch.object(server, "OFFICE_ENABLED", True), patch.object(
                server, "office_executable", return_value="/mock/soffice"
            ), patch.object(server.subprocess, "Popen", side_effect=factory), patch.object(
                server, "terminate_process_tree", side_effect=lambda process: process.kill()
            ):
                yield Path(directory)
        finally:
            server.TEMP_ROOT = original_temp


def run_office(factory, suffix=".docx"):
    with office_environment(factory) as directory:
        result = server.convert_office_document(b"mock-office-bytes", suffix)
        assert not list(directory.glob("pdfairy-office-*"))
        return result


def assert_user_error(fragment, function):
    try:
        function()
    except server.UserError as exc:
        assert fragment.lower() in str(exc).lower(), str(exc)
        return
    raise AssertionError("expected UserError")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@contextmanager
def configured_server():
    port = free_port()
    public = "https://files.example.test"
    environment = os.environ.copy()
    environment.update({
        "PDFAIRY_PORT": str(port),
        "PDFAIRY_ENV": "production",
        "PDFAIRY_PUBLIC_URL": public,
        "PDFAIRY_ALLOWED_ORIGINS": public,
        "PDFAIRY_TRUSTED_PROXIES": "127.0.0.1",
        "PDFAIRY_ENABLE_OFFICE": "false",
    })
    process = subprocess.Popen(
        [sys.executable, "server.py"], cwd=ROOT, env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urlopen(base + "/health", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("configured server did not start")
        yield base, public
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def main():
    tests = []

    def health():
        response = urlopen(BASE + "/health", timeout=10)
        data = json.loads(response.read())
        assert data["status"] == "ok" and isinstance(data["office_available"], bool)
        assert set(data) == {"status", "version", "environment", "office_enabled", "office_available"}
    tests.append(check("minimal health endpoint", health))

    def headers():
        response = urlopen(BASE + "/", timeout=10)
        values = response.headers
        assert "default-src 'self'" in values["Content-Security-Policy"]
        assert values["X-Content-Type-Options"] == "nosniff"
        assert values["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in values["Content-Security-Policy"]
        assert values["Cross-Origin-Opener-Policy"] == "same-origin"
        assert values["Cache-Control"] == "no-cache"
        assert "Strict-Transport-Security" not in values
    tests.append(check("security headers and no local HSTS", headers))

    def hsts_not_blind():
        request = Request(BASE + "/", headers={"X-Forwarded-Proto": "https"})
        assert "Strict-Transport-Security" not in urlopen(request).headers
    tests.append(check("untrusted forwarded protocol is ignored", hsts_not_blind))

    tests.append(check("sitemap fails closed without public URL", lambda: expect_http(404, BASE + "/sitemap.xml")))

    def private_paths():
        for path in ["/.env", "/.env.example", "/.gitignore", "/server.py", "/README.md", "/tests/test_api.py", "/__pycache__/server.pyc"]:
            expect_http(404, BASE + path)
    tests.append(check("repository and configuration paths stay private", private_paths))

    def traversal():
        for path in ["/..%2fserver.py", "/%2e%2e/server.py", "/.%2e/%2e%2e/server.py", "/%252e%252e%252fserver.py"]:
            expect_http(404, BASE + path)
    tests.append(check("encoded traversal forms stay private", traversal))

    def raster_limit():
        class Rect:
            width = height = 100_000
        class Page:
            rect = Rect()
        assert_user_error("too large", lambda: server.require_raster_safe(Page(), 1.0))
    tests.append(check("oversized PDF raster is rejected before allocation", raster_limit))

    def disabled():
        with patch.object(server, "OFFICE_ENABLED", False):
            assert not server.office_available()
            assert_user_error("unavailable", lambda: server.convert_office_document(b"x", ".docx"))
    tests.append(check("Office disabled state fails closed", disabled))

    def unavailable():
        with patch.object(server, "OFFICE_ENABLED", True), patch.object(server, "office_executable", return_value=None):
            assert not server.office_available()
            assert_user_error("unavailable", lambda: server.convert_office_document(b"x", ".docx"))
    tests.append(check("LibreOffice executable unavailable", unavailable))

    def startup_failure():
        with office_environment(lambda *args, **kwargs: (_ for _ in ()).throw(OSError("start failed"))) as directory:
            assert_user_error("could not start", lambda: server.convert_office_document(b"x", ".docx"))
            assert not list(directory.glob("pdfairy-office-*"))
    tests.append(check("LibreOffice startup failure cleanup", startup_failure))

    def timeout():
        with office_environment(lambda command, **kwargs: FakeProcess(command, timeout=True, **kwargs)) as directory:
            assert_user_error("timed out", lambda: server.convert_office_document(b"x", ".docx"))
            assert not list(directory.glob("pdfairy-office-*"))
    tests.append(check("LibreOffice timeout and cleanup", timeout))

    tests.append(check("LibreOffice non-zero exit", lambda: assert_user_error(
        "could not convert", lambda: run_office(lambda command, **kwargs: FakeProcess(command, returncode=1, **kwargs))
    )))
    tests.append(check("LibreOffice missing output", lambda: assert_user_error(
        "unexpected output", lambda: run_office(lambda command, **kwargs: FakeProcess(command, **kwargs))
    )))
    tests.append(check("LibreOffice malformed PDF", lambda: assert_user_error(
        "invalid", lambda: run_office(lambda command, **kwargs: FakeProcess(command, output=b"not-pdf", **kwargs))
    )))

    def wrong_name(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        original = process.communicate
        def communicate(timeout=None):
            output_dir = Path(process.args[process.args.index("--outdir") + 1])
            (output_dir / "wrong.pdf").write_bytes(valid_pdf())
            process.returncode = 0
            return b"", b""
        process.communicate = communicate
        return process
    tests.append(check("LibreOffice wrong output filename", lambda: assert_user_error(
        "unexpected output", lambda: run_office(wrong_name)
    )))
    tests.append(check("LibreOffice unexpected extra file", lambda: assert_user_error(
        "unexpected output", lambda: run_office(lambda command, **kwargs: FakeProcess(command, output=valid_pdf(), extra=True, **kwargs))
    )))

    def success():
        output = run_office(lambda command, **kwargs: FakeProcess(command, output=valid_pdf(), **kwargs))
        assert output.startswith(b"%PDF-")
    tests.append(check("mocked Office conversion success and cleanup", success))

    def invocation():
        seen = {}
        def factory(command, **kwargs):
            seen["args"], seen["kwargs"] = command, kwargs
            return FakeProcess(command, output=valid_pdf(), **kwargs)
        run_office(factory)
        args, kwargs = seen["args"], seen["kwargs"]
        assert "--headless" in args and "--norestore" in args and any(str(x).startswith("-env:UserInstallation=file:") for x in args)
        assert kwargs["shell"] is False and kwargs["stdin"] is subprocess.DEVNULL
        assert Path(args[-1]).name == "input.docx"
    tests.append(check("conservative argument-array invocation", invocation))

    def concurrency():
        errors, outputs = [], []
        factory = lambda command, **kwargs: FakeProcess(command, output=valid_pdf(), **kwargs)
        with office_environment(factory) as directory:
            def worker():
                try:
                    outputs.append(server.convert_office_document(b"mock-office-bytes", ".docx"))
                except Exception as exc:
                    errors.append(exc)
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            assert not list(directory.glob("pdfairy-office-*"))
        assert not errors and len(outputs) == 2
    tests.append(check("concurrent Office conversions stay isolated", concurrency))

    tests.append(check("Unicode upload names are neutral to Office runner", lambda: (
        server.clean_filename("資料 文書.docx") == "資料 文書.docx" or (_ for _ in ()).throw(AssertionError())
    )))

    def configured():
        with configured_server() as (base, public):
            index = urlopen(base + "/").read().decode()
            sitemap = urlopen(base + "/sitemap.xml").read().decode()
            robots = urlopen(base + "/robots.txt").read().decode()
            assert f'href="{public}/"' in index
            assert f'content="{public}/pdfairy-fairy-logo.png"' in index
            assert public + "/privacy.html" in sitemap and "/api/" not in sitemap
            assert f"Sitemap: {public}/sitemap.xml" in robots
            secure = urlopen(Request(base + "/", headers={"X-Forwarded-Proto": "https"}))
            assert "max-age=31536000" in secure.headers["Strict-Transport-Security"]
            health_data = json.loads(urlopen(base + "/health").read())
            assert health_data["environment"] == "production" and not health_data["office_available"]
    tests.append(check("public URL metadata, sitemap, trusted-proxy HSTS", configured))

    def container_files():
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        assert "USER pdfairy" in dockerfile and "HEALTHCHECK" in dockerfile
        assert ".git" in ignore and "tests" in ignore
    tests.append(check("minimal non-root container definition", container_files))

    def privacy_logging():
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        assert "aggregate_bytes" in source and "request_id" in source
        request_handler = source[source.index("    def do_POST"):source.index("    def field")]
        assert "filename=" not in request_handler and "document contents" not in request_handler
    tests.append(check("observability schema excludes filenames", privacy_logging))

    passed = sum(tests)
    print(f"\n{passed}/{len(tests)} release-readiness checks passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    raise SystemExit(main())
