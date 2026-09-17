import asyncio
import base64
import hashlib
import json
import struct
import tempfile
import unittest
from unittest import mock

from src import protocol
from tests.regress.regress_unanswered_stream import (
    FakeQuic,
    decode_frame,
    install_fake_aioquic,
)


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

    def test_protocol_identifier_is_hab_two(self) -> None:
        self.assertEqual(
            protocol.PROTOCOL,
            "hab/2",
            "backend hem ALPN'de hem Hello'da hab/2 disini reddediyor; "
            "hab/1 ile servis uretimde oludur",
        )

    def test_welcome_greeting_returns_worker_id(self) -> None:
        greeting = {"type": "welcome", "worker_id": "w7", "protocol": protocol.PROTOCOL}
        self.assertEqual(protocol.parse_greeting(greeting), "w7")

    def test_welcome_with_wrong_protocol_is_rejected_as_unsupported_protocol(self) -> None:
        for wrong in ("hab/9", "hab/1", "HAB/2", ""):
            with self.subTest(protocol=wrong):
                with self.assertRaises(protocol.HandshakeRejected) as raised:
                    protocol.parse_greeting({"type": "welcome", "protocol": wrong})
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

    def test_unauthorized_and_unsupported_protocol_are_permanent_rejects(self) -> None:
        self.assertTrue(protocol.HandshakeRejected("unauthorized", "x").permanent)
        self.assertTrue(
            protocol.HandshakeRejected("unsupported_protocol", "x").permanent
        )
        self.assertFalse(protocol.HandshakeRejected("malformed", "x").permanent)


class SchoolEchoTests(unittest.TestCase):
    def test_ok_response_echoes_the_school(self) -> None:
        self.assertEqual(
            protocol.ok_response("r1", "ataturk-ortaokulu", {"job_id": "J"}),
            {
                "status": "ok",
                "id": "r1",
                "school": "ataturk-ortaokulu",
                "payload": {"job_id": "J"},
            },
            "backend Response::Ok 'school' alanini ZORUNLU tutuyor; "
            "yankilanmazsa cevap okunamaz",
        )

    def test_err_response_echoes_the_school(self) -> None:
        self.assertEqual(
            protocol.err_response("r1", "ataturk-ortaokulu", "busy", "dolu"),
            {
                "status": "err",
                "id": "r1",
                "school": "ataturk-ortaokulu",
                "code": "busy",
                "message": "dolu",
            },
        )

    def test_api_request_carries_the_school(self) -> None:
        request = protocol.build_api_request("r1", "ataturk-ortaokulu", "/auth/me")
        self.assertEqual(
            request["school"],
            "ataturk-ortaokulu",
            "hab/2 ApiRequest 'school' istiyor; yoksa backend 'malformed' doner",
        )
        self.assertEqual(request["id"], "r1")
        self.assertEqual(request["path"], "/auth/me")

    def test_api_request_without_a_school_is_refused(self) -> None:
        for school in ("", "   ", None, 7):
            with self.subTest(school=school):
                with self.assertRaises(protocol.ApiRefused) as raised:
                    protocol.build_api_request("r1", school, "/auth/me")
                self.assertEqual(raised.exception.code, "malformed")

    def test_api_response_exposes_the_echoed_school(self) -> None:
        answer = protocol.parse_api_response(
            {"outcome": "ok", "id": "r1", "school": "ataturk-ortaokulu", "status": 200}
        )
        self.assertEqual(answer.school, "ataturk-ortaokulu")
        with self.assertRaises(protocol.ApiRefused) as raised:
            protocol.parse_api_response(
                {
                    "outcome": "err",
                    "id": "r1",
                    "school": "ataturk-ortaokulu",
                    "code": "path_not_allowed",
                    "message": "x",
                }
            )
        self.assertEqual(raised.exception.school, "ataturk-ortaokulu")


class RequestParsingTests(unittest.TestCase):
    def test_valid_request_is_unpacked(self) -> None:
        parsed = protocol.parse_request(
            {
                "id": "r1",
                "school": "ataturk-ortaokulu",
                "capability": "podcast.status",
                "payload": {"job_id": "J"},
            }
        )
        self.assertEqual(parsed[0], "r1")
        self.assertEqual(parsed[1], "ataturk-ortaokulu")
        self.assertEqual(parsed[2], "podcast.status")
        self.assertEqual(parsed[3], {"job_id": "J"})
        self.assertIsNone(parsed[4], "deadline_ms yoksa zaman asimi None olmali")

    def test_unparseable_frames_raise_bad_request(self) -> None:
        for frame in (
            [],
            "istek",
            None,
            {},
            {"id": 7, "school": "o", "capability": "podcast.status"},
            {"id": "r1", "school": "o", "capability": 7},
            {"school": "o", "capability": "podcast.status"},
            {"id": "r1", "capability": "podcast.status"},
        ):
            with self.subTest(frame=frame):
                with self.assertRaises(protocol.CapabilityError) as raised:
                    protocol.parse_request(frame)
                self.assertEqual(
                    raised.exception.code,
                    "bad_request",
                    "ayristirilamayan istek 'bad_request' ile reddedilmeli",
                )

    def test_a_request_without_a_school_is_rejected_instead_of_guessed(self) -> None:
        for school in (None, "", "   ", 7, ["o"]):
            with self.subTest(school=school):
                with self.assertRaises(protocol.CapabilityError) as raised:
                    protocol.parse_request(
                        {
                            "id": "r1",
                            "school": school,
                            "capability": "podcast.status",
                            "payload": {},
                        }
                    )
                self.assertEqual(raised.exception.code, "bad_request")

    def test_deadline_margin_is_subtracted_so_answer_beats_backend_timeout(self) -> None:
        timeout = protocol.parse_request(
            {
                "id": "r1",
                "school": "o",
                "capability": "podcast.status",
                "deadline_ms": 60000,
            }
        )[4]
        self.assertIsNotNone(timeout)
        self.assertLess(
            timeout, 60.0, "deadline payi birakilmali; cevap backend'in tavanindan once gitmeli"
        )
        self.assertAlmostEqual(timeout, 60.0 - protocol.DEADLINE_MARGIN_SECS, places=6)

    def test_short_deadline_keeps_at_least_half_the_budget(self) -> None:
        timeout = protocol.parse_request(
            {
                "id": "r1",
                "school": "o",
                "capability": "podcast.status",
                "deadline_ms": 100,
            }
        )[4]
        self.assertAlmostEqual(timeout, 0.05, places=6)
        self.assertGreater(timeout, 0.0, "kisa deadline negatif zaman asimi uretmemeli")

    def test_non_positive_or_boolean_deadline_yields_no_timeout(self) -> None:
        for value in (0, -1, True, False, "60000", None, [60000]):
            with self.subTest(deadline_ms=value):
                self.assertIsNone(
                    protocol.parse_request(
                        {
                            "id": "r1",
                            "school": "o",
                            "capability": "podcast.status",
                            "deadline_ms": value,
                        }
                    )[4]
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


class _BridgeSettings:
    def __init__(self, job_root: str) -> None:
        self.job_root = job_root
        self.service = "podcast"
        self.token = "sir"
        self.max_concurrent = 4
        self.backend_url = "http://127.0.0.1:1"
        self.host = "127.0.0.1"
        self.port = 1
        self.server_name = "localhost"
        self.tls_fingerprint = ""
        self.reconnect_secs = 0.05
        self.reconnect_max_secs = 1.0

    def summary(self) -> str:
        return "test ayarlari"


class _RegisteringQuic(FakeQuic):
    def __init__(self, proto, greeting: dict) -> None:
        super().__init__()
        self._proto = proto
        self._greeting = greeting

    def get_next_available_stream_id(self) -> int:
        return 0

    def send_stream_data(self, sid: int, data: bytes, end_stream: bool) -> None:
        super().send_stream_data(sid, data, end_stream)
        if not end_stream:
            self._proto._streams[sid].feed(
                protocol.encode_frame(self._greeting), end=True
            )


class BridgeRegistrationPath(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        install_fake_aioquic()
        from src import bridge

        cls.bridge = bridge

    def setUp(self) -> None:
        from src import config

        self._log_level = config._active_level
        config.set_log_level("error")
        self._root = tempfile.TemporaryDirectory(prefix="podcast-hab2-")
        self.settings = _BridgeSettings(self._root.name)
        self.quic = FakeQuic()
        self.proto = self.bridge.BridgeProtocol(settings=self.settings)
        self.proto._quic = self.quic

    def tearDown(self) -> None:
        from src import config

        config._active_level = self._log_level
        self._root.cleanup()

    def test_the_registration_path_announces_hab_two_on_the_control_stream(self) -> None:
        quic = _RegisteringQuic(
            self.proto,
            {"type": "welcome", "worker_id": "w1", "protocol": "hab/2"},
        )
        self.proto._quic = quic
        asyncio.run(self.proto.register())
        sid, data, end = quic.written[0]
        hello = decode_frame(data)
        self.assertEqual(
            hello["protocol"],
            "hab/2",
            "Hello hab/2 bildirmeli; backend sozlesmeyi ALPN'den SONRA burada da dogruluyor",
        )
        self.assertEqual(hello["service"], "podcast")
        self.assertEqual(hello["token"], "sir")
        self.assertFalse(
            end, "kontrol akisi kapatilmamali; kapanmasi backend icin kayittan dusmedir"
        )

    def test_a_response_frame_echoes_the_inbound_school(self) -> None:
        from src import capabilities

        seen: list[str] = []

        def handler(school: str, payload: dict) -> dict:
            seen.append(school)
            return {"job_id": "J"}

        capabilities.REGISTRY["podcast.okul"] = handler
        try:
            self.serve(
                {
                    "id": "r-1",
                    "school": "ataturk-ortaokulu",
                    "capability": "podcast.okul",
                    "payload": {},
                }
            )
        finally:
            capabilities.REGISTRY.pop("podcast.okul", None)
        response = decode_frame(self.quic.written[0][1])
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["school"], "ataturk-ortaokulu")
        self.assertEqual(
            seen, ["ataturk-ortaokulu"], "okul isleyiciye ulasmali; payload'a gomulmemeli"
        )

    def test_an_err_frame_echoes_the_school_of_a_malformed_request(self) -> None:
        self.serve({"id": "r-2", "school": "ataturk-ortaokulu", "capability": 7})
        response = decode_frame(self.quic.written[0][1])
        self.assertEqual(response["status"], "err")
        self.assertEqual(response["code"], "bad_request")
        self.assertEqual(
            response["school"],
            "ataturk-ortaokulu",
            "cozulemeyen istekte bile okul yankilanmali; backend hangi okulun "
            "istegi oldugunu ancak boyle anlar",
        )

    def test_an_unknown_capability_still_echoes_the_school(self) -> None:
        self.serve(
            {
                "id": "r-3",
                "school": "ataturk-ortaokulu",
                "capability": "podcast.yok",
                "payload": {},
            }
        )
        response = decode_frame(self.quic.written[0][1])
        self.assertEqual(response["code"], "unsupported_capability")
        self.assertEqual(response["school"], "ataturk-ortaokulu")

    def serve(self, frame: dict, sid: int = 1) -> None:
        stream = protocol.FrameStream()
        stream.feed(protocol.encode_frame(frame), end=True)
        self.proto._streams[sid] = stream
        asyncio.run(self.proto._serve_request(sid, stream))


class _FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class CertificatePinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        install_fake_aioquic()
        from src import bridge

        cls.bridge = bridge

    def setUp(self) -> None:
        from src import config

        self._log_level = config._active_level
        config.set_log_level("error")

    def tearDown(self) -> None:
        from src import config

        config._active_level = self._log_level

    def certificate(self) -> tuple[str, str]:
        der = b"\x30\x82\x01\x00" + bytes(range(64))
        body = base64.b64encode(der).decode("ascii")
        pem = "-----BEGIN CERTIFICATE-----\n" + body + "\n-----END CERTIFICATE-----\n"
        return pem, hashlib.sha256(der).hexdigest()

    def fetch(self, pem: str, reported: str, expected: str = "") -> str:
        payload = {"certificate_pem": pem, "fingerprint_sha256": reported}
        with mock.patch.object(
            self.bridge.urllib.request,
            "urlopen",
            lambda url, timeout: _FakeHttpResponse(payload),
        ):
            return self.bridge.fetch_certificate("http://backend:8080", expected)

    def test_the_pin_is_compared_against_the_sha256_of_the_pems_own_der(self) -> None:
        pem, fingerprint = self.certificate()
        self.assertEqual(
            self.fetch(pem, fingerprint, fingerprint),
            pem,
            "pin PEM'in DER'inden hesaplanan SHA-256 ile karsilastirilmali",
        )

    def test_a_pin_that_does_not_match_refuses_to_connect(self) -> None:
        pem, fingerprint = self.certificate()
        with self.assertRaises(RuntimeError) as raised:
            self.fetch(pem, fingerprint, "0" * 64)
        self.assertIn("PINLENEN", str(raised.exception))

    def test_a_server_reporting_a_fingerprint_for_another_pem_is_refused(self) -> None:
        pem, _ = self.certificate()
        with self.assertRaises(RuntimeError):
            self.fetch(pem, "0" * 64)

    def test_without_a_pin_the_certificate_is_trusted_on_first_use(self) -> None:
        pem, fingerprint = self.certificate()
        self.assertEqual(self.fetch(pem, fingerprint), pem)

    def test_a_missing_pem_is_an_error_not_an_empty_trust_store(self) -> None:
        with self.assertRaises(RuntimeError):
            self.fetch("", "0" * 64)


class ReconnectLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        install_fake_aioquic()
        from src import bridge

        cls.bridge = bridge

    def setUp(self) -> None:
        from src import config

        self._log_level = config._active_level
        config.set_log_level("error")
        self._root = tempfile.TemporaryDirectory(prefix="podcast-hab2-")
        self.settings = _BridgeSettings(self._root.name)

    def tearDown(self) -> None:
        from src import config

        config._active_level = self._log_level
        self._root.cleanup()

    def test_backoff_doubles_and_stops_at_the_configured_ceiling(self) -> None:
        self.settings.reconnect_secs = 3.0
        self.settings.reconnect_max_secs = 120.0
        self.assertEqual(self.bridge.next_backoff(3.0, self.settings), 6.0)
        self.assertEqual(self.bridge.next_backoff(6.0, self.settings), 12.0)
        self.assertEqual(self.bridge.next_backoff(100.0, self.settings), 120.0)
        self.assertEqual(
            self.bridge.next_backoff(120.0, self.settings),
            self.settings.reconnect_max_secs,
            "geri cekilme tavani asmamali; jitter de tavani kaydirmamali",
        )

    def test_a_permanent_reject_keeps_the_loop_retrying_instead_of_exiting(self) -> None:
        attempts: list[int] = []

        async def fake_run_once(settings) -> None:
            attempts.append(1)
            raise protocol.HandshakeRejected("unauthorized", "kotu token")

        async def scenario() -> None:
            with mock.patch.object(self.bridge, "run_once", fake_run_once):
                task = asyncio.ensure_future(self.bridge.run_forever(self.settings))
                await asyncio.sleep(0.2)
                self.assertFalse(
                    task.done(),
                    "kalici red donguden CIKMAMALI: restart: unless-stopped ile "
                    "exit 2 sonsuz crash-loop olur ve backend duzelince servis "
                    "kendiliginden toparlanmaz",
                )
                self.assertEqual(len(attempts), 1, "kalici red sonrasi sikistirmali deneme")
                task.cancel()
                await asyncio.sleep(0)

        asyncio.run(scenario())

    def test_a_retryable_reject_schedules_the_next_attempt(self) -> None:
        attempts: list[int] = []

        async def fake_run_once(settings) -> None:
            attempts.append(1)
            raise protocol.HandshakeRejected("malformed", "bozuk greeting")

        async def scenario() -> None:
            with mock.patch.object(self.bridge, "run_once", fake_run_once):
                task = asyncio.ensure_future(self.bridge.run_forever(self.settings))
                await asyncio.sleep(0.6)
                task.cancel()
                await asyncio.sleep(0)

        asyncio.run(scenario())
        self.assertGreaterEqual(
            len(attempts), 2, "gecici red sonrasi bir sonraki deneme zamanlanmali"
        )


if __name__ == "__main__":
    unittest.main()