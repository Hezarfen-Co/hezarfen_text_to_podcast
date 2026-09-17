import tempfile
import threading
import time
import unittest
from pathlib import Path

from src import config, jobs

SHUTDOWN_CEILING_SECS = 3.0


def wait_for(store: jobs.JobStore, job_id: str, wanted: tuple, deadline_secs: float) -> dict:
    limit = time.monotonic() + deadline_secs
    while time.monotonic() < limit:
        record = store.get(job_id)
        if record["state"] in wanted:
            return record
        time.sleep(0.005)
    return store.get(job_id)


class DaemonShutdownRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-kapanis-")
        self.root = Path(self._tmp.name)
        self.gate = threading.Event()
        self.store = jobs.JobStore(
            root=self.root / "isler",
            workers=2,
            max_jobs=8,
            stage_secs=0.0,
            runner=self.blocking_runner,
        )

    def tearDown(self) -> None:
        self.gate.set()
        self.store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def blocking_runner(self, ctx) -> None:
        self.gate.wait(10.0)

    def worker_threads(self) -> list:
        return [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("podcast-job")
        ]

    def test_worker_threads_must_be_daemon_or_interpreter_joins_them_on_exit(self) -> None:
        self.store.start()
        job_id = "6e5b0001-0000-4000-8000-000000000001"
        self.store.submit(job_id, "uzun-is", "duz_okuma")
        wait_for(self.store, job_id, (jobs.STATE_RUNNING,), 5.0)
        threads = self.worker_threads()
        self.assertTrue(threads, "isci thread bulunamadi")
        for thread in threads:
            with self.subTest(thread=thread.name):
                self.assertTrue(
                    thread.daemon,
                    "isci thread daemon degil; 45 dakikalik is SIGTERM'i SIGKILL'e cevirir",
                )

    def test_shutdown_does_not_hang_longer_than_the_given_timeout(self) -> None:
        self.store.start()
        job_id = "6e5b0001-0000-4000-8000-000000000002"
        self.store.submit(job_id, "uzun-is", "duz_okuma")
        running = wait_for(self.store, job_id, (jobs.STATE_RUNNING,), 5.0)
        self.assertEqual(running["state"], jobs.STATE_RUNNING, "uzun is kosmaya baslamadi")
        started = time.monotonic()
        self.store.shutdown(timeout=0.3)
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed,
            SHUTDOWN_CEILING_SECS,
            f"shutdown(timeout=0.3) {elapsed:.1f}s takildi; tikanan is kapanmayi kilitliyor",
        )

    def test_shutdown_must_raise_the_stopping_flag(self) -> None:
        self.store.start()
        self.assertFalse(self.store.stopping)
        self.store.shutdown(timeout=0.3)
        self.assertTrue(self.store.stopping, "shutdown sonrasi stopping False kaldi")

    def test_shutdown_must_set_the_cancel_flag_on_running_jobs(self) -> None:
        self.store.start()
        job_id = "6e5b0001-0000-4000-8000-000000000003"
        self.store.submit(job_id, "uzun-is", "duz_okuma")
        wait_for(self.store, job_id, (jobs.STATE_RUNNING,), 5.0)
        self.store.shutdown(timeout=0.3)
        self.assertTrue(
            self.store.get(job_id)["cancel_requested"],
            "kapanista kosan ise iptal bayragi konmadi; is kendini durduramaz",
        )

    def test_cancel_flag_set_at_shutdown_must_also_be_written_to_disk(self) -> None:
        self.store.start()
        job_id = "6e5b0001-0000-4000-8000-000000000004"
        self.store.submit(job_id, "uzun-is", "duz_okuma")
        wait_for(self.store, job_id, (jobs.STATE_RUNNING,), 5.0)
        self.store.shutdown(timeout=0.3)
        reopened = jobs.JobStore(
            root=self.root / "isler", workers=1, max_jobs=8, stage_secs=0.0
        )
        try:
            self.assertEqual(
                reopened.get(job_id)["state"],
                jobs.STATE_FAILED,
                "kesilen kosan is yeniden acilista supurulmeli",
            )
            self.assertEqual(reopened.get(job_id)["error_code"], jobs.INTERRUPTED_CODE)
        finally:
            reopened.shutdown(timeout=1.0)

    def test_shutdown_on_an_empty_store_must_return_immediately(self) -> None:
        self.store.start()
        started = time.monotonic()
        self.store.shutdown(timeout=5.0)
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed, 1.0, f"issiz depoda shutdown {elapsed:.1f}s bekledi; sentinel gonderilmiyor"
        )

    def test_threads_must_stay_daemon_when_the_thread_pool_is_restarted(self) -> None:
        self.store.start()
        self.store.shutdown(timeout=0.3)
        self.store.start()
        for thread in self.worker_threads():
            with self.subTest(thread=thread.name):
                self.assertTrue(thread.daemon)


if __name__ == "__main__":
    unittest.main()
