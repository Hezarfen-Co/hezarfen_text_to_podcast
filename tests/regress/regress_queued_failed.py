import tempfile
import unittest
from pathlib import Path

from src import config, jobs


class QueuedFailedRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-erken-")
        self.root = Path(self._tmp.name)
        self._stores: list = []

    def tearDown(self) -> None:
        for store in self._stores:
            store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def make_store(self, name: str, max_jobs: int = 8) -> jobs.JobStore:
        store = jobs.JobStore(
            root=self.root / name, workers=1, max_jobs=max_jobs, stage_secs=0.0
        )
        self._stores.append(store)
        return store

    def test_the_queued_to_failed_transition_must_be_legal_in_the_state_machine(self) -> None:
        self.assertIn(
            jobs.STATE_FAILED,
            jobs.TRANSITIONS[jobs.STATE_QUEUED],
            "queued -> failed yasal degil; begin() oncesi patlayan is sonsuza kadar "
            "queued kalir",
        )
        self.assertTrue(jobs.can_transition(jobs.STATE_QUEUED, jobs.STATE_FAILED))

    def test_a_job_that_fails_before_starting_must_be_able_to_become_failed(self) -> None:
        store = self.make_store("erken")
        job_id = store.submit("kaynak-erken", "duz_okuma")[0]["job_id"]
        try:
            record = store.fail(job_id, "source_not_found")
        except jobs.InvalidTransition as exc:
            self.fail(f"queued -> failed reddedildi, is kotayi kalici yer: {exc}")
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "source_not_found", "hata kodu kaybedildi")

    def test_a_failed_job_must_not_occupy_a_slot_in_the_max_jobs_quota(self) -> None:
        store = self.make_store("kota", max_jobs=1)
        job_id = store.submit("ilk", "duz_okuma")[0]["job_id"]
        with self.assertRaises(jobs.JobStoreFull):
            store.submit("ikinci", "duz_okuma")
        store.fail(job_id, "source_not_found")
        store.submit("ikinci", "duz_okuma")

    def test_a_failed_job_must_not_be_requeued_on_every_startup(self) -> None:
        seed = self.make_store("acilis")
        job_id = seed.submit("kaynak-erken", "duz_okuma")[0]["job_id"]
        seed.fail(job_id, "source_not_found")

        reopened = self.make_store("acilis")
        self.assertEqual(
            reopened.get(job_id)["state"],
            jobs.STATE_FAILED,
            "failed is yeniden acilista queued'a donmemeli",
        )
        self.assertEqual(
            reopened.start(),
            0,
            "failed is her acilista yeniden kuyruga aliniyor; kotadan slot yiyor",
        )

    def test_no_job_must_stay_queued_forever(self) -> None:
        store = self.make_store("terminal")
        for target in (jobs.STATE_RUNNING, jobs.STATE_CANCELLED, jobs.STATE_FAILED):
            with self.subTest(target=target):
                job_id = store.submit("kaynak", "duz_okuma")[0]["job_id"]
                store.transition(job_id, target)
                self.assertEqual(store.get(job_id)["state"], target)

    def test_failed_must_stay_terminal_and_must_not_go_back(self) -> None:
        store = self.make_store("geri")
        job_id = store.submit("kaynak", "duz_okuma")[0]["job_id"]
        store.fail(job_id, "source_not_found")
        for target in (jobs.STATE_QUEUED, jobs.STATE_RUNNING, jobs.STATE_DONE):
            with self.subTest(target=target):
                with self.assertRaises(jobs.InvalidTransition):
                    store.transition(job_id, target)


if __name__ == "__main__":
    unittest.main()
