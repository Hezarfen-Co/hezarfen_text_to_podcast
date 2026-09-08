from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from src import jobs


def _record(job_id, state, updated_at, audio=None, script=None):
    return {
        "job_id": job_id,
        "source_id": "kaynak.pdf",
        "format": "duz_okuma",
        "state": state,
        "stage": "done",
        "progress": 1.0,
        "error_code": None,
        "cancel_requested": False,
        "audio_id": (audio or [""])[0] if audio else None,
        "duration_secs": 1.0,
        "script_id": (script or [""])[0] if script else None,
        "created_at": updated_at,
        "updated_at": updated_at,
        "audio_ids": audio or [],
        "script_ids": script or [],
    }


class RetentionDeletesExpiredJobs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.job_root = self.root / "jobs"
        self.output = self.root / "out"
        self.job_root.mkdir()
        self.output.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, record, outputs=()):
        (self.job_root / f"{record['job_id']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )
        for rel in outputs:
            path = self.output / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * 16)

    def _store(self, days):
        return jobs.JobStore(
            root=self.job_root, retention_days=days, output_root=self.output
        )

    def test_nothing_is_deleted_while_retention_is_off(self):
        old = jobs._now_ms() - 400 * jobs.DAY_MS
        self._write(_record("ESKI", jobs.STATE_DONE, old, ["a/x.mp3"]), ["a/x.mp3"])
        store = self._store(0.0)
        self.assertEqual(store.purged, 0, "retention kapaliyken is silindi")
        self.assertTrue((self.output / "a/x.mp3").exists())

    def test_expired_finished_job_and_its_outputs_are_deleted(self):
        old = jobs._now_ms() - 40 * jobs.DAY_MS
        self._write(
            _record("ESKI", jobs.STATE_DONE, old, ["a/x.mp3"], ["a/x.script.json"]),
            ["a/x.mp3", "a/x.script.json"],
        )
        store = self._store(30.0)
        self.assertEqual(store.purged, 1)
        self.assertFalse((self.job_root / "ESKI.json").exists(), "is dosyasi kaldi")
        self.assertFalse((self.output / "a/x.mp3").exists(), "ses dosyasi kaldi")
        self.assertFalse((self.output / "a/x.script.json").exists(), "script kaldi")

    def test_a_fresh_finished_job_is_PRESERVED(self):
        fresh = jobs._now_ms() - 2 * jobs.DAY_MS
        self._write(_record("TAZE", jobs.STATE_DONE, fresh, ["a/y.mp3"]), ["a/y.mp3"])
        store = self._store(30.0)
        self.assertEqual(store.purged, 0)
        self.assertTrue((self.output / "a/y.mp3").exists())

    def test_an_unfinished_job_is_preserved_REGARDLESS_OF_ITS_AGE(self):
        old = jobs._now_ms() - 400 * jobs.DAY_MS
        for state in (jobs.STATE_QUEUED, jobs.STATE_RUNNING):
            with self.subTest(state=state):
                root = Path(tempfile.mkdtemp())
                (root / f"IS.json").write_text(
                    json.dumps(_record("IS", state, old)), encoding="utf-8"
                )
                store = jobs.JobStore(root=root, retention_days=1.0, output_root=self.output)
                self.assertEqual(store.purged, 0, f"{state} durumundaki is silindi")
                self.assertTrue((root / "IS.json").exists())

    def test_cancelled_and_failed_jobs_are_also_deleted_when_they_expire(self):
        old = jobs._now_ms() - 40 * jobs.DAY_MS
        self._write(_record("A", jobs.STATE_FAILED, old))
        self._write(_record("B", jobs.STATE_CANCELLED, old))
        store = self._store(30.0)
        self.assertEqual(store.purged, 2)


class RetentionGuardsAgainstPathTraversal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.job_root = self.root / "jobs"
        self.output = self.root / "out"
        self.outside = self.root / "disarida"
        self.job_root.mkdir()
        self.output.mkdir()
        self.outside.mkdir()
        self.victim = self.outside / "dokunulmaz.txt"
        self.victim.write_bytes(b"KORUNMALI")

    def tearDown(self):
        self._tmp.cleanup()

    def test_identifier_pointing_outside_the_output_root_is_not_deleted(self):
        old = jobs._now_ms() - 40 * jobs.DAY_MS
        bad = "../disarida/dokunulmaz.txt"
        (self.job_root / "KOTU.json").write_text(
            json.dumps(_record("KOTU", jobs.STATE_DONE, old, [bad])), encoding="utf-8"
        )
        store = jobs.JobStore(root=self.job_root, retention_days=30.0, output_root=self.output)
        self.assertEqual(store.purged, 1, "is kaydi yine de temizlenmeli")
        self.assertTrue(
            self.victim.exists(),
            "cikti kokunun DISINDAKI dosya silindi - yol kacisi korumasi calismadi",
        )

    def test_absolute_paths_and_drive_letters_are_rejected(self):
        store = jobs.JobStore(root=self.job_root, retention_days=30.0, output_root=self.output)
        for bad in ("/etc/passwd", r"\\sunucu\pay", "C:/Windows/x", "..", ".", ""):
            with self.subTest(identifier=bad):
                self.assertIsNone(
                    store._safe_output_path(bad), f"guvensiz kimlik kabul edildi: {bad!r}"
                )

    def test_a_normal_identifier_under_the_root_is_accepted(self):
        store = jobs.JobStore(root=self.job_root, retention_days=30.0, output_root=self.output)
        path = store._safe_output_path("ders/ses/duz_okuma/ders-b01.mp3")
        self.assertIsNotNone(path)
        self.assertTrue(str(path).startswith(str(self.output.resolve())))

    def test_no_file_is_deleted_when_no_output_root_is_given(self):
        old = jobs._now_ms() - 40 * jobs.DAY_MS
        (self.job_root / "X.json").write_text(
            json.dumps(_record("X", jobs.STATE_DONE, old, ["a/x.mp3"])), encoding="utf-8"
        )
        store = jobs.JobStore(root=self.job_root, retention_days=30.0, output_root=None)
        self.assertIsNone(store._safe_output_path("a/x.mp3"))
        self.assertEqual(store.purged, 1)


if __name__ == "__main__":
    unittest.main()
