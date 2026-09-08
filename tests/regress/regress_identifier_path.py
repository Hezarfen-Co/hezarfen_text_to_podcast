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


class IdentifierPathRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-kimlik-")
        self.root = Path(self._tmp.name) / "cikti"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        config._active_level = self._log_level

    def path(self, stem: str, format_name: str, file: str) -> str:
        return str(self.root / stem / "ses" / format_name / file)

    def script_path(self, stem: str, format_name: str, file: str) -> str:
        return str(self.root / stem / "script" / format_name / file)

    def output(self, mp3: list, scripts: list, duration: float = 10.0) -> dict:
        return pipeline.derive_outputs(FakeResult(mp3, scripts, duration), str(self.root))

    def test_audio_id_must_be_a_path_relative_to_the_output_root_not_the_file_name(self) -> None:
        output = self.output(
            [self.path("ders", "duz_okuma", "ders-b01.mp3")],
            [self.script_path("ders", "duz_okuma", "ders-b01.script.json")],
        )
        self.assertEqual(
            output["audio_id"],
            "ders/ses/duz_okuma/ders-b01.mp3",
            "audio_id dosya adina indirgendi; uc dizin seviyesi kayboldu",
        )
        self.assertEqual(
            output["audio_id"].count("/"),
            3,
            "audio_id <stem>/ses/<format>/<dosya> yapisini korumali",
        )

    def test_script_id_must_also_be_a_path_relative_to_the_output_root(self) -> None:
        output = self.output(
            [self.path("ders", "duz_okuma", "ders-b01.mp3")],
            [self.script_path("ders", "duz_okuma", "ders-b01.script.json")],
        )
        self.assertEqual(
            output["script_id"],
            "ders/script/duz_okuma/ders-b01.script.json",
            "script_id dosya adina indirgendi",
        )

    def test_same_pdf_in_a_different_format_must_return_a_different_identifier(self) -> None:
        plain = self.output(
            [self.path("ders", "duz_okuma", "ders-b01.mp3")],
            [self.script_path("ders", "duz_okuma", "ders-b01.script.json")],
        )
        single = self.output(
            [self.path("ders", "tek_ogretici", "ders-b01.mp3")],
            [self.script_path("ders", "tek_ogretici", "ders-b01.script.json")],
        )
        self.assertNotEqual(
            plain["audio_id"],
            single["audio_id"],
            "ayni PDF'in duz_okuma ve tek_ogretici kosusu AYNI audio_id dondu; "
            "bolum_id formattan bagimsiz oldugu icin kimlikler carpisiyor",
        )
        self.assertNotEqual(
            plain["script_id"], single["script_id"], "iki formatin script kimlikleri carpisti"
        )

    def test_different_chapters_of_the_same_pdf_must_not_collide_either(self) -> None:
        output = self.output(
            [
                self.path("ders", "duz_okuma", "ders-b01.mp3"),
                self.path("ders", "duz_okuma", "ders-b02.mp3"),
                self.path("ders", "duz_okuma", "ders-b03.mp3"),
            ],
            [],
        )
        self.assertEqual(
            len(set(output["audio_ids"])), 3, "bolum kimlikleri birbirine carpisti"
        )

    def test_different_pdfs_must_not_collide_even_with_the_same_chapter_name(self) -> None:
        first = self.output([self.path("ders-a", "duz_okuma", "ders-b01.mp3")], [])
        second = self.output([self.path("ders-b", "duz_okuma", "ders-b01.mp3")], [])
        self.assertNotEqual(
            first["audio_id"],
            second["audio_id"],
            "iki farkli PDF ayni audio_id dondu",
        )

    def test_path_falling_outside_the_root_must_drop_to_the_file_name(self) -> None:
        output = self.output(["/baska/yer/x.mp3"], ["/baska/yer/x.script.json"])
        self.assertEqual(
            output["audio_id"],
            "x.mp3",
            "cikti kokunun disindaki yol dosya adina dusmeli, mutlak yol sizmamali",
        )
        self.assertEqual(output["script_id"], "x.script.json")

    def test_identifier_must_always_use_the_posix_separator(self) -> None:
        output = self.output([self.path("ders", "duz_okuma", "ders-b01.mp3")], [])
        self.assertNotIn(
            "\\", output["audio_id"], "kimlik Windows ayraci tasiyor; kablo sozlesmesi posix"
        )

    def test_identifier_must_not_be_an_absolute_path(self) -> None:
        output = self.output([self.path("ders", "duz_okuma", "ders-b01.mp3")], [])
        self.assertFalse(
            Path(output["audio_id"]).is_absolute(),
            "kimlik mutlak yol sizdiriyor; container ici yol backend'e gitmemeli",
        )


if __name__ == "__main__":
    unittest.main()
