"""collect watched threads, keeping partial runs and source responses intact"""

import json
import os
import time
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model import fullname, thread_id


ARCTIC = "https://arctic-shift.photon-reddit.com/api/"
HOSTS = {"arctic-shift.photon-reddit.com", "www.reddit.com", "oauth.reddit.com"}


class FetchFailure(Exception):
    def __init__(self, message, status=None, body=b"", retry_after=0):
        super().__init__(message)
        self.status, self.body, self.retry_after = status, body, retry_after


def retry_delay(headers):
    value = headers.get("Retry-After") or headers.get("X-RateLimit-Reset")
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    value = headers.get("X-RateLimit-Reset-At")
    if value:
        try:
            return max(0, float(value) - time.time())
        except ValueError:
            pass
    return 60


def allowed(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise FetchFailure("the source URL isn't supported")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # credentials stay on the one host they were intended for
        raise FetchFailure("the source redirected instead of returning a capture", code)


class Network:
    def __init__(self, token=None, delay=1):
        self.token = token or os.environ.get("REDDIT_ACCESS_TOKEN")
        self.delay, self.last = delay, 0
        self.opener = build_opener(NoRedirect())

    @property
    def reddit_base(self):
        return "https://oauth.reddit.com/" if self.token else "https://www.reddit.com/"

    def get(self, url):
        allowed(url)
        time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        headers = {"User-Agent": "better-arctic-shift/0.1 (https://github.com/bubblyslimea/better_arctic_shift)", "Accept": "application/json", "Accept-Encoding": "identity"}
        if self.token and urlparse(url).hostname == "oauth.reddit.com":
            headers["Authorization"] = "Bearer " + self.token
        try:
            with self.opener.open(Request(url, headers=headers), timeout=30) as response:
                data = response.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024:
                    raise FetchFailure("the source response exceeded 64 MiB")
                return data
        except HTTPError as error:
            raw = error.read(1024 * 1024)
            wait = retry_delay(error.headers) if error.code in {429, 500, 502, 503, 504} else 0
            raise FetchFailure(f"source returned HTTP {error.code}", error.code, raw, wait) from error
        except (URLError, TimeoutError, OSError) as error:
            raise FetchFailure("the source connection failed", retry_after=60) from error


class Collector:
    def __init__(self, store, network=None, max_requests=10000, heartbeat=lambda: None):
        self.store = store
        self.network = network or Network()
        self.max_requests = max_requests
        self.heartbeat = heartbeat
        self.calls = 0
        self.blocked = {}
        self.retry_after = 0

    def fetch(self, thread, source, url, run):
        self.heartbeat()
        cached = self.store.cached(run, url)
        if cached:
            return json.loads(cached["body"]), cached["id"], cached["captured"]
        host = urlparse(url).hostname
        if host in self.blocked:
            raise self.blocked[host]
        cooldown = self.store.db.execute("SELECT until_at FROM cooldowns WHERE host=?", (host,)).fetchone()
        if cooldown and cooldown[0] > time.time():
            raise FetchFailure("this source is waiting for its retry window", 429, retry_after=cooldown[0] - time.time())
        if self.calls >= self.max_requests:
            raise FetchFailure("the request budget was reached; resume this run to continue")
        self.calls += 1
        captured = time.time()
        raw = b""
        try:
            raw = self.network.get(url)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, (list, dict)) or isinstance(body, dict) and (body.get("error") or isinstance(body.get("json"), dict) and body["json"].get("errors")):
                raise ValueError("source error")
            if isinstance(body, dict) and not any(k in body for k in ("data", "json", "kind", "id", "children")):
                raise ValueError("unrecognized response")
            with self.store.db:
                cid = self.store.capture(thread, source, url, captured, raw, run_id=run)
            return body, cid, captured
        except (ValueError, UnicodeDecodeError) as error:
            failure = FetchFailure("the source didn't return a valid record response", body=raw)
            with self.store.db:
                self.store.capture(thread, source, url, captured, raw, status=None, error=str(failure), run_id=run)
            raise failure from error
        except FetchFailure as error:
            if error.status in {401, 403, 429}:
                self.blocked[host] = error
            with self.store.db:
                self.store.capture(thread, source, url, captured, error.body, status=error.status, error=str(error), run_id=run)
                if error.retry_after:
                    self.store.db.execute("INSERT INTO cooldowns VALUES(?,?) ON CONFLICT(host) DO UPDATE SET until_at=max(until_at,excluded.until_at)", (host, captured + error.retry_after))
            raise

    def take(self, thread, source, url, run):
        body, cid, captured = self.fetch(thread, source, url, run)
        try:
            with self.store.db:
                return self.store.ingest(thread, body, source, captured, cid)
        except (ValueError, TypeError, AttributeError) as error:
            with self.store.db:
                self.store.db.execute('UPDATE captures SET error=? WHERE id=?', ('the source returned a malformed record', cid))
            raise FetchFailure('the source returned a malformed record') from error

    def arctic(self, thread, run):
        issues = []
        for path in ("posts/ids?" + urlencode({"ids": thread}),
                     "comments/tree?" + urlencode({"link_id": "t3_" + thread, "limit": 25000, "start_breadth": 25000, "start_depth": 25000})):
            try:
                _, more = self.take(thread, "arctic_shift", ARCTIC + path, run)
                if any(not n.get("children") and n.get("count", 0) for n in more):
                    issues.append("the archive returned a continuation without comment ids")
            except FetchFailure as error:
                self.retry_after = max(self.retry_after, error.retry_after)
                issues.append(str(error))
        attempted = set()
        while pending := sorted(set(self.store.unresolved(thread)) - attempted):
            batch = pending[:500]
            attempted.update(batch)
            try:
                self.take(thread, "arctic_shift", ARCTIC + "comments/ids?" + urlencode({"ids": ",".join(i.removeprefix("t1_") for i in batch)}), run)
            except FetchFailure as error:
                self.retry_after = max(self.retry_after, error.retry_after)
                issues.append(str(error))
                break
        return issues

    def reddit(self, thread, run):
        issues, pending, attempted = [], set(), set()
        base = self.network.reddit_base
        try:
            _, more = self.take(thread, "reddit", base + f"comments/{thread}.json?" + urlencode({"limit": 500, "depth": 100, "raw_json": 1, "sort": "old"}), run)
            pending.update(fullname(i) for n in more for i in n.get("children", []))
            if any(not n.get("children") and n.get("count", 0) for n in more):
                issues.append("Reddit returned a continuation without comment ids")
        except FetchFailure as error:
            self.retry_after = max(self.retry_after, error.retry_after)
            issues.append(str(error))
        while pending - attempted:
            batch = sorted(pending - attempted)[:100]
            attempted.update(batch)
            try:
                found, more = self.take(thread, "reddit", base + "api/morechildren.json?" + urlencode({"api_type": "json", "link_id": "t3_" + thread, "children": ",".join(i.removeprefix("t1_") for i in batch), "limit_children": "false", "raw_json": 1, "sort": "old"}), run)
                pending.difference_update(found)
                pending.update(fullname(i) for n in more for i in n.get("children", []))
                if any(not n.get("children") and n.get("count", 0) for n in more):
                    issues.append("Reddit returned a continuation without comment ids")
            except FetchFailure as error:
                self.retry_after = max(self.retry_after, error.retry_after)
                issues.append(str(error))
                if error.status in {401, 403, 429} or self.calls >= self.max_requests:
                    break
        # audit every saved id, including comments no longer in Reddit's visible tree
        wanted = sorted(set(self.store.entities(thread)) | set(self.store.unresolved(thread)))
        checked = set()
        for start in range(0, len(wanted), 100):
            batch = wanted[start:start + 100]
            try:
                found, _ = self.take(thread, "reddit", base + "api/info.json?" + urlencode({"id": ",".join(batch), "raw_json": 1}), run)
                url = base + "api/info.json?" + urlencode({"id": ",".join(batch), "raw_json": 1})
                capture = self.store.cached(run, url)
                known = {row['entity_id'] for row in self.store.rows(thread) if row['source'] == 'reddit' and row['observed_at'] == capture['captured'] and row['state'] != 'unknown'}
                checked.update(set(batch) & found & known)
            except FetchFailure as error:
                self.retry_after = max(self.retry_after, error.retry_after)
                issues.append(str(error))
                if error.status in {401, 403, 429} or self.calls >= self.max_requests:
                    break
        return issues, {"requested_ids": wanted, "checked_ids": sorted(checked), "missing_ids": sorted(set(wanted) - checked)}

    def collect(self, value, run_id=None):
        thread = thread_id(value)
        run = run_id or self.store.start_run(thread)
        existing = self.store.db.execute("SELECT thread FROM runs WHERE id=?", (run,)).fetchone()
        if not existing or existing[0] != thread:
            raise ValueError("the saved run belongs to a different thread")
        self.calls, self.blocked, self.retry_after = 0, {}, 0
        details = {"sources": {}, "issues": []}
        try:
            arctic_issues = self.arctic(thread, run)
            reddit_issues, audit = self.reddit(thread, run)
            coverage = self.store.coverage(thread)
            details.update(status_audit=audit, coverage=coverage, retry_after=self.retry_after)
            for source, issues in (("arctic_shift", arctic_issues), ("reddit", reddit_issues)):
                captured = self.store.db.execute("SELECT 1 FROM captures WHERE run_id=? AND source=? AND error IS NULL LIMIT 1", (run, source)).fetchone()
                state = "unavailable" if not captured else "partial" if issues or (source == "reddit" and audit["missing_ids"]) else "checked"
                details["sources"][source] = {"state": state}
                details["issues"].extend({"source": source, "message": message} for message in dict.fromkeys(issues))
            partial = details["issues"] or audit["missing_ids"] or coverage["unresolved_ids"] or coverage["missing_parent_ids"] or coverage["cycle_ids"] or coverage["comment_gap"] or coverage["status_unchecked_comments"] or not self.store.entities(thread)
            state = "partial" if partial else "finished"
            self.store.finish_run(run, state, details)
            return {"run_id": run, "state": state, **details}
        except BaseException:
            self.store.finish_run(run, "paused", details)
            raise


def work_once(store, network=None):
    job = store.claim()
    if not job:
        return None
    try:
        result = Collector(store, network, heartbeat=lambda: store.heartbeat(job)).collect(job["thread"], job["run_id"])
        store.release(job, result["state"] == "finished", result["retry_after"])
        return result
    except BaseException:
        store.pause(job)
        raise
