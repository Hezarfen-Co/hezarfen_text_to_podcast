import asyncio
import json
import struct
import unittest

from src import protocol


class FramingTests(unittest.TestCase):
    def test_length_prefix_is_four_byte_big_endian(self) -> None:
        self.assertEqual(
            protocol.encode_frame({})[:4],
            b"\x00\x00\x00\x02",
            "uzunluk oneki 4 bayt big-endian olmali",
        )

    def test_body_is_utf8_json_without_ascii_escaping(self) -> None:
        frame = protocol.encode_frame({"k": "cok"})
        self.assertEqual(json.loads(frame[4:].decode("utf-8")), {"k": "cok"})

    def test_encode_frame_raises_frame_too_large_above_eight_mib(self) -> None:
        large = {"payload": "a" * (protocol.MAX_FRAME_BYTES + 16)}
        with self.assertRaises(protocol.CapabilityError) as raised:
            protocol.encode_frame(large)
        self.assertEqual(
            raised.exception.code,
            "frame_too_large",
            "8 MiB ustu cerceve 'frame_too_large' kodu ile reddedilmeli",
        )

    def test_round_trip_survives_split_across_chunk_boundary(self) -> None:
        message = protocol.build_hello("podcast", ["podcast.submit"], "gizli", 2)

        async def round_trip() -> object:
            stream = protocol.FrameStream()
            encoded = protocol.encode_frame(message)
            stream.feed(encoded[:3], end=False)
            stream.feed(encoded[3:7], end=False)
            stream.feed(encoded[7:], end=True)
            return await stream.read_frame()

        self.assertEqual(
            asyncio.run(round_trip()),
            message,
            "cerceve parcali beslendiginde gidis-donus bozulmamali",
        )

    def test_declared_length_above_limit_is_rejected_before_body_read(self) -> None:
        async def run() -> None:
            stream = protocol.FrameStream()
            stream.feed(struct.pack(">I", protocol.MAX_FRAME_BYTES + 1), end=True)
            await stream.read_frame()

        with self.assertRaises(protocol.CapabilityError) as raised:
            asyncio.run(run())
        self.assertEqual(raised.exception.code, "frame_too_large")

    def test_truncated_frame_raises_eof_not_silent_none(self) -> None:
        async def run() -> None:
            stream = protocol.FrameStream()
            stream.feed(struct.pack(">I", 32) + b"{", end=True)
            await stream.read_frame()

        with self.assertRaises(EOFError):
            asyncio.run(run())


class HelloAndGreetingTests(unittest.TestCase):
    def test_hello_carries_protocol_service_capabilities_token_and_concurrency(self) -> None:
        hello = protocol.build_hello("podcast", ["a", "b"], "sir", 3)
        self.assertEqual(
            hello,
            {
                "protocol": protocol.PROTOCOL,
                "service": "podcast",
                "capabilities": ["a", "b"],
                "token": "sir",
                "max_concurrent": 3,
            },
        )

    def test_protocol_identifier_is_hab_one(self) -> None:
        self.assertEqual(protocol.PROTOCOL, "hab/1", "kablo sozlesmesi kimligi degismemeli")

    def test_welcome_greeting_returns_worker_id(self) -> None:
        greeting = {"type": "welcome", "worker_id": "w7", "protocol": protocol.PROTOCOL}
        self.assertEqual(protocol.parse_greeting(greeting), "w7")

    def test_welcome_with_wrong_protocol_is_rejected_as_unsupported_protocol(self) -> None:
        with self.assertRaises(protocol.HandshakeRejected) as raised:
            protocol.parse_greeting({"type": "welcome", "protocol": "hab/9"})
        self.assertEqual(raised.exception.code, "unsupported_protocol")

    def test_non_welcome_greeting_propagates_backend_error_code(self) -> None:
        with self.assertRaises(protocol.HandshakeRejected) as raised:
            protocol.parse_greeting({"code": "unauthorized", "message": "kotu token"})
        self.assertEqual(raised.exception.code, "unauthorized")

    def test_non_object_greeting_is_rejected_as_malformed(self) -> None:
        for greeting in ([], "welcome", 3, None):
            with self.subTest(greeting=greeting):
                with self.assertRaises(protocol.HandshakeRejected) as raised:
                    protocol.parse_greeting(greeting)
                self.assertEqual(raised.exception.code, "malformed")


class ResponseShapeTests(unittest.TestCase):
    def test_ok_response_shape(self) -> None:
        self.assertEqual(
            protocol.ok_response("r1", {"job_id": "J"}),
            {"status": "ok", "id": "r1", "payload": {"job_id": "J"}},
        )

    def test_err_response_shape(self) -> None:
        self.assertEqual(
            protocol.err_response("r1", "busy", "dolu"),
            {"status": "err", "id": "r1", "code": "busy", "message": "dolu"},
        )


class RequestParsingTests(unittest.TestCase):
    def test_valid_request_is_unpacked(self) -> None:
        parsed = protocol.parse_request(
            {"id": "r1", "capability": "podcast.status", "payload": {"job_id": "J"}}
        )
        self.assertEqual(parsed[0], "r1")
        self.assertEqual(parsed[1], "podcast.status")
        self.assertEqual(parsed[2], {"job_id": "J"})
        self.assertIsNone(parsed[3], "deadline_ms yoksa zaman asimi None olmali")

    def test_unparseable_frames_raise_bad_request(self) -> None:
        for frame in (
            [],
            "istek",
            None,
            {},
            {"id": 7, "capability": "podcast.status"},
            {"id": "r1", "capability": 7},
            {"capability": "podcast.status"},
            {"id": "r1"},
        ):
            with self.subTest(frame=frame):
                with self.assertRaises(protocol.CapabilityError) as raised:
                    protocol.parse_request(frame)
                self.assertEqual(
                    raised.exception.code,
                    "bad_request",
                    "ayristirilamayan istek 'bad_request' ile reddedilmeli",
                )

    def test_deadline_margin_is_subtracted_so_answer_beats_backend_timeout(self) -> None:
        timeout = protocol.parse_request(
            {"id": "r1", "capability": "podcast.status", "deadline_ms": 60000}
        )[3]
        self.assertIsNotNone(timeout)
        self.assertLess(
            timeout, 60.0, "deadline payi birakilmali; cevap backend'in tavanindan once gitmeli"
        )
        self.assertAlmostEqual(timeout, 60.0 - protocol.DEADLINE_MARGIN_SECS, places=6)

    def test_short_deadline_keeps_at_least_half_the_budget(self) -> None:
        timeout = protocol.parse_request(
            {"id": "r1", "capability": "podcast.status", "deadline_ms": 100}
        )[3]
        self.assertAlmostEqual(timeout, 0.05, places=6)
        self.assertGreater(timeout, 0.0, "kisa deadline negatif zaman asimi uretmemeli")

    def test_non_positive_or_boolean_deadline_yields_no_timeout(self) -> None:
        for value in (0, -1, True, False, "60000", None, [60000]):
            with self.subTest(deadline_ms=value):
                self.assertIsNone(
                    protocol.parse_request(
                        {"id": "r1", "capability": "podcast.status", "deadline_ms": value}
                    )[3]
                )


class ConstantTests(unittest.TestCase):
    def test_wire_constants_match_the_documented_contract(self) -> None:
        self.assertEqual(protocol.MAX_FRAME_BYTES, 8 * 1024 * 1024)
        self.assertEqual(protocol.CERTIFICATE_PATH, "/ai/certificate")
        self.assertEqual(protocol.KEEPALIVE_SECS, 10)
        self.assertEqual(protocol.IDLE_TIMEOUT_SECS, 30.0)
        self.assertLess(
            protocol.KEEPALIVE_SECS,
            protocol.IDLE_TIMEOUT_SECS,
            "keepalive araligi idle timeout'tan kisa olmali",
        )


if __name__ == "__main__":
    unittest.main()
