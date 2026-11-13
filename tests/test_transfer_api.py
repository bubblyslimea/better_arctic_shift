import contextlib
import gzip
import io
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from better_arctic_shift.cli import main
from better_arctic_shift.media import candidates
from better_arctic_shift.server import handler
from better_arctic_shift.store import Store
from better_arctic_shift.transfer import export_file, import_file, import_scrapbook, scrapbook


THREAD = '1xnxfhv'


class TransferTests(unittest.TestCase):
    def test_legacy_scrapbook_replays_raw_archive_metadata(self):
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                original, other = Store(':memory:'), Store(':memory:')
                try:
                    record = {'id': 'abc', 'body': 'old wording', 'retrieved_on': 100,
                              '_meta': {'retrieved_2nd_on': 300, 'is_edited': True}}
                    original.import_payload(THREAD, {'id': 'abc', 'body': 'new wording'}, 'reddit', 200)
                    original.import_payload(THREAD, record, 'arctic_shift', 600)
                    archive = scrapbook(original, THREAD)
                    for entity in archive['entities']:
                        for version in entity['versions']:
                            for observed in version['observations']:
                                observed.pop('content_time', None)
                    request = next(r for r in archive['requests'] if r['source'] == 'arctic_shift')
                    raw = json.dumps(record).encode()
                    if compressed:
                        raw = gzip.compress(raw)
                    import_scrapbook(other, archive, [(request, raw)])
                    self.assertEqual(other.entities(THREAD)['t1_abc']['readable']['body'], 'new wording')
                    earlier = next(r for r in other.rows(THREAD) if r['body'] == 'old wording')
                    self.assertEqual(earlier['observed_at'], 300)
                    self.assertEqual(earlier['content_at'], 100)
                    self.assertEqual(len(other.rows(THREAD)), 2)
                    import_scrapbook(other, archive, [(request, raw)])
                    self.assertEqual(len(other.rows(THREAD)), 2)
                    self.assertIn(raw, [r[0] for r in other.db.execute('SELECT body FROM captures')])
                finally:
                    original.close()
                    other.close()

    def test_imported_scrapbook_attachment_bytes_survive_export(self):
        store = Store(':memory:')
        other = Store(':memory:')
        try:
            store.import_payload(THREAD, {'id': 'abc', 'body': 'saved image'}, 'reddit', 100)
            payload = scrapbook(store, THREAD)
            asset = 'assets/' + 'a' * 64 + '.gif'
            raw = b'GIF89a saved attachment fixture'
            payload['media'] = [{'id': 'image', 'url': 'https://i.redd.it/example.gif', 'kind': 'image', 'role': 'original', 'label': 'image', 'entity_ids': ['t1_abc'], 'status': 'saved', 'path': asset}]
            response_path = 'raw/' + 'b' * 64 + '.json'
            source_bytes = b'{"data": [{"id":"abc", "body":"saved image"}]}'
            payload['requests'] = [{'id': 'capture', 'source': 'reddit', 'url': 'https://www.reddit.com/api/info.json', 'captured_at': '2026-11-13T10:53:13Z', 'status': 200, 'raw_path': response_path}]
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'original.zip'
                with zipfile.ZipFile(source, 'w') as archive:
                    archive.writestr('archive.json', json.dumps(payload))
                    archive.writestr(asset, raw)
                    archive.writestr(response_path, source_bytes)
                import_file(other, source)
                output = Path(directory) / 'roundtrip.zip'
                export_file(other, THREAD, output)
                with zipfile.ZipFile(output) as archive:
                    self.assertEqual(archive.read(asset), raw)
                    self.assertEqual(json.loads(archive.read('archive.json'))['media'][0]['status'], 'saved')
                    self.assertIn(source_bytes, [archive.read(name) for name in archive.namelist() if name.startswith('raw/')])
                import_file(other, output)
                self.assertEqual(len(scrapbook(other, THREAD)['media']), 1)
        finally:
            store.close()
            other.close()

    def test_native_zip_preserves_raw_captures_and_is_idempotent(self):
        original = Store(':memory:')
        other = Store(':memory:')
        try:
            record = {'id': '19s5dndo5wgqr', 'body': 'sample captured text', 'link_id': 't3_' + THREAD,
                      'retrieved_on': 1793842174, '_meta': {'retrieved_2nd_on': 1793870973}}
            original.import_payload(THREAD, record, 'arctic_shift', 1794560000)
            original.import_payload(THREAD, {**record, 'body': '[removed]', 'removed_by_category': 'moderator'}, 'user_report', 1794567193)
            run = original.start_run(THREAD)
            original.finish_run(run, 'partial', {'issues': [{'source': 'reddit', 'message': 'source returned HTTP 403'}]})
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'thread.zip'
                export_file(original, THREAD, path)
                import_file(other, path)
                import_file(other, path)
                import_file(original, path)
                self.assertEqual(original.rows(THREAD), other.rows(THREAD))
                self.assertEqual(len(other.rows(THREAD)), 2)
                self.assertEqual(scrapbook(original, THREAD)['runs'], scrapbook(other, THREAD)['runs'])
                self.assertEqual([r[0] for r in original.db.execute('SELECT body FROM captures ORDER BY id')], [r[0] for r in other.db.execute('SELECT body FROM captures ORDER BY id')])
                current = other.entities(THREAD)['t1_19s5dndo5wgqr']['current']
                self.assertEqual(current['state'], 'removed')
                self.assertEqual(current['source'], 'user_report')
        finally:
            original.close()
            other.close()

    def test_changed_zip_fails_without_importing(self):
        store = Store(':memory:')
        try:
            store.import_payload(THREAD, {'id': 'abc', 'body': 'text'}, 'reddit', 100)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'thread.zip'
                export_file(store, THREAD, path)
                with zipfile.ZipFile(path) as archive:
                    files = {name: archive.read(name) for name in archive.namelist()}
                files['history.jsonl'] = b'changed'
                with zipfile.ZipFile(path, 'w') as archive:
                    for name, data in files.items():
                        archive.writestr(name, data)
                other = Store(':memory:')
                with self.assertRaises(ValueError):
                    import_file(other, path)
                self.assertEqual(other.rows(THREAD), [])
                other.close()
        finally:
            store.close()

    def test_one_gif_across_metadata_preview_and_inline_forms(self):
        url = 'https://media.giphy.com/media/example123/giphy.gif'
        raw = {'body': f'![gif](giphy|example123)\n{url}', 'media_metadata': {'giphy|example123': {'s': {'gif': url, 'mp4': 'https://media.giphy.com/media/example123/giphy.mp4'}}}, 'preview': {'images': [{'source': {'url': url + '?width=100'}}]}}
        self.assertEqual(len(candidates(raw)), 1)
        store = Store(':memory:')
        try:
            store.import_payload(THREAD, {'id': 'abc', **raw}, 'reddit', 100)
            self.assertEqual(len(scrapbook(store, THREAD)['media']), 1)
        finally:
            store.close()

    def test_newer_status_does_not_make_old_text_a_new_edit(self):
        store = Store(':memory:')
        try:
            store.import_payload(THREAD, {'id': 'abc', 'body': 'new wording'}, 'reddit', 150)
            store.import_payload(THREAD, {'id': 'abc', 'body': 'old wording', 'retrieved_on': 100, '_meta': {'retrieved_2nd_on': 200, 'is_edited': True}}, 'arctic_shift', 1000)
            self.assertEqual(store.entities(THREAD)['t1_abc']['readable']['body'], 'new wording')
        finally:
            store.close()

    def test_browser_envelope_preserves_capture_date(self):
        store = Store(':memory:')
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'browser.json'
                path.write_text(json.dumps({'format': 'threadkeeper-browser-capture', 'thread_id': THREAD, 'responses': [{'captured_at': '2026-11-13T10:53:13Z', 'capture_method': 'visible_page', 'body': {'id': 'abc', 'body': '[deleted]'}}]}))
                import_file(store, path)
            row = store.entities(THREAD)['t1_abc']['current']
            self.assertEqual(row['source'], 'browser_page')
            self.assertEqual(row['observed_at'], 1794567193)
        finally:
            store.close()


class APITests(unittest.TestCase):
    def test_http_history_search_and_missing_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'history.sqlite3'
            store = Store(database)
            store.import_payload(THREAD, {'id': 'abc', 'body': '한글 sample text'}, 'reddit', 100)
            store.import_payload(THREAD, {'id': 'abc', 'body': '[deleted]'}, 'reddit', 200)
            store.close()
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(database))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base + f'/threads/{THREAD}/search?q=sample&removed=1') as response:
                    self.assertEqual(json.load(response)['matched_ids'], ['t1_abc'])
                with urlopen(base + f'/threads/{THREAD}/history/t1_abc') as response:
                    data = json.load(response)
                    self.assertEqual(data['readable']['body'], '한글 sample text')
                    self.assertEqual(data['current']['state'], 'deleted')
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + '/threads/missing')
                self.assertEqual(error.exception.code, 404)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + '/threads', data=b'{}', method='POST'))
                self.assertEqual(error.exception.code, 501)
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(base + '/threads', headers={'Host': 'unrelated.example'}))
                self.assertEqual(error.exception.code, 403)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_cli_import_search_export_flow(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            raw = root / 'records.jsonl'
            raw.write_text(json.dumps({'id': 'abc', 'body': 'saved words', 'retrieved_on': 100}) + '\n')
            prefix = ['--db', str(root / 'history.sqlite3')]
            self.assertEqual(main(prefix + ['import', str(raw), '--thread', THREAD]), 0)
            self.assertEqual(main(prefix + ['search', THREAD, 'saved']), 0)
            self.assertEqual(main(prefix + ['export', THREAD, str(root / 'archive.zip')]), 0)
            self.assertTrue((root / 'archive.zip').is_file())
