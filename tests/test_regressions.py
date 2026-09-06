#!/usr/bin/env python3
"""Regression checks for the V2 QA findings. Start server.py before running."""
import io
import json
from urllib.error import HTTPError

from PIL import Image
from test_api import PDF1, post


def expect_400(path, files, fields, fragment):
    try:
        post(path, files, fields)
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        assert exc.code == 400 and fragment in body, body
        return
    raise AssertionError("request unexpectedly succeeded")


def image_bytes(size):
    out = io.BytesIO(); Image.new("RGB", size, "white").save(out, "PNG"); return out.getvalue()


def main():
    expect_400("/api/convert", [("a.txt", b"one", "text/plain"), ("b.txt", b"two", "text/plain")], {"format": "pdf"}, "exactly one")
    expect_400("/api/convert", [("binary.txt", b"\0\xff", "text/plain")], {"format": "pdf"}, "plain-text")
    expect_400("/api/compress", [("one.pdf", PDF1, "application/pdf")], {"target_bytes": "abc"}, "Target size")
    expect_400("/api/sign", [("one.pdf", PDF1, "application/pdf")], {"signature": "Priya", "page": "abc", "position": "bottom-right"}, "Page number")
    expect_400("/api/sign", [("one.pdf", PDF1, "application/pdf")], {"signature": "Priya", "page": "1", "position": "wherever"}, "valid signature position")
    bad_edit = json.dumps([{ "type":"delete_everything", "page":0, "x":.5, "y":.5 }])
    expect_400("/api/edit", [("one.pdf", PDF1, "application/pdf")], {"operations": bad_edit}, "text, sign or redact")
    bad_page = json.dumps([{ "type":"text", "page":-1, "x":.5, "y":.5, "text":"No" }])
    expect_400("/api/edit", [("one.pdf", PDF1, "application/pdf")], {"operations": bad_page}, "outside this PDF")
    print("7/7 QA regression checks passed")


if __name__ == "__main__":
    main()
