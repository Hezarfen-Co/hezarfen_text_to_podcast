import json
import os
import tempfile
import unittest
from pathlib import Path

from src import config, jobs


class TempFileRecorder:
    def __init__(self) -> None:
        self.sources: list = []
        self._real = os.replace

    def __enter__(self) -> "TempFileRecorder":
        def record_it(src, dst, *args, **kwargs):
            self.sources.append(os.path.basename(str(src)))
            return self._real(src, dst, *args, **kwargs)

        os.replace = record_it
        return self

    def __exit__(self, *exc) -> None:
        os.replace = self._real


class TmpResidueRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-artik-")
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

    def test_the_temp_file_name_must_contain_the_pid(self) -> None:
        store = self.make_store()
        with TempFileRecorder() as recorder:
            store.submit("9d3f0001-0000-4000-8000-000000000001", "kaynak", "duz_okuma")
            jobs.write_status(self.root, True, "w1")
        self.assertTrue(recorder.sources, "atomik yazma os.replace kullanmiyor")
        for name in recorder.sources:
            with self.subTest(temp=name):
                self.assertIn(
                    f".tmp{os.getpid()}",
                    name,
                    "gecici dosya adi pid icermiyor; iki surec ayni gecici dosyayi ezer",
                )

    def test_atomic_writing_must_be_done_with_a_temp_file_plus_replace(self) -> None:
        store = self.make_store()
        with TempFileRecorder() as recorder:
            job_id = store.submit(
                "9d3f0001-0000-4000-8000-000000000002", "kaynak", "duz_okuma"
            )[0]["job_id"]
        self.assertIn(
            f"{job_id}.json.tmp{os.getpid()}",
            "".join(recorder.sources),
            "is kaydi gecici dosya uzerinden yazilmadi",
        )
        with open(self.root / f"{job_id}.json", "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["job_id"], job_id)

    def test_no_residue_must_remain_after_a_normal_write(self) -> None:
        store = self.make_store()
        store.submit("9d3f0001-0000-4000-8000-000000000003", "kaynak", "duz_okuma")
        jobs.write_status(self.root, True, "w1")
        residues = [name for name in os.listdir(self.root) if ".tmp" in name]
        self.assertEqual(residues, [], f"atomik yazmadan gecici dosya kaldi: {residues}")

    def test_temp_file_residues_must_be_cleaned_up_at_startup(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        residues = [
            self.root / "01ESKIISKAYDI000000000000.json.tmp999-dead",
            self.root / "01ESKIISKAYDI000000000000.json.tmp12345-beef",
            self.root / f"{jobs.STATUS_FILE}.tmp999-dead",
        ]
        for path in residues:
            path.write_text("{}", encoding="ascii")
        self.make_store()
        for path in residues:
            with self.subTest(residue=path.name):
                self.assertFalse(
                    path.exists(), f"acilista gecici dosya artigi temizlenmedi: {path.name}"
                )

    def test_the_cleanup_must_not_delete_real_job_records(self) -> None:
        seed = self.make_store()
        job_id = seed.submit(
            "9d3f0001-0000-4000-8000-000000000004", "saglam", "duz_okuma"
        )[0]["job_id"]
        jobs.write_status(self.root, True, "w1")
        (self.root / "01ESKIISKAYDI000000000000.json.tmp999-dead").write_text("{}", encoding="ascii")

        reopened = self.make_store()
        self.assertTrue(
            (self.root / f"{job_id}.json").is_file(),
            "temizleme filtresi gercekten uygulanmiyor; is kaydi silindi",
        )
        self.assertEqual(reopened.get(job_id)["state"], jobs.STATE_QUEUED)
        self.assertIsNotNone(
            jobs.read_status(self.root), "temizleme kopru durum dosyasini sildi"
        )

    def test_the_cleanup_must_not_delete_unrelated_files(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        preserved = [
            self.root / "notlar.txt",
            self.root / "arsiv.json.gz",
            self.root / "tmpnot.txt",
            self.root / "OKU.md",
        ]
        for path in preserved:
            path.write_text("veri", encoding="ascii")
        (self.root / "01ESKIISKAYDI000000000000.json.tmp999-dead").write_text("{}", encoding="ascii")

        self.make_store()
        for path in preserved:
            with self.subTest(file=path.name):
                self.assertTrue(
                    path.is_file(),
                    f"temizleme filtre disindaki dosyayi sildi: {path.name}",
                )

    def test_the_cleanup_must_not_touch_child_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        child = self.root / "arsiv"
        child.mkdir(parents=True, exist_ok=True)
        (child / "eski.json").write_text("{}", encoding="ascii")
        self.make_store()
        self.assertTrue((child / "eski.json").is_file(), "temizleme alt dizini bosaltti")

    def test_a_half_written_temp_file_must_not_be_loaded_as_a_record(self) -> None:
        seed = self.make_store()
        job_id = seed.submit(
            "9d3f0001-0000-4000-8000-000000000005", "saglam", "duz_okuma"
        )[0]["job_id"]
        (self.root / f"{job_id}.json.tmp999-dead").write_text(
            json.dumps({"job_id": job_id, "state": "done"}), encoding="ascii"
        )
        reopened = self.make_store()
        self.assertEqual(
            reopened.get(job_id)["state"],
            jobs.STATE_QUEUED,
            "yarim yazilmis gecici dosya gercek kaydin uzerine yuklendi",
        )


if __name__ == "__main__":
    unittest.main()
