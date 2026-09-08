from __future__ import annotations

import json
import math
import os
import queue
import re
import secrets
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Callable

from . import config

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

STATES = (STATE_QUEUED, STATE_RUNNING, STATE_DONE, STATE_FAILED, STATE_CANCELLED)
TERMINAL_STATES = (STATE_DONE, STATE_FAILED, STATE_CANCELLED)
DAY_MS = 86_400_000

TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATE_QUEUED: (STATE_RUNNING, STATE_CANCELLED, STATE_FAILED),
    STATE_RUNNING: (STATE_DONE, STATE_FAILED, STATE_CANCELLED),
    STATE_DONE: (),
    STATE_FAILED: (),
    STATE_CANCELLED: (),
}

SIMULATED_STAGES = ("ocr", "plan", "script", "tts", "mux")
INTERRUPTED_CODE = "interrupted"

REQUIRED_FIELDS = (
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
    "created_at",
    "updated_at",
)

SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

SHUTDOWN_TIMEOUT_SECS = 10.0

STATUS_FILE = ".bridge-status"

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
JOB_ID_LENGTH = 26


class JobError(Exception):
    pass


class JobNotFound(JobError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"is bulunamadi: {job_id}")
        self.job_id = job_id


class JobStoreFull(JobError):
    def __init__(self, active: int, limit: int) -> None:
        super().__init__(f"is kuyrugu dolu: {active}/{limit} is beklemede veya kosuyor")
        self.active = active
        self.limit = limit


class InvalidTransition(JobError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"gecersiz durum gecisi: {current} -> {target}")
        self.current = current
        self.target = target


class JobCancelled(JobError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"is iptal edildi: {job_id}")
        self.job_id = job_id


def new_job_id() -> str:
    value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    chars = []
    for _ in range(JOB_ID_LENGTH):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _id_list(values: list[str] | None, fallback: str) -> list[str]:
    if values:
        return [str(value) for value in values]
    return [str(fallback)] if fallback else []


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, ())


def resolve_source(media_root: str | os.PathLike, source_id: str) -> Path:
    if not isinstance(source_id, str) or SAFE_SOURCE_ID.match(source_id) is None:
        raise ValueError(f"gecersiz source_id: {source_id!r}")
    root = Path(media_root).resolve()
    candidate = (root / source_id).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"source_id medya kokunun disina cikiyor: {source_id!r}")
    return candidate


def status_path(root: str | os.PathLike) -> Path:
    return Path(root) / STATUS_FILE


def write_status(root: str | os.PathLike, registered: bool, worker_id: str = "") -> None:
    path = status_path(root)
    record = {
        "registered": bool(registered),
        "worker_id": str(worker_id),
        "pid": os.getpid(),
        "updated_at": _now_ms(),
    }
    tmp = path.parent / f"{path.name}.tmp{os.getpid()}-{secrets.token_hex(4)}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        config.log("warn", f"kopru durum dosyasi yazilamadi: {exc}")
        try:
            tmp.unlink()
        except OSError:
            pass


def read_status(root: str | os.PathLike) -> dict[str, Any] | None:
    try:
        with open(status_path(root), "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


class JobContext:
    def __init__(self, store: "JobStore", job_id: str) -> None:
        record = store.get(job_id)
        self.store = store
        self.job_id = job_id
        self.source_id = record["source_id"]
        self.format = record["format"]
        self.stages = store.stages

    def cancelled(self) -> bool:
        if self.store.stopping:
            return True
        return self.store.cancel_requested(self.job_id)

    def check(self) -> None:
        if self.cancelled():
            raise JobCancelled(self.job_id)

    def progress(self, stage: str, value: float) -> dict[str, Any] | None:
        return self.store.update_progress(self.job_id, stage, value)

    def finish(
        self,
        audio_id: str,
        duration_secs: float,
        script_id: str,
        audio_ids: list[str] | None = None,
        script_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.store.finish(
            self.job_id,
            audio_id=audio_id,
            duration_secs=duration_secs,
            script_id=script_id,
            audio_ids=audio_ids,
            script_ids=script_ids,
        )


def _simulate_pipeline(ctx: JobContext) -> None:
    stages = ctx.stages
    total = len(stages)
    for index, stage in enumerate(stages):
        ctx.check()
        ctx.progress(stage, index / total)
        if ctx.store.stage_secs > 0:
            time.sleep(ctx.store.stage_secs)
    ctx.check()
    suffix = ctx.job_id[-10:].lower()
    checksum = zlib.crc32(ctx.job_id.encode("ascii"))
    record = ctx.finish(
        audio_id=f"sim_audio_{suffix}",
        duration_secs=round(240.0 + (checksum % 12000) / 100.0, 2),
        script_id=f"sim_script_{suffix}",
    )
    if record["state"] == STATE_DONE:
        config.log("info", f"is {ctx.job_id} tamamlandi (sahte hat)")
    else:
        config.log("info", f"is {ctx.job_id} bitis aninda iptal edildi")


class JobStore:
    def __init__(
        self,
        root: str | os.PathLike,
        workers: int = 1,
        max_jobs: int = 8,
        stage_secs: float = 0.2,
        runner: Callable[[JobContext], None] | None = None,
        stages: tuple[str, ...] | None = None,
        job_secs: float | None = None,
        retention_days: float = 0.0,
        output_root: str | os.PathLike | None = None,
    ) -> None:
        self.root = Path(root)
        self.workers = max(1, int(workers))
        self.max_jobs = max(1, int(max_jobs))
        self.stage_secs = float(stage_secs)
        self.stages = tuple(stages) if stages else SIMULATED_STAGES
        self.job_secs = (
            float(job_secs) if job_secs else max(len(self.stages) * self.stage_secs, 1.0)
        )
        self.retention_days = max(0.0, float(retention_days))
        self.output_root = Path(output_root) if output_root else None
        self._runner = runner or _simulate_pipeline
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._queue: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self.root.mkdir(parents=True, exist_ok=True)
        self.swept = self._load()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def _path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def _fsync_dir(self) -> None:
        try:
            handle = os.open(self.root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(handle)
        except OSError:
            pass
        finally:
            os.close(handle)

    def _write(self, record: dict[str, Any]) -> None:
        path = self._path(record["job_id"])
        tmp = path.parent / f"{path.name}.tmp{os.getpid()}-{secrets.token_hex(4)}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        self._fsync_dir()

    def _commit(self, record: dict[str, Any], candidate: dict[str, Any]) -> None:
        self._write(candidate)
        record.clear()
        record.update(candidate)

    def _purge_temporaries(self) -> None:
        residues = sorted(self.root.glob("*.json.tmp*")) + sorted(
            self.root.glob(f"{STATUS_FILE}.tmp*")
        )
        for path in residues:
            try:
                path.unlink()
            except OSError as exc:
                config.log("warn", f"gecici is dosyasi silinemedi: {path.name} ({exc})")

    def _safe_output_path(self, identifier: Any) -> Path | None:
        if self.output_root is None:
            return None
        if not isinstance(identifier, str) or not identifier.strip():
            return None
        if identifier.startswith(("/", "\\")) or ":" in identifier:
            return None
        try:
            root = self.output_root.resolve()
            target = (root / identifier).resolve()
        except OSError:
            return None
        if target == root or root not in target.parents:
            return None
        return target

    def _purge_expired(self) -> int:
        if self.retention_days <= 0:
            return 0
        cutoff = _now_ms() - int(self.retention_days * DAY_MS)
        removed = 0
        for job_id, record in list(self._jobs.items()):
            if record.get("state") not in TERMINAL_STATES:
                continue
            try:
                updated = int(record.get("updated_at") or 0)
            except (TypeError, ValueError):
                continue
            if updated >= cutoff:
                continue
            for field in ("audio_ids", "script_ids"):
                for identifier in (record.get(field) or []):
                    path = self._safe_output_path(identifier)
                    if path is None:
                        config.log(
                            "warn",
                            f"is {job_id}: cikti kimligi cikti kokunun disinda, "
                            f"SILINMEDI: {identifier!r}",
                        )
                        continue
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as exc:
                        config.log("warn", f"cikti silinemedi {path.name}: {exc}")
            try:
                self._path(job_id).unlink(missing_ok=True)
            except OSError as exc:
                config.log("warn", f"is dosyasi silinemedi {job_id}: {exc}")
                continue
            self._jobs.pop(job_id, None)
            removed += 1
        if removed:
            config.log(
                "info",
                f"retention: {self.retention_days:g} gunden eski {removed} bitmis is "
                f"ve ciktilari silindi",
            )
        return removed

    def _load(self) -> int:
        self._purge_temporaries()
        swept = 0
        for path in sorted(self.root.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, ValueError) as exc:
                config.log("warn", f"is dosyasi okunamadi, atlandi: {path.name} ({exc})")
                continue
            if not isinstance(record, dict):
                continue
            job_id = record.get("job_id")
            if not isinstance(job_id, str) or job_id != path.stem:
                config.log("warn", f"is dosyasi kimligi adiyla uyusmuyor, atlandi: {path.name}")
                continue
            missing = [name for name in REQUIRED_FIELDS if name not in record]
            if missing:
                config.log(
                    "warn",
                    f"is dosyasinda zorunlu alan eksik, atlandi: {path.name} "
                    f"({', '.join(missing)})",
                )
                continue
            if record["state"] not in STATES:
                config.log("warn", f"is dosyasi gecersiz durumda, atlandi: {path.name}")
                continue
            if record["state"] == STATE_RUNNING:
                candidate = dict(record)
                candidate["state"] = STATE_FAILED
                candidate["error_code"] = INTERRUPTED_CODE
                candidate["updated_at"] = _now_ms()
                try:
                    self._commit(record, candidate)
                except OSError as exc:
                    config.log("error", f"supurme yazilamadi {job_id}: {exc}")
                else:
                    swept += 1
            self._jobs[job_id] = record
        self.purged = self._purge_expired()
        return swept

    def start(self) -> int:
        with self._lock:
            if not self._threads:
                self._stopping.clear()
                for index in range(self.workers):
                    thread = threading.Thread(
                        target=self._worker_loop,
                        name=f"podcast-job-{index}",
                        daemon=True,
                    )
                    self._threads.append(thread)
                    thread.start()
            pending = [
                record["job_id"]
                for record in self._jobs.values()
                if record["state"] == STATE_QUEUED
            ]
        for job_id in pending:
            self._dispatch(job_id)
        return len(pending)

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT_SECS) -> None:
        self._stopping.set()
        with self._lock:
            threads = list(self._threads)
            self._threads.clear()
            running = [
                record
                for record in self._jobs.values()
                if record["state"] == STATE_RUNNING and not record["cancel_requested"]
            ]
            for record in running:
                candidate = dict(record)
                candidate["cancel_requested"] = True
                candidate["updated_at"] = _now_ms()
                try:
                    self._commit(record, candidate)
                except OSError as exc:
                    config.log(
                        "warn",
                        f"kapanista iptal bayragi yazilamadi {record['job_id']}: {exc}",
                    )
        for _ in threads:
            self._queue.put(None)
        deadline = time.monotonic() + max(0.0, float(timeout))
        stuck = 0
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                stuck += 1
        if stuck:
            config.log(
                "warn",
                f"kapanista {stuck} isci thread {timeout}s icinde bitmedi; "
                f"daemon olduklari icin surec yine de cikabiliyor",
            )

    def _dispatch(self, job_id: str) -> None:
        self._queue.put(job_id)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            try:
                self._work(job_id)
            except Exception as exc:
                config.log("error", f"isci dongusu {job_id} isini isleyemedi: {exc}")

    def _work(self, job_id: str) -> None:
        try:
            if not self.begin(job_id):
                return
            self._runner(JobContext(self, job_id))
        except JobCancelled:
            try:
                self.abort(job_id)
                config.log("info", f"is {job_id} iptal edildi")
            except JobError:
                pass
        except Exception as exc:
            config.log("error", f"is {job_id} beklenmedik sekilde bitti: {exc}")
            try:
                self.fail(job_id, "internal")
            except JobError as inner:
                config.log("warn", f"is {job_id} hata durumuna alinamadi: {inner}")

    def _active_count(self) -> int:
        return sum(
            1
            for record in self._jobs.values()
            if record["state"] in (STATE_QUEUED, STATE_RUNNING)
        )

    def estimate_eta(self, pending: int) -> int:
        waves = max(1, math.ceil(max(pending, 1) / self.workers))
        return int(math.ceil(self.job_secs * waves))

    def submit(self, source_id: str, job_format: str) -> tuple[dict[str, Any], int]:
        now = _now_ms()
        with self._lock:
            active = self._active_count()
            if active >= self.max_jobs:
                raise JobStoreFull(active, self.max_jobs)
            job_id = new_job_id()
            record = {
                "job_id": job_id,
                "source_id": source_id,
                "format": job_format,
                "state": STATE_QUEUED,
                "stage": "",
                "progress": 0.0,
                "error_code": None,
                "cancel_requested": False,
                "audio_id": None,
                "duration_secs": None,
                "script_id": None,
                "audio_ids": [],
                "script_ids": [],
                "created_at": now,
                "updated_at": now,
            }
            self._write(record)
            self._jobs[job_id] = record
            eta = self.estimate_eta(active + 1)
            snapshot = dict(record)
        config.log("info", f"is {job_id} kuyruga alindi (format={job_format} eta={eta}s)")
        self._dispatch(job_id)
        return snapshot, eta

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            return dict(record)

    def transition(self, job_id: str, target: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            current = record["state"]
            if not can_transition(current, target):
                raise InvalidTransition(current, target)
            candidate = dict(record)
            candidate["state"] = target
            candidate.update(fields)
            candidate["updated_at"] = _now_ms()
            self._commit(record, candidate)
            return dict(record)

    def update_progress(
        self, job_id: str, stage: str, progress: float
    ) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            if record["state"] != STATE_RUNNING:
                return None
            candidate = dict(record)
            candidate["stage"] = stage
            candidate["progress"] = round(min(max(float(progress), 0.0), 1.0), 4)
            candidate["updated_at"] = _now_ms()
            self._commit(record, candidate)
            return dict(record)

    def begin(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record["state"] != STATE_QUEUED:
                return False
            if self.stopping:
                return False
            if record["cancel_requested"]:
                self.transition(job_id, STATE_CANCELLED)
                config.log("info", f"is {job_id} baslamadan iptal edildi")
                return False
            self.transition(job_id, STATE_RUNNING, stage=self.stages[0], progress=0.0)
            return True

    def cancel_requested(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            return bool(record["cancel_requested"])

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            state = record["state"]
            if state == STATE_QUEUED:
                self.transition(job_id, STATE_CANCELLED, cancel_requested=True, stage="")
                config.log("info", f"is {job_id} kuyruktan iptal edildi")
                return True
            if state == STATE_RUNNING:
                if not record["cancel_requested"]:
                    candidate = dict(record)
                    candidate["cancel_requested"] = True
                    candidate["updated_at"] = _now_ms()
                    self._commit(record, candidate)
                    config.log("info", f"is {job_id} icin iptal bayragi kondu")
                return True
            return False

    def abort(self, job_id: str) -> dict[str, Any]:
        return self.transition(job_id, STATE_CANCELLED)

    def fail(self, job_id: str, error_code: str) -> dict[str, Any]:
        return self.transition(job_id, STATE_FAILED, error_code=error_code)

    def finish(
        self,
        job_id: str,
        audio_id: str,
        duration_secs: float,
        script_id: str,
        audio_ids: list[str] | None = None,
        script_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFound(job_id)
            if record["cancel_requested"]:
                config.log("info", f"is {job_id} bitiste iptal bayragini gordu")
                return self.transition(job_id, STATE_CANCELLED)
            return self.transition(
                job_id,
                STATE_DONE,
                stage="done",
                progress=1.0,
                error_code=None,
                audio_id=audio_id,
                duration_secs=float(duration_secs),
                script_id=script_id,
                audio_ids=_id_list(audio_ids, audio_id),
                script_ids=_id_list(script_ids, script_id),
            )
