from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

KEY_ENV_NAME = "LLM_API_KEY"
RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)
BODY_SNIPPET = 200


class LlmError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def api_key() -> str:
    return os.environ.get(KEY_ENV_NAME, "").strip()


def extra_payload() -> dict[str, Any]:
    raw = os.environ.get("LLM_EXTRA_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _snippet(body: bytes) -> str:
    text = body.decode("utf-8", "replace").replace("\n", " ").strip()
    return text[:BODY_SNIPPET]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    return ""


def decode_reply(frame: Any) -> str:
    if not isinstance(frame, dict):
        raise LlmError("llm_error", "LLM cevabi bir nesne degil")
    choices = frame.get("choices")
    if not isinstance(choices, list):
        raise LlmError("llm_error", "LLM cevabinda 'choices' yok")
    if not choices:
        raise LlmError("llm_empty", "LLM cevabinda hic secenek yok")
    first = choices[0]
    if not isinstance(first, dict):
        raise LlmError("llm_error", "LLM cevabinda 'choices[0]' nesne degil")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LlmError("llm_error", "LLM cevabinda 'message' yok")
    text = _content_text(message.get("content"))
    if not text:
        raise LlmError("llm_empty", "LLM bos icerik dondurdu")
    return text


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (socket.timeout, TimeoutError))
    return False


def chat(
    prompt: str,
    settings: Any,
    system: str = "",
    api_key_value: str = "",
    check: Callable[[], None] | None = None,
) -> str:
    key = api_key_value or api_key()
    if not key:
        raise LlmError(
            "llm_unavailable",
            f"{KEY_ENV_NAME} tanimsiz",
        )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    extra = extra_payload()
    if extra:
        payload.update(extra)
    body = json.dumps(payload).encode("utf-8")
    url = f"{str(settings.llm_base_url).rstrip('/')}/chat/completions"
    attempts = max(1, int(settings.llm_attempts))
    timeout = float(settings.llm_timeout_secs)
    delay = float(settings.llm_retry_base_secs)
    last: LlmError | None = None
    for attempt in range(attempts):
        if check is not None:
            check()
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                frame = json.loads(response.read().decode("utf-8"))
            return decode_reply(frame)
        except urllib.error.HTTPError as exc:
            detail = f"HTTP {exc.code}"
            try:
                detail = f"HTTP {exc.code}: {_snippet(exc.read())}"
            except Exception:
                pass
            if exc.code not in RETRY_STATUS:
                raise LlmError("llm_error", detail) from exc
            last = LlmError("llm_error", detail)
        except (socket.timeout, TimeoutError) as exc:
            last = LlmError("llm_timeout", f"LLM zaman asimi ({timeout:g}s)")
        except urllib.error.URLError as exc:
            if _is_timeout(exc):
                last = LlmError("llm_timeout", f"LLM zaman asimi ({timeout:g}s)")
            else:
                last = LlmError("llm_unreachable", f"LLM adresine ulasilamadi: {exc.reason}")
        except json.JSONDecodeError as exc:
            raise LlmError("llm_error", f"LLM cevabi JSON degil: {exc}") from exc
        if attempt + 1 < attempts:
            time.sleep(delay)
            delay = min(delay * 2.0, float(settings.llm_retry_max_secs))
    if last is None:
        raise LlmError("llm_error", "LLM cagrisi tamamlanamadi")
    raise last


def ready(settings: Any) -> tuple[bool, str]:
    if not api_key():
        return False, f"{KEY_ENV_NAME} tanimsiz"
    return True, f"{settings.llm_model} @ {settings.llm_base_url}"
