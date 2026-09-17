import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src import capabilities, config, jobs, pipeline
from src.protocol import CapabilityError

SCHOOL = "okul-a"


class FakeResult:
    def __init__(self, mp3: list, scripts: list, duration: float) -> None:
        self.mp3_yollari = mp3
        self.script_yollari = scripts
        self.ses_toplam_sn = duration
        self.adimlar: list = []


class FakePipelineError(RuntimeError):
    pass


class FakeInterrupt(RuntimeError):
    pass


def fake_pipeline_class(mp3: list, scripts: list, duration: float, gate=None):
    class FakePipeline:
        def __init__(self, pdf, format, gunluk, cikti_koku, bolum_limiti,
                     motor_adi, ses_uret, kayit=None):
            self.pdf = pdf
            self.format = format
            self.gunluk = gunluk
            self.cikti_koku = cikti_koku
            self.bolum_limiti = bolum_limiti
            self.motor_adi = motor_adi
            self.ses_uret = ses_uret
            self.kayit = kayit
            self.sonuc = FakeResult(list(mp3), list(scripts), duration)

        def kos(self):
            for name in pipeline.REAL_STAGES:
                if gate is not None:
                    gate.wait(5.0)
                self.sonuc.adimlar.append(name)
                self.gunluk(f"[KOSTU ] {name}")
            return self.sonuc

    return FakePipeline


def wait_for(store: jobs.JobStore, job_id: str, wanted: tuple, deadline_secs: float) -> dict:
    limit = time.monotonic() + deadline_secs
    while time.monotonic() < limit:
        record = store.get(job_id)
        if record["state"] in wanted:
            return record
        time.sleep(0.005)
    return store.get(job_id)


def submit_job(source_id: str, job_format=None) -> dict:
    payload = {
        "job_id": jobs.new_job_id(),
        "source_id": source_id,
        "source_key": source_id,
        "user_id": "ogretmen-1",
    }
    if job_format is not None:
        payload["format"] = job_format
    return capabilities.dispatch("podcast.submit", SCHOOL, payload)


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-dongu-")
        self.root = Path(self._tmp.name)
        self.media_root = self.root / "medya"
        (self.media_root / SCHOOL).mkdir(parents=True, exist_ok=True)
        (self.media_root / SCHOOL / "ders.pdf").write_bytes(b"%PDF-1.4\n")
        self.output_root = self.root / "cikti"
        self._stores: list = []

    def tearDown(self) -> None:
        capabilities.configure(None, llm_ready=False)
        for store in self._stores:
            store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def pipeline_store(self, name: str, mp3: list, scripts: list, duration: float = 123.5,
                       gate=None, chapter_limit: int = 1) -> jobs.JobStore:
        runner = pipeline.make_runner(
            str(self.media_root),
            str(self.output_root),
            chapter_limit,
            "supertonic-3",
            fake_pipeline_class(mp3, scripts, duration, gate),
            FakePipelineError,
            FakeInterrupt,
        )
        store = jobs.JobStore(
            root=self.root / name,
            workers=1,
            max_jobs=8,
            stage_secs=0.0,
            runner=runner,
            stages=pipeline.REAL_STAGES,
        )
        self._stores.append(store)
        store.start()
        capabilities.configure(store, llm_ready=False)
        return store

    def output_path(self, *parts: str) -> str:
        return str(self.output_root.joinpath(*parts))


class SubmitStatusResultTests(LifecycleTestCase):
    def test_submit_status_result_walks_a_job_from_queue_to_audio(self) -> None:
        store = self.pipeline_store(
            "mutlu",
            [self.output_path("ders", "ses", "duz_okuma", "ders-b01.mp3")],
            [self.output_path("ders", "script", "duz_okuma", "ders-b01.script.json")],
        )
        submitted = submit_job("ders.pdf", "duz_okuma")
        job_id = submitted["job_id"]
        self.assertEqual(submitted["state"], "queued")

        final = wait_for(store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        self.assertEqual(final["state"], jobs.STATE_DONE, final["error_code"])

        state = store.get(job_id)
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["progress"], 1.0)
        self.assertIsNone(state["error_code"])

        result = store.get(job_id)
        self.assertEqual(result["audio_id"], "ders/ses/duz_okuma/ders-b01.mp3")
        self.assertEqual(result["script_id"], "ders/script/duz_okuma/ders-b01.script.json")
        self.assertEqual(result["duration_secs"], 123.5)
        self.assertEqual(result["format"], "duz_okuma")
        self.assertEqual(result["audio_ids"], [result["audio_id"]])

    def test_the_finished_state_is_readable_from_a_freshly_opened_store(self) -> None:
        store = self.pipeline_store(
            "kalici",
            [self.output_path("ders", "ses", "duz_okuma", "ders-b01.mp3")],
            [self.output_path("ders", "script", "duz_okuma", "ders-b01.script.json")],
        )
        job_id = submit_job("ders.pdf")["job_id"]
        wait_for(store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        store.shutdown(timeout=1.0)

        reopened = jobs.JobStore(root=self.root / "kalici", workers=1, max_jobs=8, stage_secs=0.0)
        self._stores.append(reopened)
        self.assertEqual(reopened.swept, 0, "biten is yeniden acilista supurulmemeli")
        capabilities.configure(reopened, llm_ready=False)
        result = reopened.get(job_id)
        self.assertEqual(result["state"], jobs.STATE_DONE)
        self.assertEqual(result["audio_id"], "ders/ses/duz_okuma/ders-b01.mp3")

    def test_progress_is_reported_while_the_job_is_still_running(self) -> None:
        gate = threading.Event()
        store = self.pipeline_store(
            "ilerleme",
            [self.output_path("ders", "ses", "duz_okuma", "ders-b01.mp3")],
            [self.output_path("ders", "script", "duz_okuma", "ders-b01.script.json")],
            gate=gate,
        )
        job_id = submit_job("ders.pdf")["job_id"]
        running = wait_for(store, job_id, (jobs.STATE_RUNNING,), 5.0)
        self.assertEqual(running["state"], jobs.STATE_RUNNING)
        state = store.get(job_id)
        self.assertIn(state["stage"], pipeline.REAL_STAGES)
        self.assertGreaterEqual(state["progress"], 0.0)
        self.assertLessEqual(state["progress"], 1.0)
        gate.set()
        wait_for(store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)


class FailurePathTests(LifecycleTestCase):
    def test_a_missing_source_fails_with_source_not_found_and_no_audio_id(self) -> None:
        store = self.pipeline_store("kayip", ["/o/x.mp3"], ["/o/x.json"])
        job_id = submit_job("yok.pdf")["job_id"]
        final = wait_for(store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        self.assertEqual(final["state"], jobs.STATE_FAILED)
        self.assertEqual(final["error_code"], pipeline.SOURCE_NOT_FOUND)
        self.assertIsNone(final["audio_id"])
        record = store.get(job_id)
        self.assertIsNone(record["script_id"])
        self.assertIsNone(record["duration_secs"])

    def test_a_run_that_produced_no_mp3_fails_instead_of_returning_an_empty_result(self) -> None:
        store = self.pipeline_store(
            "sessiz", [], [self.output_path("ders", "script", "duz_okuma", "ders-b01.script.json")]
        )
        job_id = submit_job("ders.pdf")["job_id"]
        final = wait_for(store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        self.assertEqual(final["state"], jobs.STATE_FAILED)
        self.assertEqual(final["error_code"], pipeline.NO_AUDIO)
        self.assertIsNone(final["audio_id"], "ses uretmeyen is audio_id yazmamali")


class CancellationTests(LifecycleTestCase):
    def test_a_running_job_stops_at_the_next_step_boundary_after_cancel(self) -> None:
        gate = threading.Event()
        store = self.pipeline_store(
            "iptal",
            [self.output_path("ders", "ses", "duz_okuma", "ders-b01.mp3")],
            [self.output_path("ders", "script", "duz_okuma", "ders-b01.script.json")],
            gate=gate,
        )
        job_id = submit_job("ders.pdf")["job_id"]
        wait_for(store, job_id, (jobs.STATE_RUNNING,), 5.0)
        self.assertTrue(capabilities.dispatch("podcast.cancel", "okul-a", {"job_id": job_id})["cancelled"])
        gate.set()
        final = wait_for(
            store, job_id, (jobs.STATE_CANCELLED, jobs.STATE_DONE, jobs.STATE_FAILED), 10.0
        )
        self.assertEqual(final["state"], jobs.STATE_CANCELLED)
        self.assertIsNone(final["audio_id"], "iptal edilen is audio_id yazmamali")

    def test_a_cancelled_job_is_cancelled_on_disk_too(self) -> None:
        store = self.pipeline_store("iptal-disk", ["/o/x.mp3"], ["/o/x.json"])
        job_id = submit_job("ders.pdf")["job_id"]
        wait_for(store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED, jobs.STATE_CANCELLED), 10.0)
        store.shutdown(timeout=1.0)
        with open(self.root / "iptal-disk" / f"{job_id}.json", "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["job_id"], job_id)
        self.assertIn(on_disk["state"], jobs.STATES)


class QuotaTests(LifecycleTestCase):
    def test_the_queue_quota_answers_busy_and_recovers_after_jobs_finish(self) -> None:
        runner = pipeline.make_runner(
            str(self.media_root),
            str(self.output_root),
            1,
            "supertonic-3",
            fake_pipeline_class([self.output_path("d", "ses", "duz_okuma", "d-b01.mp3")], [], 1.0),
            FakePipelineError,
            FakeInterrupt,
        )
        store = jobs.JobStore(
            root=self.root / "kota",
            workers=1,
            max_jobs=1,
            stage_secs=0.0,
            runner=runner,
            stages=pipeline.REAL_STAGES,
        )
        self._stores.append(store)
        capabilities.configure(store, llm_ready=False)
        first = submit_job("ders.pdf")["job_id"]
        with self.assertRaises(CapabilityError) as raised:
            submit_job("ders.pdf")
        self.assertEqual(raised.exception.code, "busy")
        capabilities.dispatch("podcast.cancel", "okul-a", {"job_id": first})
        submit_job("ders.pdf")


if __name__ == "__main__":
    unittest.main()
