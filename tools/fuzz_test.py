from __future__ import annotations

import argparse
import asyncio
import json
import random
import struct
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import capabilities, protocol

LONG = 70000
CONTROL = "".join(chr(c) for c in range(32))

CATEGORY_STRINGS = {
    "uzunluk": [
        "",
        " ",
        "A",
        "A" * 255,
        "A" * 65535,
        "A" * LONG,
        "\u00e7" * 40000,
        "\U0001f600" * 20000,
    ],
    "sayi_sinirlari": [
        "0",
        "-0",
        "-1",
        "2147483647",
        "-2147483648",
        "9223372036854775807",
        "-9223372036854775808",
        "18446744073709551616",
        "1e309",
        "-1e309",
        "0.1",
        "NaN",
        "Infinity",
        "-Infinity",
    ],
    "ozel_degerler": [
        "\x00",
        "abc\x00def",
        "\n",
        "\r\n",
        "satir\nikinci",
        "\x1a",
        "\x04",
        "\ufeff",
        "\u202e",
        "null",
        "None",
        "undefined",
        "true",
    ],
    "bicimlendirici": [
        "%s",
        "%x",
        "%n",
        "%s%s%s%s%s%s%s%s",
        "%n%n%n%n",
        "{}",
        "{0}",
        "{job_id}",
        "${jndi:ldap://x}",
        "%%d",
    ],
    "noktalama": [
        ";",
        "'",
        '"',
        "\\",
        "/",
        "../",
        "../../../../etc/passwd",
        "..\\..\\windows\\system32",
        "a';--",
        "`ls`",
        "$(ls)",
        "|ls",
        "<script>",
        "&amp;",
        CONTROL,
    ],
    "anahtar_kelimeler": [
        "halt",
        "return",
        "DROP TABLES",
        "DROP TABLE jobs;--",
        "tek_ogretici",
        "ogrenci_hoca",
        "duz_okuma",
        "podcast.submit",
        "podcast.cancel",
        "welcome",
        "ok",
        "err",
        protocol.PROTOCOL,
        "hab/2",
        "__init__",
        "__class__",
        "..",
    ],
}

CATEGORY_SCALARS = {
    "uzunluk": [[], {}, [[]] * 200],
    "sayi_sinirlari": [
        0,
        -1,
        1,
        2**31 - 1,
        -(2**31),
        2**63 - 1,
        -(2**63),
        2**70,
        -(2**70),
        0.0,
        -0.0,
        1e308,
        -1e308,
        float("inf"),
        float("-inf"),
        float("nan"),
        True,
        False,
    ],
    "ozel_degerler": [None],
    "bicimlendirici": [],
    "noktalama": [],
    "anahtar_kelimeler": [],
}

SEED_FRAMES = [
    {"id": "01SEED", "capability": "podcast.submit",
     "payload": {"file_id": "01FILE", "format": "duz_okuma"}, "deadline_ms": 30000},
    {"id": "01SEED", "capability": "podcast.status", "payload": {"job_id": "01JOB"}},
    {"id": "01SEED", "capability": "podcast.result", "payload": {"job_id": "01JOB"}},
    {"id": "01SEED", "capability": "podcast.cancel", "payload": {"job_id": "01JOB"}},
]

SEED_GREETINGS = [
    {"type": "welcome", "worker_id": "w1", "protocol": protocol.PROTOCOL},
    {"type": "rejected", "code": "unauthorized", "message": "token"},
]

SEED_API = [
    {"outcome": "ok", "id": "r1", "status": 200, "body": {"role": "student"}},
    {"outcome": "err", "id": "r1", "code": "path_not_allowed", "message": "x"},
]

GRAMMAR = {
    "frame": [["{", "pairs", "}"], ["value"]],
    "pairs": [["pair"], ["pair", ",", "pairs"]],
    "pair": [["key", ":", "value"]],
    "key": [['"id"'], ['"capability"'], ['"payload"'], ['"deadline_ms"'],
            ['"outcome"'], ['"status"'], ['"type"'], ['"protocol"'], ['"body"']],
    "value": [["scalar"], ["{", "pairs", "}"], ["[", "items", "]"]],
    "items": [["value"], ["value", ",", "items"]],
    "scalar": [["null"], ["true"], ["false"], ["0"], ["-1"], ["1e309"],
               ['""'], ['"x"'], ['"tek_ogretici"'], ["{}"], ["[]"]],
}


def derive(rng: random.Random, symbol: str = "frame", depth: int = 0) -> str:
    if depth > 6 or symbol not in GRAMMAR:
        return symbol
    rule = rng.choice(GRAMMAR[symbol])
    return "".join(derive(rng, part, depth + 1) for part in rule)


def mutate_value(rng: random.Random, value, pool: list):
    roll = rng.random()
    if roll < 0.35:
        return rng.choice(pool)
    if roll < 0.5:
        return None
    if roll < 0.65 and isinstance(value, str):
        if not value:
            return rng.choice(pool)
        cut = rng.randrange(len(value))
        return value[:cut] + str(rng.choice(pool[:8]))[:8] + value[cut + 1:]
    if roll < 0.8:
        return [value]
    if roll < 0.9:
        return {"nested": value}
    return value


def mutate_frame(rng: random.Random, seed: dict, pool: list):
    frame = json.loads(json.dumps(seed, default=str))
    action = rng.randrange(5)
    keys = list(frame.keys())
    if action == 0 and keys:
        del frame[rng.choice(keys)]
    elif action == 1 and keys:
        target = rng.choice(keys)
        frame[target] = mutate_value(rng, frame[target], pool)
    elif action == 2:
        frame[str(rng.choice(pool))[:40]] = rng.choice(pool)
    elif action == 3 and isinstance(frame.get("payload"), dict):
        inner = frame["payload"]
        inner_keys = list(inner.keys())
        if inner_keys:
            target = rng.choice(inner_keys)
            inner[target] = mutate_value(rng, inner[target], pool)
    else:
        return rng.choice([frame, [frame], rng.choice(pool)])
    return frame


def mutate_bytes(rng: random.Random, blob: bytes) -> bytes:
    if not blob:
        return b"\x00"
    data = bytearray(blob)
    for _ in range(rng.randrange(1, 5)):
        action = rng.randrange(4)
        if not data:
            break
        index = rng.randrange(len(data))
        if action == 0:
            data[index] = rng.randrange(256)
        elif action == 1:
            del data[index]
        elif action == 2:
            data.insert(index, rng.randrange(256))
        else:
            data[index] ^= 1 << rng.randrange(8)
    return bytes(data)


class Tracer:
    def __init__(self, suffixes: tuple[str, ...]) -> None:
        self.suffixes = suffixes
        self.seen: set[tuple[str, int]] = set()
        self.fresh: set[tuple[str, int]] = set()

    def _trace(self, frame, event, arg):
        if event != "line":
            return self._trace
        name = frame.f_code.co_filename
        if not name.endswith(self.suffixes):
            return self._trace
        point = (name, frame.f_lineno)
        if point not in self.seen:
            self.seen.add(point)
            self.fresh.add(point)
        return self._trace

    def watch(self, call):
        self.fresh = set()
        previous = sys.gettrace()
        sys.settrace(self._trace)
        try:
            return call()
        finally:
            sys.settrace(previous)


class Report:
    def __init__(self) -> None:
        self.runs = 0
        self.coverage = 0
        self.findings: list[dict] = []
        self.by_category: dict[str, int] = {}
        self.seen: set[tuple[str, str, str]] = set()

    def note(self, surface: str, category: str, payload, exc: BaseException) -> None:
        try:
            where = traceback.extract_tb(exc.__traceback__)[-1].name
        except Exception:
            where = "?"
        signature = (surface, type(exc).__name__, where)
        if signature in self.seen:
            return
        self.seen.add(signature)
        try:
            shown = repr(payload)
        except Exception:
            shown = "<repr basarisiz>"
        if len(shown) > 240:
            shown = shown[:240] + "...(kirpildi)"
        self.findings.append({
            "surface": surface,
            "category": category,
            "exception": type(exc).__name__,
            "message": str(exc)[:200],
            "where": where,
            "input": shown,
        })


ALLOWED_REQUEST = (protocol.CapabilityError, ValueError, EOFError)
ALLOWED_GREETING = (protocol.HandshakeRejected, ValueError, EOFError)
ALLOWED_API = (protocol.ApiRefused, ValueError, EOFError)
ALLOWED_DISPATCH = (protocol.CapabilityError,)


def probe(report: Report, surface: str, category: str, payload, call,
          allowed: tuple, tracer: Tracer) -> bool:
    report.runs += 1
    report.by_category[category] = report.by_category.get(category, 0) + 1
    try:
        tracer.watch(call)
    except allowed:
        pass
    except RecursionError:
        pass
    except BaseException as exc:
        report.note(surface, category, payload, exc)
    return bool(tracer.fresh)


def read_frame_bytes(blob: bytes):
    async def run():
        stream = protocol.FrameStream()
        stream.feed(blob, end=True)
        return await stream.read_frame()

    return asyncio.run(run())


def fuzz(cases: int, seed: int, verbose: bool) -> Report:
    rng = random.Random(seed)
    report = Report()
    tracer = Tracer(("protocol.py", "capabilities.py"))
    pool: list = []
    for name, values in CATEGORY_STRINGS.items():
        pool.extend(values)
        pool.extend(CATEGORY_SCALARS.get(name, []))
    corpus = list(SEED_FRAMES)
    byte_corpus = [protocol.encode_frame(f) for f in SEED_FRAMES]

    for name, values in CATEGORY_STRINGS.items():
        every = list(values) + list(CATEGORY_SCALARS.get(name, []))
        for value in every:
            for field in ("id", "capability", "payload", "deadline_ms"):
                frame = dict(SEED_FRAMES[0])
                frame[field] = value
                probe(report, "parse_request", name, frame,
                      lambda f=frame: protocol.parse_request(f), ALLOWED_REQUEST, tracer)
            for field in ("type", "protocol", "worker_id", "code"):
                greeting = dict(SEED_GREETINGS[0])
                greeting[field] = value
                probe(report, "parse_greeting", name, greeting,
                      lambda g=greeting: protocol.parse_greeting(g), ALLOWED_GREETING, tracer)
            for field in ("outcome", "id", "status", "body", "code"):
                answer = dict(SEED_API[0])
                answer[field] = value
                probe(report, "parse_api_response", name, answer,
                      lambda a=answer: protocol.parse_api_response(a), ALLOWED_API, tracer)
            probe(report, "encode_frame", name, value,
                  lambda v=value: protocol.encode_frame({"payload": v}),
                  (protocol.CapabilityError, ValueError), tracer)
            probe(report, "build_api_request", name, value,
                  lambda v=value: protocol.build_api_request(
                      "r", v if isinstance(v, str) else "/notes"),
                  (protocol.ApiRefused, ValueError), tracer)
            for capability in capabilities.names():
                body = {"job_id": value, "file_id": value, "format": value}
                probe(report, "dispatch", name, (capability, body),
                      lambda c=capability, b=body: capabilities.dispatch(c, b),
                      ALLOWED_DISPATCH, tracer)

    for index in range(cases):
        style = rng.randrange(4)
        if style == 0:
            candidate = mutate_frame(rng, rng.choice(corpus), pool)
            gained = probe(report, "parse_request", "sablon", candidate,
                           lambda c=candidate: protocol.parse_request(c),
                           ALLOWED_REQUEST, tracer)
            if gained and isinstance(candidate, dict) and len(corpus) < 400:
                corpus.append(candidate)
        elif style == 1:
            blob = mutate_bytes(rng, rng.choice(byte_corpus))
            gained = probe(report, "read_frame", "bayt", blob,
                           lambda b=blob: read_frame_bytes(b),
                           ALLOWED_REQUEST + (struct.error,), tracer)
            if gained and len(byte_corpus) < 400:
                byte_corpus.append(blob)
        elif style == 2:
            text = derive(rng)
            encoded = text.encode("utf-8")

            def run_text(e=encoded):
                return read_frame_bytes(struct.pack(">I", len(e)) + e)

            gained = probe(report, "read_frame", "grammar", text, run_text,
                           ALLOWED_REQUEST + (struct.error,), tracer)
            if gained and len(byte_corpus) < 400:
                byte_corpus.append(encoded)
        else:
            candidate = mutate_frame(rng, rng.choice(SEED_API), pool)
            probe(report, "parse_api_response", "sablon", candidate,
                  lambda c=candidate: protocol.parse_api_response(c),
                  ALLOWED_API, tracer)
        if verbose and (index + 1) % 2000 == 0:
            print("  [fuzz] %d/%d kosu, %d bulgu, %d kapsam noktasi"
                  % (index + 1, cases, len(report.findings), len(tracer.seen)), flush=True)

    report.coverage = len(tracer.seen)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Slayt kategorileriyle fuzzing: uzunluk, sayi sinirlari, ozel "
                    "degerler, bicimlendirici, noktalama, anahtar kelimeler")
    parser.add_argument("--cases", type=int, default=20000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--fail-on-finding", action="store_true")
    args = parser.parse_args()

    capabilities.configure(None, False)
    print("[fuzz] tohum   : %d" % args.seed, flush=True)
    print("[fuzz] kosu    : %d rastgele + tum kategori kombinasyonlari" % args.cases,
          flush=True)
    print("", flush=True)
    report = fuzz(args.cases, args.seed, not args.quiet)

    print("", flush=True)
    print("=" * 58, flush=True)
    print("FUZZING SONUCU", flush=True)
    print("  toplam kosu    : %d" % report.runs, flush=True)
    print("  kapsanan satir : %d" % report.coverage, flush=True)
    print("  benzersiz bulgu: %d" % len(report.findings), flush=True)
    print("", flush=True)
    print("  kategori dagilimi:", flush=True)
    for name in sorted(report.by_category):
        print("    %-18s %d" % (name, report.by_category[name]), flush=True)
    if report.findings:
        print("", flush=True)
        print("BULGULAR (beyan edilmemis istisna):", flush=True)
        for item in report.findings:
            print("", flush=True)
            print("  %s / %s" % (item["surface"], item["category"]), flush=True)
            print("    istisna : %s: %s" % (item["exception"], item["message"]), flush=True)
            print("    nerede  : %s" % item["where"], flush=True)
            print("    girdi   : %s" % item["input"], flush=True)
    else:
        print("", flush=True)
        print("  bulgu yok: tum girdiler beyan edilen istisnalarla karsilandi", flush=True)
    return 1 if (report.findings and args.fail_on_finding) else 0


if __name__ == "__main__":
    sys.exit(main())
