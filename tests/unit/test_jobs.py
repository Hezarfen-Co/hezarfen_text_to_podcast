import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src import config, jobs

JOB_ONE = "11111111-1111-7111-8111-111111111111"
JOB_TWO = "22222222-2222-7222-8222-222222222222"
JOB_THREE = "33333333-3333-7333-8333-333333333333"


def wait_for(store: jobs.JobStore, job_id: str, wanted: tuple, deadline_secs: float) -> dict:
    limit = time.monotonic() + deadline_secs
    while time.monotonic() < limit:
        record = store.get(job_id)
        if record["state"] in wanted:
            return record
        time.sleep(0.005)
    return store.get(job_id)


class JobTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-isler-")
        self.root = Path(self._tmp.name)
        self._stores: list = []

    def tearDown(self) -> None:
        for store in self._stores:
            store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def make_store(self, name: str, **kwargs) -> jobs.JobStore:
        defaults = {"workers": 1, "max_jobs": 8, "stage_secs": 0.0}
        defaults.update(kwargs)
        store = jobs.JobStore(root=self.root / name, **defaults)
        self._stores.append(store)
        return store


class JobIdTests(JobTestCase):
    def test_job_id_is_twenty_six_crockford_characters(self) -> None:
        for _ in range(50):
            job_id = jobs.new_job_id()
            with self.subTest(job_id=job_id):
                self.assertEqual(len(job_id), jobs.JOB_ID_LENGTH)
                self.assertTrue(
                    set(job_id) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ"),
                    f"is kimliginde Crockford disi karakter var: {job_id}",
                )

    def test_job_ids_are_unique(self) -> None:
        produced = [jobs.new_job_id() for _ in range(500)]
        self.assertEqual(len(set(produced)), len(produced), "is kimlikleri carpisti")

    def test_job_id_timestamp_prefix_never_goes_backwards(self) -> None:
        prefixes = [jobs.new_job_id()[:10] for _ in range(200)]
        self.assertEqual(
            sorted(prefixes), prefixes, "is kimliginin zaman oneki geriye gitmemeli"
        )

    def test_supplied_job_id_must_be_a_safe_name(self) -> None:
        store = self.make_store("kotu-kimlik")
        for bad in ("", "kotus id", "a" * 129, 7):
            with self.subTest(job_id=bad):
                with self.assertRaises(ValueError):
                    store.submit(bad, "x", "duz_okuma")

    def test_duplicate_supplied_job_id_is_refused(self) -> None:
        store = self.make_store("cift-kimlik")
        store.submit(JOB_ONE, "x", "duz_okuma")
        with self.assertRaises(jobs.JobExists):
            store.submit(JOB_ONE, "x", "duz_okuma")


class StateMachineTests(JobTestCase):
    def test_state_set_is_the_documented_five(self) -> None:
        self.assertEqual(
            jobs.STATES, ("queued", "running", "done", "failed", "cancelled")
        )

    def test_every_transition_target_is_a_known_state(self) -> None:
        for state, allowed in jobs.TRANSITIONS.items():
            for target in allowed:
                with self.subTest(state=state, target=target):
                    self.assertIn(target, jobs.STATES)

    def test_terminal_states_have_no_outgoing_transition(self) -> None:
        for terminal in (jobs.STATE_DONE, jobs.STATE_FAILED, jobs.STATE_CANCELLED):
            with self.subTest(state=terminal):
                self.assertEqual(
                    jobs.TRANSITIONS[terminal], (), f"'{terminal}' terminal olmali"
                )

    def test_can_transition_matches_the_table(self) -> None:
        legal = (
            ("queued", "running"),
            ("queued", "cancelled"),
            ("queued", "failed"),
            ("running", "done"),
            ("running", "failed"),
            ("running", "cancelled"),
        )
        forbidden = (
            ("queued", "done"),
            ("done", "running"),
            ("failed", "running"),
            ("cancelled", "done"),
            ("running", "queued"),
            ("queued", "bilinmeyen"),
        )
        for current, target in legal:
            with self.subTest(transition=(current, target)):
                self.assertTrue(jobs.can_transition(current, target))
        for current, target in forbidden:
            with self.subTest(transition=(current, target)):
                self.assertFalse(jobs.can_transition(current, target))

    def test_illegal_transition_raises_invalid_transition(self) -> None:
        store = self.make_store("gecis")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        with self.assertRaises(jobs.InvalidTransition):
            store.transition(job_id, jobs.STATE_DONE)

    def test_transition_on_unknown_job_raises_job_not_found(self) -> None:
        store = self.make_store("bilinmeyen")
        with self.assertRaises(jobs.JobNotFound):
            store.transition("YOKBOYLEBIRIS", jobs.STATE_RUNNING)


class ProgressTests(JobTestCase):
    def test_progress_is_ignored_unless_the_job_is_running(self) -> None:
        store = self.make_store("ilerleme")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        self.assertIsNone(
            store.update_progress(job_id, "ocr", 0.5),
            "kuyruktaki ise ilerleme yazilmamali",
        )

    def test_progress_is_clamped_to_the_unit_interval(self) -> None:
        store = self.make_store("kirpma")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        store.transition(job_id, jobs.STATE_RUNNING)
        for given, expected in ((-1.0, 0.0), (0.0, 0.0), (0.5, 0.5), (2.0, 1.0)):
            with self.subTest(given=given):
                record = store.update_progress(job_id, "ocr", given)
                self.assertEqual(record["progress"], expected)


class PersistenceTests(JobTestCase):
    def test_submitted_job_is_written_to_disk_atomically(self) -> None:
        root = self.root / "kalici"
        store = self.make_store("kalici")
        job_id = store.submit(JOB_ONE, "kaynak-1", "duz_okuma")[0]["job_id"]
        path = root / f"{job_id}.json"
        self.assertTrue(path.is_file(), "is durumu diske yazilmadi")
        with open(path, "r", encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["job_id"], job_id)
        self.assertEqual(on_disk["state"], "queued")
        self.assertEqual(
            [name for name in os.listdir(root) if ".json.tmp" in name],
            [],
            "atomik yazmadan gecici dosya artigi kalmamali",
        )

    def test_reopened_store_recovers_queued_jobs_without_sweeping_them(self) -> None:
        store = self.make_store("yeniden")
        job_id = store.submit(JOB_ONE, "kaynak-1", "duz_okuma")[0]["job_id"]
        reopened = self.make_store("yeniden")
        self.assertEqual(reopened.get(job_id)["state"], "queued")
        self.assertEqual(reopened.swept, 0, "kuyruktaki is supurulmemeli")

    def test_record_id_must_match_the_file_name(self) -> None:
        root = self.root / "kimlik"
        store = self.make_store("kimlik")
        job_id = store.submit(JOB_ONE, "kaynak-1", "duz_okuma")[0]["job_id"]
        with open(root / f"{job_id}.json", "r", encoding="utf-8") as handle:
            record = json.load(handle)
        record["job_id"] = "BASKABIRKIMLIK"
        with open(root / f"{job_id}.json", "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        reopened = self.make_store("kimlik")
        with self.assertRaises(jobs.JobNotFound):
            reopened.get(job_id)

    def test_unreadable_record_is_skipped_not_fatal(self) -> None:
        root = self.root / "bozuk"
        root.mkdir(parents=True, exist_ok=True)
        with open(root / "01BOZUKKAYIT.json", "w", encoding="utf-8") as handle:
            handle.write("{ bu json degil")
        store = self.make_store("bozuk")
        self.assertEqual(store.swept, 0)


class SweepTests(JobTestCase):
    def test_running_jobs_become_failed_interrupted_on_boot(self) -> None:
        seed = self.make_store("supurme")
        running_id = seed.submit(JOB_ONE, "kaynak-2", "duz_okuma")[0]["job_id"]
        seed.transition(running_id, jobs.STATE_RUNNING)
        queued_id = seed.submit(JOB_TWO, "kaynak-3", "duz_okuma")[0]["job_id"]

        swept = self.make_store("supurme")
        self.assertEqual(swept.swept, 1, "acilis supurmesi tam olarak 1 is bulmali")
        revived = swept.get(running_id)
        self.assertEqual(revived["state"], jobs.STATE_FAILED)
        self.assertEqual(revived["error_code"], jobs.INTERRUPTED_CODE)
        self.assertEqual(
            swept.get(queued_id)["state"], "queued", "kuyruktaki is supurmede bozulmamali"
        )

    def test_sweep_is_persisted_so_a_second_boot_finds_nothing(self) -> None:
        seed = self.make_store("iki-acilis")
        job_id = seed.submit(JOB_ONE, "kaynak", "duz_okuma")[0]["job_id"]
        seed.transition(job_id, jobs.STATE_RUNNING)
        self.assertEqual(self.make_store("iki-acilis").swept, 1)
        self.assertEqual(
            self.make_store("iki-acilis").swept, 0, "supurme diske yazilmadigi icin tekrarlaniyor"
        )
        self.assertEqual(self.make_store("iki-acilis").get(job_id)["state"], jobs.STATE_FAILED)


class QueueTests(JobTestCase):
    def test_queue_quota_counts_only_queued_and_running_jobs(self) -> None:
        store = self.make_store("kota", max_jobs=2)
        first = store.submit(JOB_ONE, "a", "duz_okuma")[0]["job_id"]
        store.submit(JOB_TWO, "b", "duz_okuma")
        with self.assertRaises(jobs.JobStoreFull):
            store.submit(JOB_THREE, "c", "duz_okuma")
        store.cancel(first)
        store.submit(JOB_THREE, "c", "duz_okuma")

    def test_queue_full_error_reports_the_limit(self) -> None:
        store = self.make_store("kota-mesaj", max_jobs=1)
        store.submit(JOB_ONE, "a", "duz_okuma")
        with self.assertRaises(jobs.JobStoreFull) as raised:
            store.submit(JOB_TWO, "b", "duz_okuma")
        self.assertEqual(raised.exception.limit, 1)
        self.assertEqual(raised.exception.active, 1)

    def test_estimate_eta_multiplies_by_worker_waves(self) -> None:
        store = self.make_store("eta", workers=2, job_secs=2700.0)
        self.assertEqual(store.estimate_eta(1), 2700)
        self.assertEqual(store.estimate_eta(2), 2700)
        self.assertEqual(store.estimate_eta(4), 5400)
        self.assertEqual(store.estimate_eta(0), 2700, "bos kuyruk icin eta bir dalga olmali")

    def test_auto_job_secs_never_drops_below_one_second(self) -> None:
        self.assertEqual(self.make_store("eta-oto").job_secs, 1.0)

    def test_custom_stages_are_honoured(self) -> None:
        self.assertEqual(self.make_store("asamalar", stages=("ocr", "tts")).stages, ("ocr", "tts"))

    def test_default_stages_are_the_simulated_five(self) -> None:
        self.assertEqual(self.make_store("varsayilan-asama").stages, jobs.SIMULATED_STAGES)


class CancellationTests(JobTestCase):
    def test_cancelling_a_queued_job_terminates_it_immediately(self) -> None:
        store = self.make_store("iptal-kuyruk")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        self.assertTrue(store.cancel(job_id))
        self.assertEqual(store.get(job_id)["state"], jobs.STATE_CANCELLED)

    def test_cancelling_a_running_job_only_raises_the_flag(self) -> None:
        store = self.make_store("iptal-kosan")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        store.transition(job_id, jobs.STATE_RUNNING)
        self.assertTrue(store.cancel(job_id))
        record = store.get(job_id)
        self.assertEqual(record["state"], jobs.STATE_RUNNING)
        self.assertTrue(record["cancel_requested"])

    def test_cancelling_a_terminal_job_returns_false(self) -> None:
        store = self.make_store("iptal-bitmis")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        store.transition(job_id, jobs.STATE_RUNNING)
        store.finish(job_id, audio_id="a", duration_secs=1.0, script_id="s")
        self.assertFalse(store.cancel(job_id))

    def test_cancel_of_unknown_job_raises_job_not_found(self) -> None:
        with self.assertRaises(jobs.JobNotFound):
            self.make_store("iptal-yok").cancel("YOKBOYLEBIRIS")

    def test_begin_refuses_a_job_cancelled_before_it_started(self) -> None:
        store = self.make_store("baslamadan")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        store.cancel(job_id)
        self.assertFalse(store.begin(job_id))
        self.assertEqual(store.get(job_id)["state"], jobs.STATE_CANCELLED)

    def test_begin_refuses_while_the_store_is_stopping(self) -> None:
        store = self.make_store("kapanirken")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        store.shutdown(timeout=0.5)
        self.assertFalse(store.begin(job_id))

    def test_job_context_sees_cancellation_through_the_store(self) -> None:
        store = self.make_store("baglam")
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        store.transition(job_id, jobs.STATE_RUNNING)
        ctx = jobs.JobContext(store, job_id)
        self.assertFalse(ctx.cancelled())
        ctx.check()
        store.cancel(job_id)
        self.assertTrue(ctx.cancelled())
        with self.assertRaises(jobs.JobCancelled):
            ctx.check()


class WorkerLoopTests(JobTestCase):
    def test_simulated_pipeline_carries_a_job_to_done(self) -> None:
        store = self.make_store("sahte-hat")
        store.start()
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        final = wait_for(store, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        self.assertEqual(final["state"], jobs.STATE_DONE, final["error_code"])
        self.assertEqual(final["progress"], 1.0)
        self.assertTrue(final["audio_id"])
        self.assertTrue(final["script_id"])
        self.assertEqual(final["audio_ids"], [final["audio_id"]])

    def test_a_runner_that_raises_moves_the_job_to_failed_internal(self) -> None:
        def raising(ctx) -> None:
            raise RuntimeError("bilerek patladi")

        store = self.make_store("patlayan", runner=raising)
        store.start()
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        final = wait_for(store, job_id, (jobs.STATE_FAILED, jobs.STATE_DONE), 10.0)
        self.assertEqual(final["state"], jobs.STATE_FAILED)
        self.assertEqual(final["error_code"], "internal")

    def test_a_runner_that_honours_cancellation_ends_as_cancelled(self) -> None:
        gate = threading.Event()

        def cooperative(ctx) -> None:
            gate.wait(5.0)
            ctx.check()

        store = self.make_store("isbirlikci", runner=cooperative)
        store.start()
        job_id = store.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        wait_for(store, job_id, (jobs.STATE_RUNNING,), 5.0)
        store.cancel(job_id)
        gate.set()
        final = wait_for(store, job_id, (jobs.STATE_CANCELLED, jobs.STATE_DONE), 10.0)
        self.assertEqual(final["state"], jobs.STATE_CANCELLED)

    def test_start_requeues_jobs_left_queued_from_a_previous_boot(self) -> None:
        seed = self.make_store("yeniden-kuyruk")
        job_id = seed.submit(JOB_ONE, "x", "duz_okuma")[0]["job_id"]
        reopened = self.make_store("yeniden-kuyruk")
        self.assertEqual(reopened.start(), 1, "acilista kuyruktaki is yeniden alinmali")
        final = wait_for(reopened, job_id, (jobs.STATE_DONE, jobs.STATE_FAILED), 10.0)
        self.assertEqual(final["state"], jobs.STATE_DONE)


class SourceResolutionTests(JobTestCase):
    def test_valid_name_resolves_under_the_school_directory(self) -> None:
        media = self.root / "medya"
        media.mkdir(parents=True, exist_ok=True)
        resolved = jobs.resolve_source(media, "okul-a", "ders-01.pdf")
        self.assertEqual(resolved, media.resolve() / "okul-a" / "ders-01.pdf")

    def test_the_school_segment_is_used_instead_of_the_flat_path(self) -> None:
        media = self.root / "medya-duz"
        media.mkdir(parents=True, exist_ok=True)
        (media / "ders-01.pdf").write_bytes(b"%PDF-1.4\n")
        resolved = jobs.resolve_source(media, "okul-a", "ders-01.pdf")
        self.assertEqual(resolved, media.resolve() / "okul-a" / "ders-01.pdf")
        self.assertFalse(resolved.exists(), "duz yoldaki kaynak okul segmenti yok sayilarak bulundu")

    def test_escaping_names_are_rejected(self) -> None:
        media = self.root / "medya2"
        media.mkdir(parents=True, exist_ok=True)
        for bad in ("..", "../x", "a/b", "/etc/passwd", "", "."):
            with self.subTest(source_key=bad):
                with self.assertRaises(ValueError):
                    jobs.resolve_source(media, "okul-a", bad)

    def test_escaping_school_slugs_are_rejected(self) -> None:
        media = self.root / "medya3"
        media.mkdir(parents=True, exist_ok=True)
        for bad in ("..", "../x", "a/b", "/etc/passwd", "", ".", "OKUL", "okul_a", "x" * 65):
            with self.subTest(school=bad):
                with self.assertRaises(ValueError, msg=f"okul slug'i kabul edildi: {bad!r}"):
                    jobs.resolve_source(media, bad, "ders.pdf")

    def test_a_non_text_school_or_key_is_rejected(self) -> None:
        media = self.root / "medya4"
        media.mkdir(parents=True, exist_ok=True)
        for bad in (None, 7, b"okul"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    jobs.resolve_source(media, bad, "ders.pdf")
        for bad in (None, 7, b"ders.pdf"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    jobs.resolve_source(media, "okul-a", bad)


class StatusFileTests(JobTestCase):
    def test_status_file_round_trips_registration_state(self) -> None:
        root = self.root / "durum"
        root.mkdir(parents=True, exist_ok=True)
        jobs.write_status(root, True, "w9")
        record = jobs.read_status(root)
        self.assertEqual(record["registered"], True)
        self.assertEqual(record["worker_id"], "w9")
        self.assertEqual(record["pid"], os.getpid())

    def test_missing_status_file_reads_as_none(self) -> None:
        root = self.root / "durumsuz"
        root.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(jobs.read_status(root))

    def test_corrupt_status_file_reads_as_none_instead_of_raising(self) -> None:
        root = self.root / "durum-bozuk"
        root.mkdir(parents=True, exist_ok=True)
        with open(jobs.status_path(root), "w", encoding="utf-8") as handle:
            handle.write("bu json degil")
        self.assertIsNone(jobs.read_status(root))


if __name__ == "__main__":
    unittest.main()
