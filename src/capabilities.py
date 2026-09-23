from __future__ import annotations

from typing import Any, Callable

from . import jobs, protocol
from .protocol import CapabilityError

Handler = Callable[[str, dict], dict]

FORMATS = ("tek_ogretici", "ogrenci_hoca", "duz_okuma")
DEFAULT_FORMAT = "duz_okuma"
LLM_FORMATS = ("tek_ogretici", "ogrenci_hoca")
KEYLESS_FORMATS = tuple(name for name in FORMATS if name not in LLM_FORMATS)
LLM_KEY_NAME = "LLM_API_KEY"

_store: jobs.JobStore | None = None
_llm_ready = False


def configure(store: jobs.JobStore | None, llm_ready: bool) -> None:
    global _store, _llm_ready
    _store = store
    _llm_ready = bool(llm_ready)


def allowed_formats(llm_ready: bool) -> tuple[str, ...]:
    return FORMATS if llm_ready else KEYLESS_FORMATS


def format_summary(llm_ready: bool) -> str:
    enabled = ", ".join(allowed_formats(llm_ready))
    if llm_ready:
        return f"acik formatlar: {enabled} ({LLM_KEY_NAME} tanimli)"
    disabled = ", ".join(LLM_FORMATS)
    return f"acik formatlar: {enabled} ({LLM_KEY_NAME} tanimsiz; {disabled} kapali)"


def _active_store() -> jobs.JobStore:
    if _store is None:
        raise CapabilityError("internal", "is deposu hazir degil")
    return _store


def _require_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError("bad_request", f"'{key}' bos olmayan bir metin olmali")
    return value.strip()


def _optional_choice(payload: dict, key: str, choices: tuple, default: str) -> str:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise CapabilityError("bad_request", f"'{key}' bir metin olmali")
    normalized = value.strip().lower()
    if normalized not in choices:
        raise CapabilityError(
            "bad_request", f"'{key}' su degerlerden biri olmali: {', '.join(choices)}"
        )
    return normalized


MAX_SOURCES = 20


def _source_keys(payload: dict, first: str) -> list[str]:
    raw = payload.get("source_keys")
    if raw is None:
        return [first]
    if not isinstance(raw, list):
        raise CapabilityError("bad_request", "'source_keys' bir liste olmali")
    keys: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise CapabilityError(
                "bad_request", "'source_keys' bos olmayan metinlerden olusmali"
            )
        keys.append(item.strip())
    if not keys:
        raise CapabilityError("bad_request", "'source_keys' bos olamaz")
    if len(keys) > MAX_SOURCES:
        raise CapabilityError(
            "bad_request",
            f"tek iste en fazla {MAX_SOURCES} kaynak birlestirilebilir; "
            f"{len(keys)} geldi",
        )
    if keys[0] != first:
        raise CapabilityError(
            "bad_request",
            "'source_keys' ilk ogesi 'source_key' ile ayni olmali",
        )
    if len(set(keys)) != len(keys):
        raise CapabilityError("bad_request", "'source_keys' ayni kaynagi tekrar ediyor")
    return keys


def submit(school: str, payload: dict) -> dict:
    job_id = _require_text(payload, "job_id")
    source_id = _require_text(payload, "source_id")
    source_key = _require_text(payload, "source_key")
    source_keys = _source_keys(payload, source_key)
    user_id = _require_text(payload, "user_id")
    job_format = _optional_choice(payload, "format", FORMATS, DEFAULT_FORMAT)
    if job_format in LLM_FORMATS and not _llm_ready:
        raise CapabilityError(
            "llm_unavailable",
            f"'{job_format}' formati LLM anahtari gerektiriyor ama {LLM_KEY_NAME} "
            f"tanimsiz; su an kullanilabilir formatlar: {', '.join(KEYLESS_FORMATS)}",
        )
    store = _active_store()
    try:
        record, eta = store.submit(
            job_id, source_id, job_format, user_id=user_id, school=school,
            source_key=source_key, source_keys=source_keys,
        )
    except jobs.JobExists as exc:
        raise CapabilityError("conflict", str(exc)) from exc
    except jobs.JobStoreFull as exc:
        raise CapabilityError("busy", str(exc)) from exc
    except ValueError as exc:
        raise CapabilityError("bad_request", str(exc)) from exc
    except OSError as exc:
        raise CapabilityError("internal", f"is durumu diske yazilamadi: {exc}") from exc
    return {
        "job_id": record["job_id"],
        "state": record["state"],
        "eta_secs": eta,
    }


def cancel(school: str, payload: dict) -> dict:
    job_id = _require_text(payload, "job_id")
    store = _active_store()
    try:
        cancelled = store.cancel(job_id)
    except jobs.JobNotFound as exc:
        raise CapabilityError("not_found", str(exc)) from exc
    except jobs.InvalidTransition as exc:
        raise CapabilityError("internal", str(exc)) from exc
    return {"job_id": job_id, "cancelled": cancelled}


REPORT_CAPABILITY = protocol.PODCAST_REPORT_CAPABILITY

REGISTRY: dict[str, Handler] = {
    "podcast.submit": submit,
    "podcast.cancel": cancel,
}


def names() -> list[str]:
    return sorted(REGISTRY)


def dispatch(capability: str, school: str, payload: Any) -> dict:
    handler = REGISTRY.get(capability)
    if handler is None:
        raise CapabilityError("unsupported_capability", f"bilinmeyen yetenek: {capability}")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise CapabilityError("bad_request", "'payload' bir nesne olmali")
    return handler(school, payload)
