# SPDX-License-Identifier: MIT
"""Check supplied public files without printing potentially private matches.

This is a limited pattern/hygiene check, not a comprehensive secret scanner.
"""

import re
import sys
from pathlib import Path

PATTERNS = {
    "internal task label": r"\b[MFBD]\d{1,2}\b",
    "private planning reference": r"(?:claude[_-]files|MASTER[_]PLAN|SESSION[_]LOG)",
    "personal email": r"[\w.+-]+@(?:gmail|hotmail|outlook|yahoo)\.com",
    "institutional email": r"[\w.+-]+@bank(?:ofcanada|[-]banque[-]canada)[.]ca",
    "token-like credential": r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})",
    "local user path": r"(?:[A-Za-z]:[\\/]Users[\\/]|/ho[m]e/|/Use[r]s/)",
}


def problems(path: Path) -> list[str]:
    data = path.read_bytes()
    if b"\0" in data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["not UTF-8"]
    found = []
    if text and not text.endswith("\n"):
        found.append("missing final newline")
    if path.suffix not in {".bat", ".cmd"} and b"\r" in data:
        found.append("non-LF newline")
    if any(line.rstrip() != line for line in text.splitlines()):
        found.append("trailing whitespace")
    if len(data) > 500_000:
        found.append("large text file requires review")
    # The designated project mailbox is the only personal-domain address allowed.
    scrubbed = text.replace("statespaceecon@gmail.com", "PROJECT_CONTACT")
    for label, pattern in PATTERNS.items():
        if re.search(pattern, scrubbed):
            found.append(label)
    return found


def main() -> int:
    failures = [(Path(name), problems(Path(name))) for name in sys.argv[1:]]
    for path, issues in failures:
        for issue in issues:
            print(f"{path}: {issue}")
    return int(any(issues for _, issues in failures))


if __name__ == "__main__":
    raise SystemExit(main())
