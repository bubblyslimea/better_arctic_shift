"""read public archive snapshots with their dates and limits attached"""

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


API = "https://arctic-shift.photon-reddit.com/api/"
USER_AGENT = "riri-reddit-reader/0.1 (+https://github.com/bubblyslimea/better_arctic_shift)"
MAX_BYTES = 16 * 1024 * 1024
CACHE_TTL = 900
REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "m.reddit.com", "sh.reddit.com", "redd.it"}


class SourceError(Exception):
    def __init__(self, status, message, http_status=None, retry_after=None):
        super().__init__(message)
        self.detail = {"status": status, "message": message}
        if http_status is not None:
            self.detail["http_status"] = http_status
        if retry_after is not None:
            self.detail["retry_after_seconds"] = retry_after


def identifier(value, kind="t3"):
    value = str(value).strip().lower()
    if re.fullmatch(kind + r"_[a-z0-9]{1,16}", value):
        return value[len(kind) + 1:]
    if re.fullmatch(r"[a-z0-9]{1,16}", value):
        return value
    raise ValueError("use a valid Reddit id")


def thread_id(value):
    value = value.strip()
    if re.fullmatch(r"(?:t3_)?[a-zA-Z0-9]{3,16}", value):
        return identifier(value)
    parsed = urlparse(value if "://" in value else "https://" + value)
    if parsed.scheme not in {"https", "http"} or parsed.hostname not in REDDIT_HOSTS or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("use a Reddit comments link or thread id")
    pattern = r"^/([a-zA-Z0-9]{3,16})/?$" if parsed.hostname == "redd.it" else r"(?:^|/)comments/([a-zA-Z0-9]{3,16})(?:/|\.json$|$)"
    found = re.search(pattern, parsed.path)
    if not found:
        raise ValueError("use the full comments link; share redirects are not fetched")
    return identifier(found[1])


def stamp(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        if not math.isfinite(result) or result < 0:
            return None
        datetime.fromtimestamp(result, timezone.utc)
        return result
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def iso(value):
    value = stamp(value)
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value is not None else None


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise SourceError("redirected", "the archive redirected; no other host was contacted")


class ArchiveClient:
    def __init__(self, opener=None):
        self.opener = opener or build_opener(NoRedirect())
        self.last_request = 0
        self.requests = []

    def get(self, path, parameters):
        if path not in {"posts/ids", "posts/search", "comments/tree"}:
            raise ValueError("that archive operation is not supported")
        url = API + path + "?" + urlencode(parameters)
        time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.requests.append(url)
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "identity"})
        try:
            with self.opener.open(request, timeout=25) as response:
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise SourceError("response_too_large", "the archive response exceeded the 16 MiB limit")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or "data" not in payload or payload.get("error"):
                raise ValueError("unrecognized response")
            if not isinstance(payload["data"], (list, dict)):
                raise ValueError("unrecognized records")
            return payload
        except HTTPError as error:
            retry = number_from_header(error.headers.get("Retry-After"))
            error.close()
            state = "access_denied" if error.code in {401, 403} else "rate_limited" if error.code == 429 else "source_unavailable"
            raise SourceError(state, "the archive request did not succeed", error.code, retry) from error
        except (URLError, TimeoutError, OSError) as error:
            raise SourceError("connection_failed", "the archive connection did not complete") from error
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise SourceError("invalid_response", "the archive did not return recognized JSON records") from error


def number_from_header(value):
    try:
        result = float(value)
        return max(0, result) if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def records(payload):
    pending = [payload]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            kind = item.get("kind")
            if kind in {"t1", "t3", "more"} and isinstance(item.get("data"), dict):
                yield kind, item["data"]
                if kind == "t1":
                    pending.append(item["data"].get("replies"))
            elif kind == "Listing":
                pending.append(item.get("data", {}).get("children", []))
            elif item.get("id") and ("body" in item or "title" in item):
                yield "t1" if "body" in item else "t3", item
                pending.append(item.get("replies"))
            elif "data" in item:
                pending.append(item["data"])
            elif "children" in item:
                pending.append(item["children"])


def normalized(raw, kind, thread):
    item_id = identifier(raw.get("id", ""), kind)
    text = raw.get("body" if kind == "t1" else "selftext") or ""
    if not isinstance(text, str) or not isinstance(raw.get("title", ""), str):
        raise ValueError("an archive record has invalid text")
    meta = raw.get("_meta") or {}
    if not isinstance(meta, dict):
        raise ValueError("an archive record has invalid metadata")
    first, second = stamp(raw.get("retrieved_on")), stamp(meta.get("retrieved_2nd_on"))
    observed = max((x for x in (first, second) if x is not None), default=None)
    content_date = first if meta.get("is_edited") or meta.get("was_deleted_later") else observed
    marker = text.strip().lower()
    category = raw.get("removed_by_category") or meta.get("removal_type")
    if meta.get("was_deleted_later"):
        state = "deleted" if category == "deleted" else "removed"
        observed = second
    elif meta.get("was_initially_deleted") and marker not in {"[deleted]", "[removed]"}:
        state, observed, content_date = "present", second, second
    elif marker == "[deleted]" or category == "deleted":
        state = "deleted"
    elif marker.startswith("[removed") or category or raw.get("removed") is True:
        state = "removed"
    else:
        state = "present" if text.strip() or raw.get("title") else "unknown"
    parent = raw.get("parent_id") if kind == "t1" else None
    if parent is not None and (not isinstance(parent, str) or not re.fullmatch(r"t[13]_[a-z0-9]{1,16}", parent)):
        raise ValueError("an archive record has an invalid parent id")
    result = {
        "id": kind + "_" + item_id,
        "parent_id": parent,
        "author": raw.get("author") if isinstance(raw.get("author"), str) else "[unknown]",
        "subreddit": raw.get("subreddit") if isinstance(raw.get("subreddit"), str) else None,
        "text": text,
        "score_at_capture": number(raw.get("score")),
        "created_at": iso(raw.get("created_utc", raw.get("created"))),
        "content_observed_at": iso(content_date),
        "status_observed_at": iso(observed),
        "observed_state": state,
        "current_state_verified": False,
        "archive_edit_flag": bool(meta.get("is_edited")),
        "historical_text_retained": bool(meta.get("was_deleted_later") and text.strip() and marker != "[deleted]" and not marker.startswith("[removed")),
        "reddit_url": "https://www.reddit.com/comments/" + thread + ("/_/" + item_id + "/" if kind == "t1" else "/"),
    }
    if kind == "t3":
        result.update(title=raw.get("title", ""), archived_reported_comments=number(raw.get("num_comments")))
    return result


def fetch_thread(thread, max_comments=1000, client=None):
    client = client or ArchiveClient()
    post, comments, continuations, issues = None, {}, 0, []
    for path, parameters in (
        ("posts/ids", {"ids": thread}),
        ("comments/tree", {"link_id": "t3_" + thread, "limit": max_comments, "start_breadth": max_comments, "start_depth": max_comments}),
    ):
        try:
            for kind, raw in records(client.get(path, parameters)):
                if kind == "more":
                    continuations += 1
                    continue
                if kind == "t3" and identifier(raw.get("id", "")) != thread:
                    continue
                if kind == "t1" and raw.get("link_id") and raw["link_id"] not in {thread, "t3_" + thread}:
                    continue
                item = normalized(raw, kind, thread)
                if kind == "t3":
                    post = item
                elif len(comments) < max_comments or item["id"] in comments:
                    comments[item["id"]] = item
        except SourceError as error:
            issues.append({"operation": path, **error.detail})
            break
        except (ValueError, TypeError, AttributeError) as error:
            issues.append({"operation": path, "status": "invalid_record", "message": "one or more archive records could not be interpreted"})
            break
    parents = sorted({r["parent_id"] for r in comments.values() if r["parent_id"] and r["parent_id"].startswith("t1_") and r["parent_id"] not in comments})
    count = post.get("archived_reported_comments") if post else None
    return {
        "format": "riri-reddit-reader", "schema_version": 1,
        "status": "partial" if issues and (post or comments) else "source_unavailable" if issues else "archive_snapshot" if post or comments else "not_in_archive",
        "thread_id": thread, "source": "Arctic Shift", "downloaded_at": iso(time.time()),
        "source_urls": client.requests, "post": post, "comments": list(comments.values()),
        "coverage": {
            "captured_comments": len(comments), "archived_reported_comments": count,
            "difference_from_archived_count": count - len(comments) if count is not None else None,
            "archive_continuations": continuations, "collection_limit_reached": len(comments) >= max_comments,
            "missing_parent_ids": parents, "current_state_verified": False, "completeness": "unverified",
        },
        "issues": issues,
        "notice": "archived observations, not a live Reddit check; capture counts, scores, edits, and removal states may have changed",
    }


def cache_path(directory, thread, max_comments):
    key = hashlib.sha256(f"v1:{thread}:{max_comments}".encode()).hexdigest()
    return Path(directory) / (key + ".json")


def read_thread(thread, max_comments, directory, refresh=False, client=None):
    path = cache_path(directory, thread, max_comments)
    if not refresh:
        try:
            if path.stat().st_size <= MAX_BYTES:
                saved = json.loads(path.read_text())
                age = time.time() - saved["saved_at"]
                if 0 <= age < CACHE_TTL and saved["thread"]["thread_id"] == thread:
                    return {**saved["thread"], "cache_hit": True}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    result = fetch_thread(thread, max_comments, client)
    if result["status"] == "archive_snapshot":
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
                temporary = file.name
                json.dump({"saved_at": time.time(), "thread": result}, file, ensure_ascii=False, allow_nan=False)
            os.replace(temporary, path)
        except OSError:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
            result["cache_warning"] = "the temporary snapshot could not be cached"
    return {**result, "cache_hit": False}


def view(snapshot, limit=40, offset=0, focus=None, query=None, body_limit=12000):
    result = copy.deepcopy(snapshot)
    comments = {item["id"]: item for item in result.pop("comments")}
    ordered = sorted(comments.values(), key=lambda x: (x.get("created_at") or "", x["id"]))
    matched = None
    if focus:
        target = "t1_" + identifier(focus, "t1")
        matched = {target} if target in comments else set()
        selected = set(matched)
        children = {}
        for item in ordered:
            children.setdefault(item["parent_id"], []).append(item["id"])
        pending = list(matched)
        while pending:
            for child in children.get(pending.pop(), []):
                if child not in selected:
                    selected.add(child)
                    pending.append(child)
        result["focus_found"] = bool(matched)
    elif query:
        matched = {item["id"] for item in ordered if query.casefold() in item["text"].casefold()}
        selected = set(matched)
    if matched is not None:
        for start in list(selected):
            parent = comments[start].get("parent_id")
            while parent in comments and parent not in selected:
                selected.add(parent)
                parent = comments[parent].get("parent_id")
        ordered = [item for item in ordered if item["id"] in selected]
        result["matched_comment_ids"] = sorted(matched)
    page = ordered[offset:offset + limit]
    page_ids = {item["id"] for item in page}
    result["page"] = {"offset": offset, "limit": limit, "returned": len(page), "selected_total": len(ordered),
                      "next_offset": offset + len(page) if offset + len(page) < len(ordered) else None,
                      "parents_outside_page": sorted({item["parent_id"] for item in page if item["parent_id"] in comments and item["parent_id"] not in page_ids})}
    for item in page + ([result["post"]] if result.get("post") else []):
        item["text_length"] = len(item["text"])
        item["text_truncated"] = len(item["text"]) > body_limit
        item["text"] = item["text"][:body_limit]
    result["comments"] = page
    return result


def search(subreddit, query, limit=10, after=None, before=None, client=None):
    if not re.fullmatch(r"[A-Za-z0-9_]{2,32}", subreddit):
        raise ValueError("use a subreddit name without r/")
    if not query.strip() or len(query) > 300:
        raise ValueError("use a search phrase between 1 and 300 characters")
    parameters = {"subreddit": subreddit, "query": query, "limit": limit, "sort": "desc"}
    for key, value in (("after", after), ("before", before)):
        if value:
            datetime.strptime(value, "%Y-%m-%d")
            parameters[key] = value
    client = client or ArchiveClient()
    posts = []
    for kind, raw in records(client.get("posts/search", parameters)):
        if kind == "t3":
            item = normalized(raw, kind, identifier(raw["id"]))
            item["text_length"] = len(item["text"])
            item["text_truncated"] = len(item["text"]) > 1200
            item["text"] = item["text"][:1200]
            posts.append(item)
            if len(posts) >= limit:
                break
    return {"status": "archive_search", "source": "Arctic Shift", "downloaded_at": iso(time.time()),
            "source_urls": client.requests, "posts": posts, "current_state_verified": False,
            "notice": "historical keyword matches, not a complete or current Reddit search"}


def bounded(low, high):
    def parse(value):
        result = int(value)
        if not low <= result <= high:
            raise argparse.ArgumentTypeError(f"choose a number from {low} to {high}")
        return result
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description="read public Reddit archive snapshots with dated evidence")
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("read", help="read one archived thread")
    read.add_argument("thread")
    read.add_argument("--limit", type=bounded(1, 200), default=40)
    read.add_argument("--offset", type=bounded(0, 25000), default=0)
    read.add_argument("--max-comments", type=bounded(1, 25000), default=1000)
    read.add_argument("--body-limit", type=bounded(1, 40000), default=12000)
    selection = read.add_mutually_exclusive_group()
    selection.add_argument("--focus", help="include this comment, its ancestors, and captured replies")
    selection.add_argument("--query", help="find captured text and include its ancestors")
    read.add_argument("--refresh", action="store_true", help="download a new archive snapshot")
    read.add_argument("--cache-dir", default=str(Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "riri-reddit-reader"))
    find = commands.add_parser("search", help="search archived posts in one subreddit")
    find.add_argument("--subreddit", required=True)
    find.add_argument("--query", required=True)
    find.add_argument("--limit", type=bounded(1, 100), default=10)
    find.add_argument("--after", help="creation date in YYYY-MM-DD form")
    find.add_argument("--before", help="creation date in YYYY-MM-DD form")
    args = parser.parse_args(argv)
    try:
        if args.command == "read":
            if args.focus:
                identifier(args.focus, "t1")
            snapshot = read_thread(thread_id(args.thread), args.max_comments, args.cache_dir, args.refresh)
            result = view(snapshot, args.limit, args.offset, args.focus, args.query, args.body_limit)
        else:
            result = search(args.subreddit, args.query, args.limit, args.after, args.before)
        code = 2 if result["status"] in {"source_unavailable", "not_in_archive"} else 0
    except SourceError as error:
        result, code = {"source": "Arctic Shift", **error.detail}, 2
    except (ValueError, TypeError, AttributeError) as error:
        result, code = {"status": "invalid_input_or_record", "message": str(error)}, 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
