from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Any, List, Optional, Tuple


def _ensure_tz(dt: datetime, tzinfo) -> datetime:
    """
    Ensure dt is timezone-aware using tzinfo if naive.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tzinfo or datetime.now().astimezone().tzinfo)
    return dt.astimezone(tzinfo or datetime.now().astimezone().tzinfo)


def _now(tzinfo=None) -> datetime:
    return datetime.now(tz=tzinfo or datetime.now().astimezone().tzinfo)


def _day_bounds(dt: datetime) -> Tuple[datetime, datetime]:
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end


def _tomorrow_bounds(now: datetime) -> Tuple[datetime, datetime]:
    tomorrow = now + timedelta(days=1)
    return _day_bounds(tomorrow)


def _week_bounds(now: datetime) -> Tuple[datetime, datetime]:
    # Monday as the first day of week; adjust if your org uses different start
    monday = now - timedelta(days=now.weekday())
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    return start, end


def _normalize_task(task: Mapping[str, Any], tzinfo) -> Optional[Mapping[str, Any]]:
    """
    Expected keys:
    - 'title' (str) or 'name'
    - 'due' (datetime)
    Optional:
    - 'course'/'source' (str)

    Returns a normalized mapping or None if invalid.
    """
    title = task.get("title") or task.get("name")
    due = task.get("due") or task.get("due_date") or task.get("deadline")
    if not title or not isinstance(due, datetime):
        return None

    due = _ensure_tz(due, tzinfo)
    course = task.get("course") or task.get("subject") or task.get("source")
    return {"title": title, "due": due, "course": course}


def summarize_period(
    tasks: Iterable[Mapping[str, Any]],
    period: str,
    now: Optional[datetime] = None,
    tzinfo=None,
) -> List[Mapping[str, Any]]:
    """
    Filter tasks for a period: 'tomorrow' or 'week'. Excludes already past-due.
    Returns a list of normalized tasks sorted by due datetime ascending.
    """
    now = _ensure_tz(now, tzinfo) if isinstance(now, datetime) else _now(tzinfo)
    start: datetime
    end: datetime

    if period == "tomorrow":
        start, end = _tomorrow_bounds(now)
    elif period == "week":
        start, end = _week_bounds(now)
    else:
        raise ValueError("period must be 'tomorrow' or 'week'")

    window: List[Mapping[str, Any]] = []
    for t in tasks or []:
        n = _normalize_task(t, tzinfo)
        if not n:
            continue
        due = n["due"]
        # in range [start, end)
        if start <= due < end and due >= now:
            window.append(n)

    window.sort(key=lambda x: x["due"])
    return window


def has_work(
    tasks: Iterable[Mapping[str, Any]],
    period: str,
    now: Optional[datetime] = None,
    tzinfo=None,
) -> bool:
    return len(summarize_period(tasks, period, now=now, tzinfo=tzinfo)) > 0


def to_lines(items: Iterable[Mapping[str, Any]]) -> List[str]:
    """
    Format items as human-readable lines. Example:
    'Wed Sep 11 14:00 — Read Ch. 5 (CS101)'
    """
    lines: List[str] = []
    for it in items:
        due: datetime = it["due"]
        title: str = it["title"]
        course = it.get("course")
        if course:
            line = f"{due:%a %b %d %H:%M} — {title} ({course})"
        else:
            line = f"{due:%a %b %d %H:%M} — {title}"
        lines.append(line)
    return lines


# --- additions for USCCB readings summarization ---
import os
import json
import google.generativeai as genai
from catholic_mass_readings import USCCB, models

_gemini_model = None
_gemini_configured = False


def _get_gemini_model():
    """
    Lazily configure Gemini and return a GenerativeModel, or None if no key.
    """
    global _gemini_model, _gemini_configured
    api_key = os.getenv("GeminiApiKey")
    if not api_key:
        return None
    if not _gemini_configured:
        try:
            genai.configure(api_key=api_key)
            _gemini_configured = True
        except Exception:
            return None
    if _gemini_model is None:
        _gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    return _gemini_model


async def fetch_usccb_daily_readings():
    """
    Fetch today's mass readings using USCCB.
    """
    async with USCCB() as usccb:
        mass = await usccb.get_mass(datetime.today().date(), models.MassType.DEFAULT)
        return mass


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        # remove leading code fence like ```json or ```
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1 :]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


async def summarize_usccb_readings(readings) -> Optional[dict]:
    """
    Ask Gemini to summarize readings into JSON:
    { link, title, date, motivational_quote, summary_paragraph }
    Returns dict on success, or None on failure.
    """
    model = _get_gemini_model()
    if model is None:
        return None
    prompt = (
        "Summarize the following daily readings and provide a motivational quote with a link embed. "
        "Respond as raw JSON with keys: link, title, date, motivational_quote, summary_paragraph. "
        "Do not include code fences or markdown. The motivational_quote must be sourced from the readings. "
        f"Readings:\n\n{readings}"
    )
    try:
        resp = model.generate_content(prompt)
        text = resp.text or ""
        text = _strip_code_fences(text)
        return json.loads(text)
    except Exception:
        return None
