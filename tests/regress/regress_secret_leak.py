import os
import tempfile
import unittest
from pathlib import Path

from src import capabilities, config, jobs

LLM_CANARY = "kanarya-deepseek-anahtari-9f3a2b"
TOKEN_CANARY = "kanarya-paylasilan-sir-7c1e8d"

SECRET_NAMES = ("DEEPSEEK_API_KEY", "AI_SHARED_TOKEN")

ENV_NAMES = (
    "LOG_LEVEL",
    "AI_SHARED_TOKEN",
    "AI_MAX_CONCURRENT",
    "AI_RECONNECT_SECS",
    "PODCAST_JOB_ROOT",
    "PODCAST_OUTPUT_ROOT",
    "PODCAST_MEDIA_ROOT",
    "PODCAST_LEDGER_DB",
    "PODCAST_MODE",
    "PODCAST_PIPELINE_PATH",
    "PODCAST_WORKERS",
    "PODCAST_MAX_JOBS",
    "PODCAST_STAGE_SECS",
    "PODCAST_ETA_SECS",
    "PODCAST_CHAPTER_LIMIT",
    "PODCAST_TTS_ENGINE",
    "DEEPSEEK_API_KEY",
)


class SecretLeakRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        self._previous = {name: os.environ.get(name) for name in ENV_NAMES}
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-sir-")
        self.root = Path(self._tmp.name)
        os.environ["LOG_LEVEL"] = "error"
        os.environ["DEEPSEEK_API_KEY"] = LLM_CANARY
        os.environ["AI_SHARED_TOKEN"] = TOKEN_CANARY
        os.environ["PODCAST_JOB_ROOT"] = str(self.root / "isler")
        os.environ["PODCAST_OUTPUT_ROOT"] = str(self.root / "cikti")
        os.environ["PODCAST_MEDIA_ROOT"] = str(self.root / "medya")
        os.environ["PODCAST_LEDGER_DB"] = str(self.root / "defter.sqlite")
        self._stores: list = []

    def tearDown(self) -> None:
        capabilities.configure(None, llm_ready=False)
        for store in self._stores:
            store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        config._active_level = self._log_level

    def find_canaries(self, text: str, where: str) -> None:
        for canary in (LLM_CANARY, TOKEN_CANARY):
            self.assertNotIn(
                canary, text, f"{where} icinde sir degeri gorunuyor: {canary[:12]}..."
            )

    def test_secret_values_must_not_appear_in_the_config_summary_output(self) -> None:
        settings = config.Config(require_token=True)
        self.find_canaries(settings.summary(), "Config.summary()")

    def test_the_config_summary_must_report_only_the_presence_of_the_secrets(self) -> None:
        settings = config.Config(require_token=True)
        summary = settings.summary()
        self.assertIn("token=tanimli", summary)
        self.assertIn("llm_anahtari=tanimli", summary)
        self.assertTrue(settings.has_token)
        self.assertTrue(settings.has_llm_key)

    def test_the_format_summary_gives_the_key_name_not_its_value(self) -> None:
        for llm_ready in (False, True):
            with self.subTest(llm_ready=llm_ready):
                summary = capabilities.format_summary(llm_ready)
                self.assertIn(capabilities.LLM_KEY_NAME, summary)
                self.find_canaries(summary, "capabilities.format_summary()")

    def test_secret_values_must_not_appear_in_the_job_json_records(self) -> None:
        settings = config.Config(require_token=True)
        store = jobs.JobStore(
            root=settings.job_root, workers=1, max_jobs=8, stage_secs=0.0
        )
        self._stores.append(store)
        capabilities.configure(store, llm_ready=True)
        job_id = capabilities.dispatch(
            "podcast.submit", {"source_id": "ders.pdf", "format": "tek_ogretici"}
        )["job_id"]
        store.transition(job_id, jobs.STATE_RUNNING)
        store.finish(job_id, audio_id="a/b.mp3", duration_secs=1.0, script_id="a/b.json")
        jobs.write_status(settings.job_root, True, "w1")

        for path in sorted(Path(settings.job_root).iterdir()):
            if not path.is_file():
                continue
            with self.subTest(file=path.name):
                self.find_canaries(
                    path.read_text(encoding="utf-8", errors="replace"), f"is kaydi {path.name}"
                )

    def test_secret_values_must_not_appear_in_exception_texts(self) -> None:
        broken_settings = (
            ("PODCAST_WORKERS", "yedi"),
            ("PODCAST_WORKERS", "9999"),
            ("PODCAST_MAX_JOBS", "sifir"),
            ("PODCAST_STAGE_SECS", "yavas"),
            ("PODCAST_ETA_SECS", "-5"),
            ("AI_MAX_CONCURRENT", "999"),
            ("AI_RECONNECT_SECS", "hemen"),
            ("PODCAST_MODE", "yari-gercek"),
            ("LOG_LEVEL", "gurultu"),
            ("PODCAST_CHAPTER_LIMIT", "cok"),
        )
        for name, value in broken_settings:
            with self.subTest(variable=name, value=value):
                previous = os.environ.get(name)
                os.environ[name] = value
                try:
                    with self.assertRaises(config.ConfigError) as raised:
                        config.Config(require_token=True)
                    self.find_canaries(str(raised.exception), f"{name} istisnasi")
                finally:
                    if previous is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = previous

    def test_env_str_must_not_put_the_raw_value_into_any_error_message(self) -> None:
        os.environ["PODCAST_TTS_ENGINE"] = LLM_CANARY
        self.assertEqual(config.env_str("PODCAST_TTS_ENGINE", "supertonic-3"), LLM_CANARY)
        os.environ["PODCAST_WORKERS"] = "yedi"
        try:
            with self.assertRaises(config.ConfigError) as raised:
                config.Config(require_token=True)
        finally:
            os.environ.pop("PODCAST_WORKERS", None)
            os.environ.pop("PODCAST_TTS_ENGINE", None)
        self.assertNotIn(
            LLM_CANARY,
            str(raised.exception),
            "env_str ile okunan bir degerin ham hali hata mesajina sizdi",
        )

    def test_the_missing_token_error_must_not_carry_a_secret_either(self) -> None:
        os.environ.pop("AI_SHARED_TOKEN", None)
        with self.assertRaises(config.ConfigError) as raised:
            config.Config(require_token=True)
        self.find_canaries(str(raised.exception), "eksik token istisnasi")

    def test_the_config_object_must_not_store_the_secrets_as_fields(self) -> None:
        settings = config.Config(require_token=True)
        for field, value in vars(settings).items():
            if field == "token":
                continue
            with self.subTest(field=field):
                self.assertNotIn(
                    LLM_CANARY,
                    str(value),
                    f"Config.{field} LLM anahtarinin degerini tasiyor",
                )
        self.assertFalse(
            hasattr(settings, "llm_key"),
            "Config LLM anahtarinin degerini saklamamali; yalnizca has_llm_key tutulur",
        )

    def test_the_bridge_status_file_must_not_carry_a_secret(self) -> None:
        settings = config.Config(require_token=True)
        Path(settings.job_root).mkdir(parents=True, exist_ok=True)
        jobs.write_status(settings.job_root, True, "w1")
        text = jobs.status_path(settings.job_root).read_text(encoding="utf-8")
        self.find_canaries(text, "kopru durum dosyasi")


if __name__ == "__main__":
    unittest.main()
