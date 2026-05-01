"""Local public-safety validation."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

BANNED_TERMS = (
    "aka" + "mai",
    "corp" + "orate email",
    "internal user" + "name",
    "proprietary ident" + "ifier",
)
# Build legacy workflow terms from fragments so the scanner cannot flag its own source.
LEGACY_WORKFLOW_TERMS = tuple(
    "".join(parts)
    for parts in (
        ("fr", "eeze"),
        ("th", "aw"),
        ("fr", "eeze-", "th", "aw"),
    )
)
EXECUTION_MODEL_DRIFT_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"\bdes" + r"ired[- ]sta" + r"te\b",
        r"\bsta" + r"te[- ]files?\b",
        r"\bdri" + r"ft[- ]recon" + r"ciliation\b",
        r"\bres" + r"ource[- ]graphs?\b",
        r"\bdep" + r"endency[- ]planning\b",
        r"\bter" + r"raform\b",
    )
)
EXECUTION_MODEL_SECTION = "Execution Model Boundary"
EXECUTION_MODEL_SECTION_FILES = {
    Path("AGENTS.md"),
    Path("README.md"),
    Path("docs/design.md"),
}
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PRIVATE_URL_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|10\.|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[0-1])\.|[^/\s]+\.internal\b|[^/\s]+\.corp\b)",
    re.I,
)
SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:token|secret|password|api[_-]?key)\s*[:=]\s*['\"](?!LINODE_TOKEN['\"])[^'\"\s]{8,}['\"]"
)
# Build these ranges from code points so the scanner cannot flag its own source.
BIDI_CONTROL_CHARS = frozenset(
    chr(codepoint)
    for start, end in ((0x202A, 0x202E), (0x2066, 0x2069))
    for codepoint in range(start, end + 1)
)
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".txt",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".sh",
    ".mk",
}
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    ".venv",
    "venv",
    "build",
    "dist",
    "htmlcov",
}
FIXTURE_ROOT = Path("tests/fixtures")
SANITIZED_FIXTURE_ROOT = FIXTURE_ROOT / "sanitized"


def should_skip_local_path(path: Path) -> bool:
    return any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts)


def iter_local_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if should_skip_local_path(path):
            continue
        if path.is_file():
            files.append(path)
    return files


def is_text_path(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name == "Makefile"


def iter_scanned_files(root: Path) -> list[Path]:
    if not (root / ".git").exists():
        return iter_local_files(root)

    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return iter_local_files(root)

    files: list[Path] = []
    for name in result.stdout.decode("utf-8").split("\0"):
        if not name:
            continue
        files.append(root / Path(name))
    return files


def iter_scanned_text_files(root: Path) -> list[Path]:
    return [path for path in iter_scanned_files(root) if is_text_path(path)]


def is_under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def scan_fixture_placement(root: Path) -> list[str]:
    sanitized_root = root / SANITIZED_FIXTURE_ROOT

    findings: list[str] = []
    for path in iter_scanned_files(root):
        relative_path = path.relative_to(root)
        if is_under(relative_path, FIXTURE_ROOT) and not is_under(path, sanitized_root):
            relative = path.relative_to(root)
            findings.append(f"{relative}: fixture files must live under {SANITIZED_FIXTURE_ROOT}/")
    return findings


def scan_public_safety(root: Path) -> list[str]:
    findings = scan_fixture_placement(root)
    for path in iter_scanned_text_files(root):
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root)
        lower_text = text.lower()

        for term in BANNED_TERMS:
            if term in lower_text:
                findings.append(f"{relative}: restricted term detected")
        if EMAIL_RE.search(text):
            findings.append(f"{relative}: email-like value detected")
        if PRIVATE_URL_RE.search(text):
            findings.append(f"{relative}: private URL detected")
        if SECRET_VALUE_RE.search(text):
            findings.append(f"{relative}: secret-like assignment detected")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(character in BIDI_CONTROL_CHARS for character in line):
                findings.append(f"{relative}:{line_number}: hidden Unicode bidi control detected")

    findings.extend(scan_terminology_drift(root))
    return findings


def scan_terminology_drift(root: Path) -> list[str]:
    findings: list[str] = []
    for path in iter_scanned_text_files(root):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(root)
        for line_number, line in enumerate(text.splitlines(), start=1):
            lower_line = line.lower()
            if any(term in lower_line for term in LEGACY_WORKFLOW_TERMS):
                findings.append(f"{relative}:{line_number}: legacy image workflow terminology detected")
            if has_execution_model_drift(line) and not is_allowed_execution_model_section(relative, line_number, text):
                findings.append(
                    f"{relative}:{line_number}: out-of-scope infrastructure-management terminology detected"
                )
    return findings


def has_execution_model_drift(line: str) -> bool:
    return any(pattern.search(line) for pattern in EXECUTION_MODEL_DRIFT_PATTERNS)


def is_allowed_execution_model_section(relative: Path, line_number: int, text: str) -> bool:
    if relative not in EXECUTION_MODEL_SECTION_FILES:
        return False

    active_heading_level: int | None = None
    in_allowed_section = False
    for current_line_number, line in enumerate(text.splitlines(), start=1):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2)
            if title == EXECUTION_MODEL_SECTION:
                active_heading_level = level
                in_allowed_section = True
            elif active_heading_level is not None and level <= active_heading_level:
                in_allowed_section = False
                active_heading_level = None
        if current_line_number == line_number:
            return in_allowed_section

    return False


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = Path(args[0] if args else ".").resolve()
    findings = scan_public_safety(root)
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1
    print("security-check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
