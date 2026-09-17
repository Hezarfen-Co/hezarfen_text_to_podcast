from __future__ import annotations

import os
from pathlib import Path
import sys

LOG_LEVELS = {"debug": 10, "info": 20, "warn": 30, "warning": 30, "error": 40}
DEFAULT_LOG_LEVEL = "info"

PIPELINE_MODES = ("simulate", "real")
PIPELINE_MODE_DEFAULT = "simulate"

PIPELINE_ENGINES = ("api", "local")
PIPELINE_ENGINE_DEFAULT = "api"

LLM_KEY_ENV_NAME = "LLM_API_KEY"
ELEVENLABS_KEY_ENV_NAME = "ELEVENLABS_API_KEY"

_active_level = LOG_LEVELS[DEFAULT_LOG_LEVEL]


class ConfigError(Exception):
    pass


def set_log_level(name: str) -> None:
    global _active_level
    level = LOG_LEVELS.get(name.strip().lower())
    if level is None:
        raise ConfigError(
            f"LOG_LEVEL gecersiz: '{name}' (beklenen: {', '.join(sorted(LOG_LEVELS))})"
        )
    _active_level = level


def log(level: str, message: str) -> None:
    if LOG_LEVELS.get(level, LOG_LEVELS[DEFAULT_LOG_LEVEL]) >= _active_level:
        print(f"[bridge] {message}", flush=True)


def env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value.strip() else default


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} bir tamsayi olmali, alinan: '{raw}'") from exc
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} {minimum}..{maximum} araliginda olmali, alinan: {value}")
    return value


def env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in choices:
        raise ConfigError(
            f"{name} su degerlerden biri olmali: {', '.join(choices)}; alinan: '{raw}'"
        )
    return value


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} bir sayi olmali, alinan: '{raw}'") from exc
    if value < minimum or value > maximum:
        raise ConfigError(f"{name} {minimum}..{maximum} araliginda olmali, alinan: {value}")
    return value


def _absolute(path: str) -> str:
    if not path:
        return path
    return str(Path(path).expanduser().resolve())


class Config:
    def __init__(self, require_token: bool = True) -> None:
        self.log_level = env_str("LOG_LEVEL", DEFAULT_LOG_LEVEL)
        set_log_level(self.log_level)
        self.host = env_str("AI_BRIDGE_HOST", "hezarfen_backend")
        self.port = env_int("AI_BRIDGE_PORT", 8090, 1, 65535)
        self.backend_url = env_str("AI_BACKEND_URL", "http://hezarfen_backend:7656").rstrip("/")
        self.server_name = env_str("AI_TLS_SERVER_NAME", "localhost")
        self.tls_fingerprint = (
            env_str("AI_TLS_FINGERPRINT", "").strip().lower().replace(":", "")
        )
        self.service = env_str("AI_SERVICE_NAME", "podcast")
        self.max_concurrent = env_int("AI_MAX_CONCURRENT", 2, 1, 64)
        self.reconnect_secs = env_float("AI_RECONNECT_SECS", 3.0, 0.1, 300.0)
        self.reconnect_max_secs = env_float(
            "AI_RECONNECT_MAX_SECS", 120.0, 1.0, 3600.0
        )
        self.token = env_str("AI_SHARED_TOKEN", "")
        self.has_token = bool(self.token)
        self.job_root = _absolute(env_str("PODCAST_JOB_ROOT", "/data/podcast/jobs"))
        self.workers = env_int("PODCAST_WORKERS", 1, 1, 32)
        self.max_jobs = env_int("PODCAST_MAX_JOBS", 8, 1, 4096)
        self.retention_days = env_float("PODCAST_RETENTION_DAYS", 0.0, 0.0, 3650.0)
        self.stage_secs = env_float("PODCAST_STAGE_SECS", 0.2, 0.0, 3600.0)
        self.eta_secs = env_float("PODCAST_ETA_SECS", 0.0, 0.0, 86400.0)
        self.media_root = _absolute(env_str("PODCAST_MEDIA_ROOT", "/data/files"))
        self.mode = env_choice("PODCAST_MODE", PIPELINE_MODE_DEFAULT, PIPELINE_MODES)
        self.engine = env_choice("PODCAST_ENGINE", PIPELINE_ENGINE_DEFAULT, PIPELINE_ENGINES)
        self.pipeline_path = env_str("PODCAST_PIPELINE_PATH", "")
        self.output_root = _absolute(env_str("PODCAST_OUTPUT_ROOT", "/data/podcast/out"))
        self.ledger_db = _absolute(env_str("PODCAST_LEDGER_DB", "/data/podcast/router.sqlite"))
        self.chapter_limit = env_int("PODCAST_CHAPTER_LIMIT", 1, -1, 4096)
        self.chapter_chars = env_int("PODCAST_CHAPTER_CHARS", 6000, 200, 100000)
        self.min_text_chars = env_int("PODCAST_MIN_TEXT_CHARS", 200, 0, 1000000)
        self.ocr_language = env_str("PODCAST_OCR_LANG", "tur")
        self.ocr_page_min_chars = env_int("PODCAST_OCR_PAGE_MIN_CHARS", 40, 0, 100000)
        self.tts_engine = env_str("PODCAST_TTS_ENGINE", "supertonic-3")
        self.llm_base_url = env_str("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
        self.llm_model = env_str("LLM_MODEL", "deepseek-chat")
        self.llm_timeout_secs = env_float("LLM_TIMEOUT_S", 30.0, 1.0, 600.0)
        self.llm_attempts = env_int("LLM_MAX_ATTEMPTS", 3, 1, 10)
        self.llm_retry_base_secs = env_float("LLM_RETRY_BASE_S", 0.5, 0.05, 60.0)
        self.llm_retry_max_secs = env_float("LLM_RETRY_MAX_S", 8.0, 0.05, 300.0)
        self.elevenlabs_base_url = env_str(
            "ELEVENLABS_BASE_URL", "https://api.elevenlabs.io"
        ).rstrip("/")
        self.elevenlabs_model = env_str("ELEVENLABS_MODEL", "eleven_flash_v2_5")
        self.elevenlabs_voice_id = env_str("ELEVENLABS_VOICE_ID", "")
        self.elevenlabs_language = env_str("ELEVENLABS_LANGUAGE", "tr")
        self.elevenlabs_output_format = env_str(
            "ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128"
        )
        self.elevenlabs_timeout_secs = env_float("ELEVENLABS_TIMEOUT_S", 120.0, 5.0, 900.0)
        self.has_llm_key = bool(os.environ.get(LLM_KEY_ENV_NAME, "").strip())
        self.has_elevenlabs_key = bool(os.environ.get(ELEVENLABS_KEY_ENV_NAME, "").strip())
        if self.tls_fingerprint and not (
            len(self.tls_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in self.tls_fingerprint)
        ):
            raise ConfigError(
                "AI_TLS_FINGERPRINT 64 karakterlik SHA-256 hex olmali "
                f"(iki nokta ayraclari atilir); alinan uzunluk: {len(self.tls_fingerprint)}"
            )
        if require_token and not self.has_token:
            raise ConfigError(
                "AI_SHARED_TOKEN tanimli degil; backend ile ayni sir olmadan kayit yapilamaz"
            )

    def summary(self) -> str:
        return (
            f"servis='{self.service}' hedef={self.host}:{self.port} "
            f"backend={self.backend_url} tls_ad={self.server_name} "
            f"tls_parmak_izi={'PINLI' if self.tls_fingerprint else 'TOFU(pinsiz)'} "
            f"max_es_zamanli={self.max_concurrent} "
            f"yeniden_baglanma={self.reconnect_secs}s..{self.reconnect_max_secs}s "
            f"log={self.log_level} token={'tanimli' if self.has_token else 'TANIMSIZ'} "
            f"is_kok={self.job_root} isciler={self.workers} max_is={self.max_jobs} "
            f"asama={self.stage_secs}s "
            f"eta={'otomatik' if self.eta_secs <= 0 else str(self.eta_secs) + 's'} "
            f"medya_kok={self.media_root} mod={self.mode} motor={self.engine} "
            f"hat_yolu={self.pipeline_path if self.pipeline_path else 'TANIMSIZ'} "
            f"cikti_kok={self.output_root} defter={self.ledger_db} "
            f"retention={self.retention_days:g}g "
            f"bolum_limiti={'hepsi' if self.chapter_limit <= 0 else self.chapter_limit} "
            f"tts={self.tts_engine} "
            f"llm={self.llm_model} llm_anahtari="
            f"{'tanimli' if self.has_llm_key else 'TANIMSIZ'} "
            f"bulut_tts={self.elevenlabs_model} bulut_ses="
            f"{'tanimli' if self.elevenlabs_voice_id else 'TANIMSIZ'} "
            f"bulut_anahtari={'tanimli' if self.has_elevenlabs_key else 'TANIMSIZ'}"
        )


def load(require_token: bool = True) -> Config:
    try:
        return Config(require_token=require_token)
    except ConfigError as exc:
        print(f"[bridge] yapilandirma hatasi: {exc}", flush=True)
        sys.exit(2)
