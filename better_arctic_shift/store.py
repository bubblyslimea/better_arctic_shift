"""keep raw captures and dated observations in one durable notebook"""

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .model import LIVE, REMOVED, canonical, current, digest, fullname, observations, rank, thread_id, walk


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError("this database was made by a newer version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS captures(
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, source TEXT NOT NULL, url TEXT NOT NULL,
                captured REAL NOT NULL, status INTEGER, error TEXT, body BLOB, run_id TEXT);
            CREATE INDEX IF NOT EXISTS capture_run ON captures(run_id, url);
            CREATE TABLE IF NOT EXISTS observations(
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, entity TEXT NOT NULL,
                captured_id TEXT REFERENCES captures(id), payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS observation_thread ON observations(thread, entity);
            CREATE TABLE IF NOT EXISTS hints(
                thread TEXT NOT NULL, entity TEXT NOT NULL, parent TEXT,
                PRIMARY KEY(thread, entity));
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, thread TEXT NOT NULL, started REAL NOT NULL,
                finished REAL, state TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS watches(
                thread TEXT PRIMARY KEY, interval REAL NOT NULL, next_due REAL NOT NULL,
                lease_until REAL, token TEXT, failures INTEGER NOT NULL DEFAULT 0, run_id TEXT);
            CREATE TABLE IF NOT EXISTS cooldowns(host TEXT PRIMARY KEY, until_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attachments(
                thread TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, body BLOB,
                PRIMARY KEY(thread, id));
            PRAGMA user_version=1;
        """)

    def close(self):
        self.db.close()

    def capture(self, thread, source, url, captured, body, status=200, error=None, run_id=None):
        cid = digest([thread, source, url, captured, digest(body or b""), status, error, run_id])
        self.db.execute("INSERT OR IGNORE INTO captures VALUES(?,?,?,?,?,?,?,?,?)",
                        (cid, thread, source, url, captured, status, error, body, run_id))
        return cid

    def ingest(self, thread, payload, source, captured, capture_id=None):
        thread = thread_id(thread)
        ids, more = set(), []
        for kind, record in walk(payload):
            if kind == "more":
                more.append(record)
                for child in record.get("children") or []:
                    self.db.execute("INSERT OR IGNORE INTO hints VALUES(?,?,?)",
                                    (thread, fullname(child), record.get("parent_id")))
                continue
            for row in observations(record, kind, thread, source, captured):
                self.db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                                (row["id"], thread, row["entity_id"], capture_id, canonical(row)))
                ids.add(row["entity_id"])
                if row["parent_id"] and row["parent_id"].startswith("t1_"):
                    self.db.execute("INSERT OR IGNORE INTO hints VALUES(?,?,NULL)", (thread, row["parent_id"]))
        return ids, more

    def import_payload(self, thread, payload, source, captured, url="import"):
        thread = thread_id(thread)
        with self.db:
            cid = self.capture(thread, source, url, captured, canonical(payload).encode())
            return self.ingest(thread, payload, source, captured, cid)

    def cached(self, run_id, url):
        row = self.db.execute("SELECT * FROM captures WHERE run_id=? AND url=? AND status=200 AND error IS NULL ORDER BY captured DESC LIMIT 1", (run_id, url)).fetchone()
        return dict(row) if row else None

    def rows(self, thread):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM observations WHERE thread=?", (thread_id(thread),))]

    def entities(self, thread):
        groups = {}
        for row in self.rows(thread):
            groups.setdefault(row["entity_id"], []).append(row)
        result = {}
        for rid, rows in groups.items():
            latest = current(rows)
            readable = [row for row in rows if row["state"] == "present"]
            relationship = max(rows, key=rank)
            result[rid] = {"id": rid, "kind": latest["kind"], "parent_id": relationship["parent_id"],
                           "created_utc": latest["created_utc"], "current": latest,
                           "readable": max(readable, key=lambda row: rank({**row, "observed_at": row.get("content_at", row["observed_at"])})) if readable else latest,
                           "history": sorted(rows, key=rank)}
        return result

    def unresolved(self, thread):
        return [r[0] for r in self.db.execute("""
            SELECT entity FROM hints WHERE thread=? AND entity NOT IN
            (SELECT entity FROM observations WHERE thread=?) ORDER BY entity
        """, (thread_id(thread), thread_id(thread)))]

    def coverage(self, thread, at=None, freshness=86400):
        at = time.time() if at is None else at
        entities = self.entities(thread)
        comments = [e for e in entities.values() if e["kind"] == "t1"]
        checked = [e for e in comments if any(row["source"] in LIVE and row["state"] != "unknown" and row["observed_at"] is not None and at - freshness <= row["observed_at"] <= at for row in e["history"])]
        post = entities.get("t3_" + thread_id(thread))
        counts = [row for row in post["history"] if isinstance(row["raw"].get("num_comments"), int)] if post else []
        reported = max(counts, key=rank)["raw"]["num_comments"] if counts else None
        missing_parents = sorted({e["parent_id"] for e in comments if e["parent_id"] and e["parent_id"] not in entities})
        cycles = set()
        for rid in entities:
            path = set()
            cursor = rid
            while cursor in entities:
                if cursor in path:
                    cycles.update(path)
                    break
                path.add(cursor)
                cursor = entities[cursor]["parent_id"]
        return {"comments": len(comments), "reported_comments": reported,
                "comment_gap": max(0, reported - len(comments)) if reported is not None else None,
                "removed_comments": sum(e["current"]["state"] in REMOVED for e in comments),
                "status_checked_comments": len(checked), "status_unchecked_comments": len(comments) - len(checked),
                "unknown_status_comments": sum(e["current"]["state"] == "unknown" for e in comments),
                "missing_parent_ids": missing_parents, "unresolved_ids": self.unresolved(thread),
                "cycle_ids": sorted(cycles), "freshness_seconds": freshness,
                "observations": sum(len(e["history"]) for e in entities.values())}

    def search(self, thread, text="", removed=False):
        entities = self.entities(thread)
        needle = text.casefold()
        matched = {rid for rid, e in entities.items() if (not removed or e["current"]["state"] in REMOVED)
                   and any(needle in (r["title"] + "\n" + r["body"]).casefold() for r in e["history"])}
        included = set(matched)
        children = {}
        for rid, e in entities.items():
            children.setdefault(e["parent_id"], []).append(rid)
        pending = list(matched)
        while pending:
            for child in children.get(pending.pop(), []):
                if child not in included:
                    included.add(child)
                    pending.append(child)
        for rid in matched:
            parent = entities[rid]["parent_id"]
            seen = {rid}
            while parent and parent not in seen:
                included.add(parent)
                seen.add(parent)
                parent = entities.get(parent, {}).get("parent_id")
        return {"matched_ids": sorted(matched), "entities": [entities[rid] if rid in entities else
                {"id": rid, "placeholder": True, "parent_id": None} for rid in sorted(included)]}

    def start_run(self, thread):
        rid = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO runs(id,thread,started,state) VALUES(?,?,?,?)", (rid, thread_id(thread), time.time(), "running"))
        return rid

    def finish_run(self, rid, state, details):
        with self.db:
            self.db.execute("UPDATE runs SET finished=?,state=?,details=? WHERE id=?", (time.time(), state, canonical(details), rid))

    def watch(self, thread, interval, at=None):
        if not 60 <= interval <= 31536000:
            raise ValueError("choose a revisit interval between one minute and one year")
        with self.db:
            self.db.execute("""INSERT INTO watches(thread,interval,next_due) VALUES(?,?,?)
                ON CONFLICT(thread) DO UPDATE SET interval=excluded.interval""",
                (thread_id(thread), interval, time.time() if at is None else at))

    def claim(self, at=None):
        at = time.time() if at is None else at
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM watches WHERE next_due<=? AND (lease_until IS NULL OR lease_until<=?) ORDER BY next_due LIMIT 1", (at, at)).fetchone()
            if not row:
                return None
            job = dict(row)
            job["token"] = uuid.uuid4().hex
            job["run_id"] = job["run_id"] or uuid.uuid4().hex
            self.db.execute("INSERT OR IGNORE INTO runs(id,thread,started,state) VALUES(?,?,?,?)", (job["run_id"], job["thread"], at, "running"))
            self.db.execute("UPDATE watches SET lease_until=?,token=?,run_id=? WHERE thread=?", (at + 120, job["token"], job["run_id"], job["thread"]))
            return job

    def heartbeat(self, job):
        with self.db:
            count = self.db.execute("UPDATE watches SET lease_until=? WHERE thread=? AND token=?", (time.time() + 120, job["thread"], job["token"])).rowcount
        if count != 1:
            raise RuntimeError("another worker has resumed this thread")

    def release(self, job, success, retry_after=0, at=None):
        at = time.time() if at is None else at
        failures = 0 if success else job["failures"] + 1
        wait = job["interval"] if success else max(retry_after, min(job["interval"], 60 * 2 ** min(failures, 8)))
        with self.db:
            self.db.execute("UPDATE watches SET next_due=?,lease_until=NULL,token=NULL,failures=?,run_id=NULL WHERE thread=? AND token=?",
                            (at + wait, failures, job["thread"], job["token"]))

    def pause(self, job):
        with self.db:
            self.db.execute("UPDATE watches SET lease_until=NULL,token=NULL WHERE thread=? AND token=?", (job["thread"], job["token"]))
