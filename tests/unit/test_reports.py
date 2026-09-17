import os
import tempfile
import time
import unittest
from pathlib import Path

from src import backend, capabilities, config, jobs, protocol

JOB_A = "11111111-1111-7111-8111-111111111111"
JOB_B = "22222222-2222-7222-8222-222222222222"
JOB_C = "33333333-3333-7333-8333-333333333333"
JOB_D = "44444444-4444-7444-8444-444444444444"
SCHOOL = "okul-1"
USER = "kullanici-1"


class FakeReporter:
    def __init__(self, refuse_state: str = "", refuse_code: str = "expired", unavailable: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.refuse_state = refuse_state
        self.refuse_code = refuse_code
        self.unavailable = unavailable

    def report(self, school: str, record: dict) -> dict:
        self.calls.append(("report", dict(record)))
        if self.unavailable:
            raise backend.BackendUnavailable("prova")
        if self.refuse_state and record["state"] == self.refuse_state:
            raise backend.BackendRefused(self.refuse_code, "prova reddi")
        return {"job_id": record["job_id"], "stored": True}

    def upload(self, school: str, record: dict, path, content_type: str = "audio/mpeg") -> dict:
        self.calls.append(("upload", dict(record)))
        if self.unavailable:
            raise backend.BackendUnavailable("prova")
        return {"key": f"podcast/{path.name}", "size": path.stat().st_size}

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def states(self) -> list[str]:
        return [record["state"] for kind, record in self.calls if kind == "report"]


class ReportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-rapor-")
        self.root = Path(self._tmp.name)
        self.output_root = self.root / "cikti"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.audio = self.output_root / "ders.mp3"
        self.audio.write_bytes(b"ID3prova-ses")

    def tearDown(self) -> None:
        self._tmp.cleanup()
        config._active_level = self._log_level

    def runner_factory(self, audio_id: str = "ders.mp3"):
        def runner(ctx) -> None:
            ctx.check()
            ctx.progress("kaynak", 0.0)
            ctx.check()
            ctx.progress("metin", 0.5)
            ctx.check()
            ctx.finish(audio_id=audio_id, duration_secs=2.0, script_id="ders.script.json")

        return runner

    def build_store(self, reporter, name: str, audio_id: str = "ders.mp3", start: bool = True):
        store = jobs.JobStore(
            root=self.root / name,
            workers=1,
            stage_secs=0.0,
            runner=self.runner_factory(audio_id),
            stages=("kaynak", "metin"),
            job_secs=1.0,
            output_root=str(self.output_root),
            reporter=reporter,
        )
        if start:
            store.start()
        return store

    def await_state(self, store, job_id: str, wanted: tuple, deadline: float = 10.0) -> dict:
        limit = time.monotonic() + deadline
        record = store.get(job_id)
        while record["state"] not in wanted and time.monotonic() < limit:
            time.sleep(0.01)
            record = store.get(job_id)
        return record


class TransitionReportTests(ReportTestCase):
    def test_every_transition_is_reported_in_order_with_the_full_payload(self) -> None:
        reporter = FakeReporter()
        store = self.build_store(reporter, "sira")
        try:
            store.submit(JOB_A, "ders.pdf", "duz_okuma", user_id=USER, school=SCHOOL)
            final = self.await_state(store, JOB_A, (jobs.STATE_DONE, jobs.STATE_FAILED))
        finally:
            store.shutdown()
        self.assertEqual(final["state"], jobs.STATE_DONE)
        self.assertEqual(reporter.kinds()[0], "report")
        self.assertEqual(reporter.states()[0], jobs.STATE_QUEUED)
        self.assertIn(jobs.STATE_RUNNING, reporter.states())
        self.assertEqual(reporter.states()[-1], jobs.STATE_DONE)
        self.assertEqual(
            reporter.states(),
            sorted(reporter.states(), key=[jobs.STATE_QUEUED, jobs.STATE_RUNNING, jobs.STATE_DONE].index),
        )
        first = reporter.calls[0][1]
        self.assertEqual(
            {key: first[key] for key in ("job_id", "source_id", "format", "user_id", "state", "stage", "progress", "error_code")},
            {
                "job_id": JOB_A,
                "source_id": "ders.pdf",
                "format": "duz_okuma",
                "user_id": USER,
                "state": jobs.STATE_QUEUED,
                "stage": "",
                "progress": 0.0,
                "error_code": None,
            },
        )
        self.assertEqual(store.flush_reports(), 0)

    def test_stage_changes_are_reported_without_failing_progress_only_writes(self) -> None:
        reporter = FakeReporter()
        store = self.build_store(reporter, "asama")
        try:
            store.submit(JOB_B, "ders.pdf", "duz_okuma", user_id=USER, school=SCHOOL)
            record = self.await_state(store, JOB_B, (jobs.STATE_DONE, jobs.STATE_FAILED))
        finally:
            store.shutdown()
        self.assertEqual(record["state"], jobs.STATE_DONE)
        stages = [call[1]["stage"] for call in reporter.calls if call[0] == "report"]
        self.assertIn("metin", stages)


class UploadOrderTests(ReportTestCase):
    def test_the_audio_upload_lands_before_the_done_report(self) -> None:
        reporter = FakeReporter()
        store = self.build_store(reporter, "yukleme")
        try:
            store.submit(JOB_A, "ders.pdf", "duz_okuma", user_id=USER, school=SCHOOL)
            record = self.await_state(store, JOB_A, (jobs.STATE_DONE, jobs.STATE_FAILED))
        finally:
            store.shutdown()
        self.assertEqual(record["state"], jobs.STATE_DONE)
        kinds = reporter.kinds()
        self.assertEqual(kinds.count("upload"), 1)
        self.assertEqual(kinds[-2], "upload")
        self.assertEqual(kinds[-1], "report")
        self.assertEqual(reporter.states()[-1], jobs.STATE_DONE)
        upload = [call[1] for call in reporter.calls if call[0] == "upload"][0]
        self.assertEqual(upload["duration_secs"], 2.0)
        self.assertEqual(upload["job_id"], JOB_A)

    def test_a_job_without_its_audio_is_failed_instead_of_reported_done(self) -> None:
        reporter = FakeReporter()
        store = self.build_store(reporter, "sessiz", audio_id="yok.mp3")
        try:
            store.submit(JOB_C, "ders.pdf", "duz_okuma", user_id=USER, school=SCHOOL)
            record = self.await_state(store, JOB_C, (jobs.STATE_DONE, jobs.STATE_FAILED))
        finally:
            store.shutdown()
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "audio_missing")
        self.assertIsNone(record["audio_id"])
        self.assertNotIn("upload", reporter.kinds())
        self.assertNotIn(jobs.STATE_DONE, reporter.states())


class RefusalTests(ReportTestCase):
    def test_a_refused_report_fails_the_job_with_that_code(self) -> None:
        reporter = FakeReporter(refuse_state=jobs.STATE_RUNNING, refuse_code="not_permitted")
        store = self.build_store(reporter, "ret")
        try:
            store.submit(JOB_A, "ders.pdf", "duz_okuma", user_id=USER, school=SCHOOL)
            record = self.await_state(store, JOB_A, (jobs.STATE_FAILED, jobs.STATE_DONE))
        finally:
            store.shutdown()
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "not_permitted")
        self.assertEqual(reporter.states()[-1], jobs.STATE_FAILED)

    def test_a_refused_done_report_keeps_the_job_out_of_done(self) -> None:
        reporter = FakeReporter(refuse_state=jobs.STATE_DONE, refuse_code="expired")
        store = self.build_store(reporter, "ret-done")
        try:
            store.submit(JOB_B, "ders.pdf", "duz_okuma", user_id=USER, school=SCHOOL)
            record = self.await_state(store, JOB_B, (jobs.STATE_FAILED, jobs.STATE_DONE))
        finally:
            store.shutdown()
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "expired")
        self.assertIsNone(record["audio_id"])
        self.assertEqual(store.flush_reports(), 0)

    def test_a_refused_upload_fails_the_job_with_the_upload_code(self) -> None:
        class UploadRefused(FakeReporter):
            def upload(self, school, record, path, content_type: str = "audio/mpeg") -> dict:
                self.calls.append(("upload", dict(record)))
                raise backend.BackendRefused("audio_missing", "prova")

        reporter = UploadRefused()
        store = self.build_store(reporter, "ret-yukleme")
        try:
            store.submit(JOB_C, "ders.pdf", "duz_okuma", user_id=USER, school=SCHOOL)
            record = self.await_state(store, JOB_C, (jobs.STATE_FAILED, jobs.STATE_DONE))
        finally:
            store.shutdown()
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "audio_missing")
        self.assertNotIn(jobs.STATE_DONE, reporter.states())


class TransportTests(ReportTestCase):
    def test_without_a_connection_reports_wait_instead_of_failing_the_job(self) -> None:
        reporter = FakeReporter(unavailable=True)
        store = self.build_store(reporter, "gecici", start=False)
        try:
            store.submit(JOB_D, "ders.pdf", "duz_okuma", user_id=USER, school=SCHOOL)
            pending = store.flush_reports()
            self.assertGreaterEqual(pending, 1)
            self.assertEqual(store.get(JOB_D)["state"], jobs.STATE_QUEUED)
            healthy = FakeReporter()
            store.reporter = healthy
            self.assertEqual(store.flush_reports(), pending)
            self.assertEqual(healthy.states(), [jobs.STATE_QUEUED])
        finally:
            store.shutdown()

    def test_a_job_that_never_had_a_backend_is_never_reported(self) -> None:
        store = self.build_store(None, "yerel")
        try:
            store.submit(JOB_A, "ders.pdf", "duz_okuma", user_id=USER, school="")
            record = self.await_state(store, JOB_A, (jobs.STATE_DONE, jobs.STATE_FAILED))
        finally:
            store.shutdown()
        self.assertEqual(record["state"], jobs.STATE_DONE)


class ContractTests(unittest.TestCase):
    def test_status_and_result_are_not_served_any_more(self) -> None:
        self.assertEqual(capabilities.names(), ["podcast.cancel", "podcast.submit"])
        for removed in ("podcast.status", "podcast.result"):
            self.assertNotIn(removed, capabilities.REGISTRY)
            with self.assertRaises(Exception) as raised:
                capabilities.dispatch(removed, SCHOOL, {"job_id": JOB_A})
            self.assertEqual(getattr(raised.exception, "code", ""), "unsupported_capability")

    def test_the_report_capability_name_is_the_protocol_constant(self) -> None:
        self.assertEqual(capabilities.REPORT_CAPABILITY, "podcast.report")
        self.assertEqual(protocol.PODCAST_REPORT_CAPABILITY, capabilities.REPORT_CAPABILITY)

    def test_the_store_never_mints_job_ids(self) -> None:
        with tempfile.TemporaryDirectory(prefix="podcast-kimlik-") as tmp:
            store = jobs.JobStore(root=tmp, workers=1, stage_secs=0.0)
            with self.assertRaises(ValueError):
                store.submit("kotu/id", "ders.pdf", "duz_okuma")
            record, _ = store.submit(JOB_A, "ders.pdf", "duz_okuma")
            self.assertEqual(record["job_id"], JOB_A)
            with self.assertRaises(jobs.JobExists):
                store.submit(JOB_A, "ders.pdf", "duz_okuma")


if __name__ == "__main__":
    unittest.main()
