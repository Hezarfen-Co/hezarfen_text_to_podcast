from __future__ import annotations

import asyncio
import json
import math
import struct
from typing import Any

PROTOCOL = "hab/1"
MAX_FRAME_BYTES = 8 * 1024 * 1024
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


class HandshakeRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"backend kaydi reddetti ({code}): {message}")
        self.code = code


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


def ok_response(request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "id": request_id, "payload": payload}


def err_response(request_id: str, code: str, message: str) -> dict[str, Any]:
    return {"status": "err", "id": request_id, "code": code, "message": message}


def parse_request(frame: Any) -> tuple[str, str, Any, float | None]:
    if not isinstance(frame, dict):
        raise CapabilityError("bad_request", "istek cercevesi bir nesne degil")
    request_id = frame.get("id")
    capability = frame.get("capability")
    if not isinstance(request_id, str) or not isinstance(capability, str):
        raise CapabilityError("bad_request", "'id' ve 'capability' metin olmali")
    deadline_ms = frame.get("deadline_ms")
    timeout = None
    if isinstance(deadline_ms, (int, float)) and not isinstance(deadline_ms, bool):
        if deadline_ms > 0:
            budget_secs = deadline_ms / 1000.0
            timeout = max(budget_secs - DEADLINE_MARGIN_SECS, budget_secs * 0.5)
    return request_id, capability, frame.get("payload"), timeout

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
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"kopru istegi reddetti ({code}): {message}")
        self.code = code


class ApiResponse:
    def __init__(self, request_id: str, status: int, body: Any) -> None:
        self.request_id = request_id
        self.status = status
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def build_api_request(
    request_id: str,
    path: str,
    query: str | None = None,
    on_behalf_of: str | None = None,
    method: str = "GET",
) -> dict[str, Any]:
    if not isinstance(path, str) or not path.startswith("/") or "?" in path:
        raise ApiRefused(
            "bad_path",
            f"yol / ile baslamali ve ? icermemeli (query ayri alanda gider): {path!r}",
        )
    request: dict[str, Any] = {"id": request_id, "path": path, "method": method}
    if query:
        request["query"] = query.lstrip("?")
    if on_behalf_of:
        request["on_behalf_of"] = on_behalf_of
    return request


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
    if outcome == "err":
        raise ApiRefused(
            str(frame.get("code", "?")), str(frame.get("message", ""))
        )
    if outcome != "ok":
        raise ApiRefused("malformed", f"bilinmeyen outcome: {outcome!r}")
    return ApiResponse(
        str(frame.get("id", "")),
        _coerce_status(frame.get("status", 0)),
        frame.get("body"),
    )
