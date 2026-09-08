import os
import tempfile
import unittest
from pathlib import Path

from src import config

ABSOLUTE_FIELDS = ("job_root", "output_root", "media_root", "ledger_db")

ENV_NAMES = (
    "LOG_LEVEL",
    "AI_SHARED_TOKEN",
    "PODCAST_JOB_ROOT",
    "PODCAST_OUTPUT_ROOT",
    "PODCAST_MEDIA_ROOT",
    "PODCAST_LEDGER_DB",
    "PODCAST_MODE",
    "PODCAST_PIPELINE_PATH",
    "DEEPSEEK_API_KEY",
)


class AbsolutePathRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        self._previous = {name: os.environ.get(name) for name in ENV_NAMES}
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        self._previous_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-mutlak-")
        os.chdir(self._tmp.name)

    def tearDown(self) -> None:
        os.chdir(self._previous_cwd)
        self._tmp.cleanup()
        for name, value in self._previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        config._active_level = self._log_level

    def test_every_relative_root_that_is_given_must_be_made_absolute(self) -> None:
        os.environ["PODCAST_JOB_ROOT"] = "isler"
        os.environ["PODCAST_OUTPUT_ROOT"] = "out"
        os.environ["PODCAST_MEDIA_ROOT"] = "medya"
        os.environ["PODCAST_LEDGER_DB"] = "defter.sqlite"
        settings = config.Config(require_token=False)
        for field in ABSOLUTE_FIELDS:
            value = getattr(settings, field)
            with self.subTest(field=field):
                self.assertTrue(
                    Path(value).is_absolute(),
                    f"{field} goreli kaldi ({value}); hat ffmpeg concat listesi yolunu "
                    f"IKIYE KATLAR ve montaj patlar",
                )

    def test_a_relative_root_starting_with_a_dot_must_also_become_absolute(self) -> None:
        os.environ["PODCAST_OUTPUT_ROOT"] = os.path.join(".", "out")
        settings = config.Config(require_token=False)
        self.assertTrue(Path(settings.output_root).is_absolute())
        self.assertNotIn(
            f"{os.sep}.{os.sep}", settings.output_root, "nokta parcasi cozulmedi"
        )

    def test_parent_directory_parts_must_be_resolved(self) -> None:
        os.environ["PODCAST_OUTPUT_ROOT"] = os.path.join("a", "..", "out")
        settings = config.Config(require_token=False)
        self.assertTrue(Path(settings.output_root).is_absolute())
        self.assertNotIn("..", settings.output_root, "ust dizin parcasi cozulmedi")

    def test_the_default_roots_must_be_absolute_too(self) -> None:
        settings = config.Config(require_token=False)
        for field in ABSOLUTE_FIELDS:
            with self.subTest(field=field):
                self.assertTrue(
                    Path(getattr(settings, field)).is_absolute(),
                    f"varsayilan {field} mutlak degil",
                )

    def test_a_root_that_is_given_as_absolute_must_not_be_changed(self) -> None:
        target = Path(self._tmp.name).resolve() / "sabit"
        os.environ["PODCAST_OUTPUT_ROOT"] = str(target)
        settings = config.Config(require_token=False)
        self.assertEqual(Path(settings.output_root), target)

    def test_the_output_root_must_be_independent_of_the_working_directory(self) -> None:
        os.environ["PODCAST_OUTPUT_ROOT"] = "out"
        first = config.Config(require_token=False).output_root
        child = Path(self._tmp.name) / "alt"
        child.mkdir(parents=True, exist_ok=True)
        os.chdir(child)
        second = config.Config(require_token=False).output_root
        self.assertNotEqual(
            first,
            second,
            "goreli kok cozulmemis; ayni ayar iki dizinde ayni yolu gosteriyor",
        )
        for value in (first, second):
            with self.subTest(value=value):
                self.assertTrue(Path(value).is_absolute())

    def test_absolute_paths_must_appear_in_the_summary_output_too(self) -> None:
        os.environ["PODCAST_OUTPUT_ROOT"] = "out"
        settings = config.Config(require_token=False)
        self.assertIn(settings.output_root, settings.summary())


if __name__ == "__main__":
    unittest.main()
