import tempfile
import unittest
from pathlib import Path

from src import config, jobs

REJECTED_NAMES = (
    "..",
    "../x",
    "a/b",
    "/etc/passwd",
    "..\\x",
    ".",
    "",
    "x" * 129,
    "\\\\srv\\share",
    "C:\\Windows",
    "ders\x00.pdf",
)

ACCEPTED_NAMES = (
    "ders-01.pdf",
    "ders_01.pdf",
    "ders.01.pdf",
    "DERS01",
    "a",
    "x" * 128,
)


class PathTraversalRegression(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-kacis-")
        self.media_root = Path(self._tmp.name) / "medya"
        self.media_root.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        config._active_level = self._log_level

    def test_traversal_attempts_must_be_rejected(self) -> None:
        for source_id in REJECTED_NAMES:
            with self.subTest(source_id=source_id):
                with self.assertRaises(ValueError, msg=f"kabul edildi: {source_id!r}"):
                    jobs.resolve_source(self.media_root, source_id)

    def test_a_non_text_source_id_must_be_rejected(self) -> None:
        for source_id in (None, 7, b"ders.pdf", ["ders.pdf"], Path("ders.pdf")):
            with self.subTest(source_id=source_id):
                with self.assertRaises(ValueError):
                    jobs.resolve_source(self.media_root, source_id)

    def test_valid_names_must_be_accepted(self) -> None:
        for source_id in ACCEPTED_NAMES:
            with self.subTest(source_id=source_id):
                resolved = jobs.resolve_source(self.media_root, source_id)
                self.assertEqual(resolved.name, source_id)

    def test_the_resolved_path_must_stay_under_the_media_root(self) -> None:
        root = self.media_root.resolve()
        for source_id in ACCEPTED_NAMES:
            with self.subTest(source_id=source_id):
                resolved = jobs.resolve_source(self.media_root, source_id)
                self.assertIn(
                    root,
                    resolved.parents,
                    f"cozulen yol medya kokunun disina cikti: {resolved}",
                )
                self.assertNotEqual(resolved, root, "cozulen yol medya kokunun kendisi olamaz")

    def test_shell_and_redirection_characters_must_be_rejected(self) -> None:
        for source_id in (
            "ders 01.pdf",
            "ders;rm -rf /",
            "ders|kat",
            "ders$(whoami)",
            "ders\npdf",
            "ders\tpdf",
            "ders*.pdf",
            "ders?.pdf",
            "ders<>.pdf",
            "ders:akis",
        ):
            with self.subTest(source_id=source_id):
                with self.assertRaises(ValueError):
                    jobs.resolve_source(self.media_root, source_id)

    def test_the_length_limit_must_cut_off_at_exactly_128_characters(self) -> None:
        jobs.resolve_source(self.media_root, "x" * 128)
        with self.assertRaises(ValueError, msg="129 karakterlik ad kabul edildi"):
            jobs.resolve_source(self.media_root, "x" * 129)

    def test_the_pipeline_layer_must_use_the_same_gate(self) -> None:
        from src import pipeline

        for source_id in REJECTED_NAMES:
            with self.subTest(source_id=source_id):
                with self.assertRaises(ValueError):
                    pipeline.resolve_pdf(str(self.media_root), source_id)


if __name__ == "__main__":
    unittest.main()
