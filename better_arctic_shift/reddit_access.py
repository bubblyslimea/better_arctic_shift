"""check one approved application-only connection without storing thread content"""

import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from .model import thread_id


REQUIRED = ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")
MESSAGES = {
    "needs_credentials": "configure the approved app's client id, client secret, and user agent in the worker environment",
    "invalid_configuration": "check the named configuration fields without sharing their values",
    "invalid_thread": "choose a public Reddit thread link or id",
    "dependency_missing": "install the optional Reddit client with python -m pip install '.[reddit]'",
    "authentication_failed": "the OAuth request was rejected; check the approved app's credentials",
    "access_denied": "the endpoint denied access; this check stopped without switching routes",
    "rate_limited": "the endpoint requested a pause; use its retry window before another check",
    "not_found": "the requested thread was not found or is not available to this app",
    "redirected": "the endpoint redirected; this check did not follow it",
    "server_error": "the service did not complete the request",
    "connection_failed": "the connection failed; no successful API read was verified",
    "unexpected_response": "the API check did not receive the expected thread response",
    "ok": "the approved API connection returned this thread and its initial comment listing",
}


def _client(praw, settings, thread, requests):
    from prawcore import Requestor

    class CheckRequestor(Requestor):
        def request(self, method, url, **kwargs):
            parsed = urlparse(url)
            form = dict(kwargs.get("data") or {})
            secure = (parsed.scheme == "https" and not parsed.username and not parsed.password
                      and parsed.port in (None, 443))
            token = (method.upper() == "POST" and parsed.hostname == "www.reddit.com"
                     and parsed.path == "/api/v1/access_token"
                     and form.get("grant_type") == "client_credentials")
            read = (method.upper() == "GET" and parsed.hostname == "oauth.reddit.com"
                    and parsed.path.rstrip("/") in {f"/comments/{thread}", f"/comments/{thread}.json"})
            if not secure or not (token or read) or len(requests) >= 4:
                raise ValueError("this request is outside the bounded read-only check")
            entry = {"stage": "authentication" if token else "thread", "http_status": None}
            requests.append(entry)
            kwargs["allow_redirects"] = False
            response = super().request(method, url, **kwargs)
            entry["http_status"] = response.status_code
            return response

    client = praw.Reddit(
        site_name="DEFAULT",
        client_id=settings["REDDIT_CLIENT_ID"],
        client_secret=settings["REDDIT_CLIENT_SECRET"],
        user_agent=settings["REDDIT_USER_AGENT"],
        username=None,
        password=None,
        refresh_token=None,
        redirect_uri=None,
        oauth_url="https://oauth.reddit.com",
        reddit_url="https://www.reddit.com",
        check_for_updates=False,
        check_for_async=False,
        ratelimit_seconds=0,
        requestor_class=CheckRequestor,
        requestor_kwargs={"timeout": 20},
    )
    client.read_only = True
    return client


def _failure(error, requests):
    response = getattr(error, "response", None)
    code = getattr(response, "status_code", None)
    if code == 401:
        status = "authentication_failed"
    elif code == 403:
        status = "access_denied"
    elif code == 429:
        status = "rate_limited"
    elif code == 404:
        status = "not_found"
    elif isinstance(code, int) and 300 <= code < 400:
        status = "redirected"
    elif isinstance(code, int) and code >= 500:
        status = "server_error"
    elif type(error).__name__ == "OAuthException":
        status = "authentication_failed"
    elif type(error).__name__ == "RequestException":
        status = "connection_failed"
    else:
        status = "unexpected_response"
    result = {"status": status, "http_status": code,
              "failed_stage": requests[-1]["stage"] if requests else "client_setup"}
    if code == 429:
        raw_wait = response.headers.get("Retry-After") or response.headers.get("X-Ratelimit-Reset")
        if raw_wait and str(raw_wait).isdigit():
            result["retry_after_seconds"] = int(raw_wait)
    # upstream exceptions and response bodies can contain credentials or tokens
    return result


def check_access(value, environ=None):
    """return a secret-free result; missing settings never trigger a network request"""
    environ = os.environ if environ is None else environ
    result = {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
              "mode": "application_only_read_only", "network_attempted": False, "requests": []}
    try:
        thread = thread_id(value)
    except (ValueError, AttributeError):
        result.update(status="invalid_thread", message=MESSAGES["invalid_thread"])
        return result
    result["thread_id"] = thread
    settings = {key: environ.get(key, "").strip() for key in REQUIRED}
    missing = [key for key, value in settings.items() if not value]
    invalid = [key for key, value in settings.items()
               if any(ord(char) < 32 or ord(char) > 126 for char in value)
               or value.upper().startswith("YOUR_")]
    if settings["REDDIT_USER_AGENT"] and len(settings["REDDIT_USER_AGENT"]) < 7:
        invalid.append("REDDIT_USER_AGENT")
    if missing or invalid:
        status = "needs_credentials" if missing else "invalid_configuration"
        result.update(status=status, message=MESSAGES[status], missing=missing,
                      invalid=sorted(set(invalid)))
        return result
    try:
        import praw
    except ImportError:
        result.update(status="dependency_missing", message=MESSAGES["dependency_missing"])
        return result

    client = None
    try:
        client = _client(praw, settings, thread, result["requests"])
        submission = client.submission(id=thread)
        submission.comment_limit = 10
        submission.comment_sort = "old"
        # loading the initial listing does not expand more-comments placeholders
        comments = submission.comments.list()
        if submission.id != thread:
            raise ValueError("the response belongs to a different thread")
        result.update(
            status="ok",
            reported_comment_count=submission.num_comments,
            sampled_comment_ids=[node.fullname for node in comments if isinstance(node, praw.models.Comment)],
            continuation_placeholders=sum(isinstance(node, praw.models.MoreComments) for node in comments),
            sample_only=True,
        )
    except Exception as error:
        result.update(_failure(error, result["requests"]))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    result["network_attempted"] = bool(result["requests"])
    result["message"] = MESSAGES[result["status"]]
    return result
