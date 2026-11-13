import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from better_arctic_shift.collector import Collector, FetchFailure, retry_delay, work_once
from better_arctic_shift.store import Store


THREAD = '1xnxfhv'


def comment(rid, **fields):
    return {'id': rid, 'body': 'saved text', 'author': 'example', 'link_id': 't3_' + THREAD,
            'parent_id': 't3_' + THREAD, 'created_utc': 1, **fields}


def listing(records):
    return {'kind': 'Listing', 'data': {'children': [{'kind': 't1' if 'body' in r else 't3', 'data': r} for r in records]}}


class FakeNetwork:
    reddit_base = 'https://www.reddit.com/'

    def __init__(self, fn):
        self.fn, self.urls = fn, []

    def get(self, url):
        self.urls.append(url)
        result = self.fn(urlparse(url), parse_qs(urlparse(url).query))
        return result if isinstance(result, bytes) else json.dumps(result).encode()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')

    def tearDown(self):
        self.store.close()

    def scenario(self, url, query):
        if url.path.endswith('/posts/ids'):
            return {'data': [{'id': THREAD, 'title': 'thread', 'selftext': '', 'num_comments': 2, 'retrieved_on': 10}]}
        if url.path.endswith('/comments/tree'):
            return {'data': [comment('hidden', retrieved_on=10), {'kind': 'more', 'data': {'children': ['child'], 'parent_id': 't1_hidden'}}]}
        if url.path.endswith('/comments/ids'):
            return {'data': [comment('child', parent_id='t1_hidden', retrieved_on=10)]}
        if '/comments/' in url.path:
            return [listing([{'id': THREAD, 'title': 'thread', 'num_comments': 2}]), listing([comment('child', parent_id='t1_hidden')])]
        if url.path.endswith('/api/info.json'):
            rows = [comment('hidden', body='[removed]', removed_by_category='moderator'), comment('child', parent_id='t1_hidden'), {'id': THREAD, 'title': 'thread', 'num_comments': 2}]
            return listing([r for r in rows if ('t1_' if 'body' in r else 't3_') + r['id'] in query['id'][0].split(',')])
        raise AssertionError(url.geturl())

    def test_audits_ids_missing_from_live_tree(self):
        network = FakeNetwork(self.scenario)
        result = Collector(self.store, network).collect(THREAD)
        self.assertEqual(result['state'], 'finished')
        self.assertIn('t1_hidden', result['status_audit']['checked_ids'])
        entity = self.store.entities(THREAD)['t1_hidden']
        self.assertEqual(entity['current']['removal']['actor'], 'mod')
        self.assertEqual(entity['readable']['body'], 'saved text')
        self.assertEqual(result['coverage']['status_unchecked_comments'], 0)

    def test_reddit_failure_keeps_arctic_and_marks_incomplete(self):
        def blocked(url, query):
            if url.hostname == 'www.reddit.com':
                raise FetchFailure('source returned HTTP 403', 403)
            return self.scenario(url, query)
        network = FakeNetwork(blocked)
        result = Collector(self.store, network).collect(THREAD)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['coverage']['comments'], 2)
        self.assertEqual(result['coverage']['status_unchecked_comments'], 2)
        self.assertEqual(len([u for u in network.urls if 'www.reddit.com' in u]), 1)
        self.assertEqual(self.store.entities(THREAD)['t1_hidden']['current']['state'], 'present')

    def test_missing_info_response_is_not_a_deletion(self):
        def missing(url, query):
            if url.path.endswith('/api/info.json'):
                return listing([])
            return self.scenario(url, query)
        result = Collector(self.store, FakeNetwork(missing)).collect(THREAD)
        self.assertEqual(result['state'], 'partial')
        self.assertIn('t1_hidden', result['status_audit']['missing_ids'])
        self.assertEqual(self.store.entities(THREAD)['t1_hidden']['current']['state'], 'present')

    def test_continuations_expand_beyond_one_page(self):
        def continuation(url, query):
            if url.path.endswith('/comments/tree'):
                return {'data': []}
            if '/comments/' in url.path and url.hostname == 'www.reddit.com':
                return [listing([{'id': THREAD, 'title': 'thread', 'num_comments': 151}]),
                        {'kind': 'Listing', 'data': {'children': [{'kind': 'more', 'data': {'children': [f'c{i}' for i in range(151)]}}]}}]
            if url.path.endswith('/api/morechildren.json'):
                return {'json': {'data': {'things': [{'kind': 't1', 'data': comment(i)} for i in query['children'][0].split(',')]}}}
            if url.path.endswith('/api/info.json'):
                return listing([{'id': THREAD, 'title': 'thread', 'num_comments': 151} if i.startswith('t3_') else comment(i[3:]) for i in query['id'][0].split(',')])
            return self.scenario(url, query)
        network = FakeNetwork(continuation)
        result = Collector(self.store, network).collect(THREAD)
        self.assertEqual(result['coverage']['comments'], 151)
        self.assertEqual(result['state'], 'finished')
        self.assertEqual(len([u for u in network.urls if 'morechildren' in u]), 2)
        self.assertEqual(len([u for u in network.urls if '/api/info' in u]), 2)

    def test_resume_reuses_successful_responses(self):
        first = FakeNetwork(self.scenario)
        result = Collector(self.store, first, max_requests=1).collect(THREAD)
        self.assertEqual(result['state'], 'partial')
        second = FakeNetwork(self.scenario)
        resumed = Collector(self.store, second).collect(THREAD, result['run_id'])
        self.assertEqual(resumed['state'], 'finished')
        self.assertFalse(any('/posts/ids' in u for u in second.urls))

    def test_rate_limit_persists_and_other_source_still_runs(self):
        def limited(url, query):
            if url.hostname != 'www.reddit.com':
                raise FetchFailure('source returned HTTP 429', 429, retry_after=7200)
            return self.scenario(url, query)
        network = FakeNetwork(limited)
        result = Collector(self.store, network).collect(THREAD)
        self.assertGreater(result['retry_after'], 7100)
        self.assertTrue(any('www.reddit.com' in u for u in network.urls))
        self.assertEqual(len([u for u in network.urls if 'arctic-shift' in u]), 1)

    def test_challenge_response_is_preserved_as_failed(self):
        result = Collector(self.store, FakeNetwork(lambda *_: b'<html>access denied</html>')).collect(THREAD)
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(self.store.rows(THREAD), [])
        row = self.store.db.execute('SELECT body,error FROM captures LIMIT 1').fetchone()
        self.assertIn(b'access denied', row[0])
        self.assertTrue(row[1])

    def test_retry_headers(self):
        self.assertEqual(retry_delay({'Retry-After': '3600'}), 3600)
        self.assertEqual(retry_delay({'X-RateLimit-Reset': '42'}), 42)

    def test_bad_records_become_failed_captures(self):
        def malformed(url, query):
            if url.path.endswith('/comments/tree'):
                return {'data': [comment('bad', body=['not text'])]}
            if url.hostname == 'www.reddit.com':
                raise FetchFailure('source returned HTTP 403', 403)
            return self.scenario(url, query)
        result = Collector(self.store, FakeNetwork(malformed)).collect(THREAD)
        self.assertEqual(result['state'], 'partial')
        self.assertTrue(any('malformed' in issue['message'] for issue in result['issues']))
        self.assertNotIn('t1_bad', self.store.entities(THREAD))


class QueueTests(unittest.TestCase):
    def test_lease_and_resume_survive_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.sqlite3'
            first = Store(path)
            first.watch(THREAD, 3600, at=0)
            job = first.claim(at=1)
            second = Store(path)
            self.assertIsNone(second.claim(at=2))
            resumed = second.claim(at=122)
            self.assertEqual(resumed['run_id'], job['run_id'])
            with self.assertRaises(RuntimeError):
                first.heartbeat(job)
            second.release(resumed, False, retry_after=7200, at=122)
            self.assertIsNone(second.claim(at=7321))
            self.assertIsNotNone(second.claim(at=7322))
            first.close()
            second.close()

    def test_worker_finishes_due_thread(self):
        store = Store(':memory:')
        try:
            store.watch(THREAD, 3600)
            fixture = CollectorTests()
            result = work_once(store, FakeNetwork(fixture.scenario))
            self.assertEqual(result['state'], 'finished')
            self.assertIsNone(work_once(store, FakeNetwork(fixture.scenario)))
        finally:
            store.close()
