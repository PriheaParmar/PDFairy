#!/usr/bin/env python3
"""Static checks for the staging container contract; no deployment is implied."""
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
    compose = (ROOT / "compose.staging.yml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    server = (ROOT / "server.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    checks = []
    checks.append(check("Office is opt-in and staging keeps it disabled", lambda: (
        require(server, 'PDFAIRY_ENABLE_OFFICE", "false"'),
        require(compose, 'PDFAIRY_ENABLE_OFFICE: "false"'),
        require(env_example, "PDFAIRY_ENABLE_OFFICE=false"),
    )))
    checks.append(check("container has an explicit non-root identity", lambda: (
        require(dockerfile, "USER pdfairy"),
        require(compose, 'user: "10001:10001"'),
    )))
    checks.append(check("staging has external CPU, memory and PID limits", lambda: require(
        compose, "mem_limit: 1g", "cpus: 1.0", "pids_limit: 64", "restart: unless-stopped"
    )))
    checks.append(check("filesystem and temporary storage are constrained", lambda: require(
        compose, "read_only: true", "cap_drop:", "no-new-privileges:true",
        "/tmp/pdfairy:rw,noexec,nosuid,nodev,size=268435456"
    )))
    checks.append(check("public URL and trusted proxy are mandatory", lambda: require(
        compose, "Set the real staging HTTPS origin", "Set exact reverse-proxy peer IPs"
    )))
    checks.append(check("local secrets and artifacts remain ignored", lambda: require(
        gitignore, ".env", "tmp/", "output/", "*.log"
    )))
    checks.append(check("staging HSTS requires a trusted secure request", lambda: require(
        server, 'ENVIRONMENT in {"production", "staging"}', "self._secure_request()"
    )))

    passed = sum(checks)
    print(f"\n{passed}/{len(checks)} staging configuration checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
