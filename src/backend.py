from __future__ import annotations

import asyncio
import threading
from typing import Any

from . import config, protocol

REPORT_TIMEOUT_SECS = 15.0
UPLOAD_TIMEOUT_SECS = 120.0
AUDIO_CONTENT_TYPE = "audio/mpeg"

_lock = threading.Lock()
_client: "BackendClient | None" = None


class BackendUnavailable(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(f"backend baglantisi yok: {message}")


class BackendRefused(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"backend reddetti ({code}): {message}")
        self.code = code


class BackendClient:
    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: Any) -> None:
        self._loop = loop
        self._protocol = protocol

    def _call(self, coro: Any, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            future.cancel()
            raise BackendUnavailable(f"{timeout:g}s icinde cevap gelmedi") from exc
        except Exception as exc:
            raise BackendUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def report(self, school: str, record: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "job_id": record["job_id"],
            "source_id": record["source_id"],
            "format": record["format"],
            "user_id": record["user_id"],
            "state": record["state"],
            "stage": record["stage"],
            "progress": record["progress"],
            "error_code": record["error_code"],
        }
        return self._call(
            self._protocol.call_capability(school, protocol.PODCAST_REPORT_CAPABILITY, payload),
            REPORT_TIMEOUT_SECS,
        )

    def upload(
        self,
        school: str,
        record: dict[str, Any],
        path: Any,
        content_type: str = AUDIO_CONTENT_TYPE,
    ) -> dict[str, Any]:
        data = path.read_bytes()
        return self._call(
            self._protocol.upload_blob(
                school,
                record["job_id"],
                path.name,
                content_type,
                data,
                record.get("duration_secs"),
            ),
            UPLOAD_TIMEOUT_SECS,
        )


def set_client(loop: asyncio.AbstractEventLoop, protocol: Any) -> None:
    global _client
    with _lock:
        _client = BackendClient(loop, protocol)


def clear_client() -> None:
    global _client
    with _lock:
        _client = None


def current() -> "BackendClient | None":
    with _lock:
        return _client


def report(school: str, record: dict[str, Any]) -> dict[str, Any]:
    client = current()
    if client is None:
        raise BackendUnavailable("kayitli bir kopru baglantisi yok")
    return client.report(school, record)


def upload(
    school: str,
    record: dict[str, Any],
    path: Any,
    content_type: str = AUDIO_CONTENT_TYPE,
) -> dict[str, Any]:
    client = current()
    if client is None:
        raise BackendUnavailable("kayitli bir kopru baglantisi yok")
    return client.upload(school, record, path, content_type)


def ready() -> bool:
    return current() is not None


def log(message: str) -> None:
    config.log("debug", message)
