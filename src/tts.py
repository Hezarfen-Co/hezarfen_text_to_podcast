from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

KEY_ENV_NAME = "ELEVENLABS_API_KEY"
VOICE_ENV_NAME = "ELEVENLABS_VOICE_ID"
BODY_SNIPPET = 200
JSON_PREFIX = b"{"


class TtsError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def api_key() -> str:
    return os.environ.get(KEY_ENV_NAME, "").strip()


def _snippet(body: bytes) -> str:
    text = body.decode("utf-8", "replace").replace("\n", " ").strip()
    return text[:BODY_SNIPPET]


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return isinstance(exc.reason, (socket.timeout, TimeoutError))
    return False


def synthesize(
    text: str,
    settings: Any,
    api_key_value: str = "",
) -> bytes:
    key = api_key_value or api_key()
    voice = str(getattr(settings, "elevenlabs_voice_id", "")).strip()
    if not key:
        raise TtsError("tts_key_missing", f"{KEY_ENV_NAME} tanimsiz; bulut TTS cagrilamaz")
    if not voice:
        raise TtsError(
            "tts_voice_missing",
            f"{VOICE_ENV_NAME} tanimsiz; hangi sesin kullanilacagi belli degil",
        )
    query = urllib.parse.urlencode(
        {"output_format": settings.elevenlabs_output_format}
    )
    url = (
        f"{str(settings.elevenlabs_base_url).rstrip('/')}"
        f"/v1/text-to-speech/{urllib.parse.quote(voice)}?{query}"
    )
    payload = {
        "text": text,
        "model_id": settings.elevenlabs_model,
        "language_code": settings.elevenlabs_language,
        "voice_settings": {"stability": 0.85, "similarity_boost": 0.85, "style": 0.0},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "xi-api-key": key},
        method="POST",
    )
    timeout = float(settings.elevenlabs_timeout_secs)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            audio = response.read()
    except urllib.error.HTTPError as exc:
        detail = f"HTTP {exc.code}"
        try:
            detail = f"HTTP {exc.code}: {_snippet(exc.read())}"
        except Exception:
            pass
        raise TtsError("tts_error", detail) from exc
    except urllib.error.URLError as exc:
        if _is_timeout(exc):
            raise TtsError("tts_timeout", f"ElevenLabs zaman asimi ({timeout:g}s)") from exc
        raise TtsError(
            "tts_unreachable", f"ElevenLabs adresine ulasilamadi: {exc.reason}"
        ) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise TtsError("tts_timeout", f"ElevenLabs zaman asimi ({timeout:g}s)") from exc
    if not audio:
        raise TtsError("tts_bad_audio", "ElevenLabs bos govde dondurdu")
    if audio.lstrip()[:1] == JSON_PREFIX:
        raise TtsError(
            "tts_bad_audio",
            f"ElevenLabs ses yerine JSON dondurdu: {_snippet(audio)}",
        )
    return audio


def ready(settings: Any) -> tuple[bool, str]:
    voice = str(getattr(settings, "elevenlabs_voice_id", "")).strip()
    if not api_key():
        return False, f"{KEY_ENV_NAME} tanimsiz"
    if not voice:
        return False, f"{VOICE_ENV_NAME} tanimsiz"
    return True, f"{settings.elevenlabs_model} ses={voice[:8]}"
