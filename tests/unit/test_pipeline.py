import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from src import config, jobs, pipeline


class FakeResult:
    def __init__(self, mp3: list, scripts: list, duration) -> None:
        self.mp3_yollari = mp3
        self.script_yollari = scripts
        self.ses_toplam_sn = duration
        self.adimlar: list = []


class FakeInterrupt(RuntimeError):
    pass


class FakeContext:
    def __init__(self, cancelled: bool, stages: tuple) -> None:
        self._cancelled = cancelled
        self.stages = stages
        self.seen: list = []

    def cancelled(self) -> bool:
        return self._cancelled

    def progress(self, stage: str, value: float) -> None:
        self.seen.append((stage, value))


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-hat-")
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        config._active_level = self._log_level

    def output_path(self, *parts: str) -> str:
        return str(self.root / "cikti" / Path(*parts))


class ModeGateTests(PipelineTestCase):
    def test_mode_set_matches_the_config_module(self) -> None:
        self.assertEqual(pipeline.MODES, ("simulate", "real"))
        self.assertEqual(pipeline.MODES, config.PIPELINE_MODES)

    def test_real_pipeline_module_is_never_imported_at_module_level(self) -> None:
        self.assertNotIn(
            pipeline.PIPELINE_MODULE,
            sys.modules,
            "gercek hat tembel import edilmeli; modul seviyesinde yuklenmemeli",
        )

    def test_blank_pipeline_path_is_rejected(self) -> None:
        for bad in ("", "   ", None, 7):
            with self.subTest(pipeline_path=bad):
                with self.assertRaises(pipeline.PipelineUnavailable):
                    pipeline.check_pipeline_path(bad)

    def test_missing_directory_is_rejected(self) -> None:
        with self.assertRaises(pipeline.PipelineUnavailable):
            pipeline.check_pipeline_path(str(self.root / "olmayan"))

    def test_directory_missing_any_required_file_is_rejected(self) -> None:
        for missing in pipeline.REQUIRED_PIPELINE_FILES:
            with self.subTest(missing=missing):
                fake_dir = self.root / ("hat-" + missing.replace("/", "-"))
                for part in pipeline.REQUIRED_PIPELINE_FILES:
                    if part == missing:
                        continue
                    target = fake_dir / part
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("", encoding="ascii")
                with self.assertRaises(pipeline.PipelineUnavailable):
                    pipeline.check_pipeline_path(str(fake_dir))

    def test_directory_with_every_required_file_is_accepted(self) -> None:
        fake_dir = self.root / "hat-tam"
        for part in pipeline.REQUIRED_PIPELINE_FILES:
            target = fake_dir / part
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="ascii")
        self.assertEqual(pipeline.check_pipeline_path(str(fake_dir)), fake_dir.resolve())

    def test_simulate_mode_forces_neither_real_stages_nor_real_eta(self) -> None:
        class FakeSettings:
            mode = "simulate"
            eta_secs = 0.0

        self.assertIsNone(pipeline.stages_for(FakeSettings))
        self.assertIsNone(pipeline.job_secs_for(FakeSettings))

    def test_real_mode_uses_the_real_stage_names_and_eta(self) -> None:
        class FakeSettings:
            mode = "real"
            engine = "local"
            eta_secs = 0.0

        self.assertEqual(pipeline.stages_for(FakeSettings), pipeline.REAL_STAGES)
        self.assertEqual(pipeline.job_secs_for(FakeSettings), pipeline.REAL_ETA_SECS)

    def test_api_engine_uses_its_own_stage_names_and_eta(self) -> None:
        from src import api_engine

        class FakeSettings:
            mode = "real"
            engine = "api"
            eta_secs = 0.0

        self.assertEqual(pipeline.stages_for(FakeSettings), api_engine.STAGES)
        self.assertEqual(pipeline.job_secs_for(FakeSettings), api_engine.ETA_SECS)
        self.assertEqual(pipeline.ENGINE_API, "api")
        self.assertEqual(pipeline.ENGINES, config.PIPELINE_ENGINES)

    def test_explicit_eta_overrides_the_mode_default(self) -> None:
        class FakeSettings:
            mode = "real"
            eta_secs = 900.0

        self.assertEqual(pipeline.job_secs_for(FakeSettings), 900.0)


class LimitTests(PipelineTestCase):
    def test_zero_and_negative_limits_mean_all_chapters(self) -> None:
        for value in (0, -1, -4096):
            with self.subTest(chapter_limit=value):
                self.assertIsNone(pipeline.normalize_limit(value))

    def test_positive_limit_is_preserved(self) -> None:
        self.assertEqual(pipeline.normalize_limit(3), 3)


class ResolvePdfTests(PipelineTestCase):
    def test_existing_file_under_the_school_directory_resolves(self) -> None:
        media = self.root / "medya"
        (media / "okul-a").mkdir(parents=True, exist_ok=True)
        (media / "okul-a" / "ders.pdf").write_bytes(b"%PDF-1.4\n")
        self.assertEqual(
            pipeline.resolve_pdf(str(media), "okul-a", "ders.pdf"),
            (media / "okul-a" / "ders.pdf").resolve(),
        )

    def test_a_flat_file_is_not_found_without_the_school_segment(self) -> None:
        media = self.root / "medya-duz"
        media.mkdir(parents=True, exist_ok=True)
        (media / "ders.pdf").write_bytes(b"%PDF-1.4\n")
        with self.assertRaises(FileNotFoundError):
            pipeline.resolve_pdf(str(media), "okul-a", "ders.pdf")

    def test_missing_file_raises_file_not_found(self) -> None:
        media = self.root / "medya-bos"
        media.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(FileNotFoundError):
            pipeline.resolve_pdf(str(media), "okul-a", "yok.pdf")

    def test_a_directory_is_not_accepted_as_a_source_file(self) -> None:
        media = self.root / "medya-dizin"
        (media / "okul-a" / "ders.pdf").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(FileNotFoundError):
            pipeline.resolve_pdf(str(media), "okul-a", "ders.pdf")

    def test_escaping_source_key_raises_value_error_before_touching_disk(self) -> None:
        media = self.root / "medya-kacis"
        media.mkdir(parents=True, exist_ok=True)
        for bad in ("..", "../x", "a/b", ""):
            with self.subTest(source_key=bad):
                with self.assertRaises(ValueError):
                    pipeline.resolve_pdf(str(media), "okul-a", bad)
        for bad in ("..", "../x", "a/b", "", "OKUL"):
            with self.subTest(school=bad):
                with self.assertRaises(ValueError):
                    pipeline.resolve_pdf(str(media), bad, "ders.pdf")


class DeriveOutputsTests(PipelineTestCase):
    def test_identifiers_are_relative_to_the_output_root(self) -> None:
        root = str(self.root / "cikti")
        outputs = pipeline.derive_outputs(
            FakeResult(
                [self.output_path("ders", "ses", "duz_okuma", "ders-b01.mp3")],
                [self.output_path("ders", "script", "duz_okuma", "ders-b01.script.json")],
                321.25,
            ),
            root,
        )
        self.assertEqual(outputs["audio_id"], "ders/ses/duz_okuma/ders-b01.mp3")
        self.assertEqual(outputs["script_id"], "ders/script/duz_okuma/ders-b01.script.json")
        self.assertEqual(outputs["duration_secs"], 321.25)

    def test_first_identifier_leads_the_plural_lists(self) -> None:
        root = str(self.root / "cikti")
        outputs = pipeline.derive_outputs(
            FakeResult(
                [self.output_path("d", "ses", "duz_okuma", "d-b01.mp3"),
                 self.output_path("d", "ses", "duz_okuma", "d-b02.mp3")],
                [self.output_path("d", "script", "duz_okuma", "d-b01.script.json"),
                 self.output_path("d", "script", "duz_okuma", "d-b02.script.json")],
                10.0,
            ),
            root,
        )
        self.assertEqual(len(outputs["audio_ids"]), 2)
        self.assertEqual(outputs["audio_id"], outputs["audio_ids"][0])
        self.assertEqual(outputs["script_id"], outputs["script_ids"][0])

    def test_paths_outside_the_root_fall_back_to_the_file_name(self) -> None:
        outputs = pipeline.derive_outputs(
            FakeResult(["/baska/yer/x.mp3"], ["/baska/yer/x.script.json"], 1.0),
            str(self.root / "cikti"),
        )
        self.assertEqual(outputs["audio_id"], "x.mp3")
        self.assertEqual(outputs["script_id"], "x.script.json")

    def test_empty_result_produces_empty_identifiers_not_none(self) -> None:
        outputs = pipeline.derive_outputs(FakeResult([], [], 0.0), str(self.root / "cikti"))
        self.assertEqual(outputs["audio_id"], "")
        self.assertEqual(outputs["script_id"], "")
        self.assertEqual(outputs["audio_ids"], [])
        self.assertEqual(outputs["script_ids"], [])

    def test_unparseable_duration_degrades_to_zero(self) -> None:
        for duration in (None, "uzun", object()):
            with self.subTest(duration=duration):
                outputs = pipeline.derive_outputs(
                    FakeResult(["/o/x.mp3"], ["/o/x.json"], duration), str(self.root / "cikti")
                )
                self.assertEqual(outputs["duration_secs"], 0.0)

    def test_without_an_output_root_identifiers_degrade_to_file_names(self) -> None:
        outputs = pipeline.derive_outputs(
            FakeResult([self.output_path("d", "ses", "duz_okuma", "d-b01.mp3")], [], 1.0)
        )
        self.assertEqual(outputs["audio_id"], "d-b01.mp3")


class LogHookTests(PipelineTestCase):
    def test_hook_raises_the_pipelines_own_interrupt_when_cancelled(self) -> None:
        ctx = FakeContext(True, pipeline.REAL_STAGES)
        hook = pipeline.build_log_hook(ctx, lambda: 1, pipeline.REAL_STAGES, FakeInterrupt)
        with self.assertRaises(FakeInterrupt):
            hook("[KOSTU ] ingest")
        self.assertEqual(
            ctx.seen, [], "iptal edilen iste kanca ilerleme yazmamali"
        )

    def test_hook_derives_progress_from_the_completed_step_count(self) -> None:
        for step, expected in ((0, ("ingest", 0.0)), (2, ("quiz", 0.5)), (3, ("ses", 0.75))):
            with self.subTest(step=step):
                ctx = FakeContext(False, pipeline.REAL_STAGES)
                hook = pipeline.build_log_hook(
                    ctx, lambda step=step: step, pipeline.REAL_STAGES, FakeInterrupt
                )
                hook("[KOSTU ] bir sey")
                self.assertEqual(ctx.seen, [expected])

    def test_step_count_beyond_the_stage_list_is_clamped_to_the_last_stage(self) -> None:
        ctx = FakeContext(False, pipeline.REAL_STAGES)
        hook = pipeline.build_log_hook(ctx, lambda: 99, pipeline.REAL_STAGES, FakeInterrupt)
        hook("[KOSTU ] tasma")
        self.assertEqual(ctx.seen, [(pipeline.REAL_STAGES[-1], 1.0)])

    def test_a_broken_step_counter_does_not_kill_the_run(self) -> None:
        def broken() -> int:
            raise RuntimeError("sayac patladi")

        ctx = FakeContext(False, pipeline.REAL_STAGES)
        hook = pipeline.build_log_hook(ctx, broken, pipeline.REAL_STAGES, FakeInterrupt)
        hook("[KOSTU ] ingest")
        self.assertEqual(ctx.seen, [(pipeline.REAL_STAGES[0], 0.0)])

    def test_negative_step_count_is_floored_at_zero(self) -> None:
        ctx = FakeContext(False, pipeline.REAL_STAGES)
        hook = pipeline.build_log_hook(ctx, lambda: -5, pipeline.REAL_STAGES, FakeInterrupt)
        hook("[KOSTU ] ingest")
        self.assertEqual(ctx.seen, [(pipeline.REAL_STAGES[0], 0.0)])


class ErrorCodeTests(PipelineTestCase):
    def test_pipeline_error_codes_are_stable(self) -> None:
        self.assertEqual(pipeline.SOURCE_NOT_FOUND, "source_not_found")
        self.assertEqual(pipeline.NO_AUDIO, "no_audio")
        self.assertEqual(pipeline.PIPELINE_MISCONFIGURED, "pipeline_misconfigured")

    def test_real_stage_names_match_the_documented_four(self) -> None:
        self.assertEqual(pipeline.REAL_STAGES, ("ingest", "scriptler", "quiz", "ses"))

    def test_simulate_warning_never_hides_that_the_audio_is_fake(self) -> None:
        self.assertIn("GERCEK DEGIL", pipeline.FAKE_WARNING)



class TranscriptJoinTests(PipelineTestCase):
    def test_chapter_bodies_are_joined_with_a_blank_line(self) -> None:
        output = self.root / "cikti"
        output.mkdir()
        first = output / "b01.script.json"
        second = output / "b02.script.json"
        first.write_text(json.dumps({"metin": "ilk bolum"}), encoding="utf-8")
        second.write_text(json.dumps({"metin": "ikinci bolum"}), encoding="utf-8")
        joined = pipeline.joined_transcript(
            ["b01.script.json", "b02.script.json"], output
        )
        self.assertEqual(joined, "ilk bolum\n\nikinci bolum")

    def test_a_missing_or_empty_script_stays_an_empty_string(self) -> None:
        output = self.root / "cikti-bos"
        output.mkdir()
        (output / "bos.script.json").write_text(
            json.dumps({"metin": ""}), encoding="utf-8"
        )
        (output / "not-json.script.json").write_text("bu json degil", encoding="utf-8")
        self.assertEqual(pipeline.joined_transcript(["yok.script.json"], output), "")
        self.assertEqual(pipeline.joined_transcript(["bos.script.json"], output), "")
        self.assertEqual(pipeline.joined_transcript(["not-json.script.json"], output), "")
        self.assertEqual(pipeline.joined_transcript([], output), "")

    def test_the_runner_puts_the_joined_transcript_on_the_done_record(self) -> None:
        media = self.root / "medya"
        (media / "okul-a").mkdir(parents=True)
        (media / "okul-a" / "ders.pdf").write_bytes(b"%PDF-1.4\n")
        output = self.root / "cikti-hat"
        output.mkdir()
        first = output / "b01.script.json"
        second = output / "b02.script.json"
        first.write_text(json.dumps({"metin": "alfa"}), encoding="utf-8")
        second.write_text(json.dumps({"metin": "beta"}), encoding="utf-8")
        audio = output / "ders.mp3"
        audio.write_bytes(b"ID3")

        class Factory:
            def __init__(self, **kwargs) -> None:
                self.sonuc = type("Result", (), {})()
                self.sonuc.mp3_yollari = [str(audio)]
                self.sonuc.script_yollari = [str(first), str(second)]
                self.sonuc.ses_toplam_sn = 4.0
                self.sonuc.adimlar = []

            def kos(self):
                return self.sonuc

        runner = pipeline.make_runner(
            str(media),
            str(output),
            0,
            "supertonic-3",
            Factory,
            RuntimeError,
            RuntimeError,
        )
        store = jobs.JobStore(
            root=self.root / "isler",
            workers=1,
            stage_secs=0.0,
            runner=runner,
            stages=pipeline.REAL_STAGES,
        )
        try:
            store.start()
            job_id = store.submit(
                "11111111-1111-7111-8111-111111111111",
                "ders.pdf",
                "duz_okuma",
                school="okul-a",
                source_key="ders.pdf",
            )[0]["job_id"]
            limit = time.monotonic() + 5.0
            final = store.get(job_id)
            while final["state"] not in (jobs.STATE_DONE, jobs.STATE_FAILED) and time.monotonic() < limit:
                time.sleep(0.01)
                final = store.get(job_id)
        finally:
            store.shutdown(timeout=1.0)
        self.assertEqual(final["state"], jobs.STATE_DONE, final.get("error_code"))
        self.assertEqual(final["transcript"], "alfa\n\nbeta")

    def test_a_runner_with_no_script_files_finishes_with_an_empty_transcript(self) -> None:
        media = self.root / "medya-bos"
        (media / "okul-a").mkdir(parents=True)
        (media / "okul-a" / "ders.pdf").write_bytes(b"%PDF-1.4\n")
        output = self.root / "cikti-yok"
        output.mkdir()
        audio = output / "ders.mp3"
        audio.write_bytes(b"ID3")

        class Factory:
            def __init__(self, **kwargs) -> None:
                self.sonuc = type("Result", (), {})()
                self.sonuc.mp3_yollari = [str(audio)]
                self.sonuc.script_yollari = [str(output / "yok.script.json")]
                self.sonuc.ses_toplam_sn = 1.0
                self.sonuc.adimlar = []

            def kos(self):
                return self.sonuc

        runner = pipeline.make_runner(
            str(media),
            str(output),
            0,
            "supertonic-3",
            Factory,
            RuntimeError,
            RuntimeError,
        )
        store = jobs.JobStore(
            root=self.root / "isler-bos",
            workers=1,
            stage_secs=0.0,
            runner=runner,
            stages=pipeline.REAL_STAGES,
        )
        try:
            store.start()
            job_id = store.submit(
                "22222222-2222-7222-8222-222222222222",
                "ders.pdf",
                "duz_okuma",
                school="okul-a",
                source_key="ders.pdf",
            )[0]["job_id"]
            limit = time.monotonic() + 5.0
            final = store.get(job_id)
            while final["state"] not in (jobs.STATE_DONE, jobs.STATE_FAILED) and time.monotonic() < limit:
                time.sleep(0.01)
                final = store.get(job_id)
        finally:
            store.shutdown(timeout=1.0)
        self.assertEqual(final["state"], jobs.STATE_DONE, final.get("error_code"))
        self.assertEqual(final["transcript"], "")


if __name__ == "__main__":
    unittest.main()
