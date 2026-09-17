import json
import tempfile
import unittest
from pathlib import Path

from src import config, jobs

FIELDS_ABSENT_FROM_OLD_RECORDS = ("audio_ids", "script_ids")


class SchemaValidationRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-sema-")
        self.root = Path(self._tmp.name) / "isler"
        self._stores: list = []

    def tearDown(self) -> None:
        for store in self._stores:
            store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def make_store(self) -> jobs.JobStore:
        store = jobs.JobStore(root=self.root, workers=1, max_jobs=8, stage_secs=0.0)
        self._stores.append(store)
        return store

    def write_record(self, job_id: str, record: dict) -> None:
        with open(self.root / f"{job_id}.json", "w", encoding="utf-8") as handle:
            json.dump(record, handle)

    def read_record(self, job_id: str) -> dict:
        with open(self.root / f"{job_id}.json", "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_a_record_with_a_missing_field_must_be_skipped_at_startup_not_raise_keyerror(self) -> None:
        for field in jobs.REQUIRED_FIELDS:
            if field == "job_id":
                continue
            with self.subTest(missing=field):
                seed = self.make_store()
                healthy_id = seed.submit(
                    "5a1e0001-0000-4000-8000-000000000001", "saglam", "duz_okuma"
                )[0]["job_id"]
                corrupt_id = seed.submit(
                    "5a1e0001-0000-4000-8000-000000000002", "bozuk", "duz_okuma"
                )[0]["job_id"]
                corrupt = self.read_record(corrupt_id)
                corrupt.pop(field)
                self.write_record(corrupt_id, corrupt)

                reopened = self.make_store()
                with self.assertRaises(
                    jobs.JobNotFound, msg=f"'{field}' alani eksik kayit acilista atlanmadi"
                ):
                    reopened.get(corrupt_id)
                self.assertEqual(
                    reopened.get(healthy_id)["state"],
                    jobs.STATE_QUEUED,
                    "saglam kayit eksik alanli komsusu yuzunden bozuldu",
                )
                for path in sorted(self.root.glob("*.json")):
                    path.unlink()

    def test_audio_ids_and_script_ids_must_not_be_added_to_required_fields(self) -> None:
        for field in FIELDS_ABSENT_FROM_OLD_RECORDS:
            with self.subTest(field=field):
                self.assertNotIn(
                    field,
                    jobs.REQUIRED_FIELDS,
                    f"REQUIRED_FIELDS'a '{field}' eklenmis; alan eklenmeden once yazilmis "
                    f"eski kayitlar acilista topluca atilir",
                )

    def test_an_old_record_without_the_plural_identifier_fields_must_still_load(self) -> None:
        seed = self.make_store()
        job_id = seed.submit(
            "5a1e0001-0000-4000-8000-000000000003", "eski", "duz_okuma"
        )[0]["job_id"]
        old = self.read_record(job_id)
        for field in FIELDS_ABSENT_FROM_OLD_RECORDS:
            old.pop(field, None)
        self.write_record(job_id, old)

        reopened = self.make_store()
        record = reopened.get(job_id)
        self.assertEqual(
            record["state"],
            jobs.STATE_QUEUED,
            "cogul kimlik alanlari olmayan eski kayit atildi; alan eklemek geriye donuk "
            "uyumlu olmaliydi",
        )

    def test_a_record_in_an_unknown_state_must_be_skipped(self) -> None:
        seed = self.make_store()
        job_id = seed.submit(
            "5a1e0001-0000-4000-8000-000000000004", "gecersiz", "duz_okuma"
        )[0]["job_id"]
        record = self.read_record(job_id)
        record["state"] = "yariyolda"
        self.write_record(job_id, record)

        reopened = self.make_store()
        with self.assertRaises(jobs.JobNotFound):
            reopened.get(job_id)

    def test_a_record_whose_identifier_does_not_match_the_file_name_must_be_skipped(self) -> None:
        seed = self.make_store()
        job_id = seed.submit(
            "5a1e0001-0000-4000-8000-000000000005", "kimlik", "duz_okuma"
        )[0]["job_id"]
        record = self.read_record(job_id)
        record["job_id"] = "BASKABIRKIMLIK00000000000"
        self.write_record(job_id, record)

        reopened = self.make_store()
        with self.assertRaises(jobs.JobNotFound):
            reopened.get(job_id)

    def test_a_record_that_is_not_json_must_not_crash_startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "01BOZUKKAYITDOSYASI000000.json", "w", encoding="utf-8") as handle:
            handle.write("{ bu json degil")
        with open(self.root / "01LISTEOLANKAYIT000000000.json", "w", encoding="utf-8") as handle:
            handle.write("[1, 2, 3]")
        store = self.make_store()
        self.assertEqual(store.swept, 0)

    def test_the_required_field_list_must_match_the_wire_contract(self) -> None:
        self.assertEqual(
            set(jobs.REQUIRED_FIELDS),
            {
                "job_id",
                "source_id",
                "format",
                "state",
                "stage",
                "progress",
                "error_code",
                "cancel_requested",
                "audio_id",
                "duration_secs",
                "script_id",
                "user_id",
                "school",
                "created_at",
                "updated_at",
            },
            "zorunlu alan listesi degisti; eski kayitlarla uyum kontrol edilmeli",
        )


if __name__ == "__main__":
    unittest.main()
