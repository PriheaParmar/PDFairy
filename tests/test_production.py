#!/usr/bin/env python3
"""Production hardening checks. Start server.py before running."""
import io
import json
import tempfile
import time
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pypdf import PdfReader, PdfWriter

from test_api import BASE, PDF1, PDF2, post, sample_pdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def expect_http(code, request, fragment=None):
    try:
        urlopen(request, timeout=10)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        assert exc.code == code, (exc.code, body)
        if fragment:
            assert fragment in body, body
        return body
    raise AssertionError(f"request unexpectedly succeeded; expected {code}")


def run(name, fn):
    try:
        fn(); print("PASS", name); return True
    except Exception as exc:
        print("FAIL", name, "—", exc); return False


def main():
    checks = []
    checks.append(run("repository files are not web-accessible", lambda: expect_http(404, BASE + "/server.py", "Not found")))
    checks.append(run("git data is not web-accessible", lambda: expect_http(404, BASE + "/.git/config", "Not found")))
    checks.append(run("unsupported methods return safe JSON", lambda: expect_http(405, Request(BASE + "/api/health", data=b"x", method="PUT"), "not supported")))
    checks.append(run("non-multipart requests are rejected", lambda: expect_http(400, Request(BASE + "/api/convert", data=b"hello", headers={"Content-Type":"text/plain"}), "multipart")))
    checks.append(run("cross-origin uploads are rejected", lambda: expect_http(403, Request(BASE + "/api/convert", data=b"x", headers={"Content-Type":"multipart/form-data; boundary=x", "Origin":"https://attacker.invalid"}), "PDFairy page")))

    def office_disguise():
        try: post("/api/convert", [("report.docx", b"MZ executable", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")], {"format":"pdf"})
        except HTTPError as exc:
            assert exc.code == 400 and b"damaged or does not match" in exc.read(); return
        raise AssertionError("disguised executable accepted")
    checks.append(run("disguised Office executable is rejected", office_disguise))

    def image_mismatch():
        try: post("/api/convert", [("photo.png", b"\xff\xd8\xff\xe0not-really-png", "image/png")], {"format":"pdf"})
        except HTTPError as exc:
            assert exc.code == 400; return
        raise AssertionError("mismatched image accepted")
    checks.append(run("mismatched image is rejected", image_mismatch))

    def encrypted_pdf():
        writer = PdfWriter(); writer.append(PdfReader(io.BytesIO(PDF2))); writer.encrypt("secret"); out = io.BytesIO(); writer.write(out)
        try: post("/api/split", [("locked.pdf", out.getvalue(), "application/pdf")], {"mode":"every"})
        except HTTPError as exc:
            assert exc.code == 400 and b"password protected" in exc.read(); return
        raise AssertionError("encrypted PDF accepted")
    checks.append(run("password-protected PDF is explained", encrypted_pdf))

    def too_many():
        files=[(f"{i}.pdf", PDF2, "application/pdf") for i in range(21)]
        try: post("/api/merge", files, {})
        except HTTPError as exc:
            assert exc.code == 400 and b"no more than 20" in exc.read(); return
        raise AssertionError("too many files accepted")
    checks.append(run("upload-count limit is enforced", too_many))

    def overlap_range():
        data,_=post("/api/split", [("one.pdf", PDF1, "application/pdf")], {"mode":"extract", "pages":"1-2,2,1"})
        assert len(PdfReader(io.BytesIO(data)).pages) == 2
    checks.append(run("overlapping page ranges are deduplicated", overlap_range))

    def last_page():
        data,_=post("/api/split", [("one.pdf", PDF1, "application/pdf")], {"mode":"extract", "pages":"2"})
        assert "page 2" in (PdfReader(io.BytesIO(data)).pages[0].extract_text() or "")
    checks.append(run("last page can be extracted", last_page))

    def hundred_pages():
        data,_=post("/api/split", [("hundred.pdf", sample_pdf(100), "application/pdf")], {"mode":"extract", "pages":"1,100"})
        assert len(PdfReader(io.BytesIO(data)).pages) == 2
    checks.append(run("100-page boundary is supported", hundred_pages))

    def over_page_limit():
        try: post("/api/split", [("too-many.pdf", sample_pdf(101), "application/pdf")], {"mode":"extract", "pages":"1"})
        except HTTPError as exc:
            assert exc.code == 400 and b"100-page limit" in exc.read(); return
        raise AssertionError("page limit not enforced")
    checks.append(run("101-page PDF is rejected", over_page_limit))

    def long_unicode_name():
        name="文档"*150+".pdf"; data,headers=post("/api/split", [(name, PDF2, "application/pdf")], {"mode":"extract", "pages":"1"})
        assert data.startswith(b"%PDF-") and len(headers["X-Output-Name-Encoded"]) < 800
    checks.append(run("long Unicode filename is bounded safely", long_unicode_name))

    def no_extension():
        try: post("/api/split", [("document", PDF2, "application/pdf")], {"mode":"every"})
        except HTTPError as exc:
            assert exc.code == 400 and b"supported file type" in exc.read(); return
        raise AssertionError("extensionless PDF accepted")
    checks.append(run("extensionless upload is rejected clearly", no_extension))

    def stale_cleanup():
        from server import cleanup_stale_tempdirs
        path=Path(tempfile.gettempdir()) / "pdfairy-office-production-test"
        path.mkdir(exist_ok=True); old=time.time()-90000; import os; os.utime(path,(old,old)); cleanup_stale_tempdirs(); assert not path.exists()
    checks.append(run("stale Office temporary folders are cleaned", stale_cleanup))

    passed=sum(checks); print(f"\n{passed}/{len(checks)} production hardening checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
