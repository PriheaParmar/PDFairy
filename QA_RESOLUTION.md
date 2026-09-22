# QA resolution summary

This build was rebuilt from the 5 September 2026 QA report. The original was a static prototype; this version includes a real local processing server.

| Original bug | Resolution |
|---|---|
| BUG-001 false image→PDF/DOCX/TXT files | Fixed. The UI only shows compatible targets; image→PDF uses a real PDF encoder. Unsupported pairs are rejected. |
| BUG-002 JPEG bytes named PNG | Fixed. Image compression preserves the actual image format and output extension. |
| BUG-003 editor could not import/export | Fixed within documented scope. It imports/render real PDFs and exports added text, typed signatures and permanent redactions. Rewriting existing PDF text is explicitly not claimed. |
| BUG-004 merge/split/sign simulated | Fixed with real PDF processing and output validation. |
| BUG-005 target size ignored | Fixed for images and PDFs, including MB/KB conversion and impossible-target feedback. |
| BUG-006 multi-file list discarded | Fixed. All merge files are stored, shown, removable and reorderable. |
| BUG-007 no runtime validation | Fixed with client and server size/type/PDF-signature/page-limit checks. |
| BUG-008 silent image downscale | Fixed for conversion. Compression downscaling occurs only when necessary to meet an explicit target and is disclosed. |
| BUG-009 quality checkbox ignored | Fixed. It controls whether target compression may reduce dimensions. |
| BUG-010 dead navigation controls | Fixed. Try PDFairy, Privacy and mobile navigation work; unbuilt Sign In was removed. |
| BUG-011 upload keyboard inaccessible | Fixed with Enter/Space handlers, roles and accessible names. |
| BUG-012 Back/refresh routing | Fixed with hash routes. |
| BUG-013 unsupported trust claims | Fixed. Copy now describes the exact local-server privacy model. |
| BUG-014 stale processing jobs | Fixed with AbortController plus job identifiers. |
| BUG-015 generic errors | Fixed with validation and task-specific server/client messages. |
| BUG-016 selected state visual only | Fixed with `aria-pressed`. |
| BUG-017 low contrast | Fixed by darkening primary and secondary foreground tokens. |
| BUG-018 no reduced motion | Fixed with `prefers-reduced-motion`. |
| BUG-019 object URL leak | Fixed on image success, image error and download paths. |
| BUG-020 favicon missing | Fixed with a local SVG favicon. |

## Verification

- JavaScript syntax check: passed
- Python syntax check: passed
- API/output integration tests: 14/14 passed (the optional LibreOffice check runs when LibreOffice is installed)
- QA regression checks: 11/11 passed
- Production hardening checks: 16/16 passed
- Output verification includes PDF signatures/page counts, DOCX ZIP structure, split ZIP contents, persisted PDF text/signatures, editor export, encrypted/corrupt-file rejection, page limits, upload-count limits, origin checks, static-file isolation and stale temporary-folder cleanup.
- Full interactive workflow verification: Chromium-based in-app browser.
- Static render verification: installed Google Chrome and Microsoft Edge.
- Firefox and Safari/WebKit were not available in this Windows environment and are not claimed as verified.
