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


import os
import json
import re
import requests
import asyncio
from pathlib import Path
from bs4 import BeautifulSoup
import google.generativeai as genai

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


async def fetch_usccb_daily_readings(date=None) -> Optional[str]:
    """
    Fetch the USCCB daily readings page for the given date (or today if None).
    This function caches the raw extracted reading text per-day in .cache/usccb_{YYYY-MM-DD}.txt
    and uses a Firefox-on-Windows user-agent to avoid being blocked.
    Returns the extracted reading text on success, or None on failure.
    """
    # determine date
    if date is None:
        from datetime import date as _date

        date = _date.today()
    # allow passing datetime.date or datetime.datetime
    if hasattr(date, "date"):
        date = date.date()
    # Build URL using DDMMYY format as requested
    url_date = date.strftime("%m%d%y")
    url = f"https://bible.usccb.org/bible/readings/{url_date}.cfm"

    # Prepare cache location (project root .cache)
    cache_dir = Path(__file__).resolve().parents[1] / ".cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"usccb_{date.isoformat()}.txt"

    # Return cached if present
    if cache_file.exists():
        try:
            return cache_file.read_text(encoding="utf-8")
        except Exception:
            # fall through to re-fetch
            pass

    # Synchronous request executed in threadpool to keep API async
    def _sync_fetch():
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:117.0) Gecko/20100101 Firefox/117.0",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.text

    loop = asyncio.get_running_loop()
    try:
        html = await loop.run_in_executor(None, _sync_fetch)
    except Exception as e:
        print(f"USCCB fetch failed for {url}: {e}")
        return None

    # Try to extract the main readings text using a list of fallback selectors
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Candidate selectors to find the main reading content
        selectors = [
            "div#maincontent",
            "div#content",
            "main",
            "article",
            "div.container",
            "div.row",
            "div.col-md-9",
            "div.b-article__content",
        ]

        main_elem = None
        for sel in selectors:
            elem = soup.select_one(sel)
            if elem and elem.get_text(strip=True):
                main_elem = elem
                break

        # If not found, attempt to find a div with "reading" or "bible" in class name
        if main_elem is None:
            re_match = re.compile(r"(reading|bible|scripture|lectionary|daily)", re.I)
            for div in soup.find_all("div"):
                cl = " ".join(div.get("class") or [])
                if re_match.search(cl) and div.get_text(strip=True):
                    main_elem = div
                    break

        # Final fallback: whole page
        if main_elem is None:
            text = soup.get_text("\n\n", strip=True)
        else:
            # Clean the text: preserve paragraphs separated by blank lines
            text = main_elem.get_text("\n\n", strip=True)

        # Minimal cleaning: collapse excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        # Save to cache
        try:
            cache_file.write_text(text, encoding="utf-8")
        except Exception:
            # cache failures are non-fatal
            pass

        return text
    except Exception as e:
        print(f"Failed to parse USCCB HTML for {url}: {e}")
        return None


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
        print(f"Gemini response: {text}")
        return json.loads(text)
    except Exception:
        print("failed")
        return None
