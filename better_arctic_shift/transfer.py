"""bring captures in, and send thread history back to the scrapbook"""

import html
import gzip
import json
import re
import time
import zipfile
from pathlib import Path

from scripts.fileStreams import getFileJsonStream
from .model import LIVE, SOURCES, canonical, digest, fullname, iso, thread_id, timestamp
from .media import candidates


def bundled_attachments(bundle, payload):
    result = []
    for item in payload.get('media', []):
        item = dict(item)
        raw = None
        if item.get('path') and item.get('status') == 'saved':
            if not re.fullmatch(r'assets/[a-f0-9]+\.[a-z0-9.]+', item['path']):
                raise ValueError('an attachment has an unsupported archive path')
            raw = bundle.read(item['path'])
        result.append((item, raw))
    return result


def keep_attachments(store, thread, attachments):
    for item, raw in attachments:
        existing = store.db.execute('SELECT payload,body FROM attachments WHERE thread=? AND id=?', (thread, item['id'])).fetchone()
        if existing:
            previous = json.loads(existing['payload'])
            ids = sorted(set(previous.get('entity_ids', [])) | set(item.get('entity_ids', [])))
            if raw is None and existing['body'] is not None:
                item, raw = previous, existing['body']
            item['entity_ids'] = ids
        store.db.execute('INSERT INTO attachments VALUES(?,?,?,?) ON CONFLICT(thread,id) DO UPDATE SET payload=excluded.payload,body=excluded.body',
                         (thread, item['id'], canonical(item), raw))


def raw_path(body):
    extension = '.json.gz' if body.startswith(b'\x1f\x8b') else '.json'
    return 'raw/' + digest(body) + extension


def keep_runs(store, thread, payload):
    for run in payload.get('runs', []):
        started = timestamp(run.get('started_at'))
        if started is None:
            raise ValueError('a saved run is missing its start date')
        finished = timestamp(run.get('finished_at'))
        existing = store.db.execute('SELECT thread FROM runs WHERE id=?', (run['id'],)).fetchone()
        if existing and existing['thread'] != thread:
            raise ValueError('a saved run belongs to another thread')
        details = {key: value for key, value in run.items() if key not in {'id', 'started_at', 'finished_at', 'status'}}
        store.db.execute('''INSERT INTO runs VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            finished=excluded.finished,state=excluded.state,details=excluded.details
            WHERE coalesce(excluded.finished,excluded.started)>=coalesce(runs.finished,runs.started)''',
            (run['id'], thread, started, finished, run.get('status', 'imported'), canonical(details)))


def import_scrapbook(store, archive, captures=(), attachments=()):
    if archive.get("format") != "threadkeeper" or archive.get("schema_version") != 1:
        raise ValueError("this isn't a supported scrapbook archive")
    thread = thread_id(archive["thread_id"])
    with store.db:
        cid = store.capture(thread, "import", "scrapbook import", time.time(), canonical(archive).encode())
        capture_ids = {}
        normalized = {}
        for request, raw in captures:
            capture_ids[request['id']] = store.capture(thread, request['source'], request['url'],
                timestamp(request['captured_at']), raw, request.get('status'), request.get('error'))
            if request['source'] == 'arctic_shift' and raw:
                try:
                    payload = json.loads(gzip.decompress(raw) if raw.startswith(b'\x1f\x8b') else raw)
                except (ValueError, OSError, EOFError):
                    continue
                normalized[request['id']], _ = store.ingest(thread, payload, 'arctic_shift', timestamp(request['captured_at']), capture_ids[request['id']])
        for entity in archive["entities"]:
            if entity.get("placeholder"):
                continue
            rid = fullname(entity["id"])
            kind = rid[:2]
            if kind == "t3" and rid != "t3_" + thread:
                raise ValueError("this archive contains a different thread")
            for version in entity["versions"]:
                for observed in version["observations"]:
                    source = observed["source"]
                    if source == 'arctic_shift' and rid in normalized.get(observed.get('request_id'), set()):
                        continue
                    if source not in SOURCES:
                        raise ValueError("this archive contains an unsupported source")
                    received = timestamp(observed["observed_at"])
                    if received is None:
                        raise ValueError("an observation is missing its capture date")
                    at = received if source in LIVE | {"user_report"} else timestamp(observed.get("source_time"))
                    state = version["state"]
                    state = "unknown" if state == "empty" else state
                    if state not in {"present", "deleted", "removed", "unknown"} or not isinstance(version["body"], str):
                        raise ValueError("this archive contains an invalid observation")
                    raw = {**{k: v for k, v in version.items() if k not in {"observations", "id", "state", "first_seen", "last_seen"}},
                           "id": rid[3:], "parent_id": entity.get("parent_id"), "link_id": "t3_" + thread,
                           "score": observed.get("score"), "num_comments": observed.get("num_comments"), "edited": observed.get("edited")}
                    if kind == "t3":
                        raw["selftext"] = raw.pop("body", "")
                    row = {"entity_id": rid, "kind": kind, "thread_id": thread,
                           "parent_id": fullname(entity["parent_id"]) if entity.get("parent_id") else None,
                           "created_utc": entity.get("created_utc") or 0, "source": source,
                           "received_at": received, "observed_at": at, "body": version["body"],
                           "content_at": timestamp(observed.get("content_time")) if "content_time" in observed else at,
                           "title": version.get("title") or "", "author": version.get("author") or "[unknown]",
                           "raw": raw, "removal": observed.get("removal"), "state": state,
                           "archive_edit_flag": bool(observed.get("archive_edit_flag"))}
                    row["id"] = digest({k: v for k, v in row.items() if k != "received_at"})
                    store.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)", (row["id"], thread, rid, capture_ids.get(observed.get('request_id'), cid), canonical(row)))
                    if row["parent_id"] and row["parent_id"].startswith("t1_"):
                        store.db.execute("INSERT OR IGNORE INTO hints VALUES(?,?,NULL)", (thread, row["parent_id"]))
        for hint in archive.get("coverage", {}).get("unresolved_ids", []):
            store.db.execute("INSERT OR IGNORE INTO hints VALUES(?,?,NULL)", (thread, fullname(hint)))
        keep_attachments(store, thread, attachments)
        keep_runs(store, thread, archive)
    return thread


def import_file(store, path, thread=None, source="arctic_shift", observed_at=None):
    path = Path(path)
    captured = timestamp(observed_at) if observed_at is not None else time.time()
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            if sum(i.file_size for i in archive.infolist()) > 512 * 1024 * 1024:
                raise ValueError("this ZIP is too large to import here")
            payload = json.loads(archive.read("archive.json").decode("utf-8"))
            manifest = json.loads(archive.read("manifest.json")) if "manifest.json" in archive.namelist() else {}
            for name, entry in manifest.get("files", {}).items():
                if digest(archive.read(name)) != entry["sha256"]:
                    raise ValueError("a file in this ZIP doesn't match its checksum")
            thread = thread_id(payload["thread_id"])
            attachments = bundled_attachments(archive, payload)
            if manifest.get("format") == "better-arctic-shift" and manifest.get("schema_version") == 1:
                if set(archive.namelist()) - {'manifest.json'} != set(manifest.get('files', {})) or len(archive.namelist()) != len(set(archive.namelist())):
                    raise ValueError("the ZIP manifest doesn't cover its files")
                with store.db:
                    capture_ids = set()
                    for request in payload["requests"]:
                        raw = archive.read(request["raw_path"]) if request.get("raw_path") else None
                        store.db.execute("INSERT OR IGNORE INTO captures VALUES(?,?,?,?,?,?,?,?,?)",
                                         (request['id'], thread, request['source'], request['url'], timestamp(request['captured_at']), request.get('status'), request.get('error'), raw, request.get('run_id')))
                        capture_ids.add(request['id'])
                    for row in (json.loads(line) for line in archive.read("history.jsonl").decode("utf-8").splitlines() if line.strip()):
                        capture_id = row.pop('capture_id', None)
                        if capture_id and capture_id not in capture_ids:
                            raise ValueError("a saved observation is missing its source capture")
                        expected = digest({k: v for k, v in row.items() if k not in {"received_at", "id"}})
                        if row["id"] != expected or row["thread_id"] != thread or row["source"] not in SOURCES or row["state"] not in {"present", "deleted", "removed", "unknown"}:
                            raise ValueError("a saved observation doesn't match its history")
                        fullname(row["entity_id"])
                        if row.get('kind') != row['entity_id'][:2] or not isinstance(row.get('raw'), dict) or not all(isinstance(row.get(key), str) for key in ('body', 'title', 'author')):
                            raise ValueError("a saved observation has invalid fields")
                        if row.get('parent_id'):
                            fullname(row['parent_id'])
                        timestamp(row["observed_at"])
                        timestamp(row["received_at"])
                        if row['received_at'] is None:
                            raise ValueError("a saved observation is missing its capture date")
                        store.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)", (row["id"], thread, row["entity_id"], capture_id, canonical(row)))
                    for hint in payload.get("coverage", {}).get("unresolved_ids", []):
                        store.db.execute("INSERT OR IGNORE INTO hints VALUES(?,?,NULL)", (thread, fullname(hint)))
                    keep_attachments(store, thread, attachments)
                    keep_runs(store, thread, payload)
                return thread
            # validate bundled bytes before changing the imported history
            captures = []
            for request in payload.get('requests', []):
                if request.get('raw_path'):
                    if not re.fullmatch(r'raw/[a-f0-9]+\.[a-z0-9.]+', request['raw_path']):
                        raise ValueError('a source response has an unsupported archive path')
                    captures.append((request, archive.read(request['raw_path'])))
            return import_scrapbook(store, payload, captures, attachments)
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("format") == "threadkeeper":
            return import_scrapbook(store, payload)
        if isinstance(payload, dict) and payload.get("format") == "threadkeeper-browser-capture":
            thread = thread_id(payload["thread_id"])
            with store.db:
                for response in payload["responses"]:
                    captured = timestamp(response["captured_at"])
                    if captured is None:
                        raise ValueError("the browser capture is missing its date")
                    source = "browser_page" if response.get("capture_method") == "visible_page" else "browser"
                    raw = canonical(response["body"]).encode()
                    cid = store.capture(thread, source, response.get("url", "browser import"), captured, raw)
                    store.ingest(thread, response["body"], source, captured, cid)
                for hint in payload.get("unresolved_ids", []):
                    store.db.execute("INSERT OR IGNORE INTO hints VALUES(?,?,NULL)", (thread, fullname(hint)))
            return thread
    if thread is None:
        raise ValueError("choose a thread with --thread for raw source files")
    thread = thread_id(thread)
    if source in LIVE | {"user_report"} and observed_at is None:
        raise ValueError("include --observed-at so an old capture isn't dated as new")
    with path.open("rb") as stream, store.db:
        rows = getFileJsonStream(str(path), stream)
        if rows is None:
            raise ValueError("this archive file type isn't supported")
        cid = store.capture(thread, source, "record import", captured, None)
        for row in rows:
            store.ingest(thread, row, source, captured, cid)
    return thread


def scrapbook(store, value):
    thread = thread_id(value)
    entities = store.entities(thread)
    now = iso(time.time())
    output = {"format": "threadkeeper", "schema_version": 1, "thread_id": thread,
              "canonical_url": f"https://www.reddit.com/comments/{thread}/", "created_at": now,
              "exported_at": now, "entities": [], "requests": [], "runs": [], "media": [], "links": [],
              "coverage": store.coverage(thread)}
    media = {}
    capture_ids = dict(store.db.execute('SELECT id,captured_id FROM observations WHERE thread=?', (thread,)))
    for entity in entities.values():
        versions = {}
        for row in entity["history"]:
            raw = row["raw"]
            for item in candidates(raw):
                attachment = media.setdefault(item["id"], item)
                if entity["id"] not in attachment["entity_ids"]:
                    attachment["entity_ids"].append(entity["id"])
            content = {"body": row["body"], "title": row["title"], "author": row["author"], "state": 'empty' if row["state"] == 'unknown' else row["state"],
                       "flair": raw.get('flair', raw.get('author_flair_text' if row['kind'] == 't1' else 'link_flair_text')),
                       "locked": bool(raw.get('locked')), "stickied": bool(raw.get('stickied')), "spoiler": bool(raw.get('spoiler')),
                       "nsfw": bool(raw.get('over_18', raw.get('nsfw'))), "distinguished": raw.get('distinguished'), "is_submitter": bool(raw.get('is_submitter')),
                       "subreddit": raw.get("subreddit") or "", "permalink": raw.get("permalink") or "",
                       "url": raw.get("url_overridden_by_dest") or raw.get("url") or "",
                       "media_metadata": raw.get("media_metadata") or {}, "gallery_data": raw.get("gallery_data") or {},
                       "media": raw.get("secure_media") or raw.get("media") or {}, "preview": raw.get("preview") or {}}
            vid = digest(content)
            received = iso(row["received_at"])
            v = versions.setdefault(vid, {**content, "id": vid, "first_seen": received, "last_seen": received, "observations": []})
            v["first_seen"] = min(v["first_seen"], received)
            v["last_seen"] = max(v["last_seen"], received)
            o = {"source": row["source"], "observed_at": iso(row["observed_at"]) if row["source"] in LIVE | {"user_report"} else received,
                 "source_time": iso(row["observed_at"]), "content_time": iso(row.get("content_at", row["observed_at"])), "request_id": capture_ids.get(row['id']), "score": raw.get("score"),
                 "num_comments": raw.get("num_comments"), "upvote_ratio": raw.get("upvote_ratio"),
                 "archive_edit_flag": row["archive_edit_flag"], "edited": raw.get('edited')}
            if row["removal"]:
                o["removal"] = row["removal"]
            v["observations"].append(o)
        output["entities"].append({"id": entity["id"], "kind": entity["kind"], "parent_id": entity["parent_id"],
                                   "created_utc": entity["created_utc"], "versions": list(versions.values())})
    for saved in store.db.execute('SELECT payload,body FROM attachments WHERE thread=?', (thread,)):
        item = json.loads(saved['payload'])
        matching = next((key for key, candidate in media.items() if candidate['url'] == item.get('url')), None)
        if matching:
            item['entity_ids'] = sorted(set(item.get('entity_ids', [])) | set(media[matching]['entity_ids']))
            del media[matching]
        media[item['id']] = item
    output["media"] = list(media.values())
    for row in store.db.execute("SELECT * FROM captures WHERE thread=? ORDER BY captured", (thread,)):
        r = {"id": row["id"], "source": row["source"], "url": row["url"], "captured_at": iso(row["captured"]),
             "status": row["status"], "error": row["error"], "run_id": row['run_id']}
        if row["body"] is not None:
            r["raw_path"] = raw_path(row['body'])
        output["requests"].append(r)
    for row in store.db.execute("SELECT * FROM runs WHERE thread=? ORDER BY started", (thread,)):
        output["runs"].append({"id": row["id"], "started_at": iso(row["started"]), "finished_at": iso(row["finished"]),
                               "status": row["state"], **json.loads(row["details"])})
    return output


def export_file(store, thread, path):
    archive = scrapbook(store, thread)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() != ".zip":
        path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    files = {"archive.json": canonical(archive).encode(), "coverage.json": canonical(archive["coverage"]).encode(),
             "history.jsonl": ("\n".join(canonical({**json.loads(row['payload']), 'capture_id': row['captured_id']}) for row in store.db.execute('SELECT payload,captured_id FROM observations WHERE thread=?', (thread_id(thread),))) + "\n").encode()}
    for row in store.db.execute("SELECT body FROM captures WHERE thread=? AND body IS NOT NULL", (thread_id(thread),)):
        files[raw_path(row[0])] = row[0]
    for row in store.db.execute('SELECT payload,body FROM attachments WHERE thread=? AND body IS NOT NULL', (thread_id(thread),)):
        item = json.loads(row['payload'])
        if item.get('path'):
            files[item['path']] = row['body']
    parts = []
    for entity in store.entities(thread).values():
        row = entity["readable"]
        status = entity["current"]
        actor = (status["removal"] or {}).get("actor", "unknown")
        label = {"user": "deleted by user", "mod": "removed by moderator", "reddit": "removed by Reddit"}.get(actor, "removed, reason unknown")
        notice = f'<p class="removed">[{label}]</p>' if status["state"] in {"deleted", "removed"} else ""
        parts.append(f'<article id="{entity["id"]}"><small>{html.escape(row["author"])} · {html.escape(entity["id"])}</small>'
                     f'<h2>{html.escape(row["title"])}</h2>{notice}<div class="body">{html.escape(row["body"])}</div></article>')
    files["index.html"] = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<title>riri’s thread history</title><style>body{font:18px/1.6 system-ui;max-width:850px;margin:3rem auto;padding:1rem;background:#fafaf6;color:#273126}article{padding:1.5rem 0;border-bottom:1px solid #ddd}.body{white-space:pre-wrap;overflow-wrap:anywhere}.removed{color:#a32b35}small{color:#66705d}</style>'
        '<h1>riri’s thread history</h1><p>saved text with the latest observed status · open archive.json in the scrapbook for the full conversation tree</p>' + ''.join(parts)).encode()
    files["README.txt"] = b"open index.html to read the saved text\nimport this ZIP into riri's reddit scrapbook for search, history, and conversation context\nraw source responses, record history, and imported attachments are included; this collector does not download new media bytes\n"
    manifest = {"format": "better-arctic-shift", "schema_version": 1,
                "files": {name: {"sha256": digest(data), "bytes": len(data)} for name, data in files.items()}}
    files["manifest.json"] = canonical(manifest).encode()
    temporary = path.with_name(path.name + ".partial")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, data in files.items():
                bundle.writestr(name, data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
