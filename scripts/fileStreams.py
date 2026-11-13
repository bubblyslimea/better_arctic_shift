"""read archive records without quietly changing or dropping their text"""

import json
from pathlib import Path
from typing import BinaryIO, Iterator

import zstandard


class ArchiveReadError(ValueError):
    pass


def _record(line: bytes, number: int) -> dict:
    try:
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value
    except (UnicodeDecodeError, ValueError) as error:
        raise ArchiveReadError(f"couldn't read record {number}: {error}") from error


def _lines(chunks: Iterator[bytes]) -> Iterator[dict]:
    pending = bytearray()
    number = 0
    for chunk in chunks:
        start = 0
        while True:
            end = chunk.find(b"\n", start)
            if end < 0:
                pending.extend(chunk[start:])
                break
            pending.extend(chunk[start:end])
            number += 1
            if pending.strip():
                yield _record(bytes(pending), number)
            pending.clear()
            start = end + 1
    if pending.strip():
        yield _record(bytes(pending), number + 1)


def _zst_chunks(f: BinaryIO, chunk_size: int) -> Iterator[bytes]:
    if chunk_size < 1:
        raise ValueError("chunk size must be positive")
    decoder = None
    seen = False
    while block := f.read(min(chunk_size, 65536)):
        seen = True
        while block:
            if decoder is None:
                decoder = zstandard.ZstdDecompressor(max_window_size=2**31).decompressobj()
            try:
                decoded = decoder.decompress(block)
            except zstandard.ZstdError as error:
                raise ArchiveReadError("couldn't decompress the archive") from error
            for start in range(0, len(decoded), chunk_size):
                yield decoded[start:start + chunk_size]
            block = decoder.unused_data if decoder.eof else b""
            if decoder.eof:
                decoder = None
    if decoder is not None or not seen:
        raise ArchiveReadError("the compressed archive is empty or unfinished")


def getZstFileJsonStream(f: BinaryIO, chunk_size=1024 * 1024 * 10) -> Iterator[dict]:
    # keep bytes together until a whole line is ready, including split UTF-8 characters
    yield from _lines(_zst_chunks(f, chunk_size))


def getJsonLinesFileJsonStream(f: BinaryIO) -> Iterator[dict]:
    yield from _lines(iter(lambda: f.read(65536), b""))


def getZstBlocksFileJsonStream(f: BinaryIO) -> Iterator[dict]:
    try:
        from .zst_blocks_format.python_cli.ZstBlocksFile import ZstBlocksFile
    except ImportError:
        try:
            from zst_blocks_format.python_cli.ZstBlocksFile import ZstBlocksFile
        except ImportError as error:
            raise ArchiveReadError("the legacy reader is missing; run git submodule update --init --recursive") from error
    for number, row in enumerate(ZstBlocksFile.streamRows(f), 1):
        if row.strip():
            yield _record(row, number)


def getJsonFileStream(f: BinaryIO) -> Iterator[dict]:
    try:
        data = json.loads(f.read().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ArchiveReadError("couldn't read the JSON file") from error
    rows = data if isinstance(data, list) else [data]
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ArchiveReadError(f"record {number} isn't a JSON object")
        yield row


def getFileJsonStream(path: str, f: BinaryIO) -> Iterator[dict] | None:
    readers = {".jsonl": getJsonLinesFileJsonStream, ".ndjson": getJsonLinesFileJsonStream,
               ".zst": getZstFileJsonStream, ".zst_blocks": getZstBlocksFileJsonStream,
               ".json": getJsonFileStream}
    reader = readers.get(Path(path).suffix.lower())
    return reader(f) if reader else None
