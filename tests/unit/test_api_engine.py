import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from src import api_engine, capabilities, config, jobs, llm, tts

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
)

PAGE_TEXT = (
    "Hucre canliligin temel birimidir. Hucre zar, sitoplazma ve cekirdekten olusur. "
    "Bitkilerde ek olarak hucre duvari ve kloroplast bulunur. Fotosentez kloroplastta "
    "gerceklesir ve gunes enerjisi kimyasal enerjiye cevrilir. Mitokondri solunumun "
    "yapildigi organeldir ve hucreye enerji saglar. Ribozomlar protein sentezinden "
    "sorumludur ve butun hucrelerde bulunur. Golgi cisimcigi salgi maddelerini paketler. "
    "Endoplazmik retikulum madde tasimasinda gorevlidir. Lizozom sindirim enzimleri tasir."
)

AUDIO_CACHE: dict[str, bytes] = {}
AUDIO_LOCK = threading.Lock()


def audio_bytes(seconds: str = "1") -> bytes:
    with AUDIO_LOCK:
        cached = AUDIO_CACHE.get(seconds)
        if cached is not None:
            return cached
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise unittest.SkipTest("ffmpeg yok; api motoru testi kosulamaz")
        with tempfile.TemporaryDirectory(prefix="podcast-audio-") as tmp:
            target = Path(tmp) / "ornek.mp3"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=440:duration={seconds}",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "64k",
                    str(target),
                ],
                check=True,
            )
            data = target.read_bytes()
        AUDIO_CACHE[seconds] = data
        return data


def make_pdf(path: Path, text: str) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    for index, line in enumerate(text.split(". ")):
        page.insert_text((40, 60 + index * 14), line, fontsize=9)
    doc.save(str(path))
    doc.close()


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.calls.append((self.path, body, {k.lower(): v for k, v in self.headers.items()}))
        if self.path.startswith("/chat/completions"):
            status, payload = self.server.llm_response
        elif "/text-to-speech/" in self.path:
            delay = self.server.tts_delay
            if delay:
                time.sleep(delay)
            status, payload = self.server.tts_response
        else:
            status, payload = 404, b"{}"
        content_type = "application/json" if payload[:1] == b"{" else "audio/mpeg"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        import socketserver

        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.calls: list = []
        self.llm_response = (200, json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())
        self.tts_response = (200, b"")
        self.tts_delay = 0.0

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def llm_calls(self) -> list:
        return [call for call in self.calls if call[0].startswith("/chat/completions")]

    def tts_calls(self) -> list:
        return [call for call in self.calls if "/text-to-speech/" in call[0]]


class EnvIsolatedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = {name: os.environ.get(name) for name in ENV_NAMES}
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        self._log_level = config._active_level
        config.set_log_level("error")

    def tearDown(self) -> None:
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        config._active_level = self._log_level


class PureFunctionTests(unittest.TestCase):
    def test_chapters_split_on_word_boundaries_and_respect_the_limit(self) -> None:
        text = " ".join(f"kelime{i}" for i in range(60))
        chunks = api_engine.split_chapters(text, 60, None)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 60)
        self.assertEqual(" ".join(chunks).split(), text.split())

    def test_chapter_limit_keeps_the_first_chapters(self) -> None:
        text = " ".join(f"kelime{i}" for i in range(60))
        chunks = api_engine.split_chapters(text, 60, 2)
        self.assertEqual(len(chunks), 2)

    def test_spoken_text_strips_markup_lines(self) -> None:
        self.assertEqual(
            api_engine._spoken_text("# Baslik\n**kalin** metin\n- madde"),
            "Baslik\nkalin metin\nmadde",
        )

    def test_engine_error_codes_and_stages_are_stable(self) -> None:
        from src import pipeline

        self.assertEqual(api_engine.SOURCE_NOT_FOUND, pipeline.SOURCE_NOT_FOUND)
        self.assertEqual(
            api_engine.STAGES, ("kaynak", "metin", "script", "tts", "mux")
        )
        self.assertEqual(api_engine.NO_TEXT, "no_text_layer")

    def test_extraction_of_a_text_pdf_returns_the_text_layer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="podcast-pdf-") as tmp:
            path = Path(tmp) / "ders.pdf"
            make_pdf(path, PAGE_TEXT)
            settings = config.Config(require_token=False)
            text, pages, ocr_pages = api_engine.extract_text(
                str(path), settings, lambda: None
            )
        self.assertIn("Hucre canliligin temel birimidir", text)
        self.assertEqual(pages, 1)
        self.assertEqual(ocr_pages, 0)

    def test_extraction_of_a_scanned_pdf_fails_loudly_instead_of_returning_empty(self) -> None:
        try:
            import pymupdf
        except ImportError:
            self.skipTest("pymupdf yok")
        with tempfile.TemporaryDirectory(prefix="podcast-pdf-") as tmp:
            path = Path(tmp) / "tarama.pdf"
            doc = pymupdf.open()
            doc.new_page()
            doc.save(str(path))
            doc.close()
            settings = config.Config(require_token=False)
            with self.assertRaises(api_engine.EngineError) as raised:
                api_engine.extract_text(str(path), settings, lambda: None)
        self.assertEqual(raised.exception.code, api_engine.NO_TEXT)


class ClientTests(EnvIsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.stub = StubServer()
        self.thread = threading.Thread(target=self.stub.serve_forever, daemon=True)
        self.thread.start()
        os.environ["LLM_API_KEY"] = "stub-anahtar"
        os.environ["ELEVENLABS_API_KEY"] = "stub-anahtar"
        os.environ["ELEVENLABS_VOICE_ID"] = "stub-ses"
        self.settings = config.Config(require_token=False)
        self.settings.llm_base_url = self.stub.base_url
        self.settings.elevenlabs_base_url = self.stub.base_url

    def tearDown(self) -> None:
        self.stub.shutdown()
        self.stub.server_close()
        super().tearDown()

    def test_llm_success_returns_the_message_content(self) -> None:
        self.stub.llm_response = (
            200,
            json.dumps({"choices": [{"message": {"content": "merhaba"}}]}).encode(),
        )
        self.assertEqual(llm.chat("soru", self.settings), "merhaba")
        self.assertEqual(len(self.stub.llm_calls()), 1)

    def test_llm_missing_key_is_a_named_error_without_a_request(self) -> None:
        os.environ.pop("LLM_API_KEY", None)
        with self.assertRaises(llm.LlmError) as raised:
            llm.chat("soru", self.settings, api_key_value="")
        self.assertEqual(raised.exception.code, "llm_unavailable")
        self.assertEqual(self.stub.llm_calls(), [])

    def test_llm_http_error_surfaces_the_status(self) -> None:
        self.stub.llm_response = (401, b'{"error": "bad key"}')
        with self.assertRaises(llm.LlmError) as raised:
            llm.chat("soru", self.settings)
        self.assertEqual(raised.exception.code, "llm_error")
        self.assertIn("401", str(raised.exception))

    def test_llm_retries_once_on_5xx_then_fails_with_the_last_status(self) -> None:
        self.settings.llm_attempts = 2
        self.settings.llm_retry_base_secs = 0.01
        self.stub.llm_response = (503, b"yogun")
        with self.assertRaises(llm.LlmError) as raised:
            llm.chat("soru", self.settings)
        self.assertEqual(raised.exception.code, "llm_error")
        self.assertEqual(len(self.stub.llm_calls()), 2)

    def test_llm_timeout_is_reported_as_a_timeout(self) -> None:
        self.settings.llm_attempts = 1
        self.settings.llm_timeout_secs = 0.4
        self.stub.llm_response = (200, b"{}")
        original = StubHandler.do_POST

        def slow(self_handler, *args, **kwargs):
            time.sleep(1.2)
            return original(self_handler, *args, **kwargs)

        StubHandler.do_POST = slow
        try:
            with self.assertRaises(llm.LlmError) as raised:
                llm.chat("soru", self.settings)
        finally:
            StubHandler.do_POST = original
        self.assertEqual(raised.exception.code, "llm_timeout")

    def test_llm_malformed_json_body_is_a_named_error(self) -> None:
        self.stub.llm_response = (200, b"bu bir json degil")
        with self.assertRaises(llm.LlmError) as raised:
            llm.chat("soru", self.settings)
        self.assertEqual(raised.exception.code, "llm_error")

    def test_llm_empty_choices_is_a_named_error(self) -> None:
        self.stub.llm_response = (200, json.dumps({"choices": []}).encode())
        with self.assertRaises(llm.LlmError) as raised:
            llm.chat("soru", self.settings)
        self.assertEqual(raised.exception.code, "llm_empty")

    def test_tts_success_returns_audio_bytes(self) -> None:
        self.stub.tts_response = (200, audio_bytes())
        audio = tts.synthesize("merhaba", self.settings)
        self.assertEqual(audio, self.stub.tts_response[1])
        path, body, headers = self.stub.tts_calls()[0]
        self.assertIn("/v1/text-to-speech/stub-ses", path)
        self.assertIn("output_format=mp3_44100_128", path)
        self.assertEqual(headers.get("xi-api-key"), "stub-anahtar")
        self.assertEqual(json.loads(body)["model_id"], self.settings.elevenlabs_model)

    def test_tts_4xx_is_a_named_error_with_a_body_snippet(self) -> None:
        self.stub.tts_response = (422, b'{"detail": "voice not found"}')
        with self.assertRaises(tts.TtsError) as raised:
            tts.synthesize("merhaba", self.settings)
        self.assertEqual(raised.exception.code, "tts_error")
        self.assertIn("422", str(raised.exception))
        self.assertIn("voice not found", str(raised.exception))

    def test_tts_5xx_is_a_named_error(self) -> None:
        self.stub.tts_response = (500, b"sunucu hatasi")
        with self.assertRaises(tts.TtsError) as raised:
            tts.synthesize("merhaba", self.settings)
        self.assertEqual(raised.exception.code, "tts_error")

    def test_tts_timeout_is_reported_as_a_timeout(self) -> None:
        self.settings.elevenlabs_timeout_secs = 0.4
        self.stub.tts_response = (200, audio_bytes())
        self.stub.tts_delay = 1.2
        with self.assertRaises(tts.TtsError) as raised:
            tts.synthesize("merhaba", self.settings)
        self.assertEqual(raised.exception.code, "tts_timeout")

    def test_tts_malformed_body_is_rejected_instead_of_written_as_audio(self) -> None:
        self.stub.tts_response = (200, json.dumps({"detail": "kvota bitti"}).encode())
        with self.assertRaises(tts.TtsError) as raised:
            tts.synthesize("merhaba", self.settings)
        self.assertEqual(raised.exception.code, "tts_bad_audio")

    def test_tts_without_a_key_or_voice_makes_no_request(self) -> None:
        os.environ.pop("ELEVENLABS_API_KEY", None)
        with self.assertRaises(tts.TtsError) as raised:
            tts.synthesize("merhaba", self.settings, api_key_value="")
        self.assertEqual(raised.exception.code, "tts_key_missing")
        self.settings.elevenlabs_voice_id = ""
        with self.assertRaises(tts.TtsError) as raised:
            tts.synthesize("merhaba", self.settings, api_key_value="anahtar")
        self.assertEqual(raised.exception.code, "tts_voice_missing")
        self.assertEqual(self.stub.tts_calls(), [])

    def test_tts_unreachable_host_is_a_named_error(self) -> None:
        self.settings.elevenlabs_base_url = "http://127.0.0.1:9"
        self.settings.elevenlabs_timeout_secs = 1.0
        with self.assertRaises(tts.TtsError) as raised:
            tts.synthesize("merhaba", self.settings)
        self.assertEqual(raised.exception.code, "tts_unreachable")


class EngineEndToEndTests(EnvIsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.stub = StubServer()
        self.thread = threading.Thread(target=self.stub.serve_forever, daemon=True)
        self.thread.start()
        os.environ["LLM_API_KEY"] = "stub-anahtar"
        os.environ["ELEVENLABS_API_KEY"] = "stub-anahtar"
        os.environ["ELEVENLABS_VOICE_ID"] = "stub-ses"
        os.environ["LLM_MAX_ATTEMPTS"] = "1"
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-api-")
        root = Path(self._tmp.name)
        self.media_root = root / "medya"
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.output_root = root / "cikti"
        self.job_root = root / "isler"
        make_pdf(self.media_root / "ders.pdf", PAGE_TEXT)
        self.settings = config.Config(require_token=False)
        self.settings.media_root = str(self.media_root)
        self.settings.output_root = str(self.output_root)
        self.settings.llm_base_url = self.stub.base_url
        self.settings.elevenlabs_base_url = self.stub.base_url
        self.stub.llm_response = (
            200,
            json.dumps({"choices": [{"message": {"content": "Hucre anlatimi. Oldukca uzun bir metin."}}]}).encode(),
        )
        self.stub.tts_response = (200, audio_bytes())

    def tearDown(self) -> None:
        self.stub.shutdown()
        self.stub.server_close()
        self._tmp.cleanup()
        super().tearDown()

    def run_job(self, source_id: str, job_format: str):
        store = jobs.JobStore(
            root=self.job_root,
            workers=1,
            stage_secs=0.0,
            runner=api_engine.make_runner(self.settings),
            stages=api_engine.STAGES,
            job_secs=1.0,
            output_root=str(self.output_root),
        )
        seen: list[tuple[str, float]] = []
        original = store.update_progress

        def recording(job_id: str, stage: str, progress: float):
            seen.append((stage, progress))
            return original(job_id, stage, progress)

        store.update_progress = recording
        store.start()
        try:
            job_id = store.submit(source_id, job_format)[0]["job_id"]
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                record = store.get(job_id)
                if record["state"] in jobs.TERMINAL_STATES:
                    return record, seen
                time.sleep(0.02)
            self.fail(f"is {job_id} zaman asiminda bitmedi")
        finally:
            store.shutdown()

    def test_success_runs_every_stage_and_writes_the_deliverables(self) -> None:
        record, seen = self.run_job("ders.pdf", "tek_ogretici")
        self.assertEqual(record["state"], jobs.STATE_DONE)
        self.assertIsNone(record["error_code"])
        stages_seen = [stage for stage, _ in seen]
        unique = [
            stage
            for index, stage in enumerate(stages_seen)
            if index == 0 or stages_seen[index - 1] != stage
        ]
        self.assertEqual(unique, list(api_engine.STAGES))
        values = [value for _, value in seen]
        self.assertEqual(values, sorted(values))
        self.assertEqual(record["stage"], "done")
        self.assertEqual(record["progress"], 1.0)
        self.assertTrue(record["audio_id"].endswith(".mp3"), record["audio_id"])
        self.assertTrue(record["script_id"].endswith(".script.json"), record["script_id"])
        self.assertEqual(record["audio_ids"], [record["audio_id"]])
        self.assertGreater(record["duration_secs"], 0.0)
        audio = self.output_root / record["audio_id"]
        script = self.output_root / record["script_id"]
        self.assertTrue(audio.is_file())
        payload = json.loads(script.read_text(encoding="utf-8"))
        self.assertEqual(payload["format"], "tek_ogretici")
        self.assertIn("Hucre anlatimi", payload["metin"])
        self.assertEqual(len(self.stub.llm_calls()), 1)
        self.assertEqual(len(self.stub.tts_calls()), 1)
        leftovers = list(self.output_root.rglob("*.tmp*")) + list(
            self.output_root.rglob(".is-*")
        )
        self.assertEqual(leftovers, [])

    def test_plain_reading_skips_the_llm_entirely(self) -> None:
        self.stub.llm_response = (500, b"bu yola hic gidilmemeli")
        record, _ = self.run_job("ders.pdf", "duz_okuma")
        self.assertEqual(record["state"], jobs.STATE_DONE)
        self.assertEqual(self.stub.llm_calls(), [])

    def test_multiple_chapters_are_muxed_into_one_deliverable(self) -> None:
        self.settings.chapter_chars = 120
        self.settings.chapter_limit = -1
        record, _ = self.run_job("ders.pdf", "duz_okuma")
        self.assertEqual(record["state"], jobs.STATE_DONE)
        self.assertGreater(len(self.stub.tts_calls()), 1)
        self.assertEqual(len(record["script_ids"]), len(self.stub.tts_calls()))
        self.assertEqual(record["audio_ids"], [record["audio_id"]])
        self.assertGreater(record["duration_secs"], 1.5)
        self.assertTrue((self.output_root / record["audio_id"]).is_file())

    def test_tts_http_error_fails_the_job_and_leaves_no_audio(self) -> None:
        self.stub.tts_response = (500, b"sunucu hatasi")
        record, seen = self.run_job("ders.pdf", "tek_ogretici")
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "tts_error")
        self.assertIsNone(record["audio_id"])
        self.assertEqual(record["audio_ids"], [])
        self.assertEqual(list(self.output_root.rglob("*.mp3")), [])
        self.assertIn("tts", [stage for stage, _ in seen])
        self.assertNotIn("mux", [stage for stage, _ in seen])

    def test_tts_json_body_fails_the_job_instead_of_writing_fake_audio(self) -> None:
        self.stub.tts_response = (200, json.dumps({"detail": "kvota"}).encode())
        record, _ = self.run_job("ders.pdf", "tek_ogretici")
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "tts_bad_audio")
        self.assertEqual(list(self.output_root.rglob("*.mp3")), [])

    def test_tts_timeout_fails_the_job_with_the_timeout_code(self) -> None:
        self.settings.elevenlabs_timeout_secs = 0.4
        self.stub.tts_delay = 1.2
        record, _ = self.run_job("ders.pdf", "tek_ogretici")
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "tts_timeout")

    def test_missing_voice_fails_the_job_without_calling_tts(self) -> None:
        os.environ.pop("ELEVENLABS_VOICE_ID", None)
        self.settings.elevenlabs_voice_id = ""
        record, seen = self.run_job("ders.pdf", "tek_ogretici")
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "tts_voice_missing")
        self.assertEqual(self.stub.tts_calls(), [])
        self.assertNotIn("mux", [stage for stage, _ in seen])

    def test_llm_error_fails_the_job_before_any_tts_call(self) -> None:
        self.stub.llm_response = (400, b'{"error": "bad request"}')
        record, _ = self.run_job("ders.pdf", "tek_ogretici")
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "llm_error")
        self.assertEqual(self.stub.tts_calls(), [])

    def test_llm_empty_content_fails_the_job(self) -> None:
        self.stub.llm_response = (200, json.dumps({"choices": [{"message": {"content": "   "}}]}).encode())
        record, _ = self.run_job("ders.pdf", "tek_ogretici")
        self.assertEqual(record["state"], jobs.STATE_FAILED)
        self.assertEqual(record["error_code"], "llm_empty")

    def test_cancelled_during_tts_ends_cancelled_without_artifacts(self) -> None:
        self.stub.tts_delay = 0.6
        store = jobs.JobStore(
            root=self.job_root,
            workers=1,
            stage_secs=0.0,
            runner=api_engine.make_runner(self.settings),
            stages=api_engine.STAGES,
            job_secs=1.0,
            output_root=str(self.output_root),
        )
        store.start()
        try:
            job_id = store.submit("ders.pdf", "duz_okuma")[0]["job_id"]
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if store.get(job_id)["stage"] == api_engine.STAGES[3]:
                    break
                time.sleep(0.005)
            store.cancel(job_id)
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                record = store.get(job_id)
                if record["state"] in jobs.TERMINAL_STATES:
                    break
                time.sleep(0.02)
            self.assertEqual(record["state"], jobs.STATE_CANCELLED)
            self.assertIsNone(record["audio_id"])
            self.assertEqual(list(self.output_root.rglob("*.mp3")), [])
        finally:
            store.shutdown()

    def test_probe_reports_missing_voice_and_key_states(self) -> None:
        probe = api_engine.probe(self.settings)
        self.assertTrue(probe["llm_ready"])
        self.assertTrue(probe["tts_ready"])
        self.assertIn("duz_okuma", probe["formats"])
        os.environ.pop("ELEVENLABS_API_KEY", None)
        os.environ.pop("LLM_API_KEY", None)
        probe = api_engine.probe(self.settings)
        self.assertFalse(probe["llm_ready"])
        self.assertFalse(probe["tts_ready"])
        self.assertNotIn("tek_ogretici", probe["formats"])
        self.assertIn(capabilities.KEYLESS_FORMATS[0], probe["formats"])


class EngineSelectionTests(EnvIsolatedTestCase):
    def test_engine_defaults_to_api_and_local_is_still_accepted(self) -> None:
        self.assertEqual(config.Config(require_token=False).engine, "api")
        os.environ["PODCAST_ENGINE"] = "local"
        self.assertEqual(config.Config(require_token=False).engine, "local")

    def test_unknown_engine_crashes_the_boot(self) -> None:
        os.environ["PODCAST_ENGINE"] = "bulut"
        with self.assertRaises(config.ConfigError):
            config.Config(require_token=False)

    def test_local_engine_in_real_mode_still_requires_the_vendored_tree(self) -> None:
        from src import pipeline

        class FakeSettings:
            mode = "real"
            engine = "local"
            eta_secs = 0.0
            pipeline_path = ""

        self.assertEqual(pipeline.stages_for(FakeSettings), pipeline.REAL_STAGES)
        with self.assertRaises(pipeline.PipelineUnavailable):
            pipeline.build_runner(FakeSettings)

    def test_api_engine_never_needs_the_vendored_tree(self) -> None:
        from src import pipeline

        os.environ["PODCAST_MODE"] = "real"
        os.environ["PODCAST_ENGINE"] = "api"
        settings = config.Config(require_token=False)
        self.assertEqual(pipeline.stages_for(settings), api_engine.STAGES)
        self.assertEqual(pipeline.job_secs_for(settings), api_engine.ETA_SECS)
        runner, probe = api_engine.build_runner(settings)
        self.assertTrue(callable(runner))
        self.assertFalse(probe["llm_ready"])
        self.assertFalse(probe["tts_ready"])
        self.assertNotIn(pipeline.PIPELINE_MODULE, __import__("sys").modules)


if __name__ == "__main__":
    unittest.main()
