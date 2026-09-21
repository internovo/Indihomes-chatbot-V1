"""
Tests that WATI sends are judged by the RESPONSE BODY, not the HTTP status,
and that session messages put messageText in the query string.

Run: python -m unittest tests.test_wati_send_result -v

Both bugs were found live on 2026-09-21 while proving the new API key worked:

  POST /api/v1/sendInteractiveButtonsMessage?whatsappNumber=7208713112
    -> HTTP 200  {"result":false,"info":"Invalid Conversation"}

  POST /api/v1/sendSessionMessage/917208713112   (messageText in JSON body)
    -> HTTP 200  {"result":false,"info":"message text can not be empty"}

Both were reported as successful sends, because _post_json() only looked at
the HTTP status. A real success looks like
{"ok":true,"result":"success","message":{...,"statusString":"SENT"}} - note
`result` is the STRING "success", so a naive bool(result) check passes for
the failure case too ("false" is a non-empty string only in some shapes, and
the real failure uses a boolean). Hence the explicit-failure-marker design.

No network: urlopen is patched.
"""

import json
import unittest
from unittest import mock

import wati_client


REAL_SUCCESS = json.dumps({
    "ok": True, "result": "success",
    "message": {"whatsappMessageId": "wamid.TEST", "statusString": "SENT"},
})
REJECT_CONVERSATION = json.dumps({"result": False, "info": "Invalid Conversation"})
REJECT_EMPTY_TEXT = json.dumps({"result": False, "info": "message text can not be empty"})


class _Resp:
    """Minimal stand-in for the urlopen context manager."""
    def __init__(self, body, status=200):
        self._body = body.encode(); self.status = status
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *e): return False


class BodySaysOkTests(unittest.TestCase):
    def test_real_success_shape_is_ok(self):
        self.assertEqual(wati_client._body_says_ok(REAL_SUCCESS), (True, ""))

    def test_explicit_false_result_is_failure_and_carries_info(self):
        ok, info = wati_client._body_says_ok(REJECT_CONVERSATION)
        self.assertFalse(ok)
        self.assertIn("Invalid Conversation", info)

        ok, info = wati_client._body_says_ok(REJECT_EMPTY_TEXT)
        self.assertFalse(ok)
        self.assertIn("message text can not be empty", info)

    def test_ok_false_is_failure(self):
        ok, _ = wati_client._body_says_ok(json.dumps({"ok": False, "info": "nope"}))
        self.assertFalse(ok)

    def test_unparseable_or_unknown_shape_counts_as_success(self):
        """Never turn a real delivery into a retry loop over a shape change."""
        for body in ("", "not json", "[]", json.dumps({"whatever": 1})):
            self.assertTrue(wati_client._body_says_ok(body)[0], body)


class PostJsonHonoursBodyTests(unittest.TestCase):
    def test_200_with_result_false_returns_False(self):
        with mock.patch.object(wati_client.urllib.request, "urlopen",
                               return_value=_Resp(REJECT_CONVERSATION)):
            self.assertFalse(wati_client._post_json("https://x/api/v1/send", {}))

    def test_200_with_real_success_returns_True(self):
        with mock.patch.object(wati_client.urllib.request, "urlopen",
                               return_value=_Resp(REAL_SUCCESS)):
            self.assertTrue(wati_client._post_json("https://x/api/v1/send", {}))


class SessionMessageUsesQueryStringTests(unittest.TestCase):
    def test_messageText_is_in_the_url_not_the_body(self):
        seen = {}

        def _capture(req, *a, **k):
            seen["url"] = req.full_url
            seen["body"] = req.data
            return _Resp(REAL_SUCCESS)

        with mock.patch.object(wati_client, "is_configured", return_value=True), \
             mock.patch.object(wati_client, "_endpoint", return_value="host/123"), \
             mock.patch.object(wati_client, "_api_key", return_value="k"), \
             mock.patch.object(wati_client.urllib.request, "urlopen", _capture):
            ok = wati_client.send_session_message("917208713112", "hello there")

        self.assertTrue(ok)
        self.assertIn("messageText=hello+there", seen["url"])
        self.assertIn("/api/v1/sendSessionMessage/917208713112?", seen["url"])
        # The text must NOT be smuggled in the JSON body - that is the bug.
        self.assertNotIn(b"hello there", seen["body"] or b"")

    def test_rejected_send_reports_failure(self):
        with mock.patch.object(wati_client, "is_configured", return_value=True), \
             mock.patch.object(wati_client, "_endpoint", return_value="host/123"), \
             mock.patch.object(wati_client, "_api_key", return_value="k"), \
             mock.patch.object(wati_client.urllib.request, "urlopen",
                               return_value=_Resp(REJECT_EMPTY_TEXT)):
            self.assertFalse(wati_client.send_session_message("917208713112", "hi"))


if __name__ == "__main__":
    unittest.main()
