"""exercise the real PRAW request path with synthetic, offline responses"""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from better_arctic_shift.reddit_access import REQUIRED, check_access


THREAD = "abc123"
SETTINGS = {
    "REDDIT_CLIENT_ID": "synthetic-client-id",
    "REDDIT_CLIENT_SECRET": "synthetic-client-secret",
    "REDDIT_USER_AGENT": "linux:riri-access-check:v0.1 (by /u/test_account)",
}
TOKEN = {"access_token": "synthetic-access-token", "token_type": "bearer",
         "expires_in": 3600, "scope": "*"}
LISTING = [
    {"kind": "Listing", "data": {"children": [
        {"kind": "t3", "data": {"id": THREAD, "name": "t3_" + THREAD,
         "title": "a synthetic thread", "num_comments": 17, "subreddit": "test"}}
    ]}},
    {"kind": "Listing", "data": {"children": [
        {"kind": "t1", "data": {"id": "def456", "name": "t1_def456",
         "body": "text stays out of the access report", "author": "test_account",
         "parent_id": "t3_" + THREAD, "link_id": "t3_" + THREAD, "replies": ""}},
        {"kind": "more", "data": {"id": "more1", "name": "t1_more1",
         "count": 16, "children": ["ghi789"], "parent_id": "t3_" + THREAD}}
    ]}},
]


class ConfigurationTests(unittest.TestCase):
    def test_missing_credentials_make_no_client(self):
        with patch("better_arctic_shift.reddit_access._client") as create:
            report = check_access(THREAD, {})
        self.assertEqual(report["status"], "needs_credentials")
        self.assertEqual(report["missing"], list(REQUIRED))
        self.assertFalse(report["network_attempted"])
        create.assert_not_called()

    def test_invalid_headers_and_placeholders_make_no_client(self):
        for key, value in [("REDDIT_USER_AGENT", "agent\r\nAuthorization: secret"),
                           ("REDDIT_CLIENT_ID", "YOUR_CLIENT_ID")]:
            with self.subTest(key=key), patch("better_arctic_shift.reddit_access._client") as create:
                report = check_access(THREAD, {**SETTINGS, key: value})
                self.assertEqual(report["status"], "invalid_configuration")
                self.assertEqual(report["invalid"], [key])
                create.assert_not_called()

    def test_other_hosts_are_not_thread_inputs(self):
        report = check_access("https://example.invalid/comments/abc123", SETTINGS)
        self.assertEqual(report["status"], "invalid_thread")
        self.assertFalse(report["network_attempted"])

    def test_cli_preflight_does_not_create_a_store(self):
        from better_arctic_shift.cli import main
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            target = Path(directory) / "history.sqlite3"
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = main(["--db", str(target), "check-access", THREAD])
            self.assertEqual(code, 2)
            self.assertFalse(target.exists())
            self.assertEqual(json.loads(stream.getvalue())["status"], "needs_credentials")


@unittest.skipUnless(importlib.util.find_spec("praw"), "install the optional reddit extra to check PRAW")
class PrawAccessTests(unittest.TestCase):
    def simulate(self, responses, environment=None):
        import requests
        seen = []

        def send(prepared, **kwargs):
            seen.append((prepared, kwargs))
            if len(seen) > len(responses):
                raise AssertionError("the check attempted an unexpected request")
            status, body, headers = responses[len(seen) - 1]
            response = requests.Response()
            response.status_code = status
            response.url = prepared.url
            response.request = prepared
            response.headers.update({"Content-Type": "application/json", **headers})
            response._content = (body if isinstance(body, str) else json.dumps(body)).encode()
            return response

        with patch("requests.sessions.Session.send", side_effect=send), patch.dict(os.environ, environment or {}):
            report = check_access(THREAD, SETTINGS)
        return report, seen

    def test_real_praw_app_only_flow_and_bounded_comment_sample(self):
        report, seen = self.simulate([(200, TOKEN, {}), (200, LISTING, {})], {
            "praw_username": "unrelated_account", "praw_password": "unrelated_password",
            "praw_refresh_token": "unrelated_refresh_token",
            "praw_oauth_url": "https://example.invalid", "praw_site": "unrelated_site",
        })
        self.assertEqual(report["status"], "ok", report)
        self.assertTrue(report["network_attempted"])
        self.assertEqual(len(seen), 2)
        token_request, token_options = seen[0]
        self.assertEqual(token_request.url, "https://www.reddit.com/api/v1/access_token")
        self.assertEqual(parse_qs(token_request.body), {"grant_type": ["client_credentials"]})
        self.assertFalse(token_options["allow_redirects"])
        read_request, read_options = seen[1]
        parsed = urlparse(read_request.url)
        self.assertEqual((parsed.scheme, parsed.hostname, parsed.path.rstrip("/")),
                         ("https", "oauth.reddit.com", "/comments/" + THREAD))
        self.assertEqual(parse_qs(parsed.query)["limit"], ["10"])
        self.assertFalse(read_options["allow_redirects"])
        self.assertEqual(report["reported_comment_count"], 17)
        self.assertEqual(report["sampled_comment_ids"], ["t1_def456"])
        self.assertEqual(report["continuation_placeholders"], 1)
        self.assertTrue(report["sample_only"])
        output = json.dumps(report)
        for private in (*SETTINGS.values(), TOKEN["access_token"], "text stays out", "unrelated_password"):
            self.assertNotIn(private, output)

    def test_401_at_token_endpoint_stops_at_authentication(self):
        report, seen = self.simulate([(401, {"message": "unauthorized"}, {})])
        self.assertEqual(report["status"], "authentication_failed")
        self.assertEqual(report["failed_stage"], "authentication")
        self.assertEqual(len(seen), 1)

    def test_403_never_falls_back_to_unauthenticated_access(self):
        for stage in ("authentication", "thread"):
            with self.subTest(stage=stage):
                replies = [] if stage == "authentication" else [(200, TOKEN, {})]
                replies.append((403, {"message": SETTINGS["REDDIT_CLIENT_SECRET"]}, {}))
                report, seen = self.simulate(replies)
                self.assertEqual(report["status"], "access_denied")
                self.assertEqual(report["failed_stage"], stage)
                self.assertEqual(len(seen), len(replies))
                self.assertNotIn(SETTINGS["REDDIT_CLIENT_SECRET"], json.dumps(report))

    def test_rate_limit_returns_retry_window_without_more_requests(self):
        report, seen = self.simulate([(200, TOKEN, {}), (429, {}, {"Retry-After": "45"})])
        self.assertEqual(report["status"], "rate_limited")
        self.assertEqual(report["retry_after_seconds"], 45)
        self.assertEqual(len(seen), 2)

    def test_redirect_does_not_forward_credentials(self):
        report, seen = self.simulate([(200, TOKEN, {}), (302, {}, {"Location": "https://example.invalid/"})])
        self.assertEqual(report["status"], "redirected")
        self.assertEqual(len(seen), 2)

    def test_oauth_error_description_is_not_echoed(self):
        report, seen = self.simulate([(200, {"error": "invalid_client", "error_description": SETTINGS["REDDIT_CLIENT_SECRET"]}, {})])
        self.assertEqual(report["status"], "authentication_failed")
        self.assertEqual(len(seen), 1)
        self.assertNotIn(SETTINGS["REDDIT_CLIENT_SECRET"], json.dumps(report))

    def test_html_response_is_not_a_successful_read(self):
        report, _ = self.simulate([(200, TOKEN, {}), (200, "<html>access denied</html>", {})])
        self.assertEqual(report["status"], "unexpected_response")
        self.assertNotIn("sampled_comment_ids", report)

    def test_missing_thread_is_reported(self):
        report, seen = self.simulate([(200, TOKEN, {}), (404, {}, {})])
        self.assertEqual(report["status"], "not_found")
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()
