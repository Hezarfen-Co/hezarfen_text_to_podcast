import tempfile
import unittest
from pathlib import Path

from src import config, pipeline


class FakeResult:
    def __init__(self, mp3: list, scripts: list, duration: float) -> None:
        self.mp3_yollari = mp3
        self.script_yollari = scripts
        self.ses_toplam_sn = duration
        self.adimlar: list = []


class ScriptAlignmentRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-hizalama-")
        self.root = Path(self._tmp.name) / "cikti"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        config._active_level = self._log_level

    def audio(self, chapter: str, format_name: str = "duz_okuma") -> str:
        return str(self.root / "ders" / "ses" / format_name / f"ders-{chapter}.mp3")

    def script(self, chapter: str, format_name: str = "duz_okuma") -> str:
        return str(self.root / "ders" / "script" / format_name / f"ders-{chapter}.script.json")

    def output(self, mp3: list, scripts: list) -> dict:
        return pipeline.derive_outputs(FakeResult(mp3, scripts, 10.0), str(self.root))

    def test_scripts_must_be_trimmed_when_the_chapter_limit_applies_only_to_audio(self) -> None:
        outputs = self.output(
            [self.audio("b01")],
            [self.script("b01"), self.script("b02"), self.script("b03")],
        )
        self.assertEqual(
            len(outputs["script_ids"]),
            1,
            "bolum_limiti yalnizca sese uygulanirken script_ids TUM bolumleri tasidi; "
            "sonuc ses ile script arasinda uyumsuz",
        )
        self.assertEqual(outputs["script_ids"], ["ders/script/duz_okuma/ders-b01.script.json"])

    def test_the_trimmed_script_must_belong_to_the_chapter_that_was_voiced(self) -> None:
        outputs = self.output(
            [self.audio("b02")],
            [self.script("b01"), self.script("b02"), self.script("b03")],
        )
        self.assertEqual(outputs["script_ids"], ["ders/script/duz_okuma/ders-b02.script.json"])
        self.assertEqual(outputs["script_id"], "ders/script/duz_okuma/ders-b02.script.json")

    def test_nothing_must_be_trimmed_when_the_audio_and_script_counts_are_equal(self) -> None:
        outputs = self.output(
            [self.audio("b01"), self.audio("b02")],
            [self.script("b01"), self.script("b02")],
        )
        self.assertEqual(len(outputs["script_ids"]), 2)
        self.assertEqual(len(outputs["audio_ids"]), 2)

    def test_scripts_must_be_returned_unfiltered_when_nothing_matches_at_all(self) -> None:
        outputs = self.output(
            [self.audio("b01")],
            [self.script("x99"), self.script("y98")],
        )
        self.assertEqual(
            len(outputs["script_ids"]),
            2,
            "eslesme hic tutmadiginda scriptler tamamen silinmemeli; "
            "bos script listesi donmek veri kaybidir",
        )

    def test_the_audio_identifiers_must_not_be_broken_when_there_is_no_script(self) -> None:
        outputs = self.output([self.audio("b01")], [])
        self.assertEqual(outputs["audio_ids"], ["ders/ses/duz_okuma/ders-b01.mp3"])
        self.assertEqual(outputs["script_ids"], [])
        self.assertEqual(outputs["script_id"], "")

    def test_the_scripts_must_stay_untouched_when_there_is_no_audio(self) -> None:
        outputs = self.output([], [self.script("b01"), self.script("b02")])
        self.assertEqual(len(outputs["script_ids"]), 2)

    def test_the_chapter_key_must_be_normalised_regardless_of_the_extension(self) -> None:
        for extension in (".script.json", ".json", ""):
            with self.subTest(extension=extension):
                self.assertEqual(pipeline._chapter_key(f"a/b/ders-b01{extension}"), "ders-b01")
        self.assertEqual(pipeline._chapter_key("a/b/ders-b01.mp3"), "ders-b01")

    def test_the_singular_script_id_must_be_the_first_of_the_trimmed_list(self) -> None:
        outputs = self.output(
            [self.audio("b03")],
            [self.script("b01"), self.script("b02"), self.script("b03")],
        )
        self.assertEqual(outputs["script_id"], outputs["script_ids"][0])
        self.assertIn("b03", outputs["script_id"])


if __name__ == "__main__":
    unittest.main()
