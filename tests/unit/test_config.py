import os
import unittest
from pathlib import Path

from src import config

ENV_NAMES = (
    "LOG_LEVEL",
    "AI_BRIDGE_HOST",
    "AI_BRIDGE_PORT",
    "AI_BACKEND_URL",
    "AI_TLS_SERVER_NAME",
    "AI_SERVICE_NAME",
    "AI_MAX_CONCURRENT",
    "AI_RECONNECT_SECS",
    "AI_RECONNECT_MAX_SECS",
    "AI_TLS_FINGERPRINT",
    "AI_SHARED_TOKEN",
    "PODCAST_JOB_ROOT",
    "PODCAST_WORKERS",
    "PODCAST_MAX_JOBS",
    "PODCAST_STAGE_SECS",
    "PODCAST_ETA_SECS",
    "PODCAST_MEDIA_ROOT",
    "PODCAST_MODE",
    "PODCAST_ENGINE",
    "PODCAST_PIPELINE_PATH",
    "PODCAST_OUTPUT_ROOT",
    "PODCAST_LEDGER_DB",
    "PODCAST_CHAPTER_LIMIT",
    "PODCAST_CHAPTER_CHARS",
    "PODCAST_MIN_TEXT_CHARS",
    "PODCAST_OCR_LANG",
    "PODCAST_OCR_PAGE_MIN_CHARS",
    "PODCAST_TTS_ENGINE",
    "PODCAST_RETENTION_DAYS",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_EXTRA_JSON",
    "LLM_TIMEOUT_S",
    "LLM_MAX_ATTEMPTS",
    "LLM_RETRY_BASE_S",
    "LLM_RETRY_MAX_S",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_BASE_URL",
    "ELEVENLABS_MODEL",
    "ELEVENLABS_VOICE_ID",
    "ELEVENLABS_LANGUAGE",
    "ELEVENLABS_OUTPUT_FORMAT",
    "ELEVENLABS_TIMEOUT_S",
    "PODCAST_TEST_DEGISKENI",
)


class EnvIsolatedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = {name: os.environ.get(name) for name in ENV_NAMES}
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        self._log_level = config._active_level

    def tearDown(self) -> None:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        config._active_level = self._log_level


class EnvStrTests(EnvIsolatedTestCase):
    def test_missing_or_blank_value_falls_back_to_default(self) -> None:
        for raw in (None, "", "   ", "\t"):
            with self.subTest(raw=raw):
                if raw is None:
                    os.environ.pop("PODCAST_TEST_DEGISKENI", None)
                else:
                    os.environ["PODCAST_TEST_DEGISKENI"] = raw
                self.assertEqual(
                    config.env_str("PODCAST_TEST_DEGISKENI", "varsayilan"),
                    "varsayilan",
                    "bos ya da tanimsiz deger varsayilana dusmeli",
                )

    def test_value_is_returned_unstripped_when_present(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = " deger "
        self.assertEqual(config.env_str("PODCAST_TEST_DEGISKENI", "x"), " deger ")


class EnvIntTests(EnvIsolatedTestCase):
    def test_valid_values_are_parsed(self) -> None:
        for raw, expected in (("7", 7), (" 7 ", 7), ("-3", -3), ("0", 0)):
            with self.subTest(raw=raw):
                os.environ["PODCAST_TEST_DEGISKENI"] = raw
                self.assertEqual(
                    config.env_int("PODCAST_TEST_DEGISKENI", 1, -10, 10), expected
                )

    def test_non_integer_is_a_config_error(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = "yedi"
        with self.assertRaises(config.ConfigError):
            config.env_int("PODCAST_TEST_DEGISKENI", 1, 0, 10)

    def test_out_of_range_is_a_config_error_on_both_ends(self) -> None:
        for raw in ("-1", "11"):
            with self.subTest(raw=raw):
                os.environ["PODCAST_TEST_DEGISKENI"] = raw
                with self.assertRaises(config.ConfigError):
                    config.env_int("PODCAST_TEST_DEGISKENI", 1, 0, 10)

    def test_blank_value_uses_default_without_raising(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = "  "
        self.assertEqual(config.env_int("PODCAST_TEST_DEGISKENI", 4, 0, 10), 4)


class EnvFloatTests(EnvIsolatedTestCase):
    def test_valid_values_are_parsed(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = " 2.5 "
        self.assertEqual(config.env_float("PODCAST_TEST_DEGISKENI", 1.0, 0.0, 10.0), 2.5)

    def test_non_number_is_a_config_error(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = "iki-bucuk"
        with self.assertRaises(config.ConfigError):
            config.env_float("PODCAST_TEST_DEGISKENI", 1.0, 0.0, 10.0)

    def test_out_of_range_is_a_config_error(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = "10.5"
        with self.assertRaises(config.ConfigError):
            config.env_float("PODCAST_TEST_DEGISKENI", 1.0, 0.0, 10.0)


class EnvChoiceTests(EnvIsolatedTestCase):
    def test_choice_is_case_insensitive_and_trimmed(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = "  REAL "
        self.assertEqual(
            config.env_choice("PODCAST_TEST_DEGISKENI", "simulate", config.PIPELINE_MODES),
            "real",
        )

    def test_unknown_choice_is_a_config_error(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = "yari-gercek"
        with self.assertRaises(config.ConfigError):
            config.env_choice("PODCAST_TEST_DEGISKENI", "simulate", config.PIPELINE_MODES)

    def test_blank_choice_uses_default(self) -> None:
        os.environ["PODCAST_TEST_DEGISKENI"] = ""
        self.assertEqual(
            config.env_choice("PODCAST_TEST_DEGISKENI", "simulate", config.PIPELINE_MODES),
            "simulate",
        )


class LogLevelTests(EnvIsolatedTestCase):
    def test_known_levels_are_accepted_and_actually_applied(self) -> None:
        for name, expected in (
            ("debug", 10),
            ("INFO", 20),
            (" warn ", 30),
            ("warning", 30),
            ("error", 40),
        ):
            with self.subTest(name=name):
                config.set_log_level(name)
                self.assertEqual(
                    config._active_level,
                    expected,
                    f"'{name}' kabul edildi ama etkin seviye uygulanmadi",
                )

    def test_unknown_level_is_a_config_error(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.set_log_level("gurultu")

    def test_lower_priority_messages_are_filtered_out(self) -> None:
        config.set_log_level("error")
        self.assertEqual(config.LOG_LEVELS["debug"], 10)
        self.assertLess(
            config.LOG_LEVELS["debug"],
            config._active_level,
            "error seviyesinde debug mesaji basilmamali",
        )


class AbsolutePathTests(EnvIsolatedTestCase):
    def test_relative_path_becomes_absolute(self) -> None:
        resolved = config._absolute("out")
        self.assertTrue(Path(resolved).is_absolute(), f"goreli yol mutlaklastirilmadi: {resolved}")

    def test_empty_path_stays_empty(self) -> None:
        self.assertEqual(config._absolute(""), "")

    def test_dot_segments_are_collapsed(self) -> None:
        resolved = config._absolute(os.path.join("a", "..", "b"))
        self.assertNotIn("..", resolved, "nokta parcalari cozulmeli")


class ConfigDefaultsTests(EnvIsolatedTestCase):
    def test_defaults_match_the_documented_contract(self) -> None:
        settings = config.Config(require_token=False)
        self.assertEqual(settings.service, "podcast")
        self.assertEqual(settings.port, 8090)
        self.assertEqual(settings.mode, config.PIPELINE_MODE_DEFAULT)
        self.assertEqual(settings.max_concurrent, 2)
        self.assertEqual(settings.workers, 1)
        self.assertEqual(settings.max_jobs, 8)
        self.assertEqual(settings.chapter_limit, 1)
        self.assertEqual(settings.tts_engine, "supertonic-3")
        self.assertEqual(settings.engine, config.PIPELINE_ENGINE_DEFAULT)
        self.assertEqual(settings.chapter_chars, 6000)
        self.assertEqual(settings.min_text_chars, 200)
        self.assertEqual(settings.ocr_language, "tur")
        self.assertEqual(settings.llm_base_url, "https://api.deepseek.com")
        self.assertEqual(settings.llm_model, "deepseek-chat")
        self.assertEqual(settings.llm_timeout_secs, 30.0)
        self.assertEqual(settings.llm_attempts, 3)
        self.assertEqual(settings.elevenlabs_base_url, "https://api.elevenlabs.io")
        self.assertEqual(settings.elevenlabs_model, "eleven_flash_v2_5")
        self.assertEqual(settings.elevenlabs_voice_id, "")
        self.assertEqual(settings.elevenlabs_language, "tr")
        self.assertEqual(settings.elevenlabs_output_format, "mp3_44100_128")
        self.assertFalse(settings.has_elevenlabs_key)
        self.assertFalse(settings.has_token)
        self.assertFalse(settings.has_llm_key)
        self.assertEqual(settings.reconnect_max_secs, 120.0)
        self.assertEqual(settings.tls_fingerprint, "")

    def test_backend_url_trailing_slash_is_trimmed(self) -> None:
        os.environ["AI_BACKEND_URL"] = "http://backend:8080/"
        self.assertEqual(config.Config(require_token=False).backend_url, "http://backend:8080")

    def test_missing_token_is_fatal_only_when_required(self) -> None:
        config.Config(require_token=False)
        with self.assertRaises(config.ConfigError):
            config.Config(require_token=True)

    def test_llm_key_presence_is_recorded_as_a_boolean_not_the_value(self) -> None:
        os.environ["LLM_API_KEY"] = "sk-ornek"
        settings = config.Config(require_token=False)
        self.assertTrue(settings.has_llm_key)
        self.assertFalse(
            hasattr(settings, "llm_key"),
            "Config LLM anahtarinin degerini saklamamali, yalnizca varligini",
        )

    def test_elevenlabs_key_presence_does_not_store_the_value(self) -> None:
        os.environ["ELEVENLABS_API_KEY"] = "sk-ornek"
        settings = config.Config(require_token=False)
        self.assertTrue(settings.has_elevenlabs_key)
        self.assertNotIn("sk-ornek", settings.summary())
        self.assertFalse(hasattr(settings, "elevenlabs_api_key"))


class TlsFingerprintTests(EnvIsolatedTestCase):
    def test_default_is_empty_and_reported_as_tofu(self) -> None:
        settings = config.Config(require_token=False)
        self.assertEqual(settings.tls_fingerprint, "")
        self.assertIn("TOFU", settings.summary())

    def test_colons_spaces_and_case_are_normalized(self) -> None:
        raw = "AB:cd" * 16
        os.environ["AI_TLS_FINGERPRINT"] = "  " + raw + "  "
        settings = config.Config(require_token=False)
        self.assertEqual(settings.tls_fingerprint, raw.replace(":", "").lower())
        self.assertEqual(len(settings.tls_fingerprint), 64)
        self.assertIn("PINLI", settings.summary())

    def test_a_pin_that_is_not_sha256_hex_crashes_the_boot(self) -> None:
        for raw in ("kisa", "z" * 64, "a" * 63, "a" * 65):
            with self.subTest(raw=raw[:12]):
                os.environ["AI_TLS_FINGERPRINT"] = raw
                with self.assertRaises(config.ConfigError):
                    config.Config(require_token=False)

    def test_the_pinned_value_itself_is_not_printed_in_the_summary(self) -> None:
        pin = "a1b2c3d4" * 8
        os.environ["AI_TLS_FINGERPRINT"] = pin
        settings = config.Config(require_token=False)
        self.assertNotIn(pin, settings.summary())
        self.assertNotIn(pin[:12], settings.summary())


class ReconnectCeilingTests(EnvIsolatedTestCase):
    def test_the_ceiling_is_read_and_bounded_on_both_ends(self) -> None:
        os.environ["AI_RECONNECT_MAX_SECS"] = "30"
        self.assertEqual(config.Config(require_token=False).reconnect_max_secs, 30.0)
        for raw in ("0", "0.5", "3601", "-5"):
            with self.subTest(raw=raw):
                os.environ["AI_RECONNECT_MAX_SECS"] = raw
                with self.assertRaises(config.ConfigError):
                    config.Config(require_token=False)


class ModeGateTests(EnvIsolatedTestCase):
    def test_pipeline_modes_are_exactly_simulate_and_real(self) -> None:
        self.assertEqual(config.PIPELINE_MODES, ("simulate", "real"))
        self.assertEqual(config.PIPELINE_MODE_DEFAULT, "simulate")

    def test_both_modes_are_accepted(self) -> None:
        for mode in config.PIPELINE_MODES:
            with self.subTest(mode=mode):
                os.environ["PODCAST_MODE"] = mode
                self.assertEqual(config.Config(require_token=False).mode, mode)

    def test_unknown_mode_crashes_the_boot_instead_of_silently_simulating(self) -> None:
        os.environ["PODCAST_MODE"] = "yari-gercek"
        with self.assertRaises(config.ConfigError):
            config.Config(require_token=False)


class EngineGateTests(EnvIsolatedTestCase):
    def test_engines_are_exactly_api_and_local_with_api_default(self) -> None:
        self.assertEqual(config.PIPELINE_ENGINES, ("api", "local"))
        self.assertEqual(config.PIPELINE_ENGINE_DEFAULT, "api")
        self.assertEqual(config.Config(require_token=False).engine, "api")

    def test_both_engines_are_accepted(self) -> None:
        for engine in config.PIPELINE_ENGINES:
            with self.subTest(engine=engine):
                os.environ["PODCAST_ENGINE"] = engine
                self.assertEqual(config.Config(require_token=False).engine, engine)

    def test_unknown_engine_crashes_the_boot_instead_of_falling_back(self) -> None:
        os.environ["PODCAST_ENGINE"] = "bulut"
        with self.assertRaises(config.ConfigError):
            config.Config(require_token=False)

    def test_mode_and_engine_are_independent_axes(self) -> None:
        os.environ["PODCAST_MODE"] = "simulate"
        os.environ["PODCAST_ENGINE"] = "local"
        settings = config.Config(require_token=False)
        self.assertEqual(settings.mode, "simulate")
        self.assertEqual(settings.engine, "local")


if __name__ == "__main__":
    unittest.main()
