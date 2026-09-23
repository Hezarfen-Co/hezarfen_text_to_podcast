from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import extract

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def docx_bytes(paragraphs: list[str]) -> bytes:
    body = []
    for text in paragraphs:
        body.append(
            f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
        )
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{WORD_NS}"><w:body>{"".join(body)}</w:body>'
        f"</w:document>"
    )
    return _zip({"word/document.xml": document, "[Content_Types].xml": "<Types/>"})


def pptx_bytes(slides: list[list[str]]) -> bytes:
    members = {
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{PRESENTATION_NS}"/>',
        "[Content_Types].xml": "<Types/>",
    }
    for index, lines in enumerate(slides, 1):
        shapes = "".join(
            f'<a:p><a:r><a:t>{line}</a:t></a:r></a:p>' for line in lines
        )
        members[f"ppt/slides/slide{index}.xml"] = (
            f'<p:sld xmlns:p="{PRESENTATION_NS}" xmlns:a="{DRAWING_NS}">'
            f"{shapes}</p:sld>"
        )
    return _zip(members)


def _zip(members: dict[str, str]) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class ExtractTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="extract-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path


class SniffIdentifiesTheFormat(ExtractTestCase):
    def test_a_pdf_is_recognised_by_its_magic_bytes(self) -> None:
        path = self.write("kaynak", b"%PDF-1.7\nrest")
        self.assertEqual(extract.sniff(path), extract.KIND_PDF)

    def test_a_docx_is_recognised_by_its_body_member(self) -> None:
        path = self.write("kaynak", docx_bytes(["merhaba"]))
        self.assertEqual(extract.sniff(path), extract.KIND_DOCX)

    def test_a_pptx_is_recognised_by_its_slides(self) -> None:
        path = self.write("kaynak", pptx_bytes([["merhaba"]]))
        self.assertEqual(extract.sniff(path), extract.KIND_PPTX)

    def test_a_legacy_ole_document_is_named_not_guessed(self) -> None:
        path = self.write("kaynak", extract.OLE_MAGIC + b"\x00" * 32)
        self.assertEqual(extract.sniff(path), extract.KIND_OLE)
        self.assertIn(extract.KIND_OLE, extract.READABLE_KINDS)

    def test_a_plain_zip_is_not_mistaken_for_a_document(self) -> None:
        path = self.write("kaynak", _zip({"okuma.txt": "merhaba"}))
        self.assertEqual(extract.sniff(path), extract.KIND_ZIP)

    def test_an_unknown_blob_is_reported_as_unknown(self) -> None:
        path = self.write("kaynak", b"\x00\x01\x02\x03rastgele")
        self.assertEqual(extract.sniff(path), extract.KIND_UNKNOWN)

    def test_a_missing_file_raises_a_named_error(self) -> None:
        with self.assertRaises(extract.ExtractError) as raised:
            extract.sniff(self.root / "yok")
        self.assertEqual(raised.exception.code, extract.SOURCE_UNREADABLE)

    def test_the_source_is_never_identified_by_its_name(self) -> None:
        path = self.write("ders.pdf", docx_bytes(["aslinda docx"]))
        self.assertEqual(
            extract.sniff(path),
            extract.KIND_DOCX,
            "tur icerikten belirlenmeli; blob adlari uzantisizdir",
        )


class DocxTextIsRead(ExtractTestCase):
    def test_every_paragraph_becomes_a_block(self) -> None:
        path = self.write("kaynak", docx_bytes(["birinci", "ikinci", "ucuncu"]))
        self.assertEqual(extract.docx_blocks(path), ["birinci", "ikinci", "ucuncu"])

    def test_empty_paragraphs_are_dropped(self) -> None:
        path = self.write("kaynak", docx_bytes(["dolu", "", "   ", "yine dolu"]))
        self.assertEqual(extract.docx_blocks(path), ["dolu", "yine dolu"])

    def test_a_corrupt_archive_raises_a_named_error(self) -> None:
        path = self.write("kaynak", b"PK\x03\x04bozuk")
        with self.assertRaises(extract.ExtractError) as raised:
            extract.docx_blocks(path)
        self.assertEqual(raised.exception.code, extract.SOURCE_UNREADABLE)

    def test_unparseable_xml_raises_a_named_error(self) -> None:
        path = self.write("kaynak", _zip({"word/document.xml": "<w:document"}))
        with self.assertRaises(extract.ExtractError) as raised:
            extract.docx_blocks(path)
        self.assertEqual(raised.exception.code, extract.SOURCE_UNREADABLE)


class PptxTextIsRead(ExtractTestCase):
    def test_every_slide_becomes_one_block(self) -> None:
        path = self.write(
            "kaynak", pptx_bytes([["baslik", "alt"], ["ikinci slayt"]])
        )
        self.assertEqual(
            extract.pptx_slides(path), ["baslik\nalt", "ikinci slayt"]
        )

    def test_slides_keep_their_numeric_order_not_the_zip_order(self) -> None:
        members = {
            "ppt/presentation.xml": f'<p:presentation xmlns:p="{PRESENTATION_NS}"/>',
        }
        for index in (10, 2, 1):
            members[f"ppt/slides/slide{index}.xml"] = (
                f'<p:sld xmlns:p="{PRESENTATION_NS}" xmlns:a="{DRAWING_NS}">'
                f"<a:p><a:r><a:t>slayt{index}</a:t></a:r></a:p></p:sld>"
            )
        path = self.write("kaynak", _zip(members))
        self.assertEqual(
            extract.pptx_slides(path),
            ["slayt1", "slayt2", "slayt10"],
            "slayt10 sayisal olarak slayt2'den sonra gelmeli, alfabetik degil",
        )

    def test_a_slide_without_text_stays_as_an_empty_block(self) -> None:
        path = self.write("kaynak", pptx_bytes([["dolu"], []]))
        self.assertEqual(extract.pptx_slides(path), ["dolu", ""])


class ArchiveLimitsAreEnforced(ExtractTestCase):
    def test_an_oversized_member_is_refused_before_it_is_read(self) -> None:
        big = "a" * (extract.MAX_MEMBER_BYTES + 1)
        path = self.write("kaynak", _zip({"word/document.xml": big}))
        with self.assertRaises(extract.ExtractError) as raised:
            extract.docx_blocks(path)
        self.assertEqual(raised.exception.code, extract.SOURCE_UNREADABLE)
        self.assertIn("uye siniri", str(raised.exception))


class EngineDispatchesByFormat(ExtractTestCase):
    def _settings(self):
        from src import config

        return config.Config(require_token=False)

    def _extract(self, path):
        from src import api_engine

        return api_engine.extract_text(str(path), self._settings(), lambda: None)

    def test_a_docx_reaches_the_engine_as_text(self) -> None:
        body = ["Hucre canliligin temel birimidir." * 8, "Ikinci paragraf." * 8]
        path = self.write("kaynak", docx_bytes(body))
        text, blocks, ocr_pages = self._extract(path)
        self.assertIn("Hucre canliligin temel birimidir", text)
        self.assertEqual(blocks, 2)
        self.assertEqual(ocr_pages, 0)

    def test_a_pptx_reaches_the_engine_as_text(self) -> None:
        slides = [["Hucre canliligin temel birimidir." * 8], ["Ikinci slayt." * 8]]
        path = self.write("kaynak", pptx_bytes(slides))
        text, blocks, ocr_pages = self._extract(path)
        self.assertIn("Ikinci slayt", text)
        self.assertEqual(blocks, 2)
        self.assertEqual(ocr_pages, 0)

    def test_a_legacy_doc_dispatches_to_the_ole_extractor(self) -> None:
        import shutil

        from src import api_engine

        path = self.write("kaynak", extract.OLE_MAGIC + b"\x00" * 64)
        with self.assertRaises(api_engine.EngineError) as raised:
            self._extract(path)
        if shutil.which("antiword") or shutil.which("catppt"):
            self.assertEqual(
                raised.exception.code, api_engine.SOURCE_UNREADABLE
            )
        else:
            self.assertEqual(
                raised.exception.code, api_engine.EXTRACTOR_UNAVAILABLE
            )

    def test_an_unknown_blob_is_refused_with_unsupported_source(self) -> None:
        from src import api_engine

        path = self.write("kaynak", b"\x00rastgele bayt")
        with self.assertRaises(api_engine.EngineError) as raised:
            self._extract(path)
        self.assertEqual(raised.exception.code, api_engine.UNSUPPORTED_SOURCE)

    def test_a_docx_with_no_text_at_all_fails_as_no_text_layer(self) -> None:
        from src import api_engine

        path = self.write("kaynak", docx_bytes(["", "   "]))
        with self.assertRaises(api_engine.EngineError) as raised:
            self._extract(path)
        self.assertEqual(raised.exception.code, api_engine.NO_TEXT)

    def test_a_docx_with_a_little_text_still_extracts(self) -> None:
        from src import api_engine

        path = self.write("kaynak", docx_bytes(["kisa"]))
        text, blocks, ocr_pages = self._extract(path)
        self.assertEqual(text, "kisa")
        self.assertEqual(blocks, 1)
        self.assertEqual(ocr_pages, 0)

    def test_the_error_code_fits_the_backend_limit(self) -> None:
        from src import api_engine

        self.assertLessEqual(
            len(api_engine.UNSUPPORTED_SOURCE),
            64,
            "backend error_code icin 64 karakter siniri koyuyor",
        )


if __name__ == "__main__":
    unittest.main()
