"""shared history rules for the collector and riri's reddit scrapbook"""

import math
import re
from datetime import datetime, timezone

HISTORY_VERSION = 2
LIVE = {"reddit", "browser", "browser_page"}


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


def removal(record, text, category=None):
    category = category or record.get("removed_by_category")
    marker = text.strip().lower()
    actor = None
    if category == "deleted" or marker == "[deleted]":
        actor = "user"
    elif category in {"reddit", "removed by reddit", "anti_evil_ops", "community_ops", "content_takedown", "copyright_takedown"} or re.match(r"\[\s*removed by reddit\b", marker):
        actor = "reddit"
    elif category in {"moderator", "automod_filtered"} or record.get("banned_by") == "AutoModerator":
        actor = "mod"
    elif category or marker == "[removed]" or record.get("removed") is True:
        actor = "unknown"
    return {"actor": actor, "category": category, "reason": record.get("mod_reason_title")} if actor else None


def capture_states(record, kind, source, captured):
    text = record.get("body" if kind == "t1" else "selftext", "") or ""
    if not isinstance(text, str):
        raise ValueError("the record text isn't a string")
    meta = record.get("_meta") or {}
    first = timestamp(record.get("retrieved_on"))
    second = timestamp(meta.get("retrieved_2nd_on"))
    dated = [t for t in (first, second) if t is not None]
    observed = captured if source in LIVE | {"user_report"} else timestamp(record.get("_source_time")) if source == "wayback" and record.get("_source_time") else max(dated, default=None)
    base = {"observed_at": observed, "content_at": first if source == "arctic_shift" and meta.get("is_edited") and not meta.get("was_initially_deleted") else observed,
            "body": text, "removal": removal(record, text), "archive_edit_flag": bool(meta.get("is_edited"))}
    rows = []
    if source == "arctic_shift" and meta.get("was_deleted_later") is True:
        if text.strip() and not re.fullmatch(r"\[\s*(?:deleted|removed|removed by reddit[^\]]*)\s*\]", text.strip(), re.I):
            rows.append({**base, "observed_at": first, "content_at": first, "removal": None, "state": "present"})
        info = removal({}, "", meta.get("removal_type") or "unknown")
        rows.append({**base, "observed_at": second, "content_at": second, "body": "[deleted]" if info["actor"] == "user" else "[removed]",
                     "removal": info, "state": "deleted" if info["actor"] == "user" else "removed"})
    else:
        if source == "arctic_shift" and meta.get("was_initially_deleted") is True:
            info = removal({}, "", meta.get("removal_type") or "unknown")
            rows.append({**base, "observed_at": first, "content_at": first, "body": "[deleted]" if info["actor"] == "user" else "[removed]",
                         "removal": info, "state": "deleted" if info["actor"] == "user" else "removed"})
            base.update(removal=None, observed_at=second, content_at=second)
        info = base["removal"]
        state = "deleted" if info and info["actor"] == "user" else "removed" if info else "present" if text.strip() or record.get("title") else "unknown"
        rows.append({**base, "state": state})
    return rows


def observation_rank(observation, state, readable=False):
    source = observation["source"]
    value = observation.get("content_time") if readable and "content_time" in observation else observation.get("observed_at") if source in LIVE | {"user_report"} else observation.get("source_time")
    at = timestamp(value)
    return (at if at is not None else -1, 3 if source in LIVE else 2 if source == "user_report" else 1, int(state in {"deleted", "removed"}))
