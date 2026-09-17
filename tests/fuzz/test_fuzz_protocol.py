from __future__ import annotations

import asyncio
import random
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import capabilities, protocol
from tools import fuzz_test

ALLOWED_REQUEST = (protocol.CapabilityError, ValueError, EOFError)
ALLOWED_GREETING = (protocol.HandshakeRejected, ValueError, EOFError)
ALLOWED_API = (protocol.ApiRefused, ValueError, EOFError)


def read_frame_bytes(blob: bytes):
    async def run():
        stream = protocol.FrameStream()
        stream.feed(blob, end=True)
        return await stream.read_frame()

    return asyncio.run(run())


class KnownCrashersStayFixed(unittest.TestCase):
    def test_a_non_numeric_status_is_refused_not_crashed(self) -> None:
        with self.assertRaises(protocol.ApiRefused) as caught:
            protocol.parse_api_response(
                {"outcome": "ok", "id": "r1", "status": [], "body": {}}
            )
        self.assertEqual(caught.exception.code, "malformed")

    def test_an_infinite_status_is_refused_not_crashed(self) -> None:
        with self.assertRaises(protocol.ApiRefused) as caught:
            protocol.parse_api_response(
                {"outcome": "ok", "id": "r1", "status": float("inf"), "body": {}}
            )
        self.assertEqual(caught.exception.code, "malformed")

    def test_every_unusable_status_shape_is_refused(self) -> None:
        for status in ([], {}, None, "abc", float("nan"), float("-inf"),
                       "1e309", object()):
            with self.subTest(status=status):
                with self.assertRaises(protocol.ApiRefused):
                    protocol.parse_api_response(
                        {"outcome": "ok", "id": "r", "status": status}
                    )

    def test_a_usable_status_still_passes_through(self) -> None:
        for status, expected in ((200, 200), ("404", 404), (301.0, 301), (True, 1)):
            with self.subTest(status=status):
                answer = protocol.parse_api_response(
                    {"outcome": "ok", "id": "r", "status": status}
                )
                self.assertEqual(answer.status, expected)
                self.assertIs(
                    type(answer.status), int,
                    "status tam sayiya normallestirilmeli; bool dondurulurse "
                    "assertEqual yakalayamaz cunku True == 1",
                )


class EveryCategoryStaysInsideTheDeclaredContract(unittest.TestCase):
    def _values(self):
        for name, values in fuzz_test.CATEGORY_STRINGS.items():
            for value in values:
                yield name, value
            for value in fuzz_test.CATEGORY_SCALARS.get(name, []):
                yield name, value

    def _check(self, name, value, call, allowed) -> None:
        try:
            call()
        except allowed:
            return
        except RecursionError:
            return
        except BaseException as exc:
            self.fail(
                "kategori %s: beyan edilmemis %s -> %s (girdi %.80r)"
                % (name, type(exc).__name__, exc, value)
            )

    def test_parse_request_never_raises_an_undeclared_exception(self) -> None:
        for name, value in self._values():
            for field in ("id", "capability", "payload", "deadline_ms"):
                frame = dict(fuzz_test.SEED_FRAMES[0])
                frame[field] = value
                with self.subTest(category=name, field=field):
                    self._check(name, value,
                                lambda f=frame: protocol.parse_request(f),
                                ALLOWED_REQUEST)

    def test_parse_greeting_never_raises_an_undeclared_exception(self) -> None:
        for name, value in self._values():
            for field in ("type", "protocol", "worker_id", "code"):
                greeting = dict(fuzz_test.SEED_GREETINGS[0])
                greeting[field] = value
                with self.subTest(category=name, field=field):
                    self._check(name, value,
                                lambda g=greeting: protocol.parse_greeting(g),
                                ALLOWED_GREETING)

    def test_parse_api_response_never_raises_an_undeclared_exception(self) -> None:
        for name, value in self._values():
            for field in ("outcome", "id", "status", "body", "code"):
                answer = dict(fuzz_test.SEED_API[0])
                answer[field] = value
                with self.subTest(category=name, field=field):
                    self._check(name, value,
                                lambda a=answer: protocol.parse_api_response(a),
                                ALLOWED_API)

    def test_dispatch_never_raises_an_undeclared_exception(self) -> None:
        capabilities.configure(None, False)
        for name, value in self._values():
            body = {"job_id": value, "file_id": value, "format": value}
            for capability in capabilities.names():
                with self.subTest(category=name, capability=capability):
                    self._check(name, value,
                                lambda c=capability, b=body:
                                capabilities.dispatch(c, "okul-a", b),
                                (protocol.CapabilityError,))


class MutatedAndGeneratedFramesStayInsideTheContract(unittest.TestCase):
    SEEDS = (1, 7, 99)
    CASES = 400

    def test_template_mutation_of_request_frames_is_survivable(self) -> None:
        pool = []
        for name, values in fuzz_test.CATEGORY_STRINGS.items():
            pool.extend(values)
            pool.extend(fuzz_test.CATEGORY_SCALARS.get(name, []))
        for seed in self.SEEDS:
            rng = random.Random(seed)
            for _ in range(self.CASES):
                candidate = fuzz_test.mutate_frame(
                    rng, rng.choice(fuzz_test.SEED_FRAMES), pool
                )
                try:
                    protocol.parse_request(candidate)
                except ALLOWED_REQUEST:
                    pass
                except RecursionError:
                    pass
                except BaseException as exc:
                    self.fail("tohum %d: beyan edilmemis %s -> %s"
                              % (seed, type(exc).__name__, exc))

    def test_byte_level_mutation_of_frames_is_survivable(self) -> None:
        base = [protocol.encode_frame(f) for f in fuzz_test.SEED_FRAMES]
        for seed in self.SEEDS:
            rng = random.Random(seed)
            for _ in range(self.CASES):
                blob = fuzz_test.mutate_bytes(rng, rng.choice(base))
                try:
                    read_frame_bytes(blob)
                except ALLOWED_REQUEST + (struct.error,):
                    pass
                except BaseException as exc:
                    self.fail("tohum %d: beyan edilmemis %s -> %s"
                              % (seed, type(exc).__name__, exc))

    def test_grammar_generated_frames_are_survivable(self) -> None:
        for seed in self.SEEDS:
            rng = random.Random(seed)
            for _ in range(self.CASES):
                encoded = fuzz_test.derive(rng).encode("utf-8")
                blob = struct.pack(">I", len(encoded)) + encoded
                try:
                    read_frame_bytes(blob)
                except ALLOWED_REQUEST + (struct.error,):
                    pass
                except BaseException as exc:
                    self.fail("tohum %d: beyan edilmemis %s -> %s"
                              % (seed, type(exc).__name__, exc))


class TheFuzzerItselfNeverCrashes(unittest.TestCase):
    def test_a_short_run_completes_and_reports(self) -> None:
        capabilities.configure(None, False)
        report = fuzz_test.fuzz(cases=120, seed=4242, verbose=False)
        self.assertGreater(report.runs, 120)
        self.assertGreater(report.coverage, 0)
        self.assertEqual(
            report.findings,
            [],
            "fuzzer yeni bir beyan edilmemis istisna buldu; "
            "tools/fuzz_test.py ciktisina bak",
        )

    def test_all_six_slide_categories_are_exercised(self) -> None:
        capabilities.configure(None, False)
        report = fuzz_test.fuzz(cases=40, seed=5, verbose=False)
        for name in ("uzunluk", "sayi_sinirlari", "ozel_degerler",
                     "bicimlendirici", "noktalama", "anahtar_kelimeler"):
            self.assertIn(name, report.by_category)
            self.assertGreater(report.by_category[name], 0)


if __name__ == "__main__":
    unittest.main()
