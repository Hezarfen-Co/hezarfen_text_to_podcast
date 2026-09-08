from __future__ import annotations

import unittest

from src import protocol


class ApiRequestFormat(unittest.TestCase):
    def test_query_goes_in_a_separate_field_question_mark_inside_path_is_rejected(self):
        with self.assertRaises(protocol.ApiRefused) as raised:
            protocol.build_api_request("r1", "/notes?limit=10")
        self.assertEqual(raised.exception.code, "bad_path")

    def test_path_must_start_with_slash(self):
        with self.assertRaises(protocol.ApiRefused):
            protocol.build_api_request("r1", "notes")

    def test_request_fields_are_identical_to_the_backend_protocol(self):
        request = protocol.build_api_request(
            "r1", "/notes", query="limit=10&offset=0", on_behalf_of="user:01J"
        )
        self.assertEqual(request["id"], "r1")
        self.assertEqual(request["path"], "/notes")
        self.assertEqual(request["query"], "limit=10&offset=0")
        self.assertEqual(request["on_behalf_of"], "user:01J")
        self.assertEqual(request["method"], "GET")

    def test_leading_question_mark_of_the_query_is_trimmed(self):
        request = protocol.build_api_request("r1", "/notes", query="?limit=10")
        self.assertEqual(request["query"], "limit=10")

    def test_on_behalf_of_field_is_not_sent_at_all_when_not_given(self):
        request = protocol.build_api_request("r1", "/auth/me")
        self.assertNotIn("on_behalf_of", request)


class ApiResponseInterpretation(unittest.TestCase):
    def test_outcome_ok_status_404_IS_NOT_AN_ERROR_the_api_did_respond(self):
        response = protocol.parse_api_response(
            {"outcome": "ok", "id": "r1", "status": 404, "body": None}
        )
        self.assertEqual(response.status, 404)
        self.assertFalse(response.ok)

    def test_outcome_ok_status_403_IS_NOT_AN_ERROR_it_means_insufficient_permission(self):
        response = protocol.parse_api_response(
            {"outcome": "ok", "id": "r1", "status": 403, "body": {"error": "requires teacher role or higher"}}
        )
        self.assertEqual(response.status, 403)
        self.assertFalse(response.ok)

    def test_outcome_err_is_a_bridge_refusal_and_raises_an_exception(self):
        with self.assertRaises(protocol.ApiRefused) as raised:
            protocol.parse_api_response(
                {"outcome": "err", "id": "r1", "code": "path_not_allowed", "message": "..."}
            )
        self.assertEqual(raised.exception.code, "path_not_allowed")

    def test_ok_is_true_only_for_2xx(self):
        for status, expected in ((200, True), (201, True), (299, True), (300, False), (404, False), (500, False)):
            with self.subTest(status=status):
                response = protocol.parse_api_response(
                    {"outcome": "ok", "id": "r", "status": status, "body": None}
                )
                self.assertEqual(response.ok, expected)

    def test_unknown_outcome_is_rejected(self):
        with self.assertRaises(protocol.ApiRefused):
            protocol.parse_api_response({"outcome": "belki", "id": "r"})

    def test_non_object_frame_is_rejected(self):
        with self.assertRaises(protocol.ApiRefused):
            protocol.parse_api_response(["ok"])


class AllowlistRecord(unittest.TestCase):
    def test_allowlist_is_the_same_set_as_the_backend_constant(self):
        self.assertEqual(len(protocol.API_ALLOWLIST), 16)
        for path in protocol.API_ALLOWLIST:
            with self.subTest(path=path):
                self.assertTrue(path.startswith("/"))
                self.assertNotIn("?", path)

    def test_byte_serving_routes_are_NOT_in_the_allowlist(self):
        for forbidden in ("/notes/{id}/files", "/notes/{id}/files/{file_id}", "/users/{id}/avatar"):
            with self.subTest(path=forbidden):
                self.assertNotIn(forbidden, protocol.API_ALLOWLIST)


if __name__ == "__main__":
    unittest.main()
