import tempfile
import unittest
from pathlib import Path

from src import capabilities, config, jobs
from src.protocol import CapabilityError

JOB_ONE = "11111111-1111-7111-8111-111111111111"
JOB_TWO = "22222222-2222-7222-8222-222222222222"
USER_ID = "kullanici-1"


class CapabilityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-yetenek-")
        self.root = Path(self._tmp.name)
        self.store = jobs.JobStore(
            root=self.root / "isler", workers=1, max_jobs=8, stage_secs=0.0
        )
        capabilities.configure(self.store, llm_ready=False)

    def tearDown(self) -> None:
        capabilities.configure(None, llm_ready=False)
        self.store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def error_code(self, capability: str, payload: dict) -> str:
        with self.assertRaises(CapabilityError) as raised:
            capabilities.dispatch(capability, "okul-a", payload)
        return raised.exception.code

    def submit(self, job_id: str, source_id: str, **extra: object) -> dict:
        payload = {"job_id": job_id, "source_id": source_id, "user_id": USER_ID}
        payload.update(extra)
        return capabilities.dispatch("podcast.submit", "okul-a", payload)


class RegistryTests(CapabilityTestCase):
    def test_registry_holds_exactly_the_submit_and_cancel_capabilities(self) -> None:
        self.assertEqual(
            capabilities.names(),
            ["podcast.cancel", "podcast.submit"],
        )

    def test_deleted_lookup_capabilities_are_unsupported_capability(self) -> None:
        for name in ("podcast.status", "podcast.result"):
            with self.subTest(capability=name):
                self.assertEqual(
                    self.error_code(name, {"job_id": JOB_ONE}), "unsupported_capability"
                )

    def test_every_handler_is_callable(self) -> None:
        for name, handler in capabilities.REGISTRY.items():
            with self.subTest(capability=name):
                self.assertTrue(callable(handler), f"{name} isleyicisi cagrilabilir degil")

    def test_unknown_capability_is_unsupported_capability(self) -> None:
        self.assertEqual(self.error_code("podcast.bilinmeyen", {}), "unsupported_capability")

    def test_non_object_payload_is_bad_request(self) -> None:
        for payload in ([], "metin", 7, True):
            with self.subTest(payload=payload):
                self.assertEqual(self.error_code("podcast.submit", payload), "bad_request")

    def test_none_payload_is_treated_as_empty_object(self) -> None:
        with self.assertRaises(CapabilityError) as raised:
            capabilities.dispatch("podcast.submit", "okul-a", None)
        self.assertEqual(raised.exception.code, "bad_request")


class FormatDictionaryTests(CapabilityTestCase):
    def test_format_set_and_default_match_the_locked_contract(self) -> None:
        self.assertEqual(
            capabilities.FORMATS, ("tek_ogretici", "ogrenci_hoca", "duz_okuma")
        )
        self.assertEqual(capabilities.DEFAULT_FORMAT, "duz_okuma")
        self.assertEqual(capabilities.LLM_FORMATS, ("tek_ogretici", "ogrenci_hoca"))
        self.assertEqual(capabilities.KEYLESS_FORMATS, ("duz_okuma",))

    def test_default_format_never_needs_an_llm_key(self) -> None:
        self.assertIn(
            capabilities.DEFAULT_FORMAT,
            capabilities.KEYLESS_FORMATS,
            "varsayilan format anahtarsiz kosabilmeli",
        )

    def test_allowed_formats_shrink_to_keyless_set_without_key(self) -> None:
        self.assertEqual(capabilities.allowed_formats(False), capabilities.KEYLESS_FORMATS)
        self.assertEqual(capabilities.allowed_formats(True), capabilities.FORMATS)

    def test_format_summary_names_the_key_variable_in_both_states(self) -> None:
        for llm_ready in (False, True):
            with self.subTest(llm_ready=llm_ready):
                self.assertIn(
                    capabilities.LLM_KEY_NAME, capabilities.format_summary(llm_ready)
                )

    def test_keyless_summary_lists_the_disabled_formats(self) -> None:
        summary = capabilities.format_summary(False)
        for name in capabilities.LLM_FORMATS:
            with self.subTest(format=name):
                self.assertIn(name, summary, "kapali formatlar ozette adiyla anilmali")


class SubmitTests(CapabilityTestCase):
    def test_keyless_submit_returns_queued_job_with_positive_eta(self) -> None:
        response = self.submit(JOB_ONE, "ders.pdf")
        self.assertEqual(response["state"], "queued")
        self.assertEqual(response["job_id"], JOB_ONE)
        self.assertIsInstance(response["eta_secs"], int)
        self.assertGreaterEqual(response["eta_secs"], 1)

    def test_submitted_record_carries_the_caller_school_and_user(self) -> None:
        self.submit(JOB_ONE, "ders.pdf")
        record = self.store.get(JOB_ONE)
        self.assertEqual(record["school"], "okul-a")
        self.assertEqual(record["user_id"], USER_ID)

    def test_missing_or_blank_source_id_is_bad_request(self) -> None:
        for payload in (
            {"job_id": JOB_ONE, "user_id": USER_ID},
            {"job_id": JOB_ONE, "user_id": USER_ID, "source_id": ""},
            {"job_id": JOB_ONE, "user_id": USER_ID, "source_id": "   "},
            {"job_id": JOB_ONE, "user_id": USER_ID, "source_id": 7},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.error_code("podcast.submit", payload), "bad_request")

    def test_missing_job_id_or_user_id_is_bad_request(self) -> None:
        for payload in (
            {"source_id": "x", "user_id": USER_ID},
            {"job_id": JOB_ONE, "source_id": "x"},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.error_code("podcast.submit", payload), "bad_request")

    def test_duplicate_job_id_is_a_conflict(self) -> None:
        self.assertEqual(self.submit(JOB_ONE, "x")["job_id"], JOB_ONE)
        self.assertEqual(self.error_code("podcast.submit", {
            "job_id": JOB_ONE,
            "source_id": "x",
            "user_id": USER_ID,
        }), "conflict")

    def test_malformed_job_id_is_bad_request(self) -> None:
        self.assertEqual(
            self.error_code("podcast.submit", {
                "job_id": "kotus id",
                "source_id": "x",
                "user_id": USER_ID,
            }),
            "bad_request",
        )

    def test_unknown_format_is_bad_request(self) -> None:
        self.assertEqual(
            self.error_code("podcast.submit", {
                "job_id": JOB_ONE,
                "source_id": "x",
                "user_id": USER_ID,
                "format": "opera",
            }),
            "bad_request",
        )

    def test_non_text_format_is_bad_request(self) -> None:
        self.assertEqual(
            self.error_code("podcast.submit", {
                "job_id": JOB_ONE,
                "source_id": "x",
                "user_id": USER_ID,
                "format": 7,
            }),
            "bad_request",
        )

    def test_llm_formats_are_llm_unavailable_without_the_key(self) -> None:
        for name in capabilities.LLM_FORMATS:
            with self.subTest(format=name):
                self.assertEqual(
                    self.error_code("podcast.submit", {
                        "job_id": JOB_ONE,
                        "source_id": "x",
                        "user_id": USER_ID,
                        "format": name,
                    }),
                    "llm_unavailable",
                )

    def test_llm_unavailable_message_names_the_usable_formats(self) -> None:
        with self.assertRaises(CapabilityError) as raised:
            capabilities.dispatch("podcast.submit", "okul-a", {
                "job_id": JOB_ONE,
                "source_id": "x",
                "user_id": USER_ID,
                "format": "tek_ogretici",
            })
        self.assertIn(capabilities.DEFAULT_FORMAT, str(raised.exception))

    def test_llm_formats_are_accepted_once_the_key_is_ready(self) -> None:
        capabilities.configure(self.store, llm_ready=True)
        response = self.submit(JOB_ONE, "x", format="ogrenci_hoca")
        self.assertEqual(response["state"], "queued")

    def test_full_queue_answers_busy_not_internal(self) -> None:
        small = jobs.JobStore(root=self.root / "dar", workers=1, max_jobs=1, stage_secs=0.0)
        capabilities.configure(small, llm_ready=False)
        try:
            self.submit(JOB_ONE, "ilk")
            self.assertEqual(self.error_code("podcast.submit", {
                "job_id": JOB_TWO,
                "source_id": "ikinci",
                "user_id": USER_ID,
            }), "busy")
        finally:
            small.shutdown(timeout=1.0)

    def test_missing_store_answers_internal(self) -> None:
        capabilities.configure(None, llm_ready=False)
        self.assertEqual(self.error_code("podcast.submit", {
            "job_id": JOB_ONE,
            "source_id": "x",
            "user_id": USER_ID,
        }), "internal")


class StatusResultCancelTests(CapabilityTestCase):
    def test_submitted_job_reports_queued_without_error_code(self) -> None:
        self.submit(JOB_ONE, "x")
        record = self.store.get(JOB_ONE)
        self.assertEqual(record["state"], "queued")
        self.assertEqual(record["progress"], 0.0)
        self.assertIn("error_code", record)
        self.assertIsNone(record["error_code"])

    def test_result_identifiers_are_absent_before_completion(self) -> None:
        self.submit(JOB_ONE, "x")
        record = self.store.get(JOB_ONE)
        self.assertIsNone(record["audio_id"])
        self.assertIsNone(record["script_id"])
        self.assertIsNone(record["duration_secs"])

    def test_unknown_job_is_not_found_for_cancel_and_the_store(self) -> None:
        self.assertEqual(
            self.error_code("podcast.cancel", {"job_id": "YOKBOYLEBIRIS"}), "not_found"
        )
        with self.assertRaises(jobs.JobNotFound):
            self.store.get("YOKBOYLEBIRIS")

    def test_blank_job_id_is_bad_request_for_cancel(self) -> None:
        for payload in ({}, {"job_id": ""}, {"job_id": 7}):
            with self.subTest(payload=payload):
                self.assertEqual(self.error_code("podcast.cancel", payload), "bad_request")

    def test_cancel_of_queued_job_reports_true_then_false(self) -> None:
        self.submit(JOB_ONE, "x")
        self.assertTrue(
            capabilities.dispatch("podcast.cancel", "okul-a", {"job_id": JOB_ONE})["cancelled"]
        )
        self.assertEqual(self.store.get(JOB_ONE)["state"], "cancelled")
        self.assertFalse(
            capabilities.dispatch("podcast.cancel", "okul-a", {"job_id": JOB_ONE})["cancelled"],
            "bitmis is icin ikinci iptal False donmeli",
        )

    def test_result_record_carries_both_singular_and_plural_identifiers(self) -> None:
        self.submit(JOB_ONE, "x")
        self.store.transition(JOB_ONE, jobs.STATE_RUNNING)
        self.store.finish(
            JOB_ONE,
            audio_id="a/b.mp3",
            duration_secs=12.5,
            script_id="a/b.json",
            audio_ids=["a/b.mp3", "a/c.mp3"],
            script_ids=["a/b.json", "a/c.json"],
        )
        record = self.store.get(JOB_ONE)
        self.assertEqual(record["audio_id"], "a/b.mp3")
        self.assertEqual(record["script_id"], "a/b.json")
        self.assertEqual(record["duration_secs"], 12.5)
        self.assertEqual(record["audio_ids"], ["a/b.mp3", "a/c.mp3"])
        self.assertEqual(record["script_ids"], ["a/b.json", "a/c.json"])
        self.assertEqual(record["format"], "duz_okuma")


if __name__ == "__main__":
    unittest.main()
