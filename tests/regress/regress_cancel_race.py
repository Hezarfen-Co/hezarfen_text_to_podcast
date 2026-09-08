import json
import tempfile
import threading
import unittest
from pathlib import Path

from src import config, jobs


class CancelRaceRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-yaris-")
        self.root = Path(self._tmp.name)
        self.store = jobs.JobStore(
            root=self.root / "isler", workers=1, max_jobs=8, stage_secs=0.0
        )

    def tearDown(self) -> None:
        self.store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def running_job(self) -> str:
        job_id = self.store.submit("kaynak-yaris", "duz_okuma")[0]["job_id"]
        self.store.transition(job_id, jobs.STATE_RUNNING)
        return job_id

    def read_from_disk(self, job_id: str) -> dict:
        with open(self.root / "isler" / f"{job_id}.json", "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_finish_returns_cancelled_not_done_when_cancel_flag_set(self) -> None:
        job_id = self.running_job()
        self.assertTrue(self.store.cancel(job_id))
        finished = self.store.finish(
            job_id, audio_id="a1", duration_secs=12.0, script_id="s1"
        )
        self.assertEqual(
            finished["state"],
            jobs.STATE_CANCELLED,
            "iptal bayragi setken finish() 'done' verdi; iptal yarisi kaybedildi",
        )

    def test_finish_must_not_write_audio_id_when_cancel_flag_set(self) -> None:
        job_id = self.running_job()
        self.store.cancel(job_id)
        finished = self.store.finish(
            job_id, audio_id="a1", duration_secs=12.0, script_id="s1"
        )
        self.assertIsNone(
            finished["audio_id"], "iptal edilen is yine de audio_id yazdi"
        )
        self.assertIsNone(finished["script_id"], "iptal edilen is yine de script_id yazdi")
        self.assertIsNone(finished["duration_secs"])
        self.assertEqual(finished["audio_ids"], [])
        self.assertEqual(finished["script_ids"], [])

    def test_in_memory_and_on_disk_result_must_be_identical(self) -> None:
        job_id = self.running_job()
        self.store.cancel(job_id)
        self.store.finish(job_id, audio_id="a1", duration_secs=12.0, script_id="s1")
        memory = self.store.get(job_id)
        disk = self.read_from_disk(job_id)
        self.assertEqual(
            memory["state"],
            disk["state"],
            f"bellek={memory['state']} disk={disk['state']}; iptal diske yansimadi",
        )
        self.assertEqual(memory["state"], jobs.STATE_CANCELLED)
        self.assertIsNone(disk["audio_id"], "iptal edilen isin audio_id'si diske yazildi")

    def test_job_that_was_not_cancelled_must_become_done_normally(self) -> None:
        job_id = self.running_job()
        finished = self.store.finish(
            job_id, audio_id="a1", duration_secs=12.0, script_id="s1"
        )
        self.assertEqual(finished["state"], jobs.STATE_DONE)
        self.assertEqual(finished["audio_id"], "a1")
        self.assertEqual(self.read_from_disk(job_id)["audio_id"], "a1")

    def test_result_must_stay_consistent_when_cancel_and_finish_run_concurrently(self) -> None:
        for round_index in range(20):
            with self.subTest(round_index=round_index):
                job_id = self.running_job()
                barrier = threading.Barrier(2)
                output: dict = {}

                def do_cancel() -> None:
                    barrier.wait()
                    self.store.cancel(job_id)

                def do_finish() -> None:
                    barrier.wait()
                    output["record"] = self.store.finish(
                        job_id, audio_id="a1", duration_secs=1.0, script_id="s1"
                    )

                thread_a = threading.Thread(target=do_cancel)
                thread_b = threading.Thread(target=do_finish)
                thread_a.start()
                thread_b.start()
                thread_a.join(5.0)
                thread_b.join(5.0)

                record = output["record"]
                disk = self.read_from_disk(job_id)
                self.assertEqual(
                    record["state"],
                    disk["state"],
                    "yaris sonunda bellek ve disk ayrildi",
                )
                if record["state"] == jobs.STATE_CANCELLED:
                    self.assertIsNone(
                        disk["audio_id"],
                        "cancelled biten kosu diske audio_id yazdi",
                    )
                else:
                    self.assertEqual(record["state"], jobs.STATE_DONE)
                    self.assertEqual(disk["audio_id"], "a1")

    def test_finish_must_hold_the_same_lock_while_a_running_job_is_cancelled(self) -> None:
        job_id = self.running_job()
        self.store.cancel(job_id)
        self.assertTrue(
            self.store.cancel_requested(job_id),
            "iptal bayragi finish() ile ayni kilit altinda okunmali",
        )
        self.store.finish(job_id, audio_id="a1", duration_secs=1.0, script_id="s1")
        self.assertEqual(self.store.get(job_id)["state"], jobs.STATE_CANCELLED)


if __name__ == "__main__":
    unittest.main()
