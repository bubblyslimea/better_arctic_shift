import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import zstandard

from scripts.fileStreams import ArchiveReadError, getFileJsonStream, getJsonLinesFileJsonStream, getZstFileJsonStream
from scripts import processFiles


class ReaderTests(unittest.TestCase):
    def test_multilingual_text_survives_every_boundary(self):
        expected = [{"body": "한글🙂 café 東京"}, {"body": "second line"}]
        data = b"\r\n".join(json.dumps(r, ensure_ascii=False).encode() for r in expected)
        packed = zstandard.ZstdCompressor().compress(data)
        for size in range(1, 65):
            with self.subTest(size=size):
                self.assertEqual(expected, list(getZstFileJsonStream(io.BytesIO(packed), size)))

    def test_default_ten_mib_boundary(self):
        size = 10 * 1024 * 1024
        prefix = b'{"body":"'
        data = prefix + b'x' * (size - len(prefix) - 1) + '한글🙂'.encode() + b'"}\n'
        actual = list(getZstFileJsonStream(io.BytesIO(zstandard.ZstdCompressor().compress(data))))
        self.assertEqual(actual, [json.loads(data)])

    def test_concatenated_frames_and_no_final_newline(self):
        compress = zstandard.ZstdCompressor().compress
        packed = compress(b'{"id":"a"}\n') + compress(b'{"id":"b"}')
        self.assertEqual([r["id"] for r in getZstFileJsonStream(io.BytesIO(packed), 2)], ["a", "b"])

    def test_truncated_or_corrupt_frames_fail(self):
        packed = zstandard.ZstdCompressor().compress(b'{"id":"a"}\n')
        for data in (packed[:-1], packed + b'broken', b'', b'broken'):
            with self.subTest(data=data), self.assertRaises(ArchiveReadError):
                list(getZstFileJsonStream(io.BytesIO(data)))

    def test_invalid_utf8_and_malformed_rows_fail(self):
        for data in (b'{"body":"\xff"}\n', b'{"id":"a"}\nnot json\n', b'[1,2]\n'):
            with self.assertRaises(ArchiveReadError):
                list(getJsonLinesFileJsonStream(io.BytesIO(data)))

    def test_empty_files_and_blank_lines(self):
        self.assertEqual(list(getJsonLinesFileJsonStream(io.BytesIO(b'\n\r\n'))), [])
        self.assertEqual(list(getZstFileJsonStream(io.BytesIO(zstandard.ZstdCompressor().compress(b'')))), [])
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            (root / 'empty.jsonl').touch()
            (root / 'nested').mkdir()
            (root / 'one.jsonl').write_text('{"id":"a"}\n', encoding='utf-8')
            (root / 'nested' / 'two.jsonl').write_text('{"id":"b"}\n', encoding='utf-8')
            processFiles.recursive = False
            self.assertEqual(processFiles.processFolder(directory), 1)
            processFiles.recursive = True
            try:
                self.assertEqual(processFiles.processFolder(directory), 2)
            finally:
                processFiles.recursive = False

    def test_json_shapes_and_uppercase_extension(self):
        self.assertEqual(list(getFileJsonStream('ONE.JSON', io.BytesIO(b'{"id":"a"}'))), [{"id": "a"}])
        self.assertEqual(list(getFileJsonStream('one.json', io.BytesIO(b'[{"id":"a"}]'))), [{"id": "a"}])
        with self.assertRaises(ArchiveReadError):
            list(getFileJsonStream('one.json', io.BytesIO(b'["a"]')))

    def test_legacy_blocks_still_read(self):
        try:
            from scripts.zst_blocks_format.python_cli.ZstBlocksFile import ZstBlocksFile
        except ImportError:
            self.skipTest('legacy reader submodule is not installed')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.zst_blocks'
            with path.open('wb') as stream:
                ZstBlocksFile.writeStream(stream, [json.dumps({'body': '한글🙂'}, ensure_ascii=False).encode()], 10)
            with path.open('rb') as stream:
                self.assertEqual(list(getFileJsonStream(str(path), stream)), [{'body': '한글🙂'}])
