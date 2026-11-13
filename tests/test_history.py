import json
import tempfile
import unittest
from pathlib import Path

from better_arctic_shift.model import observations, thread_id
from better_arctic_shift.store import Store
from better_arctic_shift.transfer import export_file, import_file


THREAD = '1xnxfhv'


def comment(rid='parent', **fields):
    return {"id": rid, "body": "saved words", "author": "example", "link_id": 't3_' + THREAD,
            "parent_id": 't3_' + THREAD, "created_utc": 10, **fields}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')

    def tearDown(self):
        self.store.close()

    def put(self, value, source='reddit', at=100):
        self.store.import_payload(THREAD, value, source, at)

    def test_later_removal_keeps_text_and_reason(self):
        self.put(comment(), 'arctic_shift', 500)
        self.put(comment(body='[removed]', removed_by_category='moderator'), at=200)
        self.put(comment(retrieved_on=50), 'arctic_shift', 900)
        entity = self.store.entities(THREAD)['t1_parent']
        self.assertEqual(entity['current']['state'], 'removed')
        self.assertEqual(entity['current']['removal']['actor'], 'mod')
        self.assertEqual(entity['readable']['body'], 'saved words')

    def test_newer_archive_removal_beats_old_live_capture(self):
        self.put(comment(), at=100)
        self.put(comment(retrieved_on=50, _meta={'retrieved_2nd_on': 200, 'was_deleted_later': True, 'removal_type': 'deleted'}), 'arctic_shift', 900)
        self.assertEqual(self.store.entities(THREAD)['t1_parent']['current']['state'], 'deleted')

    def test_undated_archive_never_becomes_a_fresh_status(self):
        self.put(comment(body='[deleted]'), at=100)
        self.put(comment(), 'arctic_shift', 1000)
        self.assertEqual(self.store.entities(THREAD)['t1_parent']['current']['state'], 'deleted')

    def test_deleted_author_does_not_mean_deleted_comment(self):
        self.put(comment(author='[deleted]'))
        self.assertEqual(self.store.entities(THREAD)['t1_parent']['current']['state'], 'present')

    def test_restoration_and_empty_responses(self):
        self.put(comment(body='[removed]'), at=100)
        self.put(comment(body=''), at=200)
        self.assertEqual(self.store.entities(THREAD)['t1_parent']['current']['state'], 'removed')
        self.put(comment(body='restored text'), at=300)
        self.assertEqual(self.store.entities(THREAD)['t1_parent']['current']['state'], 'present')

    def test_arctic_initially_removed_and_reddit_removal_type(self):
        rows = observations(comment(retrieved_on=50, _meta={'retrieved_2nd_on': 100, 'was_initially_deleted': True, 'removal_type': 'removed by reddit'}), 't1', THREAD, 'arctic_shift', 500)
        self.assertEqual([(r['state'], r['observed_at']) for r in rows], [('removed', 50), ('present', 100)])
        self.assertEqual(rows[0]['removal']['actor'], 'reddit')

    def test_duplicate_import_does_not_duplicate_history(self):
        record = comment(retrieved_on=50)
        self.put(record, 'arctic_shift', 100)
        self.put(record, 'arctic_shift', 200)
        self.assertEqual(len(self.store.rows(THREAD)), 1)

    def test_removed_search_includes_parents_and_all_descendants(self):
        self.put([comment(), comment('child', body='disputed claim', parent_id='t1_parent'), comment('grandchild', parent_id='t1_child'), comment('sibling', parent_id='t1_parent')])
        self.put(comment('child', body='[removed]', parent_id='t1_parent'), at=200)
        result = self.store.search(THREAD, 'disputed', removed=True)
        self.assertEqual(result['matched_ids'], ['t1_child'])
        self.assertEqual({e['id'] for e in result['entities']}, {'t3_' + THREAD, 't1_parent', 't1_child', 't1_grandchild'})

    def test_coverage_tracks_missing_and_stale_separately(self):
        self.put(comment(parent_id='t1_missing'))
        self.put({'kind': 'more', 'data': {'children': ['uncaptured'], 'parent_id': 't1_parent'}})
        coverage = self.store.coverage(THREAD, at=200, freshness=50)
        self.assertEqual(coverage['status_unchecked_comments'], 1)
        self.assertEqual(coverage['missing_parent_ids'], ['t1_missing'])
        self.assertEqual(coverage['unresolved_ids'], ['t1_missing', 't1_uncaptured'])

    def test_cycles_do_not_hang_search(self):
        self.put([comment('first', parent_id='t1_second'), comment('second', parent_id='t1_first')])
        self.assertTrue(self.store.coverage(THREAD)['cycle_ids'])
        self.assertEqual(len(self.store.search(THREAD, 'saved')['entities']), 2)

    def test_other_thread_is_not_imported(self):
        self.put(comment(link_id='t3_other'))
        self.assertEqual(self.store.rows(THREAD), [])

    def test_raw_import_is_atomic_on_bad_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.jsonl'
            path.write_text(json.dumps(comment()) + '\nnot json\n')
            with self.assertRaises(ValueError):
                import_file(self.store, path, THREAD)
        self.assertEqual(self.store.rows(THREAD), [])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM captures').fetchone()[0], 0)

    def test_browser_import_requires_original_date(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reddit.json'
            path.write_text(json.dumps(comment()))
            with self.assertRaises(ValueError):
                import_file(self.store, path, THREAD, source='browser')

    def test_zip_roundtrip_keeps_text_status_and_history(self):
        self.put(comment(), at=100)
        self.put(comment(body='new words'), at=200)
        self.put(comment(body='[deleted]'), at=300)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'thread.zip'
            export_file(self.store, THREAD, path)
            other = Store(':memory:')
            try:
                import_file(other, path)
                entity = other.entities(THREAD)['t1_parent']
                self.assertEqual(entity['current']['state'], 'deleted')
                self.assertEqual(entity['readable']['body'], 'new words')
                self.assertEqual(len(entity['history']), 3)
            finally:
                other.close()

    def test_thread_links_are_constrained(self):
        self.assertEqual(thread_id('https://www.reddit.com/r/example/comments/1xnxfhv/title/'), THREAD)
        self.assertEqual(thread_id('https://redd.it/1xnxfhv'), THREAD)
        with self.assertRaises(ValueError):
            thread_id('http://localhost/comments/1xnxfhv')
