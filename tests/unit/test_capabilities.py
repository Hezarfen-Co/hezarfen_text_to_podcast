import tempfile
import unittest
from pathlib import Path

from src import capabilities, config, jobs
from src.protocol import CapabilityError


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
            capabilities.dispatch(capability, payload)
        return raised.exception.code


class RegistryTests(CapabilityTestCase):
    def test_registry_holds_exactly_the_four_wire_capabilities(self) -> None:
        self.assertEqual(
            capabilities.names(),
            ["podcast.cancel", "podcast.result", "podcast.status", "podcast.submit"],
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
            capabilities.dispatch("podcast.submit", None)
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
        response = capabilities.dispatch("podcast.submit", {"source_id": "ders.pdf"})
        self.assertEqual(response["state"], "queued")
        self.assertEqual(len(response["job_id"]), jobs.JOB_ID_LENGTH)
        self.assertIsInstance(response["eta_secs"], int)
        self.assertGreaterEqual(response["eta_secs"], 1)

    def test_missing_or_blank_source_id_is_bad_request(self) -> None:
        for payload in ({}, {"source_id": ""}, {"source_id": "   "}, {"source_id": 7}):
            with self.subTest(payload=payload):
                self.assertEqual(self.error_code("podcast.submit", payload), "bad_request")

    def test_unknown_format_is_bad_request(self) -> None:
        self.assertEqual(
            self.error_code("podcast.submit", {"source_id": "x", "format": "opera"}), "bad_request"
        )

    def test_non_text_format_is_bad_request(self) -> None:
        self.assertEqual(
            self.error_code("podcast.submit", {"source_id": "x", "format": 7}), "bad_request"
        )

    def test_llm_formats_are_llm_unavailable_without_the_key(self) -> None:
        for name in capabilities.LLM_FORMATS:
            with self.subTest(format=name):
                self.assertEqual(
                    self.error_code("podcast.submit", {"source_id": "x", "format": name}),
                    "llm_unavailable",
                )

    def test_llm_unavailable_message_names_the_usable_formats(self) -> None:
        with self.assertRaises(CapabilityError) as raised:
            capabilities.dispatch(
                "podcast.submit", {"source_id": "x", "format": "tek_ogretici"}
            )
        self.assertIn(capabilities.DEFAULT_FORMAT, str(raised.exception))

    def test_llm_formats_are_accepted_once_the_key_is_ready(self) -> None:
        capabilities.configure(self.store, llm_ready=True)
        response = capabilities.dispatch(
            "podcast.submit", {"source_id": "x", "format": "ogrenci_hoca"}
        )
        self.assertEqual(response["state"], "queued")

    def test_full_queue_answers_busy_not_internal(self) -> None:
        small = jobs.JobStore(root=self.root / "dar", workers=1, max_jobs=1, stage_secs=0.0)
        capabilities.configure(small, llm_ready=False)
        try:
            capabilities.dispatch("podcast.submit", {"source_id": "ilk"})
            self.assertEqual(self.error_code("podcast.submit", {"source_id": "ikinci"}), "busy")
        finally:
            small.shutdown(timeout=1.0)

    def test_missing_store_answers_internal(self) -> None:
        capabilities.configure(None, llm_ready=False)
        self.assertEqual(self.error_code("podcast.submit", {"source_id": "x"}), "internal")


class StatusResultCancelTests(CapabilityTestCase):
    def test_status_reports_queued_job_without_error_code(self) -> None:
        job_id = capabilities.dispatch("podcast.submit", {"source_id": "x"})["job_id"]
        response = capabilities.dispatch("podcast.status", {"job_id": job_id})
        self.assertEqual(response["state"], "queued")
        self.assertEqual(response["progress"], 0.0)
        self.assertIn("error_code", response)
        self.assertIsNone(response["error_code"])

    def test_result_before_completion_is_not_ready(self) -> None:
        job_id = capabilities.dispatch("podcast.submit", {"source_id": "x"})["job_id"]
        self.assertEqual(self.error_code("podcast.result", {"job_id": job_id}), "not_ready")

    def test_unknown_job_is_not_found_for_every_lookup_capability(self) -> None:
        for name in ("podcast.status", "podcast.result", "podcast.cancel"):
            with self.subTest(capability=name):
                self.assertEqual(self.error_code(name, {"job_id": "YOKBOYLEBIRIS"}), "not_found")

    def test_blank_job_id_is_bad_request_for_every_lookup_capability(self) -> None:
        for name in ("podcast.status", "podcast.result", "podcast.cancel"):
            for payload in ({}, {"job_id": ""}, {"job_id": 7}):
                with self.subTest(capability=name, payload=payload):
                    self.assertEqual(self.error_code(name, payload), "bad_request")

    def test_cancel_of_queued_job_reports_true_then_false(self) -> None:
        job_id = capabilities.dispatch("podcast.submit", {"source_id": "x"})["job_id"]
        self.assertTrue(capabilities.dispatch("podcast.cancel", {"job_id": job_id})["cancelled"])
        self.assertEqual(
            capabilities.dispatch("podcast.status", {"job_id": job_id})["state"], "cancelled"
        )
        self.assertFalse(
            capabilities.dispatch("podcast.cancel", {"job_id": job_id})["cancelled"],
            "bitmis is icin ikinci iptal False donmeli",
        )

    def test_result_payload_carries_both_singular_and_plural_identifiers(self) -> None:
        job_id = capabilities.dispatch("podcast.submit", {"source_id": "x"})["job_id"]
        self.store.transition(job_id, jobs.STATE_RUNNING)
        self.store.finish(
            job_id,
            audio_id="a/b.mp3",
            duration_secs=12.5,
            script_id="a/b.json",
            audio_ids=["a/b.mp3", "a/c.mp3"],
            script_ids=["a/b.json", "a/c.json"],
        )
        response = capabilities.dispatch("podcast.result", {"job_id": job_id})
        self.assertEqual(response["audio_id"], "a/b.mp3")
        self.assertEqual(response["script_id"], "a/b.json")
        self.assertEqual(response["duration_secs"], 12.5)
        self.assertEqual(response["audio_ids"], ["a/b.mp3", "a/c.mp3"])
        self.assertEqual(response["script_ids"], ["a/b.json", "a/c.json"])
        self.assertEqual(response["format"], "duz_okuma")


if __name__ == "__main__":
    unittest.main()
