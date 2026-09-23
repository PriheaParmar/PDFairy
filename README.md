# PDFairy V2

PDFairy is a focused browser workspace for converting, compressing, merging, splitting, signing, and editing PDFs. The approved interface is served by a dependency-light Python application.

## Local development

Requires Python 3.10 or newer.

```bash
python -m pip install -r requirements.txt
python server.py
```

Open `http://127.0.0.1:8080`. Copy `.env.example` values into your process environment when changing defaults; `server.py` does not load or require a committed `.env` file.

Run checks while the server is running:

```bash
python tests/test_api.py
python tests/test_regressions.py
python tests/test_production.py
python tests/test_release.py
```

## Production configuration

Set these values explicitly in production:

| Variable | Production value |
|---|---|
| `PDFAIRY_HOST` | Internal bind address, commonly `0.0.0.0` in a container |
| `PDFAIRY_PORT` | Internal application port |
| `PDFAIRY_ENV` | `production` |
| `PDFAIRY_PUBLIC_URL` | Final absolute HTTPS origin, for example `https://pdf.example.com` |
| `PDFAIRY_ALLOWED_ORIGINS` | Comma-separated permitted browser origins; normally the public origin |
| `PDFAIRY_TRUSTED_PROXIES` | Exact proxy IP addresses allowed to supply `X-Forwarded-Proto` |
| `PDFAIRY_PROXY_MODE` | Set to `render` only for a Render web service |

`PDFAIRY_PUBLIC_URL` drives canonical, Open Graph, Twitter/X image, robots, and sitemap URLs. When unset, local development works and no fake canonical URL or sitemap is emitted.

## Optional environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `PDFAIRY_MAX_FILE_MB` | `50` | Per-file input limit |
| `PDFAIRY_MAX_BODY_MB` | `80` | Aggregate request limit |
| `PDFAIRY_MAX_OUTPUT_MB` | `100` | Response/output sanity limit |
| `PDFAIRY_MAX_FILES` | `20` | Files in one request |
| `PDFAIRY_MAX_PAGES` | `100` | Pages in one PDF |
| `PDFAIRY_MAX_CONCURRENT` | `2` | Concurrent processing requests per process |
| `PDFAIRY_REQUEST_TIMEOUT` | `120` | Request socket timeout in seconds |
| `PDFAIRY_OFFICE_TIMEOUT` | `90` | LibreOffice conversion timeout in seconds |
| `PDFAIRY_ENABLE_OFFICE` | `false` | Opt-in Office input; enable only in an isolated, verified environment |
| `PDFAIRY_LIBREOFFICE_PATH` | auto-detect | Explicit LibreOffice executable path |
| `PDFAIRY_TEMP_DIR` | OS temp | Root for PDFairy Office temporary directories |
| `PDFAIRY_LOG_REQUESTS` | `false` | Privacy-preserving structured operational logs |

Do not place secrets in these values or commit a populated `.env` file.

## LibreOffice

Office-to-PDF conversion is optional. Install LibreOffice and expose `soffice`/`libreoffice`, or set `PDFAIRY_LIBREOFFICE_PATH`. Each conversion receives separate input, output, working, and LibreOffice profile directories. PDFairy uses an argument array with `shell=False`, conservative headless flags, a timeout, process-tree termination, exact output-name checks, PDF validation, and cleanup.

If disabled or unavailable, `/health` reports `office_available: false`, the browser stops advertising Office input, and all PDF/image/text tools continue operating. The default Docker image deliberately excludes LibreOffice to keep the attack surface and image size smaller. Build a separately reviewed worker image if Office support is required.

This is process hygiene, not an operating-system sandbox. Office processing must still run in an isolated container/service with no application secrets, no host mounts, no internal-network access, and no access to other users' jobs.

## Security model

All uploads are untrusted. PDFairy validates extensions and document structure, enforces byte/page/file/concurrency/time/output limits, and does not claim to scan files for malware. Server-side uploads and results are not intentionally retained. Office inputs briefly exist under `PDFAIRY_TEMP_DIR`; normal success/failure removes them, graceful shutdown terminates active conversions, and startup removes stale folders older than 24 hours.

Optional logs contain request ID, endpoint, status, duration, file count, aggregate bytes, and a generic error category. They exclude complete filenames, file contents, extracted text, bytes, and document metadata. Configure proxy/platform logs separately.

The processing boundary is kept in `convert_office_document` so it can later be moved to a dedicated worker without changing browser flows.

## Reverse proxy / HTTPS

Terminate public TLS at a maintained reverse proxy or platform and forward traffic to PDFairy over a private network. Preserve the original `Host`, set `X-Forwarded-Proto`, cap request bodies at or below `PDFAIRY_MAX_BODY_MB`, and use request timeouts slightly above `PDFAIRY_REQUEST_TIMEOUT`.

PDFairy trusts forwarded protocol only when the direct peer IP is listed in `PDFAIRY_TRUSTED_PROXIES`. HSTS is emitted only for production requests proven HTTPS by a TLS socket or a trusted proxy. Redirect HTTP to HTTPS at the edge so internal health probes can remain private HTTP.

On Render, set `PDFAIRY_PROXY_MODE=render`. PDFairy then uses Render's platform-provided `PORT` and `RENDER_EXTERNAL_URL`, and accepts `X-Forwarded-Proto` only when the process is identified as a Render web service and the request includes Render's Cloudflare edge headers. Do not enable this mode on another platform.

Illustrative proxy behavior (adapt to the chosen provider): public HTTPS -> private PDFairy port; request body <= 80 MB; proxy timeout 130 seconds; no buffering of sensitive bodies to disk; no cache for API responses; rate limiting at the edge.

## Container recommendations

`Dockerfile` runs as a non-root user, copies only runtime files, uses `/tmp/pdfairy`, disables Office by default, and provides a health check. Suggested starting limits per replica:

- 1 CPU; 1 GiB RAM for PDF-only use, then load-test representative documents.
- 64 processes/PIDs; 256 MiB temporary storage; read-only root filesystem plus a writable tmpfs at `/tmp/pdfairy`.
- Two concurrent jobs, one replica initially, restart policy `unless-stopped`/platform equivalent.
- No privileged mode, host filesystem mounts, cloud credentials, or internal-network route.

LibreOffice may require more memory and PID headroom. Measure it in a separately isolated Office-enabled image; application limits are not a substitute for container/OS quotas.

### Staging container

`compose.staging.yml` is the production-like staging baseline. It requires the real staging HTTPS origin and exact reverse-proxy peer IPs, binds the application only to host loopback, runs with a read-only root filesystem and no Linux capabilities, and uses these initial limits: 1 CPU, 1 GiB RAM, 64 PIDs, and a 256 MiB temporary `tmpfs`. Office conversion remains disabled.

```bash
PDFAIRY_PUBLIC_URL=https://your-staging-origin.example \
PDFAIRY_TRUSTED_PROXIES=127.0.0.1 \
docker compose -f compose.staging.yml up --build -d
```

The reverse proxy must terminate HTTPS and forward to `127.0.0.1:8081`. Replace the example values with the real staging origin and proxy address; do not expose port 8081 publicly. Check `docker compose -f compose.staging.yml ps`, `docker inspect pdfairy-staging`, and the external `/health` endpoint before workflow testing.

## Health check

`GET /health` (and the backward-compatible `/api/health`) returns only status, app version/environment, and Office enabled/available booleans. It exposes no paths, dependency versions, host details, or secrets.

## Launch checklist

The owner-facing production configuration, monitoring, recovery, Git, and deployment runbook is in [`PRODUCTION.md`](PRODUCTION.md). The separate Render Blueprint is `render.production.yaml`; it does not replace the staging service, uses the owner-approved Free/no-custom-domain configuration, and has automatic deploys disabled.

- Set the final HTTPS `PDFAIRY_PUBLIC_URL` and allowed origin. On Render use `PDFAIRY_PROXY_MODE=render`; on other platforms supply only exact trusted proxy peer IPs.
- Configure TLS, HTTP-to-HTTPS redirect, body/time/rate limits, container quotas, tmpfs, and restart/monitoring at the hosting layer.
- Decide whether Office conversion is disabled or deployed in a dedicated isolated worker/container; run a genuine end-to-end conversion there.
- Supply legal operator, legal address, privacy/contact email, governing jurisdiction, and lawyer-reviewed warranty/liability text.
- Review hosting/proxy log retention and temporary-volume behavior against the published Privacy Policy.
- Run Firefox, WebKit, and native Safari checks on the final deployment where available.
- Run the complete automated suite and manual retrieval/traversal checks before promotion.
