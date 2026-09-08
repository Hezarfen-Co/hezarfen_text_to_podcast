from __future__ import annotations

import ast
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import config, jobs

SOURCE = Path(__file__).resolve().parent.parent.parent / "src" / "jobs.py"


def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _flag_writers() -> set[str]:
    tree = ast.parse(io.open(SOURCE, encoding="utf-8").read())
    owners = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            written = False
            if isinstance(inner, ast.Assign) and not _is_false(inner.value):
                for target in inner.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "cancel_requested"
                    ):
                        written = True
            if isinstance(inner, ast.Call):
                for keyword in inner.keywords:
                    if keyword.arg == "cancel_requested" and not _is_false(
                        keyword.value
                    ):
                        written = True
            if isinstance(inner, ast.Dict):
                for key, value in zip(inner.keys, inner.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "cancel_requested"
                        and not _is_false(value)
                    ):
                        written = True
            if written:
                owners.add(node.name)
    return owners


class CancelFlagOnQueuedIsUnreachable(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-guard-")
        self.store = jobs.JobStore(
            root=Path(self._tmp.name) / "isler", workers=1, max_jobs=64, stage_secs=0.0
        )

    def tearDown(self) -> None:
        self.store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def test_only_cancel_and_shutdown_may_raise_the_cancel_flag(self) -> None:
        self.assertEqual(
            _flag_writers(),
            {"cancel", "shutdown"},
            "cancel_requested bayragini yazan yeni bir fonksiyon eklenmis; "
            "JobStore.begin icindeki kuyruk korumasi artik olu kod olmayabilir, "
            "tests/model/test_model_jobstore.py modelini gozden gecir",
        )

    def test_cancelling_a_queued_job_leaves_it_cancelled_not_queued(self) -> None:
        record, _ = self.store.submit("01SOURCE", "duz_okuma")
        job_id = record["job_id"]
        self.assertTrue(self.store.cancel(job_id))
        after = self.store.get(job_id)
        self.assertEqual(after["state"], jobs.STATE_CANCELLED)
        self.assertTrue(after["cancel_requested"])

    def test_shutdown_only_flags_running_jobs(self) -> None:
        queued, _ = self.store.submit("01SOURCE", "duz_okuma")
        self.store.shutdown(timeout=1.0)
        after = self.store.get(queued["job_id"])
        self.assertEqual(after["state"], jobs.STATE_QUEUED)
        self.assertFalse(
            after["cancel_requested"],
            "shutdown kuyruktaki ise iptal bayragi koydu; begin korumasi canlandi",
        )

    STEPS = {
        "begin": jobs.STATE_RUNNING,
        "cancel": jobs.STATE_CANCELLED,
        "fail": jobs.STATE_FAILED,
        "shutdown": jobs.STATE_QUEUED,
    }

    def test_no_public_sequence_produces_a_queued_job_with_the_flag(self) -> None:
        for step, expected_state in self.STEPS.items():
            with self.subTest(step=step):
                store = jobs.JobStore(
                    root=Path(self._tmp.name) / step, workers=1,
                    max_jobs=64, stage_secs=0.0,
                )
                try:
                    record, _ = store.submit("01SOURCE", "duz_okuma")
                    job_id = record["job_id"]
                    if step == "begin":
                        store.begin(job_id)
                    elif step == "cancel":
                        store.cancel(job_id)
                    elif step == "fail":
                        store.fail(job_id, "test")
                    else:
                        store.shutdown(timeout=1.0)
                    after = store.get(job_id)
                    self.assertEqual(
                        after["state"], expected_state,
                        "%s adimi beklenen duruma goturmedi" % step,
                    )
                    self.assertFalse(
                        after["state"] == jobs.STATE_QUEUED
                        and after["cancel_requested"],
                        "queued + cancel_requested durumu uretildi; "
                        "begin icindeki koruma artik olu kod degil",
                    )
                finally:
                    store.shutdown(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
