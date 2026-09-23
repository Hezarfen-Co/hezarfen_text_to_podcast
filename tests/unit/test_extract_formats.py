from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import extract

OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
ODF_TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
ODF_DRAW_NS = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def zip_bytes(members: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def docx_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{item}</w:t></w:r></w:p>" for item in paragraphs
    )
    document = (
        f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}</w:body></w:document>'
    )
    return zip_bytes({"word/document.xml": document})


def pptx_bytes(slides: list[list[str]]) -> bytes:
    members = {"ppt/presentation.xml": f'<p:presentation xmlns:p="{PRESENTATION_NS}"/>'}
    for index, lines in enumerate(slides, 1):
        shapes = "".join(
            f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines
        )
        members[f"ppt/slides/slide{index}.xml"] = (
            f'<p:sld xmlns:p="{PRESENTATION_NS}" xmlns:a="{DRAWING_NS}">'
            f"{shapes}</p:sld>"
        )
    return zip_bytes(members)


def odt_bytes(paragraphs: list[str]) -> bytes:
    body = "".join(f"<text:p>{item}</text:p>" for item in paragraphs)
    content = (
        f'<office:document-content xmlns:office="{OFFICE_NS}" '
        f'xmlns:text="{ODF_TEXT_NS}"><office:body><office:text>{body}'
        f"</office:text></office:body></office:document-content>"
    )
    return zip_bytes(
        {"mimetype": extract.ODF_TEXT_MIME, "content.xml": content}
    )


def odp_bytes(slides: list[list[str]]) -> bytes:
    pages = []
    for lines in slides:
        text = "".join(f"<text:p>{line}</text:p>" for line in lines)
        pages.append(f"<draw:page>{text}</draw:page>")
    content = (
        f'<office:document-content xmlns:office="{OFFICE_NS}" '
        f'xmlns:text="{ODF_TEXT_NS}" xmlns:draw="{ODF_DRAW_NS}"><office:body>'
        f'<office:presentation>{"".join(pages)}</office:presentation>'
        f"</office:body></office:document-content>"
    )
    return zip_bytes(
        {"mimetype": extract.ODF_PRESENTATION_MIME, "content.xml": content}
    )


class FormatTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="bicim-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path


class OpenDocumentIsRead(FormatTestCase):
    def test_an_odt_is_recognised_by_its_declared_mimetype(self) -> None:
        path = self.write("kaynak", odt_bytes(["merhaba"]))
        self.assertEqual(extract.sniff(path), extract.KIND_ODT)

    def test_an_odp_is_recognised_by_its_declared_mimetype(self) -> None:
        path = self.write("kaynak", odp_bytes([["merhaba"]]))
        self.assertEqual(extract.sniff(path), extract.KIND_ODP)

    def test_odt_paragraphs_and_headings_become_blocks(self) -> None:
        path = self.write("kaynak", odt_bytes(["birinci", "ikinci"]))
        self.assertEqual(extract.odt_blocks(path), ["birinci", "ikinci"])

    def test_odp_pages_become_one_block_each(self) -> None:
        path = self.write("kaynak", odp_bytes([["ust", "alt"], ["tek"]]))
        self.assertEqual(extract.odp_slides(path), ["ust\nalt", "tek"])

    def test_a_nested_paragraph_is_not_emitted_twice(self) -> None:
        content = (
            f'<office:document-content xmlns:office="{OFFICE_NS}" '
            f'xmlns:text="{ODF_TEXT_NS}"><office:body><office:text>'
            f"<text:p>dis<text:p>ic</text:p></text:p>"
            f"</office:text></office:body></office:document-content>"
        )
        path = self.write(
            "kaynak",
            zip_bytes({"mimetype": extract.ODF_TEXT_MIME, "content.xml": content}),
        )
        self.assertEqual(
            extract.odt_blocks(path),
            ["disic"],
            "ic paragraf ayri bir blok olarak tekrar edilmemeli",
        )

    def test_a_corrupt_odt_raises_a_named_error(self) -> None:
        path = self.write(
            "kaynak", zip_bytes({"mimetype": extract.ODF_TEXT_MIME})
        )
        with self.assertRaises(extract.ExtractError) as raised:
            extract.odt_blocks(path)
        self.assertEqual(raised.exception.code, extract.SOURCE_UNREADABLE)


class PlainTextIsRead(FormatTestCase):
    def test_a_utf8_note_is_recognised_as_text(self) -> None:
        path = self.write("kaynak", "ders notu".encode("utf-8"))
        self.assertEqual(extract.sniff(path), extract.KIND_TEXT)

    def test_blank_lines_separate_blocks(self) -> None:
        body = "birinci" + "\n\n\n" + "ikinci" + "\n\n" + "ucuncu"
        path = self.write("kaynak", body.encode("utf-8"))
        self.assertEqual(
            extract.text_blocks(path), ["birinci", "ikinci", "ucuncu"]
        )

    def test_windows_line_endings_are_handled(self) -> None:
        body = "birinci" + "\r\n\r\n" + "ikinci"
        path = self.write("kaynak", body.encode("utf-8"))
        self.assertEqual(extract.text_blocks(path), ["birinci", "ikinci"])

    def test_a_turkish_note_in_legacy_encoding_still_decodes(self) -> None:
        body = "\u00e7al\u0131\u015fma notu"
        path = self.write("kaynak", body.encode("cp1254"))
        self.assertEqual(extract.sniff(path), extract.KIND_TEXT)
        self.assertEqual(extract.text_blocks(path), [body])

    def test_a_binary_blob_is_not_mistaken_for_text(self) -> None:
        path = self.write("kaynak", bytes(range(0, 32)) * 8)
        self.assertNotEqual(extract.sniff(path), extract.KIND_TEXT)

    def test_an_empty_file_is_not_text(self) -> None:
        path = self.write("kaynak", b"")
        self.assertEqual(extract.sniff(path), extract.KIND_UNKNOWN)


class RelatedExtensionsNeedNoExtraCode(FormatTestCase):
    def test_a_macro_enabled_word_file_reads_as_docx(self) -> None:
        path = self.write("ders.docm", docx_bytes(["makro belge"]))
        self.assertEqual(extract.sniff(path), extract.KIND_DOCX)
        self.assertEqual(extract.docx_blocks(path), ["makro belge"])

    def test_a_word_template_reads_as_docx(self) -> None:
        path = self.write("sablon.dotx", docx_bytes(["sablon"]))
        self.assertEqual(extract.sniff(path), extract.KIND_DOCX)

    def test_a_powerpoint_template_reads_as_pptx(self) -> None:
        path = self.write("sablon.potx", pptx_bytes([["sablon slayt"]]))
        self.assertEqual(extract.sniff(path), extract.KIND_PPTX)
        self.assertEqual(extract.pptx_slides(path), ["sablon slayt"])

    def test_a_slideshow_file_reads_as_pptx(self) -> None:
        path = self.write("gosteri.ppsx", pptx_bytes([["gosteri"]]))
        self.assertEqual(extract.sniff(path), extract.KIND_PPTX)

    def test_the_extension_never_decides_the_format(self) -> None:
        path = self.write("rapor.xlsx", odt_bytes(["aslinda odt"]))
        self.assertEqual(
            extract.sniff(path),
            extract.KIND_ODT,
            "blob adlari uzantisizdir; tur her zaman icerikten gelir",
        )


class RefusedFormatsSayWhyAndWhatToDo(FormatTestCase):
    def test_an_ooxml_spreadsheet_is_named_and_refused(self) -> None:
        path = self.write("kaynak", zip_bytes({"xl/workbook.xml": "<workbook/>"}))
        self.assertEqual(extract.sniff(path), extract.KIND_SHEET)
        self.assertIn(
            "hesap tablosu", extract.unsupported_message(extract.KIND_SHEET)
        )

    def test_an_opendocument_spreadsheet_is_also_refused(self) -> None:
        path = self.write(
            "kaynak",
            zip_bytes(
                {"mimetype": extract.ODF_SHEET_MIME, "content.xml": "<x/>"}
            ),
        )
        self.assertEqual(extract.sniff(path), extract.KIND_SHEET)

    def test_rtf_is_named_and_refused_with_a_way_out(self) -> None:
        path = self.write("kaynak", extract.RTF_MAGIC + b"1 ansi metin}")
        self.assertEqual(extract.sniff(path), extract.KIND_RTF)
        self.assertIn(".docx", extract.unsupported_message(extract.KIND_RTF))

    def test_every_refusal_lists_the_supported_formats(self) -> None:
        for kind in (
            extract.KIND_ZIP,
            extract.KIND_UNKNOWN,
            extract.KIND_OLE,
            extract.KIND_SHEET,
            extract.KIND_RTF,
        ):
            with self.subTest(kind=kind):
                message = extract.unsupported_message(kind)
                self.assertTrue(message.strip(), "her red bir gerekce tasimali")

    def test_a_refused_kind_is_never_in_the_readable_set(self) -> None:
        for kind in (
            extract.KIND_OLE,
            extract.KIND_SHEET,
            extract.KIND_RTF,
            extract.KIND_ZIP,
            extract.KIND_UNKNOWN,
        ):
            with self.subTest(kind=kind):
                self.assertNotIn(kind, extract.READABLE_KINDS)


class EngineReadsEveryReadableKind(FormatTestCase):
    def _extract(self, path):
        from src import api_engine, config

        return api_engine.extract_text(
            str(path), config.Config(require_token=False), lambda: None
        )

    def test_an_odt_reaches_the_engine(self) -> None:
        body = ["Hucre canliligin temel birimidir." * 8]
        path = self.write("kaynak", odt_bytes(body))
        text, blocks, ocr_pages = self._extract(path)
        self.assertIn("Hucre canliligin temel birimidir", text)
        self.assertEqual(blocks, 1)
        self.assertEqual(ocr_pages, 0)

    def test_an_odp_reaches_the_engine(self) -> None:
        slides = [["Hucre canliligin temel birimidir." * 8], ["Ikinci." * 8]]
        path = self.write("kaynak", odp_bytes(slides))
        text, blocks, _ = self._extract(path)
        self.assertIn("Ikinci", text)
        self.assertEqual(blocks, 2)

    def test_a_plain_note_reaches_the_engine(self) -> None:
        body = "Hucre canliligin temel birimidir. " * 10
        path = self.write("kaynak", body.encode("utf-8"))
        text, blocks, _ = self._extract(path)
        self.assertIn("Hucre canliligin temel birimidir", text)
        self.assertGreaterEqual(blocks, 1)

    def test_a_spreadsheet_is_refused_as_unsupported_source(self) -> None:
        from src import api_engine

        path = self.write("kaynak", zip_bytes({"xl/workbook.xml": "<workbook/>"}))
        with self.assertRaises(api_engine.EngineError) as raised:
            self._extract(path)
        self.assertEqual(raised.exception.code, api_engine.UNSUPPORTED_SOURCE)
        self.assertIn("hesap tablosu", str(raised.exception))

    def test_rtf_is_refused_as_unsupported_source(self) -> None:
        from src import api_engine

        path = self.write("kaynak", extract.RTF_MAGIC + b"1 ansi metin}")
        with self.assertRaises(api_engine.EngineError) as raised:
            self._extract(path)
        self.assertEqual(raised.exception.code, api_engine.UNSUPPORTED_SOURCE)


if __name__ == "__main__":
    unittest.main()
