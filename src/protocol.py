from __future__ import annotations

import asyncio
import json
import math
import struct
from typing import Any

PROTOCOL = "hab/2"
MAX_FRAME_BYTES = 8 * 1024 * 1024
PODCAST_REPORT_CAPABILITY = "podcast.report"
KEEPALIVE_SECS = 10
IDLE_TIMEOUT_SECS = 30.0
GREETING_TIMEOUT_SECS = 8.0
CERTIFICATE_PATH = "/ai/certificate"
DEADLINE_MARGIN_SECS = 0.25
API_TIMEOUT_SECS = 15.0


class CapabilityError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


PERMANENT_REJECTS = ("unsupported_protocol", "unauthorized")


class HandshakeRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"backend kaydi reddetti ({code}): {message}")
        self.code = code

    @property
    def permanent(self) -> bool:
        return self.code in PERMANENT_REJECTS


def encode_frame(obj: Any) -> bytes:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise CapabilityError("frame_too_large", f"{len(body)} bayt cerceve sinirini asiyor")
    return struct.pack(">I", len(body)) + body


class FrameStream:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._queue: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        self._eof = False

    def feed(self, data: bytes, end: bool) -> None:
        if data:
            self._queue.put_nowait(data)
        if end:
            self._queue.put_nowait(None)

    async def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            if self._eof:
                raise EOFError("cerceve tamamlanmadan akis bitti")
            chunk = await self._queue.get()
            if chunk is None:
                self._eof = True
                continue
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def read_frame(self) -> Any:
        header = await self._read_exact(4)
        (length,) = struct.unpack(">I", header)
        if length > MAX_FRAME_BYTES:
            raise CapabilityError("frame_too_large", f"{length} bayt cerceve sinirini asiyor")
        body = await self._read_exact(length)
        return json.loads(body)


def build_hello(
    service: str, capabilities: list[str], token: str, max_concurrent: int
) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "service": service,
        "capabilities": list(capabilities),
        "token": token,
        "max_concurrent": max_concurrent,
    }


def parse_greeting(greeting: Any) -> str:
    if not isinstance(greeting, dict):
        raise HandshakeRejected("malformed", "greeting bir nesne degil")
    if greeting.get("type") == "welcome":
        if greeting.get("protocol") != PROTOCOL:
            raise HandshakeRejected(
                "unsupported_protocol", f"beklenmeyen protokol: {greeting.get('protocol')}"
            )
        return str(greeting.get("worker_id", "?"))
    raise HandshakeRejected(
        str(greeting.get("code", "?")), str(greeting.get("message", ""))
    )


def ok_response(request_id: str, school: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "id": request_id, "school": school, "payload": payload}


def err_response(
    request_id: str, school: str, code: str, message: str
) -> dict[str, Any]:
    return {
        "status": "err",
        "id": request_id,
        "school": school,
        "code": code,
        "message": message,
    }


def parse_request(frame: Any) -> tuple[str, str, str, Any, float | None]:
    if not isinstance(frame, dict):
        raise CapabilityError("bad_request", "istek cercevesi bir nesne degil")
    request_id = frame.get("id")
    school = frame.get("school")
    capability = frame.get("capability")
    if not isinstance(request_id, str) or not isinstance(capability, str):
        raise CapabilityError("bad_request", "'id' ve 'capability' metin olmali")
    if not isinstance(school, str) or not school.strip():
        raise CapabilityError("bad_request", "'school' bos olmayan bir metin olmali")
    deadline_ms = frame.get("deadline_ms")
    timeout = None
    if isinstance(deadline_ms, (int, float)) and not isinstance(deadline_ms, bool):
        if deadline_ms > 0:
            budget_secs = deadline_ms / 1000.0
            timeout = max(budget_secs - DEADLINE_MARGIN_SECS, budget_secs * 0.5)
    return request_id, school, capability, frame.get("payload"), timeout

API_ALLOWLIST = (
    "/auth/me",
    "/users/me/profile",
    "/users/{id}/profile",
    "/notes",
    "/notes/{id}",
    "/homework",
    "/homework/{id}",
    "/homework/{id}/result",
    "/homework/{id}/submission",
    "/homework/report/{user}",
    "/marks/me",
    "/marks/{user}",
    "/attendance/me",
    "/attendance/{user}",
    "/pomodoro/me",
    "/pomodoro/{user}",
)


class ApiRefused(Exception):
    def __init__(
        self, code: str, message: str, school: str = "", request_id: str = ""
    ) -> None:
        super().__init__(f"kopru istegi reddetti ({code}): {message}")
        self.code = code
        self.school = school
        self.request_id = request_id


class ApiResponse:
    def __init__(self, request_id: str, school: str, status: int, body: Any) -> None:
        self.request_id = request_id
        self.school = school
        self.status = status
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def build_api_request(
    request_id: str,
    school: str,
    path: str,
    query: str | None = None,
    on_behalf_of: str | None = None,
    method: str = "GET",
) -> dict[str, Any]:
    if not isinstance(school, str) or not school.strip():
        raise ApiRefused("malformed", "okul slug'i zorunlu; varsayilan yok")
    if not isinstance(path, str) or not path.startswith("/") or "?" in path:
        raise ApiRefused(
            "bad_path",
            f"yol / ile baslamali ve ? icermemeli (query ayri alanda gider): {path!r}",
            school=school,
        )
    request: dict[str, Any] = {
        "id": request_id,
        "school": school,
        "path": path,
        "method": method,
    }
    if query:
        request["query"] = query.lstrip("?")
    if on_behalf_of:
        request["on_behalf_of"] = on_behalf_of
    return request


def build_capability_call(
    request_id: str, school: str, capability: str, payload: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(school, str) or not school.strip():
        raise CapabilityError("malformed", "okul slug'i zorunlu; varsayilan yok")
    if not isinstance(capability, str) or not capability.strip():
        raise CapabilityError("malformed", "yetenek adi zorunlu")
    return {
        "id": request_id,
        "school": school,
        "capability": capability,
        "payload": payload,
    }


def parse_capability_response(frame: Any, request_id: str) -> dict[str, Any]:
    if not isinstance(frame, dict):
        raise CapabilityError("malformed", "yetenek cevabi bir nesne degil")
    status_field = frame.get("status")
    if status_field == "ok":
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            raise CapabilityError("malformed", "yetenek cevabinda 'payload' nesne degil")
        return payload
    if status_field == "err":
        raise CapabilityError(
            str(frame.get("code", "?")), str(frame.get("message", ""))
        )
    raise CapabilityError("malformed", f"bilinmeyen cevap durumu: {status_field!r}")


def build_upload_request(
    request_id: str,
    school: str,
    job_id: str,
    name: str,
    content_type: str,
    size: int,
    duration_secs: float | None = None,
) -> dict[str, Any]:
    if not isinstance(school, str) or not school.strip():
        raise CapabilityError("malformed", "okul slug'i zorunlu; varsayilan yok")
    request: dict[str, Any] = {
        "id": request_id,
        "upload": True,
        "school": school,
        "job_id": job_id,
        "name": name,
        "content_type": content_type,
        "size": int(size),
    }
    if duration_secs is not None:
        request["duration_secs"] = float(duration_secs)
    return request


def parse_upload_response(frame: Any) -> dict[str, Any]:
    if not isinstance(frame, dict):
        raise CapabilityError("malformed", "yukleme cevabi bir nesne degil")
    status_field = frame.get("status")
    if status_field == "ok":
        key = frame.get("key")
        if not isinstance(key, str) or not key:
            raise CapabilityError("malformed", "yukleme cevabinda 'key' yok")
        return frame
    if status_field == "err":
        raise CapabilityError(
            str(frame.get("code", "?")), str(frame.get("message", ""))
        )
    raise CapabilityError("malformed", f"bilinmeyen yukleme durumu: {status_field!r}")


def _coerce_status(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ApiRefused("malformed", f"status sayisal degil: {value!r}")
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ApiRefused("malformed", f"status sayisal degil: {value!r}") from exc
    raise ApiRefused("malformed", f"status sayisal degil: {value!r}")


def parse_api_response(frame: Any) -> ApiResponse:
    if not isinstance(frame, dict):
        raise ApiRefused("malformed", "api cevabi bir nesne degil")
    outcome = frame.get("outcome")
    school = str(frame.get("school", ""))
    request_id = str(frame.get("id", ""))
    if outcome == "err":
        raise ApiRefused(
            str(frame.get("code", "?")),
            str(frame.get("message", "")),
            school=school,
            request_id=request_id,
        )
    if outcome != "ok":
        raise ApiRefused("malformed", f"bilinmeyen outcome: {outcome!r}", school=school)
    return ApiResponse(
        request_id,
        school,
        _coerce_status(frame.get("status", 0)),
        frame.get("body"),
    )
