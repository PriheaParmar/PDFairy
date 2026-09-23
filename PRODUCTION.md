# PDFairy production launch

The externally verified release candidate is commit `af82a5ee8b525a24ae44bb0e95a3d526bfdb2913` on `codex/render-staging`. Keep the staging service in place while creating production as a separate Render Web Service from `render.production.yaml`. The owner has chosen Render's generated `onrender.com` hostname, Render-managed HTTPS, and the Free compute plan. No custom domain or DNS configuration is part of this launch. Automatic deploys remain disabled.

## Owner inputs required

- Legal operator's full registered name or the individual's legal name, as applicable.
- Legal address to publish.
- Public contact email.
- Privacy contact email.
- Governing law and jurisdiction.
- Qualified legal review and approved warranty/liability wording.

## Production environment

Set these on the production service:

| Variable | Production value |
|---|---|
| `PDFAIRY_HOST` | `0.0.0.0` |
| `PORT` | Render-provided; do not override |
| `PDFAIRY_PORT` | Unset so the app uses Render's `PORT` |
| `PDFAIRY_ENV` | `production` |
| `PDFAIRY_PUBLIC_URL` | Exact Render-generated production HTTPS origin, with no trailing slash |
| `PDFAIRY_ALLOWED_ORIGINS` | The same exact Render-generated production origin |
| `PDFAIRY_PROXY_MODE` | `render` |
| `PDFAIRY_TRUSTED_PROXIES` | Unset on Render; Render proxy attestation is used instead |
| `PDFAIRY_ENABLE_OFFICE` | `false` |
| `PDFAIRY_LIBREOFFICE_PATH` | Unset |
| `PDFAIRY_MAX_FILE_MB` | `50` |
| `PDFAIRY_MAX_BODY_MB` | `80` |
| `PDFAIRY_MAX_OUTPUT_MB` | `100` |
| `PDFAIRY_MAX_FILES` | `20` |
| `PDFAIRY_MAX_PAGES` | `100` |
| `PDFAIRY_MAX_CONCURRENT` | `2` |
| `PDFAIRY_REQUEST_TIMEOUT` | `120` |
| `PDFAIRY_OFFICE_TIMEOUT` | `90` (inactive while Office is disabled) |
| `PDFAIRY_TEMP_DIR` | `/tmp/pdfairy` |
| `PDFAIRY_LOG_REQUESTS` | `true` |

No secret is required by PDFairy. Do not commit a populated `.env` file. Create the service first without guessing its hostname; the app initially falls back to Render's `RENDER_EXTERNAL_URL`. After Render assigns the production URL, set both owner variables above to that exact origin. `PDFAIRY_PUBLIC_URL` drives canonical, Open Graph, social-image, robots sitemap, and sitemap URLs. It is also automatically accepted as a same-origin upload source.

## Hosting and capacity

The owner has explicitly chosen one Render Free instance for the initial launch and accepts idle sleep and cold starts. Free provides 0.1 CPU and 512 MB RAM, so it is a constrained launch tier rather than a capacity guarantee. Two concurrent in-process PDF jobs can each hold input, decoded page/raster data, and output in memory; the 80 MB request and 100 MB output ceilings are not memory ceilings. Monitor memory pressure, restarts, timeouts, busy responses, and latency closely. If real traffic or representative 50 MB/100-page tests exceed the tier, stop and obtain approval before moving to a paid plan. Do not attach a persistent disk: user documents are intentionally ephemeral.

## Monitoring

- Keep Render's HTTP health check on `/health`; add a simple external HTTPS uptime probe for the final domain.
- In Render Metrics, watch CPU, memory, request volume, 5xx responses, and instance count/restarts.
- In application logs, watch `status`, `duration_ms`, and `error_category`; alert or investigate repeated 5xx responses, processing failures, timeouts, or busy responses.
- Review deploy events and instance restarts. Establish initial CPU/memory and latency baselines during launch testing, then set operational thresholds from observed traffic.
- Use Render's built-in metrics and privacy-preserving logs first. Do not add document analytics or log filenames/content.

## Recovery model

User files and generated results are not application records and must not be backed up. Recovery depends on the Git repository, the production Blueprint, a securely recorded copy of environment values, the approved legal pages, Render service settings, and registrar/DNS configuration. Keep repository protection and an off-platform record of DNS and Render configuration. Redeploy the tagged release into a replacement service if recovery is required.

## Git and release path

1. Keep `codex/render-staging` and the staging service unchanged.
2. Prepare and verify production on `codex/production-launch`, then open a normal merge or pull request into `main`; require the complete automated suite and review the production-launch diff.
3. Create the separate production service from `render.production.yaml` on the Free plan. Leave automatic deploys off for the first launch.
4. Set the assigned Render HTTPS origin as the public URL and allowed origin, then run the full external and browser checks against production.
5. After production verification, tag that exact commit `v2.0.0` and push the tag. Do not tag an unverified deployment.

## Practical launch checklist

Before: retain the unresolved legal placeholders, confirm the Free/no-custom-domain decision, and set all non-URL production environment values.

During: create the separate `pdfairy` service if that Render name is available, verify `/health` and Render-managed TLS, set the assigned generated HTTPS URL as the public URL and allowed origin, verify generated metadata/sitemap/robots, and keep Office disabled. Do not add a custom domain.

After: rerun every PDF workflow and download, representative failure cases, HTTPS/security headers, origin rejection, source-exposure probes, privacy-safe logs, responsive widths, Chrome/Chromium plus available Firefox/WebKit/Safari coverage, uptime checks, and initial CPU/memory observations. Tag `v2.0.0` only after these checks pass.
