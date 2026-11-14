"""keep an assistant's archive evidence honest and its requests bounded"""

import copy
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlparse

from better_arctic_shift import agent_reader as reader


POST = {"id": "abc123", "title": "a synthetic discussion", "selftext": "first post", "num_comments": 7,
        "created_utc": 100, "retrieved_on": 110, "_meta": {"retrieved_2nd_on": 140}}


def comment(item_id, parent="t3_abc123", **extra):
    return {"id": item_id, "body": "a captured reply", "parent_id": parent, "link_id": "t3_abc123",
            "author": "[deleted]", "created_utc": 120, "retrieved_on": 130, **extra}


class Client:
    def __init__(self, tree=None, failure=None):
        self.requests, self.tree, self.failure = [], tree or [], failure

    def get(self, path, parameters):
        self.requests.append(reader.API + path)
        if self.failure:
            raise self.failure
        return {"data": [POST]} if path == "posts/ids" else {"data": self.tree}


class ReaderTests(unittest.TestCase):
    def test_archive_dates_do_not_become_the_download_time(self):
        raw = comment("aaa", body="original words", _meta={"was_deleted_later": True, "retrieved_2nd_on": 180})
        result = reader.fetch_thread("abc123", client=Client([raw]))
        saved = result["comments"][0]
        self.assertEqual(saved["content_observed_at"], reader.iso(130))
        self.assertEqual(saved["status_observed_at"], reader.iso(180))
        self.assertEqual(saved["observed_state"], "removed")
        self.assertTrue(saved["historical_text_retained"])
        self.assertFalse(saved["current_state_verified"])
        self.assertNotEqual(saved["status_observed_at"], result["downloaded_at"])

    def test_deleted_author_is_not_deleted_comment_and_unknown_dates_stay_unknown(self):
        item = reader.normalized(comment("aaa", retrieved_on=None), "t1", "abc123")
        self.assertEqual(item["observed_state"], "present")
        self.assertIsNone(item["content_observed_at"])
        self.assertIsNone(item["status_observed_at"])

    def test_unexpanded_replies_and_missing_parents_are_explicit(self):
        tree = [{"kind": "t1", "data": comment("aaa", "t1_missing", replies={"kind": "Listing", "data": {"children": [
            {"kind": "more", "data": {"children": ["bbb"], "count": 1}}
        ]}})}]
        result = reader.fetch_thread("abc123", max_comments=1, client=Client(tree))
        coverage = result["coverage"]
        self.assertEqual(coverage["captured_comments"], 1)
        self.assertEqual(coverage["archive_continuations"], 1)
        self.assertEqual(coverage["missing_parent_ids"], ["t1_missing"])
        self.assertTrue(coverage["collection_limit_reached"])
        self.assertEqual(coverage["completeness"], "unverified")

    def test_focus_includes_ancestors_and_descendants_with_pagination(self):
        tree = [comment("aaa"), comment("bbb", "t1_aaa"), comment("ccc", "t1_bbb"), comment("ddd")]
        snapshot = reader.fetch_thread("abc123", client=Client(tree))
        first = reader.view(snapshot, focus="bbb", limit=2)
        second = reader.view(snapshot, focus="bbb", limit=2, offset=2)
        self.assertEqual([x["id"] for x in first["comments"]], ["t1_aaa", "t1_bbb"])
        self.assertEqual([x["id"] for x in second["comments"]], ["t1_ccc"])
        self.assertEqual(second["page"]["parents_outside_page"], ["t1_bbb"])
        self.assertEqual(len(snapshot["comments"]), 4)

    def test_phrase_search_preserves_parent_context_and_marks_truncation(self):
        snapshot = reader.fetch_thread("abc123", client=Client([comment("aaa"), comment("bbb", "t1_aaa", body="the unique phrase")]))
        result = reader.view(snapshot, query="UNIQUE", body_limit=5)
        self.assertEqual(result["matched_comment_ids"], ["t1_bbb"])
        self.assertEqual(len(result["comments"]), 2)
        self.assertTrue(result["comments"][1]["text_truncated"])
        self.assertEqual(result["comments"][1]["text_length"], 17)

    def test_cyclic_parent_links_do_not_hang_context_reading(self):
        snapshot = reader.fetch_thread("abc123", client=Client([comment("aaa", "t1_bbb"), comment("bbb", "t1_aaa")]))
        self.assertEqual(len(reader.view(snapshot, focus="aaa")["comments"]), 2)

    def test_request_block_stops_without_trying_another_route(self):
        client = Client(failure=reader.SourceError("access_denied", "blocked", 403))
        result = reader.fetch_thread("abc123", client=client)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result["status"], "source_unavailable")
        self.assertEqual(result["issues"][0]["http_status"], 403)

    def test_cache_keeps_original_snapshot_date_and_avoids_more_network(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Client([comment("aaa")])
            first = reader.read_thread("abc123", 1000, directory, client=client)
            unavailable = Client(failure=AssertionError("no new request expected"))
            second = reader.read_thread("abc123", 1000, directory, client=unavailable)
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second["downloaded_at"], first["downloaded_at"])
            self.assertEqual(unavailable.requests, [])

    def test_links_are_parsed_without_becoming_fetch_targets(self):
        self.assertEqual(reader.thread_id("https://www.reddit.com/r/test/comments/abc123/title/"), "abc123")
        self.assertEqual(reader.thread_id("https://redd.it/abc123"), "abc123")
        for url in ("https://reddit.com.evil.example/comments/abc123", "https://u:p@reddit.com/comments/abc123", "http://127.0.0.1/comments/abc123", "https://reddit.com/r/test/s/share123"):
            with self.assertRaises(ValueError):
                reader.thread_id(url)

    def test_real_transport_constructs_only_archive_requests(self):
        class Opener:
            def __init__(self):
                self.calls = []

            def open(self, request, timeout):
                self.calls.append((request, timeout))
                return io.BytesIO(json.dumps({"data": [POST]}).encode())

        opener = Opener()
        client = reader.ArchiveClient(opener)
        with patch.dict("os.environ", {"REDDIT_ACCESS_TOKEN": "never-send-this"}):
            client.get("posts/ids", {"ids": "abc123"})
        request, timeout = opener.calls[0]
        self.assertEqual(urlparse(request.full_url).hostname, "arctic-shift.photon-reddit.com")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(timeout, 25)

    def test_transport_rejects_html_and_reports_rate_limit_without_retry(self):
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 429, "busy", {"Retry-After": "45"}, io.BytesIO(b"private response"))

        with self.assertRaises(reader.SourceError) as caught:
            reader.ArchiveClient(Opener()).get("posts/ids", {"ids": "abc123"})
        self.assertEqual(caught.exception.detail["retry_after_seconds"], 45)
        self.assertNotIn("private response", str(caught.exception.detail))

    def test_removed_marker_is_not_claimed_as_retained_text(self):
        raw = comment("aaa", body="[Removed by Reddit]", _meta={"was_deleted_later": True})
        self.assertFalse(reader.normalized(raw, "t1", "abc123")["historical_text_retained"])


if __name__ == "__main__":
    unittest.main()
