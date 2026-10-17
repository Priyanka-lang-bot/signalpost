"""Rules for deciding whether an incoming fact may replace a stored one."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

Decision = Literal["new", "unchanged", "changed", "stale"]


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)  # HTTP-date (Last-Modified)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_stale(
    incoming_retrieved_at: str,
    incoming_modified_at: str | None,
    stored_retrieved_at: str,
    stored_modified_at: str | None,
) -> bool:
    """True when the incoming observation is older than what is already stored."""
    in_mod, st_mod = parse_time(incoming_modified_at), parse_time(stored_modified_at)
    if in_mod and st_mod and in_mod < st_mod:
        return True
    in_ret, st_ret = parse_time(incoming_retrieved_at), parse_time(stored_retrieved_at)
    return bool(in_ret and st_ret and in_ret < st_ret)


def decide(stored: dict[str, Any] | None, value: Any, retrieved_at: str, modified_at: str | None) -> Decision:
    if stored is None:
        return "new"
    if is_stale(retrieved_at, modified_at, stored["retrieved_at"], stored.get("source_modified_at")):
        return "stale"
    return "unchanged" if stored["value"] == value else "changed"


def fact_group(field: str) -> str:
    if field == "roles":
        return "roles"
    if field.startswith("financials."):
        return "financials"
    return "entity"
