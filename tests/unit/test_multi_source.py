from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import api_engine, capabilities, config, jobs
from tests.unit.test_extract_formats import docx_bytes, odt_bytes, pptx_bytes

LONG = "Hucre canliligin temel birimidir. " * 8


class MultiSourceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="coklu-")
        self.root = Path(self._tmp.name)
        self.media = self.root / "medya"
        self.school = "okul-a"
        (self.media / self.school).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        config._active_level = self._log_level

    def source(self, key: str, data: bytes) -> str:
        (self.media / self.school / key).write_bytes(data)
        return key

    def settings(self):
        settings = config.Config(require_token=False)
        settings.media_root = str(self.media)
        return settings


class SourceKeysAreValidated(unittest.TestCase):
    def _payload(self, **extra):
        payload = {
            "job_id": "aaaaaaaa-aaaa-7aaa-8aaa-000000000001",
            "source_id": "not-1",
            "source_key": "a",
            "user_id": "kullanici-1",
        }
        payload.update(extra)
        return payload

    def test_a_missing_list_falls_back_to_the_single_key(self) -> None:
        self.assertEqual(
            capabilities._source_keys(self._payload(), "a"), ["a"]
        )

    def test_a_non_list_is_a_bad_request(self) -> None:
        with self.assertRaises(capabilities.CapabilityError) as raised:
            capabilities._source_keys(self._payload(source_keys="a"), "a")
        self.assertEqual(raised.exception.code, "bad_request")

    def test_an_empty_list_is_a_bad_request(self) -> None:
        with self.assertRaises(capabilities.CapabilityError):
            capabilities._source_keys(self._payload(source_keys=[]), "a")

    def test_a_blank_member_is_a_bad_request(self) -> None:
        with self.assertRaises(capabilities.CapabilityError):
            capabilities._source_keys(self._payload(source_keys=["a", "  "]), "a")

    def test_a_repeated_source_is_a_bad_request(self) -> None:
        with self.assertRaises(capabilities.CapabilityError) as raised:
            capabilities._source_keys(self._payload(source_keys=["a", "b", "a"]), "a")
        self.assertIn("tekrar", str(raised.exception))

    def test_the_first_key_must_match_the_singular_field(self) -> None:
        with self.assertRaises(capabilities.CapabilityError) as raised:
            capabilities._source_keys(self._payload(source_keys=["b", "a"]), "a")
        self.assertIn("ilk ogesi", str(raised.exception))

    def test_more_than_the_ceiling_is_refused(self) -> None:
        keys = ["a"] + [f"k{index}" for index in range(capabilities.MAX_SOURCES)]
        with self.assertRaises(capabilities.CapabilityError) as raised:
            capabilities._source_keys(self._payload(source_keys=keys), "a")
        self.assertEqual(raised.exception.code, "bad_request")

    def test_the_ceiling_itself_is_accepted(self) -> None:
        keys = ["a"] + [f"k{index}" for index in range(capabilities.MAX_SOURCES - 1)]
        self.assertEqual(
            len(capabilities._source_keys(self._payload(source_keys=keys), "a")),
            capabilities.MAX_SOURCES,
        )


class TheRecordKeepsBothShapes(MultiSourceTestCase):
    def make_store(self) -> jobs.JobStore:
        store = jobs.JobStore(root=self.root / "isler", workers=1, max_jobs=64,
                              stage_secs=0.0)
        self.addCleanup(store.shutdown, 1.0)
        return store

    def test_the_singular_field_stays_the_first_source(self) -> None:
        store = self.make_store()
        record, _ = store.submit(
            "aaaaaaaa-aaaa-7aaa-8aaa-000000000001", "not-1", "duz_okuma",
            source_key="bir", source_keys=["bir", "iki", "uc"],
        )
        self.assertEqual(record["source_key"], "bir")
        self.assertEqual(record["source_keys"], ["bir", "iki", "uc"])

    def test_a_single_source_job_still_fills_the_plural_field(self) -> None:
        store = self.make_store()
        record, _ = store.submit(
            "aaaaaaaa-aaaa-7aaa-8aaa-000000000002", "not-1", "duz_okuma",
            source_key="tek",
        )
        self.assertEqual(record["source_keys"], ["tek"])

    def test_source_keys_is_not_a_required_field(self) -> None:
        self.assertNotIn(
            "source_keys",
            jobs.REQUIRED_FIELDS,
            "REQUIRED_FIELDS'a eklenirse alan eklenmeden once yazilmis eski "
            "kayitlar acilista topluca atilir",
        )

    def test_an_old_record_without_the_plural_field_still_loads(self) -> None:
        store = self.make_store()
        job_id = "aaaaaaaa-aaaa-7aaa-8aaa-000000000003"
        record, _ = store.submit(job_id, "not-1", "duz_okuma", source_key="eski")
        path = Path(store.root) / f"{job_id}.json"
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("source_keys", None)
        path.write_text(json.dumps(data), encoding="utf-8")
        reopened = jobs.JobStore(root=store.root, workers=1, max_jobs=64,
                                 stage_secs=0.0)
        self.addCleanup(reopened.shutdown, 1.0)
        loaded = reopened.get(job_id)
        self.assertEqual(loaded["source_key"], "eski")
        context = jobs.JobContext(reopened, job_id)
        self.assertEqual(
            context.source_keys,
            ["eski"],
            "cogul alani olmayan eski kayit tekil alandan turetilmeli",
        )


class SourcesAreCombinedIntoOneText(MultiSourceTestCase):
    def test_three_documents_of_different_formats_become_one_text(self) -> None:
        paths = [
            self.media / self.school / self.source("a", docx_bytes([LONG + "birinci"])),
            self.media / self.school / self.source("b", pptx_bytes([[LONG + "ikinci"]])),
            self.media / self.school / self.source("c", odt_bytes([LONG + "ucuncu"])),
        ]
        text, blocks, ocr = api_engine.extract_sources(
            paths, self.settings(), lambda: None
        )
        self.assertIn("birinci", text)
        self.assertIn("ikinci", text)
        self.assertIn("ucuncu", text)
        self.assertEqual(blocks, 3)
        self.assertEqual(ocr, 0)

    def test_the_submitted_order_is_preserved(self) -> None:
        paths = [
            self.media / self.school / self.source("a", docx_bytes([LONG + "ALFA"])),
            self.media / self.school / self.source("b", docx_bytes([LONG + "BETA"])),
        ]
        text, _, _ = api_engine.extract_sources(paths, self.settings(), lambda: None)
        self.assertLess(
            text.index("ALFA"), text.index("BETA"),
            "kaynaklar gonderildikleri sirayla birlestirilmeli",
        )

    def test_a_single_source_behaves_exactly_as_before(self) -> None:
        path = self.media / self.school / self.source("a", docx_bytes([LONG]))
        birlesik = api_engine.extract_sources([path], self.settings(), lambda: None)
        tekil = api_engine.extract_text(str(path), self.settings(), lambda: None)
        self.assertEqual(birlesik, tekil)

    def test_an_unreadable_source_fails_the_job_and_names_which_one(self) -> None:
        paths = [
            self.media / self.school / self.source("a", docx_bytes([LONG])),
            self.media / self.school / self.source("b", b"\x00bozuk veri"),
        ]
        with self.assertRaises(api_engine.EngineError) as raised:
            api_engine.extract_sources(paths, self.settings(), lambda: None)
        self.assertEqual(raised.exception.code, api_engine.UNSUPPORTED_SOURCE)
        self.assertIn("2. kaynak", str(raised.exception))

    def test_no_source_at_all_is_refused(self) -> None:
        with self.assertRaises(api_engine.EngineError) as raised:
            api_engine.extract_sources([], self.settings(), lambda: None)
        self.assertEqual(raised.exception.code, api_engine.SOURCE_NOT_FOUND)

    def test_cancellation_is_checked_between_sources(self) -> None:
        paths = [
            self.media / self.school / self.source("a", docx_bytes([LONG])),
            self.media / self.school / self.source("b", docx_bytes([LONG])),
        ]
        cagrilar = []

        def check():
            cagrilar.append(1)
            if len(cagrilar) > 1:
                raise jobs.JobCancelled("iptal")

        with self.assertRaises(jobs.JobCancelled):
            api_engine.extract_sources(paths, self.settings(), check)


if __name__ == "__main__":
    unittest.main()
