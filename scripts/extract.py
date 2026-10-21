"""Plain-text extraction for the non-markdown sources in raw/.

Every raw file becomes a page. Text-bearing formats (pdf, office, html, csv,
xml, source code) are converted to markdown-ish plain text; binaries that carry
no readable text (images, archives, media) become metadata-only stub pages so
nothing in raw/ is silently skipped.

Heavy parsers (pypdf, python-docx, openpyxl, python-pptx) are imported lazily so
that llmwiki.py keeps working on a bare interpreter.
"""
from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

MAX_CHARS = 400_000          # a single page's extracted body cap
MAX_ROWS = 400               # per spreadsheet sheet
MAX_COLS = 40


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, path: Path) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + f"\n\n> …잘림 — 전문은 원본 `{path.name}` 참조."


# --------------------------------------------------------------------- formats
MAX_PDF_PAGES = 300           # design exports run to thousands of vector pages


def from_pdf(path: Path) -> str:
    """poppler's pdftotext when available (orders of magnitude faster on the
    image-heavy design exports); pypdf otherwise."""
    import shutil as _shutil
    import subprocess
    binary = _shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
    if Path(binary).exists():
        try:
            out = subprocess.run([binary, "-l", str(MAX_PDF_PAGES), "-q", str(path), "-"],
                                 capture_output=True, timeout=180)
            text = _clean(out.stdout.decode("utf-8", errors="replace"))
            if text:
                return text
        except Exception:                                     # noqa: BLE001
            pass
    return _from_pdf_pypdf(path)


def _from_pdf_pypdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    out = []
    for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1):
        try:
            body = _clean(page.extract_text() or "")
        except Exception:                                     # noqa: BLE001
            body = ""
        if body:
            out.append(f"## p.{i}\n\n{body}")
    return "\n\n".join(out)


def from_docx(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    out = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower()
        if style.startswith("heading"):
            level = "".join(ch for ch in style if ch.isdigit()) or "2"
            out.append("#" * min(int(level) + 1, 6) + " " + text)
        else:
            out.append(text)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            if any(cells):
                out.append("| " + " | ".join(cells) + " |")
    return "\n\n".join(out)


def from_xlsx(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for r, row in enumerate(ws.iter_rows(values_only=True)):
            if r >= MAX_ROWS:
                rows.append(f"> …{ws.max_row - MAX_ROWS} 행 더 있음")
                break
            cells = ["" if v is None else str(v).replace("\n", " ").replace("|", "/")
                     for v in row[:MAX_COLS]]
            if any(c.strip() for c in cells):
                rows.append("| " + " | ".join(cells) + " |")
        if rows:
            out.append(f"## sheet: {ws.title}\n\n" + "\n".join(rows))
    wb.close()
    return "\n\n".join(out)


def from_pptx(path: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(path))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    if any(cells):
                        parts.append("| " + " | ".join(cells) + " |")
        if parts:
            out.append(f"## slide {i}\n\n" + "\n\n".join(parts))
    return "\n\n".join(out)


def from_html(path: Path) -> str:
    parser = _Text()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    return _clean(html.unescape("".join(parser.parts)))


def from_csv(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    out = []
    for r, row in enumerate(csv.reader(io.StringIO(text), dialect)):
        if r >= MAX_ROWS:
            out.append("> …이후 행 생략")
            break
        cells = [c.strip().replace("\n", " ").replace("|", "/") for c in row[:MAX_COLS]]
        if any(cells):
            out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


def from_drawio(path: Path) -> str:
    """drawio files are XML; pull the visible node labels."""
    text = path.read_text(encoding="utf-8", errors="replace")
    labels = [html.unescape(m) for m in re.findall(r'value="([^"]+)"', text)]
    labels = [_clean(re.sub(r"<[^>]+>", " ", lab)) for lab in labels]
    return "\n".join(f"- {lab}" for lab in labels if lab)


def from_json(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return "```json\n" + json.dumps(json.loads(text), ensure_ascii=False, indent=2) + "\n```"
    except json.JSONDecodeError:
        return "```\n" + text + "\n```"


def from_plain(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def from_code(path: Path) -> str:
    lang = path.suffix.lstrip(".")
    return f"```{lang}\n" + path.read_text(encoding="utf-8", errors="replace") + "\n```"


def from_keynote(path: Path) -> str:
    """.key is a zip bundle; surface whatever plain text the preview carries."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
        return "\n".join(f"- {n}" for n in names[:200])
    except Exception:                                         # noqa: BLE001
        return ""


EXTRACTORS = {
    ".pdf": from_pdf, ".docx": from_docx, ".xlsx": from_xlsx, ".xlsm": from_xlsx,
    ".pptx": from_pptx, ".html": from_html, ".htm": from_html, ".csv": from_csv,
    ".tsv": from_csv, ".drawio": from_drawio, ".json": from_json, ".ndjson": from_plain,
    ".txt": from_plain, ".url": from_plain, ".sql": from_code, ".py": from_code,
    ".js": from_code, ".sh": from_code, ".key": from_keynote,
}

# Carry no extractable text — recorded as metadata-only pages.
BINARY_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ai", ".psd",
                   ".fig", ".zip", ".mp4", ".mov", ".m4a", ".mp3", ".dxp",
                   ".crdownload", ".bkp", ".xls", ".doc", ".ppt"}


def extract(path: Path) -> tuple[str, bool]:
    """Return (text, has_text). has_text=False means a metadata-only stub."""
    fn = EXTRACTORS.get(path.suffix.lower())
    if fn is None:
        return "", False
    try:
        text = _clean(fn(path))
    except Exception as exc:                                  # noqa: BLE001
        return f"> 추출 실패: {type(exc).__name__}: {exc}", False
    return (_truncate(text, path), True) if text else ("", False)
