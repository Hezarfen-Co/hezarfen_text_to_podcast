import os
import re
import unittest
from pathlib import Path

from src import capabilities

LOCKED_FORMATS = ("tek_ogretici", "ogrenci_hoca", "duz_okuma")
LOCKED_DEFAULT = "duz_okuma"

REPO_ROOT = Path(__file__).resolve().parents[2]

ENUM_PATTERN = re.compile(r"^\s{4}[A-Z_]+\s*=\s*\"([a-z_]+)\"\s*$", re.MULTILINE)
CHOICES_PATTERN = re.compile(r"choices=\[([^\]]*)\]")
CLI_DEFAULT_PATTERN = re.compile(r"\"--format\",\s*default=\"([a-z_]+)\"")


def pipeline_roots() -> list:
    candidates = []
    env_path = os.environ.get("PODCAST_PIPELINE_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(REPO_ROOT / "vendor" / "pipeline")
    candidates.append(REPO_ROOT.parent)
    return candidates


def pipeline_file(relative: str) -> tuple:
    tried = []
    for root in pipeline_roots():
        candidate = root / relative
        tried.append(str(candidate))
        if candidate.is_file():
            return candidate, tried
    return None, tried


class FormatContractRegression(unittest.TestCase):
    def test_service_format_set_must_not_drift_from_the_locked_contract(self) -> None:
        self.assertEqual(
            tuple(capabilities.FORMATS),
            LOCKED_FORMATS,
            "format kumesi projenin kilitli sozlesmesinden sapti",
        )

    def test_default_format_must_stay_duz_okuma(self) -> None:
        self.assertEqual(
            capabilities.DEFAULT_FORMAT,
            LOCKED_DEFAULT,
            "varsayilan format hattin varsayilaniyla ayni kalmali",
        )

    def test_keyless_format_set_must_be_only_duz_okuma(self) -> None:
        self.assertEqual(
            capabilities.KEYLESS_FORMATS,
            (LOCKED_DEFAULT,),
            "LLM anahtari yokken yalnizca duz_okuma acik kalmali",
        )

    def test_pipeline_plan_py_format_enum_must_define_the_same_set_as_the_service(self) -> None:
        path, tried = pipeline_file(os.path.join("script", "plan.py"))
        if path is None:
            self.fail(
                "gercegin kaynagi script/plan.py bulunamadi; hat yolu degismis olabilir. "
                f"denenen yollar: {tried}"
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        body = text.split("class Format(str, Enum):", 1)
        self.assertEqual(
            len(body), 2, f"{path} icinde 'class Format(str, Enum):' bulunamadi"
        )
        section = body[1].split("\nclass ", 1)[0]
        values = ENUM_PATTERN.findall(section)
        self.assertEqual(
            set(values),
            set(LOCKED_FORMATS),
            f"hattin Format enum'u servisin format kumesinden farkli ({path})",
        )
        self.assertEqual(
            set(values),
            set(capabilities.FORMATS),
            f"capabilities.FORMATS hattin Format enum'uyla ayni kumeyi tasimali ({path})",
        )

    def test_pipeline_cli_format_choices_must_define_the_same_set_as_the_service(self) -> None:
        path, tried = pipeline_file(os.path.join("router", "cli.py"))
        if path is None:
            self.fail(
                "gercegin kaynagi router/cli.py bulunamadi; hat yolu degismis olabilir. "
                f"denenen yollar: {tried}"
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        format_lists = []
        for raw in CHOICES_PATTERN.findall(text):
            choices = {part.strip().strip("\"'") for part in raw.split(",") if part.strip()}
            if LOCKED_DEFAULT in choices:
                format_lists.append(choices)
        self.assertTrue(
            format_lists,
            f"{path} icinde format choices listesi bulunamadi; cli sozlesmesi degismis olabilir",
        )
        for choices in format_lists:
            with self.subTest(choices=sorted(choices)):
                self.assertEqual(
                    choices,
                    set(capabilities.FORMATS),
                    f"cli format secenekleri servisin format kumesinden farkli ({path})",
                )

    def test_pipeline_cli_default_format_must_match_the_service_default(self) -> None:
        path, tried = pipeline_file(os.path.join("router", "cli.py"))
        if path is None:
            self.fail(
                "gercegin kaynagi router/cli.py bulunamadi; hat yolu degismis olabilir. "
                f"denenen yollar: {tried}"
            )
        text = path.read_text(encoding="utf-8", errors="replace")
        defaults = set(CLI_DEFAULT_PATTERN.findall(text))
        self.assertTrue(
            defaults, f"{path} icinde --format varsayilani bulunamadi"
        )
        self.assertEqual(
            defaults,
            {capabilities.DEFAULT_FORMAT},
            f"cli varsayilan formati servisin varsayilanindan farkli ({path})",
        )


if __name__ == "__main__":
    unittest.main()
