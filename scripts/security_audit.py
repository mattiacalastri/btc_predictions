#!/usr/bin/env python3
"""
BTC Prediction Bot — Security Audit Script
==========================================
Standalone, zero external dependencies.
Run: python3 scripts/security_audit.py

Exit code: 0 = no CRITICAL, 1 = CRITICAL found.
"""

import os
import re
import sys
import json
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent.resolve()

SKIP_DIRS = {
    ".git", ".pytest_cache", "__pycache__", "node_modules",
    ".claude", "datasets", "memory",
}
SKIP_EXTS = {".pkl", ".pyc", ".pyo", ".jsonl", ".png", ".jpg", ".jpeg",
             ".gif", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".eot",
             ".pdf", ".zip", ".gz", ".tar", ".bin"}

# ── Secret Patterns ──────────────────────────────────────────────────────────

SECRET_PATTERNS = [
    # JWT tokens (eyJ... base64url encoded header)
    (r"eyJ[A-Za-z0-9_/+=.-]{40,}\.[A-Za-z0-9_/+=.-]{10,}", "JWT_TOKEN", "CRITICAL"),
    # Anthropic API key
    (r"sk-ant-api0[0-9]-[A-Za-z0-9_-]{20,}", "ANTHROPIC_API_KEY", "CRITICAL"),
    # Generic sk- API key (OpenAI, Anthropic legacy, etc.)
    (r"sk-[A-Za-z0-9]{32,}", "SK_API_KEY", "CRITICAL"),
    # Slack Bot/User token
    (r"xox[bpas]-[0-9A-Za-z-]{20,}", "SLACK_TOKEN", "CRITICAL"),
    # Hex strings > 32 chars (potential private keys, supabase keys, etc.)
    (r"\b[0-9a-fA-F]{64}\b", "HEX_64CHAR", "HIGH"),
    # URL with embedded password: scheme://user:pass@host
    (r"[a-z]+://[^/\s\"']+:[^/\s\"'@]{4,}@[a-z0-9._-]+", "URL_WITH_PASSWORD", "HIGH"),
    # "REDACTED" placeholder — signals incomplete cleanup
    (r"\bREDACTED\b", "REDACTED_MARKER", "MEDIUM"),
    # Hardcoded IP + credentials pattern
    (r"password\s*=\s*[\"'][^\"']{6,}[\"']", "HARDCODED_PASSWORD", "HIGH"),
    # Private key PEM header
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "PEM_PRIVATE_KEY", "CRITICAL"),
]

# Lines that are clearly false positives (env var reads, comments, docstrings)
FALSE_POSITIVE_PATTERNS = [
    re.compile(r"os\.environ"),
    re.compile(r"os\.getenv"),
    re.compile(r"getenv\("),
    re.compile(r"^\s*#"),
    re.compile(r"^\s*[\"']{3}"),   # docstring lines
    re.compile(r"sk-ant-api03-\.\.\.$"),  # placeholder in .env.example
    re.compile(r"your_"),          # explicit placeholder
    re.compile(r"<your"),
    re.compile(r"\byour[\s_]"),
    re.compile(r"example|placeholder|dummy|sample|test_key|fake"),
    re.compile(r"eyJ[A-Za-z0-9_+/=]{0,10}$"),  # too short to be real
]

GITIGNORE_REQUIRED = [
    (".env",          r"^\.env$|^\.env\b"),
    ("*.pkl",         r"^\*\.pkl"),
    ("__pycache__",   r"^__pycache__"),
    (".DS_Store",     r"^\.DS_Store"),
    ("node_modules",  r"^node_modules"),
    ("*.log",         r"^\*\.log"),
]

APP_PY = REPO_ROOT / "app.py"
GITIGNORE = REPO_ROOT / ".gitignore"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _iter_source_files():
    """Yield (path, lines) for all text files in repo."""
    for root, dirs, files in os.walk(REPO_ROOT):
        # Prune skip dirs in-place
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            p = Path(root) / fname
            if p.suffix.lower() in SKIP_EXTS:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
                yield p, text.splitlines()
            except Exception:
                continue


def _is_false_positive(line: str) -> bool:
    return any(fp.search(line) for fp in FALSE_POSITIVE_PATTERNS)


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ── Check 1: Hardcoded Secrets ─────────────────────────────────────────────

def check_secrets(files_cache):
    findings = []
    for path, lines in files_cache:
        rel = _rel(path)
        # Skip .env.example (it's the template — allowed to have sk-ant-api03-...)
        if rel == ".env.example":
            continue
        for lineno, line in enumerate(lines, 1):
            if _is_false_positive(line):
                continue
            for pattern, label, severity in SECRET_PATTERNS:
                m = re.search(pattern, line)
                if m:
                    snippet = line.strip()[:120]
                    findings.append({
                        "check": "HARDCODED_SECRET",
                        "severity": severity,
                        "file": rel,
                        "line": lineno,
                        "label": label,
                        "snippet": snippet,
                    })
                    break  # one finding per line
    return findings


# ── Check 2: .gitignore Completeness ─────────────────────────────────────────

def check_gitignore():
    findings = []
    if not GITIGNORE.exists():
        findings.append({
            "check": "GITIGNORE_MISSING",
            "severity": "CRITICAL",
            "file": ".gitignore",
            "line": 0,
            "label": "GITIGNORE_ABSENT",
            "snippet": ".gitignore does not exist",
        })
        return findings

    content = GITIGNORE.read_text(encoding="utf-8")
    lines = content.splitlines()

    for label, pattern in GITIGNORE_REQUIRED:
        rx = re.compile(pattern, re.MULTILINE)
        if not rx.search(content):
            # Check also with leading slash variant
            findings.append({
                "check": "GITIGNORE_INCOMPLETE",
                "severity": "MEDIUM",
                "file": ".gitignore",
                "line": 0,
                "label": f"MISSING_{label.replace('*','STAR').replace('.','DOT')}",
                "snippet": f"No rule matching '{label}' found in .gitignore",
            })
    return findings


# ── Check 3: CSP — unsafe-eval / unsafe-inline ───────────────────────────────

def check_csp():
    findings = []
    if not APP_PY.exists():
        return findings
    lines = APP_PY.read_text(encoding="utf-8", errors="replace").splitlines()
    for lineno, line in enumerate(lines, 1):
        if "'unsafe-eval'" in line:
            findings.append({
                "check": "CSP_UNSAFE",
                "severity": "HIGH",
                "file": "app.py",
                "line": lineno,
                "label": "CSP_UNSAFE_EVAL",
                "snippet": line.strip()[:120],
            })
        if "'unsafe-inline'" in line:
            findings.append({
                "check": "CSP_UNSAFE",
                "severity": "MEDIUM",
                "file": "app.py",
                "line": lineno,
                "label": "CSP_UNSAFE_INLINE",
                "snippet": line.strip()[:120],
            })
    return findings


# ── Check 4: Unprotected POST Endpoints ──────────────────────────────────────

# Endpoints that are intentionally public (documented design choice)
INTENTIONALLY_PUBLIC_POST = {
    "/submit-contribution",  # reCAPTCHA + IP rate limit
    "/contribute",           # alias for submit-contribution
    "/satoshi-lead",         # reCAPTCHA + Turnstile
    "/force-retrain",        # documented public, 1h cooldown, read-only memory refresh
    "/cockpit/api/auth",     # is the auth endpoint itself
}

PROTECTION_FNS = [
    "_check_api_key",
    "_check_rate_limit",
    "_verify_recaptcha",
    "_check_cockpit_auth",
    "_check_turnstile",
]

def check_unprotected_post():
    findings = []
    if not APP_PY.exists():
        return findings
    lines = APP_PY.read_text(encoding="utf-8", errors="replace").splitlines()
    route_re = re.compile(r'@app\.route\("([^"]+)".*?methods.*?["\']POST["\']', re.IGNORECASE)
    for lineno, line in enumerate(lines, 1):
        m = route_re.search(line)
        if not m:
            # Also handle multi-arg route decorators
            m2 = re.search(r'@app\.route\("([^"]+)"', line)
            if m2 and lineno + 1 <= len(lines):
                # Check if next line has POST
                next_line = lines[lineno] if lineno < len(lines) else ""
                if "POST" not in line and "POST" not in next_line:
                    continue
                route = m2.group(1)
            else:
                continue
        else:
            route = m.group(1)

        if route in INTENTIONALLY_PUBLIC_POST:
            continue

        # Check the next 15 lines for any protection function
        window = lines[lineno: lineno + 15]
        protected = any(
            fn in wline for fn in PROTECTION_FNS for wline in window
        )
        if not protected:
            findings.append({
                "check": "UNPROTECTED_POST",
                "severity": "MEDIUM",
                "file": "app.py",
                "line": lineno,
                "label": f"NO_AUTH: {route}",
                "snippet": line.strip()[:120],
            })
    return findings


# ── Check 5: CORS Wildcard ────────────────────────────────────────────────────

def check_cors(files_cache):
    findings = []
    cors_re = re.compile(r'Access-Control-Allow-Origin["\s:]+\*')
    for path, lines in files_cache:
        rel = _rel(path)
        for lineno, line in enumerate(lines, 1):
            if cors_re.search(line):
                findings.append({
                    "check": "CORS_WILDCARD",
                    "severity": "HIGH",
                    "file": rel,
                    "line": lineno,
                    "label": "CORS_ALLOW_ALL",
                    "snippet": line.strip()[:120],
                })
    return findings


# ── Runner ────────────────────────────────────────────────────────────────────

SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


def main():
    t0 = time.time()
    print("=" * 60)
    print("BTC Prediction Bot — Security Audit")
    print(f"Repo: {REPO_ROOT}")
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print("=" * 60)

    # Load all files once
    print("\n[*] Scanning source files...")
    files_cache = list(_iter_source_files())
    print(f"    {len(files_cache)} files scanned.")

    all_findings = []
    all_findings += check_secrets(files_cache)
    all_findings += check_gitignore()
    all_findings += check_csp()
    all_findings += check_unprotected_post()
    all_findings += check_cors(files_cache)

    # Sort by severity
    all_findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 9), f["file"], f["line"]))

    # ── JSON report ──────────────────────────────────────────────────────────
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo": str(REPO_ROOT),
        "files_scanned": len(files_cache),
        "total_findings": len(all_findings),
        "summary": {},
        "findings": all_findings,
    }
    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
        report["summary"][sev] = sum(1 for f in all_findings if f["severity"] == sev)

    print("\n" + json.dumps(report, indent=2))

    # ── Human-readable summary ───────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for sev in ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]:
        count = report["summary"][sev]
        icon = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢", "INFO": "ℹ️"}.get(sev, " ")
        print(f"  {icon} {sev:8s}: {count}")

    if all_findings:
        print("\nFINDINGS:")
        for f in all_findings:
            print(f"  [{f['severity']:8s}] {f['file']}:{f['line']}  {f['label']}")
            print(f"             → {f['snippet'][:80]}")
    else:
        print("\n  ✅ No findings.")

    elapsed = time.time() - t0
    print(f"\nCompleted in {elapsed:.1f}s")
    print("=" * 60)

    has_critical = report["summary"]["CRITICAL"] > 0
    if has_critical:
        print("\n⛔ ALERT: CRITICAL findings detected — exit code 1")
    else:
        print("\n✅ No CRITICAL findings — exit code 0")

    sys.exit(1 if has_critical else 0)


if __name__ == "__main__":
    main()
