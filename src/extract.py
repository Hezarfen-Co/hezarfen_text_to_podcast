from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_PPTX = "pptx"
KIND_ODT = "odt"
KIND_ODP = "odp"
KIND_TEXT = "text"
KIND_SHEET = "sheet"
KIND_RTF = "rtf"
KIND_OLE = "ole"
KIND_ZIP = "zip"
KIND_UNKNOWN = "unknown"

UNSUPPORTED_SOURCE = "unsupported_source"
SOURCE_UNREADABLE = "source_unreadable"

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
ZIP_EMPTY_MAGIC = b"PK\x05\x06"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
RTF_MAGIC = b"{\\rtf"
MAGIC_BYTES = 8

TEXT_PROBE_BYTES = 4096
TEXT_PRINTABLE_RATIO = 0.9
TEXT_ENCODINGS = ("utf-8-sig", "cp1254", "latin-1")

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DRAWING_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

DOCX_BODY = "word/document.xml"
PPTX_ROOT = "ppt/presentation.xml"
XLSX_ROOT = "xl/workbook.xml"
SLIDE_PATTERN = re.compile(r"^ppt/slides/slide(\d+)\.xml$")

ODF_MIMETYPE = "mimetype"
ODF_CONTENT = "content.xml"
ODF_TEXT_MIME = "application/vnd.oasis.opendocument.text"
ODF_PRESENTATION_MIME = "application/vnd.oasis.opendocument.presentation"
ODF_SHEET_MIME = "application/vnd.oasis.opendocument.spreadsheet"

TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
DRAW_NS = "{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}"

MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 16 * 1024 * 1024

READABLE_KINDS = (KIND_PDF, KIND_DOCX, KIND_PPTX, KIND_ODT, KIND_ODP, KIND_TEXT)
SUPPORTED_NOTE = "desteklenen bicimler: pdf, docx, pptx, odt, odp, duz metin"

OLE_HINT = (
    "eski ikili Office bicimi (.doc/.ppt/.xls) desteklenmiyor; "
    "dosyayi .docx veya .pptx olarak kaydedip yeniden yukleyin"
)
SHEET_HINT = (
    "hesap tablosu (.xlsx/.ods) anlatima uygun degil; ders metnini "
    "belge olarak yukleyin"
)
RTF_HINT = (
    "rtf desteklenmiyor; dosyayi .docx veya .odt olarak kaydedip "
    "yeniden yukleyin"
)


class ExtractError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _head(path: str | Path, size: int = MAGIC_BYTES) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError as exc:
        raise ExtractError(
            SOURCE_UNREADABLE, f"kaynak dosya okunamadi: {exc}"
        ) from exc


def _odf_kind(names: set[str], archive: zipfile.ZipFile) -> str | None:
    if ODF_MIMETYPE not in names:
        return None
    try:
        declared = archive.read(ODF_MIMETYPE).decode("ascii", "replace").strip()
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    if declared == ODF_TEXT_MIME:
        return KIND_ODT
    if declared == ODF_PRESENTATION_MIME:
        return KIND_ODP
    if declared == ODF_SHEET_MIME:
        return KIND_SHEET
    return None


def _zip_kind(path: str | Path) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            odf = _odf_kind(names, archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExtractError(
            SOURCE_UNREADABLE, f"zip tabanli belge acilamadi: {exc}"
        ) from exc
    if odf is not None:
        return odf
    if DOCX_BODY in names:
        return KIND_DOCX
    if PPTX_ROOT in names or any(SLIDE_PATTERN.match(name) for name in names):
        return KIND_PPTX
    if XLSX_ROOT in names:
        return KIND_SHEET
    return KIND_ZIP


def _looks_like_text(probe: bytes) -> bool:
    if not probe:
        return False
    if b"\x00" in probe:
        return False
    for encoding in TEXT_ENCODINGS:
        try:
            decoded = probe.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        printable = sum(
            1 for char in decoded if char.isprintable() or char in "\t\n\r"
        )
        return printable / len(decoded) >= TEXT_PRINTABLE_RATIO
    return False


def sniff(path: str | Path) -> str:
    head = _head(path)
    if head.startswith(PDF_MAGIC):
        return KIND_PDF
    if head.startswith(OLE_MAGIC):
        return KIND_OLE
    if head.startswith(RTF_MAGIC):
        return KIND_RTF
    if head.startswith(ZIP_MAGIC) or head.startswith(ZIP_EMPTY_MAGIC):
        return _zip_kind(path)
    if _looks_like_text(_head(path, TEXT_PROBE_BYTES)):
        return KIND_TEXT
    return KIND_UNKNOWN


def _guard_size(archive: zipfile.ZipFile, members: list[str]) -> None:
    total = 0
    for name in members:
        try:
            size = archive.getinfo(name).file_size
        except KeyError:
            continue
        if size > MAX_MEMBER_BYTES:
            raise ExtractError(
                SOURCE_UNREADABLE,
                f"{name} acildiginda {size} bayt; uye siniri "
                f"{MAX_MEMBER_BYTES} bayt",
            )
        total += size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ExtractError(
                SOURCE_UNREADABLE,
                f"belge acildiginda {MAX_UNCOMPRESSED_BYTES} bayt sinirini asiyor",
            )


def _parse_member(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        raw = archive.read(name)
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise ExtractError(
            SOURCE_UNREADABLE, f"{name} okunamadi: {exc}"
        ) from exc
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ExtractError(
            SOURCE_UNREADABLE, f"{name} xml olarak ayristirilamadi: {exc}"
        ) from exc


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{WORD_NS}t":
            parts.append(node.text or "")
        elif node.tag == f"{WORD_NS}tab":
            parts.append(" ")
        elif node.tag in (f"{WORD_NS}br", f"{WORD_NS}cr"):
            parts.append("\n")
    return "".join(parts).strip()


def docx_blocks(path: str | Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            _guard_size(archive, [DOCX_BODY])
            root = _parse_member(archive, DOCX_BODY)
    except zipfile.BadZipFile as exc:
        raise ExtractError(SOURCE_UNREADABLE, f"docx acilamadi: {exc}") from exc
    blocks: list[str] = []
    for paragraph in root.iter(f"{WORD_NS}p"):
        text = _paragraph_text(paragraph)
        if text:
            blocks.append(text)
    return blocks


def _slide_names(archive: zipfile.ZipFile) -> list[str]:
    numbered: list[tuple[int, str]] = []
    for name in archive.namelist():
        match = SLIDE_PATTERN.match(name)
        if match is not None:
            numbered.append((int(match.group(1)), name))
    numbered.sort()
    return [name for _, name in numbered]


def pptx_slides(path: str | Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = _slide_names(archive)
            _guard_size(archive, names)
            slides: list[str] = []
            for name in names:
                root = _parse_member(archive, name)
                parts = [
                    node.text
                    for node in root.iter(f"{DRAWING_NS}t")
                    if node.text and node.text.strip()
                ]
                slides.append("\n".join(part.strip() for part in parts))
    except zipfile.BadZipFile as exc:
        raise ExtractError(SOURCE_UNREADABLE, f"pptx acilamadi: {exc}") from exc
    return slides


def _odf_root(path: str | Path, label: str) -> ElementTree.Element:
    try:
        with zipfile.ZipFile(path) as archive:
            _guard_size(archive, [ODF_CONTENT])
            return _parse_member(archive, ODF_CONTENT)
    except zipfile.BadZipFile as exc:
        raise ExtractError(
            SOURCE_UNREADABLE, f"{label} acilamadi: {exc}"
        ) from exc


def _odf_paragraphs(scope: ElementTree.Element) -> list[str]:
    wanted = (f"{TEXT_NS}p", f"{TEXT_NS}h")
    consumed: set[int] = set()
    blocks: list[str] = []
    for node in scope.iter():
        if node.tag not in wanted or id(node) in consumed:
            continue
        for nested in node.iter():
            consumed.add(id(nested))
        text = "".join(node.itertext()).strip()
        if text:
            blocks.append(text)
    return blocks


def odt_blocks(path: str | Path) -> list[str]:
    return _odf_paragraphs(_odf_root(path, "odt"))


def odp_slides(path: str | Path) -> list[str]:
    root = _odf_root(path, "odp")
    slides: list[str] = []
    for page in root.iter(f"{DRAW_NS}page"):
        slides.append("\n".join(_odf_paragraphs(page)))
    return slides


def text_blocks(path: str | Path) -> list[str]:
    raw = _head(path, MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        raise ExtractError(
            SOURCE_UNREADABLE,
            f"duz metin {MAX_TEXT_BYTES} bayt sinirini asiyor",
        )
    for encoding in TEXT_ENCODINGS:
        try:
            decoded = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        blocks = [
            block.strip()
            for block in re.split(r"\n\s*\n", decoded.replace("\r\n", "\n"))
        ]
        return [block for block in blocks if block]
    raise ExtractError(SOURCE_UNREADABLE, "duz metin cozulemedi")


def unsupported_message(kind: str) -> str:
    if kind == KIND_OLE:
        return OLE_HINT
    if kind == KIND_SHEET:
        return SHEET_HINT
    if kind == KIND_RTF:
        return RTF_HINT
    if kind == KIND_ZIP:
        return f"zip tabanli ama taninan bir belge degil; {SUPPORTED_NOTE}"
    return f"taninmayan belge bicimi; {SUPPORTED_NOTE}"
