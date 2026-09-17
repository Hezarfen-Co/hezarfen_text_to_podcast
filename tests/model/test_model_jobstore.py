from __future__ import annotations

import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src import config, jobs

INITIAL = "Q"

INPUTS = (
    "begin",
    "finish",
    "fail",
    "cancel",
    "progress",
    "status",
    "cancel_requested",
)

MODEL = {
    "Q": {
        "begin": ("True", "R"),
        "finish": ("queued", "Q"),
        "fail": ("failed", "F"),
        "cancel": ("True", "C"),
        "progress": ("None", "Q"),
        "status": ("queued", "Q"),
        "cancel_requested": ("False", "Q"),
    },
    "R": {
        "begin": ("False", "R"),
        "finish": ("done", "D"),
        "fail": ("failed", "F"),
        "cancel": ("True", "RC"),
        "progress": ("running", "R"),
        "status": ("running", "R"),
        "cancel_requested": ("False", "R"),
    },
    "RC": {
        "begin": ("False", "RC"),
        "finish": ("cancelled", "C"),
        "fail": ("failed", "FC"),
        "cancel": ("True", "RC"),
        "progress": ("running", "RC"),
        "status": ("running", "RC"),
        "cancel_requested": ("True", "RC"),
    },
    "D": {
        "begin": ("False", "D"),
        "finish": ("done", "D"),
        "fail": ("InvalidTransition", "D"),
        "cancel": ("False", "D"),
        "progress": ("None", "D"),
        "status": ("done", "D"),
        "cancel_requested": ("False", "D"),
    },
    "F": {
        "begin": ("False", "F"),
        "finish": ("failed", "F"),
        "fail": ("InvalidTransition", "F"),
        "cancel": ("False", "F"),
        "progress": ("None", "F"),
        "status": ("failed", "F"),
        "cancel_requested": ("False", "F"),
    },
    "FC": {
        "begin": ("False", "FC"),
        "finish": ("failed", "FC"),
        "fail": ("InvalidTransition", "FC"),
        "cancel": ("False", "FC"),
        "progress": ("None", "FC"),
        "status": ("failed", "FC"),
        "cancel_requested": ("True", "FC"),
    },
    "C": {
        "begin": ("False", "C"),
        "finish": ("cancelled", "C"),
        "fail": ("InvalidTransition", "C"),
        "cancel": ("False", "C"),
        "progress": ("None", "C"),
        "status": ("cancelled", "C"),
        "cancel_requested": ("True", "C"),
    },
}


def trace(state: str, sequence: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    outputs = []
    for symbol in sequence:
        output, state = MODEL[state][symbol]
        outputs.append(output)
    return state, tuple(outputs)


def state_cover() -> dict[str, tuple[str, ...]]:
    cover = {INITIAL: ()}
    frontier = deque([INITIAL])
    while frontier:
        state = frontier.popleft()
        for symbol in INPUTS:
            _, target = MODEL[state][symbol]
            if target not in cover:
                cover[target] = cover[state] + (symbol,)
                frontier.append(target)
    return cover


def characterizing_set() -> list[tuple[str, ...]]:
    blocks = {state: "0" for state in MODEL}
    witnesses: list[tuple[str, ...]] = []
    while True:
        split_found = False
        for symbol in INPUTS:
            refined = {
                state: blocks[state] + "|" + MODEL[state][symbol][0] + "->"
                + blocks[MODEL[state][symbol][1]]
                for state in MODEL
            }
            if len(set(refined.values())) > len(set(blocks.values())):
                blocks = {
                    state: str(sorted(set(refined.values())).index(refined[state]))
                    for state in MODEL
                }
                witnesses.append((symbol,))
                split_found = True
        if not split_found:
            break
    return _minimize(witnesses)


def _pairs() -> list[tuple[str, str]]:
    ordered = sorted(MODEL)
    return [
        (a, b)
        for index, a in enumerate(ordered)
        for b in ordered[index + 1:]
    ]


def _separates_all(candidates: list[tuple[str, ...]]) -> bool:
    return all(
        any(trace(a, seq)[1] != trace(b, seq)[1] for seq in candidates)
        for a, b in _pairs()
    )


def _minimize(witnesses: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    kept: list[tuple[str, ...]] = []
    for candidate in witnesses:
        unresolved = [
            pair for pair in _pairs()
            if all(trace(pair[0], seq)[1] == trace(pair[1], seq)[1] for seq in kept)
        ]
        if any(
            trace(a, candidate)[1] != trace(b, candidate)[1] for a, b in unresolved
        ):
            kept.append(candidate)
    for candidate in list(kept):
        trimmed = [seq for seq in kept if seq != candidate]
        if trimmed and _separates_all(trimmed):
            kept = trimmed
    return kept


class Adapter:
    def __init__(self, store: jobs.JobStore) -> None:
        self.store = store
        self.job_id = ""
        self.counter = 0

    def reset(self) -> None:
        self.counter += 1
        job_id = "01MODEL%04d" % self.counter
        record, _ = self.store.submit(job_id, "01SOURCE", "duz_okuma", user_id="ogretmen-1")
        self.job_id = record["job_id"]

    def send(self, symbol: str) -> str:
        try:
            if symbol == "begin":
                return str(self.store.begin(self.job_id))
            if symbol == "finish":
                return self.store.finish(self.job_id, "a.mp3", 1.0, "s.txt")["state"]
            if symbol == "fail":
                return self.store.fail(self.job_id, "test")["state"]
            if symbol == "cancel":
                return str(self.store.cancel(self.job_id))
            if symbol == "progress":
                answer = self.store.update_progress(self.job_id, "ingest", 0.5)
                return "None" if answer is None else answer["state"]
            if symbol == "status":
                return self.store.get(self.job_id)["state"]
            if symbol == "cancel_requested":
                return str(self.store.cancel_requested(self.job_id))
        except jobs.InvalidTransition:
            return "InvalidTransition"
        raise AssertionError("bilinmeyen girdi: %s" % symbol)


class ModelStructureTests(unittest.TestCase):
    def test_the_model_is_complete_over_the_input_alphabet(self) -> None:
        for state, rows in MODEL.items():
            self.assertEqual(
                tuple(sorted(rows)), tuple(sorted(INPUTS)),
                "durum %s girdi alfabesini tam kapsamiyor" % state,
            )
            for symbol, (_, target) in rows.items():
                self.assertIn(target, MODEL,
                              "%s/%s tanimsiz duruma gidiyor" % (state, symbol))

    def test_every_state_is_reachable_from_the_initial_state(self) -> None:
        cover = state_cover()
        self.assertEqual(sorted(cover), sorted(MODEL),
                         "erisilemeyen durum var; model minimal degil")

    def test_the_model_is_minimal_under_the_computed_w_set(self) -> None:
        w_set = characterizing_set()
        self.assertTrue(w_set, "W kumesi bos cikti")
        signatures = {
            state: tuple(trace(state, seq)[1] for seq in w_set) for state in MODEL
        }
        self.assertEqual(
            len(set(signatures.values())), len(MODEL),
            "W kumesi tum durum ciftlerini ayirmiyor: %r" % (signatures,),
        )

    def test_the_w_set_respects_the_size_bound_from_the_lecture(self) -> None:
        w_set = characterizing_set()
        self.assertLessEqual(
            len(w_set), len(MODEL) - 1,
            "n durumlu minimal FSM icin W en fazla n-1 dizi icermeli",
        )
        for seq in w_set:
            self.assertLessEqual(
                len(seq), len(MODEL) - 1,
                "W icindeki her dizi en fazla n-1 uzunlukta olmali",
            )

    def test_the_w_set_is_irredundant(self) -> None:
        w_set = characterizing_set()
        for candidate in w_set:
            trimmed = [seq for seq in w_set if seq != candidate]
            self.assertFalse(
                _separates_all(trimmed),
                "W kumesinde gereksiz dizi var: %r" % (candidate,),
            )

    def test_the_state_cover_reaches_each_state_exactly_once(self) -> None:
        cover = state_cover()
        self.assertIn((), cover.values(), "durum ortusu bos diziyi icermeli")
        for state, sequence in cover.items():
            self.assertEqual(
                trace(INITIAL, sequence)[0], state,
                "V dizisi %r hedeflenen %s durumuna gitmiyor" % (sequence, state),
            )


class WMethodConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self._tmp = tempfile.TemporaryDirectory(prefix="podcast-model-")
        self.store = jobs.JobStore(
            root=Path(self._tmp.name) / "isler",
            workers=1,
            max_jobs=100000,
            stage_secs=0.0,
        )
        self.adapter = Adapter(self.store)

    def tearDown(self) -> None:
        self.store.shutdown(timeout=1.0)
        self._tmp.cleanup()
        config._active_level = self._log_level

    def _suite(self) -> list[tuple[str, ...]]:
        cover = state_cover()
        w_set = characterizing_set()
        prefixes = sorted(set(cover.values()))
        middles = [()] + [(symbol,) for symbol in INPUTS]
        return sorted({
            prefix + middle + suffix
            for prefix in prefixes
            for middle in middles
            for suffix in w_set
        })

    def _run(self, sequence: tuple[str, ...]) -> tuple[str, ...]:
        self.adapter.reset()
        return tuple(self.adapter.send(symbol) for symbol in sequence)

    def test_the_implementation_conforms_to_the_model(self) -> None:
        suite = self._suite()
        self.assertGreaterEqual(len(suite), 100,
                                "V.W union V.X.W beklenenden kucuk")
        for sequence in suite:
            expected = trace(INITIAL, sequence)[1]
            with self.subTest(sequence="/".join(sequence)):
                self.assertEqual(
                    self._run(sequence), expected,
                    "model ile gercek JobStore ayrisiyor: %s" % ("/".join(sequence),),
                )

    def test_the_suite_visits_every_modelled_state(self) -> None:
        visited = set()
        for sequence in self._suite():
            state = INITIAL
            visited.add(state)
            for symbol in sequence:
                state = MODEL[state][symbol][1]
                visited.add(state)
        self.assertEqual(sorted(visited), sorted(MODEL))

    def test_the_suite_exercises_every_modelled_transition(self) -> None:
        covered = set()
        for sequence in self._suite():
            state = INITIAL
            for symbol in sequence:
                covered.add((state, symbol))
                state = MODEL[state][symbol][1]
        expected = {(state, symbol) for state in MODEL for symbol in INPUTS}
        self.assertEqual(
            covered, expected,
            "kapsanmayan gecis: %r" % (sorted(expected - covered),),
        )


if __name__ == "__main__":
    unittest.main()
