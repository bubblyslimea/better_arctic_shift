"""turn source records into dated observations without guessing missing events"""

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


LIVE = {"reddit", "browser", "browser_page"}
SOURCES = LIVE | {"arctic_shift", "user_report", "pullpush", "wayback"}
REMOVED = {"deleted", "removed"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


def timestamp(value):
    if value is None or value == "":
        return None
    if isinstance(value, str) and not re.fullmatch(r"\d+(\.\d+)?", value):
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError("include a timezone with the observation date")
        value = date.timestamp()
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError("the observation date isn't valid")
    return result


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


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


def removal(record, text, category=None):
    category = category or record.get("removed_by_category")
    marker = text.strip().lower()
    actor = None
    if category == "deleted" or marker == "[deleted]":
        actor = "user"
    elif category in {"reddit", "removed by reddit", "anti_evil_ops", "community_ops", "content_takedown", "copyright_takedown"} or marker.startswith("[removed by reddit"):
        actor = "reddit"
    elif category in {"moderator", "automod_filtered"} or record.get("banned_by") == "AutoModerator":
        actor = "mod"
    elif category or marker == "[removed]" or record.get("removed") is True:
        actor = "unknown"
    return {"actor": actor, "category": category, "reason": record.get("mod_reason_title")} if actor else None


def observations(record, kind, thread, source, captured):
    if source not in SOURCES:
        raise ValueError("choose a supported observation source")
    rid = fullname(record.get("id"), kind)
    if not rid.startswith(kind + "_"):
        raise ValueError("record kind and id disagree")
    root = "t3_" + thread
    if kind == "t3" and rid != root or kind == "t1" and record.get("link_id") and fullname(record["link_id"], "t3") != root:
        return []
    text = record.get("body" if kind == "t1" else "selftext", "") or ""
    if not isinstance(text, str):
        raise ValueError("the record text isn't a string")
    meta = record.get("_meta") or {}
    first = timestamp(record.get("retrieved_on"))
    second = timestamp(meta.get("retrieved_2nd_on"))
    dated = [t for t in (first, second) if t is not None]
    observed = captured if source in LIVE | {"user_report"} else max(dated, default=None)
    base = {"entity_id": rid, "kind": kind, "thread_id": thread,
            "parent_id": fullname(record["parent_id"]) if record.get("parent_id") else (root if kind == "t1" else None),
            "created_utc": timestamp(record.get("created_utc", record.get("created"))) or 0,
            "source": source, "received_at": captured, "observed_at": observed,
            "content_at": first if source == "arctic_shift" and meta.get("is_edited") and not meta.get("was_initially_deleted") else observed,
            "body": text, "title": record.get("title") or "", "author": record.get("author") or "[unknown]",
            "raw": record, "removal": removal(record, text), "archive_edit_flag": bool(meta.get("is_edited"))}
    rows = []
    if source == "arctic_shift" and meta.get("was_deleted_later"):
        if text.strip() and not text.strip().lower().startswith(("[deleted]", "[removed")):
            rows.append({**base, "observed_at": first, "content_at": first, "removal": None, "state": "present"})
        info = removal({}, "", meta.get("removal_type") or "unknown")
        rows.append({**base, "observed_at": second, "body": "[deleted]" if info["actor"] == "user" else "[removed]",
                     "removal": info, "state": "deleted" if info["actor"] == "user" else "removed"})
    else:
        if source == "arctic_shift" and meta.get("was_initially_deleted"):
            info = removal({}, "", meta.get("removal_type") or "unknown")
            rows.append({**base, "observed_at": first, "body": "[deleted]" if info["actor"] == "user" else "[removed]",
                         "removal": info, "state": "deleted" if info["actor"] == "user" else "removed"})
            base["removal"] = None
            base["observed_at"] = second
            base["content_at"] = second
        info = base["removal"]
        state = "deleted" if info and info["actor"] == "user" else "removed" if info else "present" if text.strip() or base["title"] else "unknown"
        rows.append({**base, "state": state})
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
