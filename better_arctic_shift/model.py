"""turn source records into dated observations without guessing missing events"""

import hashlib
import json
import re
from urllib.parse import urlparse
from .history import LIVE, timestamp, iso, removal, capture_states


SOURCES = LIVE | {"arctic_shift", "user_report", "pullpush", "wayback"}
REMOVED = {"deleted", "removed"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def thread_id(value):
    value = value.strip().lower()
    if re.fullmatch(r"(?:t3_)?[a-z0-9]{3,16}", value):
        return value.removeprefix("t3_")
    url = urlparse(value if "://" in value else "https://" + value)
    hosts = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "m.reddit.com", "sh.reddit.com", "redd.it"}
    if url.scheme not in {"http", "https"} or url.hostname not in hosts or url.username or url.password:
        raise ValueError("paste a Reddit thread link or thread id")
    pattern = r"^/([a-z0-9]+)/?$" if url.hostname == "redd.it" else r"/comments/([a-z0-9]+)(?:/|\.json|$)"
    match = re.search(pattern, url.path)
    if not match:
        raise ValueError("copy the thread's full comments link")
    return thread_id(match[1])


def fullname(value, kind="t1"):
    value = str(value or "").lower()
    value = value if value.startswith(("t1_", "t3_")) else kind + "_" + value
    if not re.fullmatch(r"t[13]_[a-z0-9]{1,16}", value):
        raise ValueError("the record id isn't valid")
    return value


def walk(payload):
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
        elif isinstance(node, dict):
            kind = node.get("kind")
            if kind in {"t1", "t3", "more"} and isinstance(node.get("data"), dict):
                yield kind, node["data"]
                if kind == "t1":
                    stack.append(node["data"].get("replies"))
            elif kind == "Listing":
                stack.append(node.get("data", {}).get("children", []))
            elif "json" in node:
                data = node["json"]
                if data.get("errors"):
                    raise ValueError("Reddit returned an API error")
                stack.append(data.get("data", {}).get("things", []))
            elif node.get("id") and ("body" in node or "title" in node):
                yield "t1" if "body" in node else "t3", node
                stack.append(node.get("replies"))
            elif "data" in node:
                stack.append(node["data"])
            elif "children" in node:
                stack.append(node["children"])


def observations(record, kind, thread, source, captured):
    if source not in SOURCES:
        raise ValueError("choose a supported observation source")
    rid = fullname(record.get("id"), kind)
    if not rid.startswith(kind + "_"):
        raise ValueError("record kind and id disagree")
    root = "t3_" + thread
    if kind == "t3" and rid != root or kind == "t1" and record.get("link_id") and fullname(record["link_id"], "t3") != root:
        return []
    base = {"entity_id": rid, "kind": kind, "thread_id": thread,
            "parent_id": fullname(record["parent_id"]) if record.get("parent_id") else (root if kind == "t1" else None),
            "created_utc": timestamp(record.get("created_utc", record.get("created"))) or 0,
            "source": source, "received_at": captured,
            "title": record.get("title") or "", "author": record.get("author") or "[unknown]", "raw": record}
    rows = [{**base, **state} for state in capture_states(record, kind, source, captured)]
    for row in rows:
        row["id"] = digest({k: v for k, v in row.items() if k != "received_at"})
    return rows


def rank(row):
    return (row["observed_at"] if row["observed_at"] is not None else -1,
            3 if row["source"] in LIVE else 2 if row["source"] == "user_report" else 1,
            row["state"] in REMOVED, row["id"])


def current(rows):
    known = [row for row in rows if row["state"] != "unknown"]
    return max(known or rows, key=rank)
