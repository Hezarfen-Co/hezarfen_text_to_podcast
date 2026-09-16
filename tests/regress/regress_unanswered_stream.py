import asyncio
import json
import struct
import sys
import types
import unittest

from src import config, protocol


SAHTE_MODULLER = (
    "aioquic",
    "aioquic.asyncio",
    "aioquic.asyncio.protocol",
    "aioquic.quic",
    "aioquic.quic.configuration",
    "aioquic.quic.events",
    "src.bridge",
)


def install_fake_aioquic() -> dict:
    onceki = {ad: sys.modules.get(ad) for ad in SAHTE_MODULLER}

    class QuicConnectionProtocol:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def transmit(self) -> None:
            pass

    class QuicConfiguration:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class QuicEvent:
        pass

    class StreamDataReceived(QuicEvent):
        pass

    class ConnectionTerminated(QuicEvent):
        pass

    def connect(*args, **kwargs):
        raise RuntimeError("kukla aioquic ag acmaz")

    root = types.ModuleType("aioquic")
    asyncio_module = types.ModuleType("aioquic.asyncio")
    protocol_module = types.ModuleType("aioquic.asyncio.protocol")
    quic_module = types.ModuleType("aioquic.quic")
    configuration_module = types.ModuleType("aioquic.quic.configuration")
    events_module = types.ModuleType("aioquic.quic.events")

    asyncio_module.connect = connect
    protocol_module.QuicConnectionProtocol = QuicConnectionProtocol
    configuration_module.QuicConfiguration = QuicConfiguration
    events_module.QuicEvent = QuicEvent
    events_module.StreamDataReceived = StreamDataReceived
    events_module.ConnectionTerminated = ConnectionTerminated
    asyncio_module.protocol = protocol_module
    quic_module.configuration = configuration_module
    quic_module.events = events_module
    root.asyncio = asyncio_module
    root.quic = quic_module

    sys.modules["aioquic"] = root
    sys.modules["aioquic.asyncio"] = asyncio_module
    sys.modules["aioquic.asyncio.protocol"] = protocol_module
    sys.modules["aioquic.quic"] = quic_module
    sys.modules["aioquic.quic.configuration"] = configuration_module
    sys.modules["aioquic.quic.events"] = events_module
    sys.modules.pop("src.bridge", None)
    return onceki


def restore_modules(onceki: dict) -> None:
    for ad, modul in onceki.items():
        if modul is None:
            sys.modules.pop(ad, None)
        else:
            sys.modules[ad] = modul


class FakeQuic:
    def __init__(self) -> None:
        self.written: list = []

    def send_stream_data(self, sid: int, data: bytes, end_stream: bool) -> None:
        self.written.append((sid, data, end_stream))


class FakeSettings:
    max_concurrent = 4
    service = "podcast"
    token = "sir"
    job_root = ""


def decode_frame(data: bytes) -> dict:
    (length,) = struct.unpack(">I", data[:4])
    return json.loads(data[4 : 4 + length].decode("utf-8"))


class UnansweredStreamRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._onceki_moduller = install_fake_aioquic()
        from src import bridge

        cls.bridge = bridge
        taban = bridge.BridgeProtocol.__mro__[1]
        kukla = sys.modules["aioquic.asyncio.protocol"].QuicConnectionProtocol
        assert taban is kukla, (
            "BridgeProtocol kukla tabandan turemedi; gercek aioquic kurulu olsa "
            "bile test ag ACMAMALI (taban=%r)" % (taban,)
        )

    @classmethod
    def tearDownClass(cls) -> None:
        restore_modules(cls._onceki_moduller)

    def setUp(self) -> None:
        self._log_level = config._active_level
        config.set_log_level("error")
        self.quic = FakeQuic()
        self.proto = self.bridge.BridgeProtocol(settings=FakeSettings())
        self.proto._quic = self.quic

    def tearDown(self) -> None:
        config._active_level = self._log_level

    def run_request(self, body: object, sid: int = 1) -> None:
        stream = protocol.FrameStream()
        stream.feed(protocol.encode_frame(body), end=True)
        self.proto._streams[sid] = stream
        asyncio.run(self.proto._serve_request(sid, stream))

    def test_unparseable_request_is_answered_with_bad_request_not_left_hanging(self) -> None:
        for body in ([1, 2], "istek", {}, {"capability": "podcast.status"}, {"id": 7}):
            with self.subTest(body=body):
                self.quic.written.clear()
                self.proto._streams.clear()
                self.run_request(body)
                self.assertEqual(
                    len(self.quic.written),
                    1,
                    "ayristirilamayan istek cevapsiz birakildi; backend deadline boyunca "
                    "bekler ve worker kiralamasini tutar",
                )
                sid, data, end = self.quic.written[0]
                response = decode_frame(data)
                self.assertEqual(response["status"], "err")
                self.assertEqual(response["code"], "bad_request")
                self.assertTrue(end, "cevap yazildiktan sonra akis kapatilmali")

    def test_request_identifier_is_preserved_on_an_unparseable_request(self) -> None:
        self.run_request({"id": "r-42", "capability": 7})
        response = decode_frame(self.quic.written[0][1])
        self.assertEqual(
            response["id"], "r-42", "hata cevabi istegin kimligini kaybetmemeli"
        )

    def test_answered_stream_is_dropped_from_the_registry(self) -> None:
        self.run_request({"id": "r-1", "capability": 7}, sid=5)
        self.assertNotIn(
            5, self.proto._streams, "cevaplanan akis kaydi sizdirilmamali"
        )

    def test_closed_stream_leaks_no_registry_entry_even_when_left_unanswered(self) -> None:
        stream = protocol.FrameStream()
        stream.feed(b"", end=True)
        self.proto._streams[9] = stream
        asyncio.run(self.proto._serve_request(9, stream))
        self.assertEqual(
            self.quic.written, [], "karsi taraf akisi kapattiysa cevap yazilamaz"
        )
        self.assertNotIn(9, self.proto._streams)

    def test_response_over_eight_mib_returns_frame_too_large_instead_of_being_silently_swallowed(self) -> None:
        huge = protocol.ok_response(
            "r-9", {"payload": "a" * (protocol.MAX_FRAME_BYTES + 16)}
        )
        self.proto._streams[3] = protocol.FrameStream()
        self.proto._finish(3, "r-9", huge)
        self.assertEqual(
            len(self.quic.written),
            1,
            "cerceve sinirini asan cevap sessizce yutulmamali; hata cevabi yazilmali",
        )
        response = decode_frame(self.quic.written[0][1])
        self.assertEqual(response["status"], "err")
        self.assertEqual(
            response["code"],
            "frame_too_large",
            "buyuk cevap 'frame_too_large' err'ine dusurulmeli",
        )
        self.assertEqual(response["id"], "r-9")
        self.assertTrue(self.quic.written[0][2])
        self.assertNotIn(3, self.proto._streams)

    def test_response_under_the_limit_is_sent_as_is(self) -> None:
        response = protocol.ok_response("r-8", {"job_id": "J"})
        self.proto._streams[7] = protocol.FrameStream()
        self.proto._finish(7, "r-8", response)
        self.assertEqual(decode_frame(self.quic.written[0][1]), response)

    def test_huge_payload_produced_by_a_capability_also_returns_frame_too_large(self) -> None:
        from src import capabilities

        def huge_capability(payload: dict) -> dict:
            return {"payload": "a" * (protocol.MAX_FRAME_BYTES + 16)}

        capabilities.REGISTRY["podcast.devasa"] = huge_capability
        try:
            self.run_request({"id": "r-7", "capability": "podcast.devasa", "payload": {}})
        finally:
            capabilities.REGISTRY.pop("podcast.devasa", None)
        self.assertEqual(len(self.quic.written), 1)
        response = decode_frame(self.quic.written[0][1])
        self.assertEqual(response["code"], "frame_too_large")

    def test_busy_is_returned_at_the_concurrent_request_ceiling_without_hanging_the_stream(self) -> None:
        self.proto._inflight = FakeSettings.max_concurrent
        self.run_request({"id": "r-6", "capability": "podcast.status", "payload": {}})
        response = decode_frame(self.quic.written[0][1])
        self.assertEqual(response["code"], "busy")


if __name__ == "__main__":
    unittest.main()
