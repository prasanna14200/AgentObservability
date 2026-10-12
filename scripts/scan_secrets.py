"""Local, value-suppressing secret audit for the worktree and reachable Git history.

This is a heuristic supplement to a dedicated scanner. It never prints matches.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".pytest_cache", "__pycache__", ".venv", "venv"}
PATTERNS = {
    "private_key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"\b(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}"),
    "openai_key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "google_api_key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}"),
    "credential_assignment": re.compile(
        rb"(?im)^\s*(?:[\"']?)(?:PAYPAL_CLIENT_SECRET|PAYPAL_ACCESS_TOKEN|"
        rb"CLIENT_SECRET|API_SECRET|ACCESS_TOKEN|PRIVATE_KEY)(?:[\"']?)\s*[:=]\s*"
        rb"[\"']?([A-Za-z0-9_./+=:-]{20,})"
    ),
}
PLACEHOLDERS = (b"YOUR_", b"EXAMPLE", b"PLACEHOLDER", b"DUMMY", b"TEST_", b"RAW-")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL)


def findings(data: bytes) -> set[str]:
    if b"\x00" in data[:4096]:
        return set()
    names: set[str] = set()
    for name, pattern in PATTERNS.items():
        for match in pattern.finditer(data):
            candidate = match.group(1) if name == "credential_assignment" else match.group(0)
            if any(marker in candidate.upper() for marker in PLACEHOLDERS):
                continue
            names.add(name)
    return names


def main() -> int:
    alerts: set[tuple[str, str, str]] = set()
    worktree_files = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        worktree_files += 1
        if path.stat().st_size > 5_000_000:
            continue
        for name in findings(path.read_bytes()):
            alerts.add(("worktree", str(path.relative_to(ROOT)), name))

    history_blobs = 0
    for line in git("rev-list", "--objects", "--all").decode("utf-8", errors="replace").splitlines():
        sha, _, path = line.partition(" ")
        if git("cat-file", "-t", sha).strip() != b"blob":
            continue
        history_blobs += 1
        if int(git("cat-file", "-s", sha)) > 5_000_000:
            continue
        for name in findings(git("cat-file", "blob", sha)):
            alerts.add(("history", path or "<unknown path>", name))

    suspicious_paths = [
        line for line in git("log", "--all", "--name-only", "--format=").decode("utf-8", errors="replace").splitlines()
        if re.search(r"(^|/)(?:\.env(?:\.|$)|[^/]*(?:credential|secret|private[-_]key)[^/]*)", line, re.I)
        and line not in {".env.example", "scripts/scan_secrets.py"}
    ]
    for path in sorted(set(suspicious_paths)):
        alerts.add(("history_filename", path, "credential-shaped filename"))

    print(f"Scanned {worktree_files} worktree files and {history_blobs} reachable history blobs.")
    if alerts:
        for scope, path, name in sorted(alerts):
            print(f"CANDIDATE {scope} {path} [{name}]")
        return 1
    print("No high-confidence credential signatures or credential-shaped historical filenames found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
