from __future__ import annotations

import asyncio
import json
import struct
import unittest

from src import protocol


class FrameSizeBoundaryIsExact(unittest.TestCase):
    def _payload_of_exact_size(self, size: int) -> dict:
        filler = "a" * size
        while True:
            body = json.dumps({"v": filler}, ensure_ascii=False).encode("utf-8")
            if len(body) == size:
                return {"v": filler}
            filler = filler[: len(filler) - (len(body) - size)]

    def test_a_frame_of_exactly_the_limit_must_be_accepted(self) -> None:
        payload = self._payload_of_exact_size(protocol.MAX_FRAME_BYTES)
        encoded = protocol.encode_frame(payload)
        self.assertEqual(
            len(encoded) - 4,
            protocol.MAX_FRAME_BYTES,
            "tam sinirdaki cerceve reddedildi; sinir '>' degil '>=' yazilmis olabilir",
        )

    def test_a_frame_one_byte_over_the_limit_must_be_refused(self) -> None:
        payload = self._payload_of_exact_size(protocol.MAX_FRAME_BYTES + 1)
        with self.assertRaises(protocol.CapabilityError) as caught:
            protocol.encode_frame(payload)
        self.assertEqual(caught.exception.code, "frame_too_large")

    def test_reader_accepts_a_declared_length_exactly_at_the_limit(self) -> None:
        async def read_it() -> None:
            stream = protocol.FrameStream()
            payload = self._payload_of_exact_size(protocol.MAX_FRAME_BYTES)
            encoded = protocol.encode_frame(payload)
            stream.feed(encoded, end=True)
            decoded = await stream.read_frame()
            self.assertEqual(decoded, payload)

        asyncio.run(read_it())

    def test_reader_refuses_a_declared_length_one_byte_over_the_limit(self) -> None:
        async def read_it() -> None:
            stream = protocol.FrameStream()
            stream.feed(struct.pack(">I", protocol.MAX_FRAME_BYTES + 1), end=True)
            with self.assertRaises(protocol.CapabilityError) as caught:
                await stream.read_frame()
            self.assertEqual(caught.exception.code, "frame_too_large")

        asyncio.run(read_it())


class ExceptionsCarryTheirMessage(unittest.TestCase):
    def test_capability_error_text_is_exactly_the_message(self) -> None:
        error = protocol.CapabilityError("bad_request", "alan eksik")
        self.assertEqual(
            str(error), "alan eksik",
            "metin tam esit olmali; alt dize kontrolu super().__init__ "
            "silinse bile gecer cunku BaseException.__new__ args'i saklar",
        )
        self.assertEqual(error.args, ("alan eksik",))
        self.assertEqual(error.code, "bad_request")

    def test_handshake_rejection_text_is_the_formatted_sentence(self) -> None:
        error = protocol.HandshakeRejected("unauthorized", "token yanlis")
        self.assertEqual(
            str(error), "backend kaydi reddetti (unauthorized): token yanlis"
        )
        self.assertEqual(len(error.args), 1)

    def test_api_refusal_text_is_the_formatted_sentence(self) -> None:
        error = protocol.ApiRefused("path_not_allowed", "/courses yasak")
        self.assertEqual(
            str(error), "kopru istegi reddetti (path_not_allowed): /courses yasak"
        )
        self.assertEqual(len(error.args), 1)


class ApiResponseKeepsEveryField(unittest.TestCase):
    def test_request_id_is_carried_through(self) -> None:
        answer = protocol.parse_api_response(
            {"outcome": "ok", "id": "01ABC", "status": 200, "body": {"x": 1}}
        )
        self.assertEqual(answer.request_id, "01ABC")

    def test_body_is_carried_through(self) -> None:
        answer = protocol.parse_api_response(
            {"outcome": "ok", "id": "r", "status": 200, "body": {"role": "ai"}}
        )
        self.assertEqual(answer.body, {"role": "ai"})

    def test_status_is_carried_through(self) -> None:
        answer = protocol.parse_api_response(
            {"outcome": "ok", "id": "r", "status": 404, "body": None}
        )
        self.assertEqual(answer.status, 404)


class ComparisonsRejectTheWrongSide(unittest.TestCase):
    def test_a_welcome_with_a_different_protocol_is_rejected(self) -> None:
        with self.assertRaises(protocol.HandshakeRejected):
            protocol.parse_greeting(
                {"type": "welcome", "worker_id": "w", "protocol": "hab/2"}
            )

    def test_a_welcome_with_the_right_protocol_is_accepted(self) -> None:
        worker = protocol.parse_greeting(
            {"type": "welcome", "worker_id": "w9", "protocol": protocol.PROTOCOL}
        )
        self.assertEqual(worker, "w9")

    def test_an_outcome_other_than_ok_or_err_is_rejected(self) -> None:
        for outcome in ("okk", "OK", "ok ", "", "success"):
            with self.subTest(outcome=outcome):
                with self.assertRaises(protocol.ApiRefused):
                    protocol.parse_api_response({"outcome": outcome, "id": "r"})


class TimeoutConstantsStayInsideTheBackendWindow(unittest.TestCase):
    def test_greeting_timeout_is_below_the_backend_handshake_window(self) -> None:
        self.assertLess(
            protocol.GREETING_TIMEOUT_SECS,
            10.0,
            "backend AI_HANDSHAKE_TIMEOUT_SECS=10; esit veya buyuk olursa bizim "
            "zaman asimimiz hic atesnemez, jenerik EOFError aliriz",
        )
        self.assertGreater(protocol.GREETING_TIMEOUT_SECS, 0.0)

    def test_keepalive_stays_below_the_backend_idle_timeout(self) -> None:
        self.assertLess(
            protocol.KEEPALIVE_SECS,
            protocol.IDLE_TIMEOUT_SECS,
            "keepalive idle timeout'un altinda kalmali, yoksa baglanti duser",
        )


if __name__ == "__main__":
    unittest.main()
