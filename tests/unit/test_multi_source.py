from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import api_engine, config, jobs
from tests.unit.test_extract_formats import docx_bytes, odt_bytes, pptx_bytes

LONG = "Hucre canliligin temel birimidir. " * 8
SCHOOL = "01a0b0a5-ffb5-7682-8726-7e4ef1a5c68d"


class MultiSourceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="coklu-")
        self.root = Path(self._tmp.name)
        self.media = self.root / "medya"
        (self.media / SCHOOL).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        config._active_level = self._log_level

    def source(self, key: str, data: bytes) -> Path:
        path = self.media / SCHOOL / key
        path.write_bytes(data)
        return path

    def settings(self):
        settings = config.Config(require_token=False)
        settings.media_root = str(self.media)
        return settings


class TheRecordCarriesSources(MultiSourceTestCase):
    def make_store(self) -> jobs.JobStore:
        store = jobs.JobStore(
            root=self.root / "isler", workers=1, max_jobs=64, stage_secs=0.0
        )
        self.addCleanup(store.shutdown, 1.0)
        return store

    def test_the_singular_field_stays_the_first_source(self) -> None:
        store = self.make_store()
        sources = [
            {"key": "bir", "name": "Birinci", "content_type": "application/pdf"},
            {"key": "iki", "name": "Ikinci", "content_type": "text/plain"},
        ]
        record, _ = store.submit(
            "aaaaaaaa-aaaa-7aaa-8aaa-000000000001",
            "not-1",
            "duz_okuma",
            source_key="bir",
            sources=sources,
        )
        self.assertEqual(record["source_key"], "bir")
        self.assertEqual(
            record["sources"],
            [
                {
                    "key": "bir",
                    "name": "Birinci",
                    "content_type": "application/pdf",
                    "status": "pending",
                },
                {
                    "key": "iki",
                    "name": "Ikinci",
                    "content_type": "text/plain",
                    "status": "pending",
                },
            ],
        )

    def test_a_legacy_single_key_submit_fills_the_sources_list(self) -> None:
        store = self.make_store()
        record, _ = store.submit(
            "aaaaaaaa-aaaa-7aaa-8aaa-000000000002",
            "not-1",
            "duz_okuma",
            source_key="tek",
        )
        self.assertEqual(
            record["sources"],
            [
                {
                    "key": "tek",
                    "name": "tek",
                    "content_type": "",
                    "status": "pending",
                }
            ],
        )

    def test_sources_is_not_a_required_field(self) -> None:
        self.assertNotIn(
            "sources",
            jobs.REQUIRED_FIELDS,
            "REQUIRED_FIELDS'a eklenirse alan eklenmeden once yazilmis eski "
            "kayitlar acilista topluca atilir",
        )

    def test_an_old_record_without_sources_still_loads(self) -> None:
        store = self.make_store()
        job_id = "aaaaaaaa-aaaa-7aaa-8aaa-000000000003"
        store.submit(job_id, "not-1", "duz_okuma", source_key="eski")
        path = Path(store.root) / f"{job_id}.json"
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("sources", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        reopened = jobs.JobStore(
            root=store.root, workers=1, max_jobs=64, stage_secs=0.0
        )
        self.addCleanup(reopened.shutdown, 1.0)
        loaded = reopened.get(job_id)
        self.assertEqual(loaded["source_key"], "eski")
        context = jobs.JobContext(reopened, job_id)
        self.assertEqual(
            [item["key"] for item in context.sources],
            ["eski"],
            "sources alani olmayan eski kayit tekil alandan turetilmeli",
        )

    def test_sources_survive_a_full_job_lifecycle(self) -> None:
        store = self.make_store()
        store.start()
        job_id = "aaaaaaaa-aaaa-7aaa-8aaa-000000000004"
        store.submit(
            job_id,
            "not-1",
            "duz_okuma",
            school=SCHOOL,
            source_key="tek",
            sources=[
                {"key": "tek", "name": "Tek", "content_type": "text/plain"}
            ],
        )
        deadline = time.monotonic() + 10.0
        record = store.get(job_id)
        while record["state"] not in jobs.TERMINAL_STATES:
            if time.monotonic() > deadline:
                self.fail("sahte is zaman asiminda bitmedi")
            time.sleep(0.02)
            record = store.get(job_id)
        self.assertEqual(
            record["sources"],
            [
                {
                    "key": "tek",
                    "name": "Tek",
                    "content_type": "text/plain",
                    "status": "pending",
                }
            ],
        )

    def test_update_sources_needs_a_running_job(self) -> None:
        store = self.make_store()
        record, _ = store.submit(
            "aaaaaaaa-aaaa-7aaa-8aaa-000000000005",
            "not-1",
            "duz_okuma",
            source_key="tek",
        )
        self.assertIsNone(
            store.update_sources(record["job_id"], record["sources"]),
            "kuyruktaki bir isin kaynak durumu guncellenmemeli",
        )
        with self.assertRaises(jobs.JobNotFound):
            store.update_sources("aaaaaaaa-aaaa-7aaa-8aaa-999999999999", [])


class SourcesAreCombinedIntoOneText(MultiSourceTestCase):
    def documents(self, *pairs: tuple[str, Path]) -> list[dict]:
        return [{"name": name, "path": path} for name, path in pairs]

    def test_three_documents_are_merged_with_per_document_headers(self) -> None:
        docs = self.documents(
            ("Birinci", self.source("a", docx_bytes([LONG + "birinci"]))),
            ("Ikinci", self.source("b", pptx_bytes([[LONG + "ikinci"]]))),
            ("Ucuncu", self.source("c", odt_bytes([LONG + "ucuncu"]))),
        )
        text, blocks, ocr = api_engine.extract_sources(
            docs, self.settings(), lambda: None
        )
        self.assertIn("birinci", text)
        self.assertIn("ikinci", text)
        self.assertIn("ucuncu", text)
        self.assertIn("\n\n=== Ikinci ===\n\n", text)
        self.assertIn("\n\n=== Ucuncu ===\n\n", text)
        self.assertLess(text.index("birinci"), text.index("=== Ikinci ==="))
        self.assertLess(text.index("=== Ikinci ==="), text.index("ikinci"))
        self.assertLess(text.index("ikinci"), text.index("=== Ucuncu ==="))
        self.assertEqual(blocks, 3)
        self.assertEqual(ocr, 0)
        self.assertEqual(
            [item["status"] for item in docs], ["ok", "ok", "ok"]
        )

    def test_the_submitted_order_is_preserved(self) -> None:
        docs = self.documents(
            ("Alfa", self.source("a", docx_bytes([LONG + "ALFA"]))),
            ("Beta", self.source("b", docx_bytes([LONG + "BETA"]))),
        )
        text, _, _ = api_engine.extract_sources(
            docs, self.settings(), lambda: None
        )
        self.assertLess(
            text.index("ALFA"),
            text.index("BETA"),
            "kaynaklar gonderildikleri sirayla birlestirilmeli",
        )

    def test_a_single_source_has_no_header(self) -> None:
        path = self.source("a", docx_bytes([LONG]))
        docs = self.documents(("Tek", path))
        birlesik = api_engine.extract_sources(
            docs, self.settings(), lambda: None
        )
        tekil = api_engine.extract_text(str(path), self.settings(), lambda: None)
        self.assertEqual(birlesik, tekil)
        self.assertNotIn("===", birlesik[0])

    def test_a_corrupt_source_is_skipped_and_the_rest_continue(self) -> None:
        docs = self.documents(
            ("Birinci", self.source("a", docx_bytes([LONG + "birinci"]))),
            ("Bozuk", self.source("b", b"\x00bozuk veri")),
            ("Ucuncu", self.source("c", odt_bytes([LONG + "ucuncu"]))),
        )
        text, _, _ = api_engine.extract_sources(
            docs, self.settings(), lambda: None
        )
        self.assertIn("birinci", text)
        self.assertIn("ucuncu", text)
        self.assertNotIn("bozuk veri", text)
        self.assertEqual(
            [item["status"] for item in docs],
            ["ok", "skipped:unsupported_source", "ok"],
        )

    def test_a_textless_source_is_skipped_as_no_text_layer(self) -> None:
        docs = self.documents(
            ("Birinci", self.source("a", docx_bytes([LONG + "birinci"]))),
            ("Bos", self.source("b", docx_bytes(["", "   "]))),
        )
        text, _, _ = api_engine.extract_sources(
            docs, self.settings(), lambda: None
        )
        self.assertIn("birinci", text)
        self.assertEqual(
            [item["status"] for item in docs],
            ["ok", "skipped:no_text_layer"],
        )

    def test_all_sources_failing_yields_an_empty_merge_with_statuses(self) -> None:
        docs = self.documents(
            ("Birinci", self.source("a", b"\x00bozuk veri")),
            ("Ikinci", self.source("b", docx_bytes(["", ""]))),
        )
        text, blocks, ocr = api_engine.extract_sources(
            docs, self.settings(), lambda: None
        )
        self.assertEqual(text, "")
        self.assertEqual(blocks, 0)
        self.assertEqual(ocr, 0)
        self.assertEqual(
            [item["status"] for item in docs],
            ["skipped:unsupported_source", "skipped:no_text_layer"],
        )

    def test_no_source_at_all_is_refused(self) -> None:
        with self.assertRaises(api_engine.EngineError) as raised:
            api_engine.extract_sources([], self.settings(), lambda: None)
        self.assertEqual(raised.exception.code, api_engine.SOURCE_NOT_FOUND)

    def test_cancellation_is_checked_between_sources(self) -> None:
        docs = self.documents(
            ("Birinci", self.source("a", docx_bytes([LONG]))),
            ("Ikinci", self.source("b", docx_bytes([LONG]))),
        )
        cagrilar = []

        def check():
            cagrilar.append(1)
            if len(cagrilar) > 1:
                raise jobs.JobCancelled("iptal")

        with self.assertRaises(jobs.JobCancelled):
            api_engine.extract_sources(docs, self.settings(), check)


if __name__ == "__main__":
    unittest.main()
