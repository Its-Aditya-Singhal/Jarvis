"""The text of a file on this Mac, for summarising it: PDFs (pypdf), Word, RTF, OpenDocument and
web pages (macOS ``textutil``), and plain text. Only files the runner already allowed reach here."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

MAX_CHARS = 60_000  # about 15k tokens: enough for a long report, within the free tier's limits
MAX_PAGES = 60
PLAIN = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".py", ".js", ".ts", ".html", ".htm", ".xml", ".yaml", ".yml"}
TEXTUTIL = {".doc", ".docx", ".rtf", ".rtfd", ".odt", ".wordml", ".webarchive"}
READABLE = PLAIN | TEXTUTIL | {".pdf"}


class ReadError(ValueError):
    pass


def _textutil(path: Path) -> str:
    try:
        out = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True, text=True,
                             timeout=30, check=True)
    except FileNotFoundError as exc:
        raise ReadError("reading Word files needs macOS") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReadError("macOS couldn't read that document") from exc
    return out.stdout


def _pdf(path: Path) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ReadError("that PDF is password-protected")
        parts, size = [], 0
        for page in reader.pages[:MAX_PAGES]:
            t = page.extract_text() or ""
            parts.append(t)
            size += len(t)
            if size > MAX_CHARS:
                break
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        raise ReadError("that PDF couldn't be read") from exc
    return "\n".join(parts)


def read_text(path: Path, textutil: Callable[[Path], str] = _textutil) -> str:
    """The file's words (cut to MAX_CHARS). ReadError when the kind can't be read or holds no text."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        text = _pdf(path)
    elif ext in TEXTUTIL:
        text = textutil(path)
    elif ext in PLAIN:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_CHARS + 1)
        except OSError as exc:
            raise ReadError("I couldn't open that file") from exc
    else:
        raise ReadError(f"I can't read {ext or 'that kind of'} files; PDFs, Word and text files work")
    text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
    if not text.strip():
        raise ReadError("that file has no text I can read (a scanned PDF needs OCR first)")
    return text[:MAX_CHARS]
