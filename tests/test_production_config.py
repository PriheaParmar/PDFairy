#!/usr/bin/env python3
"""Static checks for the separate production deployment contract."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check(name, function):
    try:
        function()
        print("PASS", name)
        return True
    except Exception as exc:
        print("FAIL", name, "-", exc)
        return False


def require(text, *fragments):
    for fragment in fragments:
        assert fragment in text, fragment


def main():
    blueprint = (ROOT / "render.production.yaml").read_text(encoding="utf-8")
    runbook = (ROOT / "PRODUCTION.md").read_text(encoding="utf-8")
    privacy = (ROOT / "privacy.html").read_text(encoding="utf-8")
    terms = (ROOT / "terms.html").read_text(encoding="utf-8")

    checks = []
    checks.append(check("production is a separate controlled service", lambda: require(
        blueprint, "name: pdfairy", "branch: codex/production-launch", "autoDeployTrigger: off", "healthCheckPath: /health"
    )))
    checks.append(check("production uses the owner-approved Free tier", lambda: require(
        blueprint, "plan: free", 'PDFAIRY_MAX_CONCURRENT', 'value: "2"'
    )))
    checks.append(check("generated production origin is set only after assignment", lambda: (
        "key: PDFAIRY_PUBLIC_URL" not in blueprint
        and "key: PDFAIRY_ALLOWED_ORIGINS" not in blueprint
        and "RENDER_EXTERNAL_URL" in runbook
        or (_ for _ in ()).throw(AssertionError("production origin was guessed in Blueprint"))
    )))
    checks.append(check("Render proxy is constrained and Office stays disabled", lambda: require(
        blueprint, "key: PDFAIRY_PROXY_MODE", "value: render", "key: PDFAIRY_ENABLE_OFFICE", 'value: "false"'
    )))
    checks.append(check("production files contain no staging URL", lambda: (
        "pdfairy-staging.onrender.com" not in blueprint + runbook
        or (_ for _ in ()).throw(AssertionError("staging URL found"))
    )))
    checks.append(check("legal owner fields are uniquely searchable", lambda: require(
        privacy + terms,
        "[OWNER_INPUT: LEGAL_OPERATOR_NAME]",
        "[OWNER_INPUT: LEGAL_ADDRESS]",
        "[OWNER_INPUT: PUBLIC_CONTACT_EMAIL]",
        "[OWNER_INPUT: PRIVACY_CONTACT_EMAIL]",
        "[OWNER_INPUT: GOVERNING_JURISDICTION]",
        "[LEGAL_REVIEW_REQUIRED: WARRANTY_AND_LIABILITY_TERMS]",
    )))

    passed = sum(checks)
    print(f"\n{passed}/{len(checks)} production configuration checks passed")
    raise SystemExit(0 if passed == len(checks) else 1)


if __name__ == "__main__":
    main()
