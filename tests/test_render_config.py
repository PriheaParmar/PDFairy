#!/usr/bin/env python3
"""Render-specific deployment contract checks."""
from __future__ import annotations

import http.client
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_URL = "https://pdfairy-staging.example.onrender.com"


def check(name, function):
    try:
        function()
        print("PASS", name)
        return True
    except Exception as exc:
        print("FAIL", name, "-", exc)
        return False


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, path="/", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    body = response.read().decode("utf-8", "replace")
    result = response.status, {key.lower(): value for key, value in response.getheaders()}, body
    connection.close()
    return result


def keepalive_recovery(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("PUT", "/api/health", body=b"x")
    response = connection.getresponse()
    assert response.status == 405
    response.read()
    connection.request("POST", "/api/convert", body=b"hello", headers={"Content-Type": "text/plain"})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    assert response.status == 400
    assert b"multipart" in body


def wait_ready(port, process):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"server exited with {process.returncode}")
        try:
            if request(port, "/health")[0] == 200:
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("server did not bind Render PORT")


def main():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    port = free_port()
    env = os.environ.copy()
    for key in ("PDFAIRY_PORT", "PDFAIRY_PUBLIC_URL", "PDFAIRY_TRUSTED_PROXIES"):
        env.pop(key, None)
    env.update({
        "PDFAIRY_HOST": "127.0.0.1",
        "PDFAIRY_ENV": "staging",
        "PDFAIRY_PROXY_MODE": "render",
        "PORT": str(port),
        "RENDER": "true",
        "RENDER_SERVICE_TYPE": "web",
        "RENDER_EXTERNAL_URL": PUBLIC_URL,
    })
    process = subprocess.Popen(
        [sys.executable, "server.py"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        checks = []
        checks.append(check("container does not override Render PORT", lambda: (
            (_ for _ in ()).throw(AssertionError("Dockerfile forces PDFAIRY_PORT"))
            if "PDFAIRY_PORT=8080" in dockerfile else None,
            (_ for _ in ()).throw(AssertionError("health check does not use Render PORT"))
            if "os.getenv('PORT','8080')" not in dockerfile else None,
        )))
        checks.append(check("Render PORT is honored", lambda: wait_ready(port, process)))
        checks.append(check("Render external URL drives public metadata", lambda: (
            (_ for _ in ()).throw(AssertionError("canonical URL missing"))
            if f'<link rel="canonical" href="{PUBLIC_URL}/">' not in request(port)[2] else None,
            (_ for _ in ()).throw(AssertionError("sitemap URL missing"))
            if f"<loc>{PUBLIC_URL}/</loc>" not in request(port, "/sitemap.xml")[2] else None,
        )))
        checks.append(check("unattested forwarded HTTPS is ignored", lambda: (
            (_ for _ in ()).throw(AssertionError("HSTS trusted without edge attestation"))
            if "strict-transport-security" in request(port, headers={"X-Forwarded-Proto": "https"})[1] else None
        )))
        checks.append(check("Render edge HTTPS enables HSTS", lambda: (
            (_ for _ in ()).throw(AssertionError("HSTS missing for Render HTTPS"))
            if "strict-transport-security" not in request(port, headers={
                "X-Forwarded-Proto": "https", "CF-Ray": "test-ray", "CF-Connecting-IP": "203.0.113.7"
            })[1] else None
        )))
        checks.append(check("HTTP at the Render edge does not enable HSTS", lambda: (
            (_ for _ in ()).throw(AssertionError("HSTS emitted for forwarded HTTP"))
            if "strict-transport-security" in request(port, headers={
                "X-Forwarded-Proto": "http", "CF-Ray": "test-ray", "CF-Connecting-IP": "203.0.113.7"
            })[1] else None
        )))
        checks.append(check("unconsumed request bodies cannot corrupt keep-alive", lambda: keepalive_recovery(port)))
        passed = sum(checks)
        print(f"\n{passed}/{len(checks)} Render configuration checks passed")
        return 0 if passed == len(checks) else 1
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
