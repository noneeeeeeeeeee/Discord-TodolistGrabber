from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo


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
from datetime import datetime, timedelta


_gemini_model = None
_gemini_configured = False
CACHE_DIR = Path(__file__).resolve().parents[1] / ".cache"


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


def _us_date_str(date_obj) -> str:
    try:
        if hasattr(date_obj, "date"):
            date_obj = date_obj.date()
        # Example: "August 14, 2025"
        return date_obj.strftime("%B %d, %Y")
    except Exception:
        return ""


def _extract_text_for_date(raw_text: str, date_obj) -> str:
    """
    From a multi-day page dump, return only the section for the given date
    by locating a heading like 'August 15, 2025' and slicing until the next date heading.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        return ""
    lines = [ln.strip() for ln in raw_text.splitlines()]
    date_pat = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")
    hdrs: list[tuple[int, str]] = [
        (i, ln) for i, ln in enumerate(lines) if date_pat.match(ln)
    ]
    if not hdrs:
        return raw_text
    target = _us_date_str(date_obj)
    start_idx = -1
    for i, ln in hdrs:
        if ln == target:
            start_idx = i
            break
    if start_idx == -1:
        return raw_text
    end_idx = len(lines)
    for i, _ in hdrs:
        if i > start_idx:
            end_idx = i
            break
    sliced = "\n".join(lines[start_idx:end_idx]).strip()
    return sliced or raw_text


def _looks_like_daily_readings(text: str) -> bool:
    """
    Heuristic: must contain at least one of 'Reading', 'Gospel', or 'Responsorial Psalm'
    and be sufficiently long.
    """
    if not isinstance(text, str):
        return False
    t = text.lower()
    markers = ["reading 1", "reading i", "gospel", "responsorial psalm"]
    return any(m in t for m in markers) and len(text) > 200


def _summary_seems_valid(summary: dict, expect_date=None) -> bool:
    if not isinstance(summary, dict):
        return False
    para = str(summary.get("summary_paragraph") or "").strip()
    quote = str(summary.get("motivational_quote") or "")
    if len(para) < 50:
        return False
    if "No motivational quote" in quote:
        return False
    # Optional sanity check on date field; do not strictly enforce
    try:
        if expect_date:
            ds = str(summary.get("date") or "")
            if ds:
                _ = expect_date.strftime("%B")
    except Exception:
        pass
    return True


def purge_usccb_cache(max_age_days: int = 2) -> None:
    """
    Remove cached USCCB text and JSON summary files older than max_age_days.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        patterns = ["usccb_*.txt", "usccb_summary_*.json"]
        for pat in patterns:
            for p in CACHE_DIR.glob(pat):
                try:
                    mtime = datetime.utcfromtimestamp(p.stat().st_mtime)
                    if mtime < cutoff:
                        p.unlink(missing_ok=True)
                except Exception:
                    continue
    except Exception:
        pass


def _summary_cache_file(date_obj) -> Path:
    if hasattr(date_obj, "date"):
        date_obj = date_obj.date()
    return CACHE_DIR / f"usccb_summary_{date_obj.isoformat()}.json"


def load_usccb_summary_from_cache(date_obj=None) -> Optional[dict]:
    """
    Load the JSON summary for the given date (or today if None) from cache.
    """
    from datetime import date as _date

    if date_obj is None:
        date_obj = _date.today()
    if hasattr(date_obj, "date"):
        date_obj = date_obj.date()
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fp = _summary_cache_file(date_obj)
        if fp.exists():
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None
    return None


async def fetch_usccb_daily_readings(date=None) -> Optional[str]:
    """
    Fetch the USCCB daily readings page for the given date (or today if None).
    This function caches the extracted reading text per-day in .cache/usccb_{YYYY-MM-DD}.txt
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

    # Build URL using MMDDYY format as requested
    url_date = date.strftime("%m%d%y")
    url = f"https://bible.usccb.org/bible/readings/{url_date}.cfm"

    # Prepare cache location (project root .cache)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"usccb_{date.isoformat()}.txt"

    # Cleanup old cache files (older than 3 days)
    purge_usccb_cache(max_age_days=2)

    # Return cached if present
    if cache_file.exists():
        try:
            return cache_file.read_text(encoding="utf-8")
        except Exception:
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

        if main_elem is None:
            re_match = re.compile(r"(reading|bible|scripture|lectionary|daily)", re.I)
            for div in soup.find_all("div"):
                cl = " ".join(div.get("class") or [])
                if re_match.search(cl) and div.get_text(strip=True):
                    main_elem = div
                    break

        if main_elem is None:
            text = soup.get_text("\n\n", strip=True)
        else:
            text = main_elem.get_text("\n\n", strip=True)

        text = re.sub(r"\n{3,}", "\n\n", text).strip()

        try:
            cache_file.write_text(text, encoding="utf-8")
        except Exception:
            pass

        return text
    except Exception as e:
        print(f"Failed to parse USCCB HTML for {url}: {e}")
        return None


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        first_nl = t.find("\n")
        if first_nl != -1:
            t = t[first_nl + 1 :]
    if t.endswith("```"):
        t = t[:-3]
    return t.strip()


async def summarize_usccb_readings(readings, date=None) -> Optional[dict]:
    """
    Ask Gemini to summarize readings into JSON:
    { link, title, date, motivational_quote, summary_paragraph }
    Returns dict on success, or None on failure.
    """
    # Resolve date for cache naming
    from datetime import date as _date

    if date is None:
        date = _date.today()
    if hasattr(date, "date"):
        date = date.date()
    # Try cached JSON summary first
    cached = load_usccb_summary_from_cache(date)
    if isinstance(cached, dict):
        return cached

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
        data = json.loads(text)
        # Save JSON summary cache for future reuse
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(_summary_cache_file(date), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return data
    except Exception:
        return None


# Backward-compatible alias expected by callers
async def summarize_usccb_daily_readings(readings, date=None) -> Optional[dict]:
    return await summarize_usccb_readings(readings, date=date)


async def get_usccb_daily_readings_summary(
    tz_name: str | None = None,
) -> Optional[dict]:
    """
    High-level orchestrator:
    - Purge old caches (>= 2 days)
    - Return today's JSON summary from cache if valid
    - Else fetch today's page, slice to today's section, summarize with Gemini, cache JSON, and return
    - Else fallback to yesterday's cached/derived summary
    - Return None if nothing valid is available
    This function guarantees at most one website fetch per day because it honors the JSON cache.
    """
    try:
        purge_usccb_cache(2)
    except Exception:
        pass
    try:
        tz = ZoneInfo(tz_name) if tz_name else None
    except Exception:
        tz = None
    now = datetime.now(tz or datetime.now().astimezone().tzinfo)
    today = now.date()
    # 1) Try today's cached JSON summary
    cached = load_usccb_summary_from_cache(today)
    if _summary_seems_valid(cached or {}, expect_date=today):
        return cached
    # 2) Fetch today's readings and summarize if content looks valid
    text = await fetch_usccb_daily_readings(today)
    if isinstance(text, str) and text.strip():
        sliced = _extract_text_for_date(text, today)
        if _looks_like_daily_readings(sliced):
            summary = await summarize_usccb_readings(sliced, date=today)
            if _summary_seems_valid(summary or {}, expect_date=today):
                return summary
    # 3) Fallback to yesterday
    yday = today - timedelta(days=1)
    y_cached = load_usccb_summary_from_cache(yday)
    if _summary_seems_valid(y_cached or {}, expect_date=yday):
        return y_cached
    y_text = await fetch_usccb_daily_readings(yday)
    if isinstance(y_text, str) and y_text.strip():
        y_sliced = _extract_text_for_date(y_text, yday)
        if _looks_like_daily_readings(y_sliced):
            y_summary = await summarize_usccb_readings(y_sliced, date=yday)
            if _summary_seems_valid(y_summary or {}, expect_date=yday):
                return y_summary
    return None
