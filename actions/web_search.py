#web_search.py
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from memory.config_manager import get_tavily_key


def _searched_on() -> str:
    """Stamp every result block with the date it was fetched.

    Results carry no dates of their own — Tavily's general topic omits
    published_date entirely and DDG text() never had one — so a page headed
    "current models (April 2026)" reads as current no matter how long ago that
    was. The assistant knows today's date from its system prompt; giving it the
    fetch date too is what lets it notice the gap and say so."""
    return f"(searched {datetime.now().strftime('%d %B %Y')})"


# Pinned on purpose, not lazily. "gemini-flash-latest" is a floating alias that
# follows whichever model Google is currently promoting, and on this account it
# lands on a 3.x model with no grounding allocation: 429 in under a second,
# three times out of three, while 2.5-flash serves the same grounded request in
# five seconds. An alias that can silently move onto a model the key cannot use
# is a poor default for the one backend that is supposed to be the reliable one
# — and it fails as a quota error, which is exactly the wrong diagnosis.
_SEARCH_MODEL = "gemini-2.5-flash"


# ── Gemini grounding quota circuit breaker ────────────────────────────────────
# The google_search grounding tool has its own small quota, separate from plain
# generation.  Once it is spent every call returns 429 — so retrying it at the
# top of every search only adds a dead round-trip before the DDG fallback runs.
# After a quota error, skip Gemini entirely for a cooldown period.
_QUOTA_COOLDOWN_SEC  = 900          # 15 minutes
_quota_blocked_until = 0.0
_quota_lock          = threading.Lock()


def _gemini_available() -> bool:
    with _quota_lock:
        return time.monotonic() >= _quota_blocked_until


def _note_gemini_error(exc: Exception) -> None:
    """Trip the breaker when the error is a quota / rate-limit rejection."""
    global _quota_blocked_until
    msg = str(exc)
    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        with _quota_lock:
            already = time.monotonic() < _quota_blocked_until
            _quota_blocked_until = time.monotonic() + _QUOTA_COOLDOWN_SEC
        if not already:
            # Deliberately not phrased as "quota exhausted". A 429 here means
            # either the day's grounding is spent OR the key has no allocation
            # for this model at all, and the two are indistinguishable from the
            # response. Reading it as the first cost an afternoon.
            print(
                f"[WebSearch] {_SEARCH_MODEL} refused grounding (429) — skipping "
                f"it for {_QUOTA_COOLDOWN_SEC // 60} min, serving from DDG. "
                "Either the daily grounding quota is spent, or this key has no "
                "quota for this model."
            )


class _QuotaCooldown(RuntimeError):
    """Raised instead of calling Gemini while the quota breaker is open."""


def _log_gemini_failure(context: str, exc: Exception) -> None:
    """Log a Gemini failure — silently when it is just the expected cooldown."""
    if isinstance(exc, _QuotaCooldown):
        return          # announced once when the breaker tripped; not a warning
    print(f"[WebSearch] \u26a0\ufe0f {context} failed ({exc}) — using DDG instead")


def _run_bounded(fn, timeout: float, label: str = "task"):
    """Run fn() in a daemon thread; return its result, or None if it overruns."""
    box = [None]

    def _run():
        try:
            box[0] = fn()
        except Exception as e:
            _log_gemini_failure(label, e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"[WebSearch] {label} exceeded {timeout:.0f}s — moving on")
    return box[0]

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _gemini_search(query: str) -> str:
    if not _gemini_available():
        raise _QuotaCooldown("Gemini grounding is in quota cooldown")

    from google import genai

    client = genai.Client(api_key=_get_api_key())
    try:
        response = client.models.generate_content(
            model=_SEARCH_MODEL,
            contents=query,
            config={"tools": [{"google_search": {}}]},
        )
    except Exception as e:
        _note_gemini_error(e)
        raise

    text = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            text += part.text

    text = text.strip()
    if not text:
        raise ValueError("Gemini returned an empty response.")
    # Grounded prose is no less able to quote a page written months ago, so it
    # gets the same fetch stamp as the other two backends.
    return f"{_searched_on()}\n\n{text}"


# ── Tavily ────────────────────────────────────────────────────────────────────
# Optional middle backend, tried after Gemini and before DDG. It is worth the
# extra hop because it returns a synthesised answer like Gemini does, whereas
# DDG only returns raw snippets — so when Gemini's grounding quota is spent the
# assistant still has something worth reading aloud.
#
# Every failure path returns None rather than raising, so the caller never has
# to tell "no key configured" apart from "the request failed": both simply fall
# through to DDG.

_TAVILY_URL     = "https://api.tavily.com/search"
_TAVILY_TIMEOUT = 8.0


def _tavily_search(
    query: str,
    topic: str = "general",
    max_results: int = 6,
    advanced: bool = False,
) -> str | None:
    key = get_tavily_key()
    if not key:
        return None

    import requests

    try:
        r = requests.post(
            _TAVILY_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "query":          query,
                "topic":          topic,
                "max_results":    max_results,
                "search_depth":   "advanced" if advanced else "basic",
                "include_answer": True,
            },
            timeout=_TAVILY_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[WebSearch] ⚠️ Tavily failed ({e}) — using DDG instead")
        return None

    results = [
        {
            "title":   x.get("title", ""),
            "snippet": x.get("content", ""),
            "url":     x.get("url", ""),
        }
        for x in data.get("results", [])
    ]

    answer = (data.get("answer") or "").strip()
    if not answer and not results:
        return None

    # The summary and the snippets always travel together. Returning the summary
    # alone was a real defect: it is generated from these same sources and has
    # been observed to garble them outright — one run reported GPT-5 as "built
    # by a team of inventors at Amazon" — and with the snippets dropped there
    # was nothing left for the assistant to check it against.
    lines = [f"Search results for: {query}", _searched_on(), ""]
    if answer:
        lines += ["Summary (generated — verify against the sources below):",
                  answer, ""]

    for i, r in enumerate(results, 1):
        if r.get("title"):
            lines.append(f"{i}. {r['title']}")
        if r.get("snippet"):
            # Capped because Tavily returns up to three chunks per source and a
            # Live session pays for every one of them in context.
            lines.append(f"   {r['snippet'][:500]}")
        if r.get("url"):
            lines.append(f"   Source: {r['url']}")
        lines.append("")

    return "\n".join(lines).strip()


def _get_ddgs():
    """
    Returns the DDGS class.  The package was renamed duckduckgo-search -> ddgs;
    the legacy package's endpoints are now rejected by DuckDuckGo (news() gets a
    403 Ratelimit, text() silently returns zero results), so warn loudly if we
    end up on it instead of failing in silence.
    """
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        from duckduckgo_search import DDGS
        print(
            "[WebSearch] ⚠️ Using the deprecated 'duckduckgo-search' package — "
            "DuckDuckGo blocks its endpoints, so every search will come back "
            "empty.  Fix with:  pip install -U ddgs"
        )
        return DDGS


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    DDGS = _get_ddgs()
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("href",   ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG text() failed: {e}")
    return results


def _ddg_news(query: str, max_results: int = 8) -> list[dict]:
    """DDG news search — returns actual articles, not website homepages."""
    DDGS = _get_ddgs()
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.news(query, max_results=max_results):
                results.append({
                    "title":   r.get("title",  ""),
                    "snippet": r.get("body",   ""),
                    "url":     r.get("url",    ""),
                    "source":  r.get("source", ""),
                })
    except Exception as e:
        print(f"[WebSearch] ⚠️ DDG news() failed ({e}) — falling back to text search")
    # Also covers the legacy-package case, where news() returns an empty list
    # instead of raising.
    if not results:
        results = _ddg_search(query, max_results=max_results)
    return results


def _format_ddg(query: str, results: list[dict]) -> str:
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}", _searched_on(), ""]
    for i, r in enumerate(results, 1):
        if r.get("title"):   lines.append(f"{i}. {r['title']}")
        if r.get("snippet"): lines.append(f"   {r['snippet']}")
        if r.get("url"):     lines.append(f"   Source: {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_news(query: str, results: list[dict]) -> str:
    if not results:
        return f"No news found for: {query}"

    lines = [f"Latest news: {query}", _searched_on(), ""]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        if not title:
            continue
        src = f"  [{r['source']}]" if r.get("source") else ""
        lines.append(f"{i}. {title}{src}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet'][:140]}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        lines.append("")
    return "\n".join(lines).strip()


# ── Briefing helper ────────────────────────────────────────────────────────────

def _gemini_headlines(n: int = 5) -> tuple[list[str], str]:
    """
    Fetches current headlines via Gemini grounded search.
    Optimised for speed: minimal prompt + strict token cap.
    Returns (headline_list, raw_text_for_display).
    """
    import re
    from google import genai

    client = genai.Client(api_key=_get_api_key())
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=f"Current world news: {n} headlines. Numbered list, titles only.",
        config={"tools": [{"google_search": {}}]},
    )

    raw = ""
    for part in response.candidates[0].content.parts:
        if hasattr(part, "text") and part.text:
            raw += part.text

    headlines = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Only accept lines that begin with a number — skips preamble/closing sentences
        if not re.match(r'^[\d]+[.\)\-]', line):
            continue
        clean = re.sub(r'^[\d]+[.\)\-]\s*', '', line)
        clean = re.sub(r'^\*+\s*',          '', clean).strip()
        if clean and len(clean) > 10:
            headlines.append(clean)

    return headlines[:n], raw.strip()


# ── Modes ──────────────────────────────────────────────────────────────────────

def _search(query: str) -> str:
    """Default search — Gemini grounded, then Tavily, then DDG."""
    try:
        return _gemini_search(query)
    except Exception as e:
        _log_gemini_failure("Gemini search", e)

    text = _tavily_search(query)
    if text:
        return text

    return _format_ddg(query, _ddg_search(query))


def _news(query: str) -> str:
    """
    DDG first, Gemini as backup.

    The old version raced both backends in parallel and kept the first answer.
    That burned one google_search grounding call on *every* news request —
    including the startup briefing — even when DDG had already won the race.
    Grounding has a small quota, so it ran dry after a handful of launches and
    then 429'd for everything else (research/compare), which are the modes that
    actually need a synthesised answer.

    DDG news returns in well under a second and gives raw headlines, which is
    exactly what the briefing wants, so it goes first and Gemini is only touched
    when DDG comes back empty.

    Tavily sits between the two for the same reason DDG leads: it has its own
    credit pool, so spending one there is cheaper than spending the grounding
    call that research/compare cannot do without.
    """
    gemini_query = f"latest news today: {query}" if query else "top world news today"
    ddg_query    = query if query else "world news today"

    def _ddg_attempt() -> str:
        return _format_news(ddg_query, _ddg_news(ddg_query, max_results=8))

    text = _run_bounded(_ddg_attempt, timeout=5.0, label="DDG news")
    if text and len(text) > 60 and not text.startswith("No news found"):
        return text

    text = _run_bounded(
        lambda: _tavily_search(ddg_query, topic="news", max_results=8),
        timeout=9.0, label="Tavily news",
    )
    if text and len(text) > 60:
        return text

    text = _run_bounded(
        lambda: _gemini_search(gemini_query), timeout=6.0, label="Gemini news"
    )
    if text and len(text) > 60:
        return text

    return f"No news found for: {query}"


def _research(query: str) -> str:
    """
    Deep dive — asks Gemini for a comprehensive answer with context.
    Falls back to a wider DDG fetch.
    """
    research_query = (
        f"Comprehensive, detailed explanation of: {query}. "
        "Include background context, key facts, current state, and important nuances."
    )
    try:
        return _gemini_search(research_query)
    except Exception as e:
        _log_gemini_failure("Gemini research", e)

    # The one mode that pays for Tavily's advanced depth: research is where a
    # thin answer is most obviously worse than none.
    text = _tavily_search(query, max_results=10, advanced=True)
    if text:
        return text

    return _format_ddg(query, _ddg_search(query, max_results=10))


def _price(query: str) -> str:
    """Product price lookup — searches for current market prices."""
    price_query = f"current price of {query} — how much does it cost today"
    try:
        return _gemini_search(price_query)
    except Exception as e:
        _log_gemini_failure("Gemini price", e)

    text = _tavily_search(price_query)
    if text:
        return text

    return _format_ddg(query, _ddg_search(f"{query} price buy", max_results=6))


def _compare(items: list[str], aspect: str) -> str:
    query = (
        f"Compare {', '.join(items)} in terms of {aspect}. "
        "Give specific facts and data."
    )
    try:
        return _gemini_search(query)
    except Exception as e:
        _log_gemini_failure("Gemini compare", e)

    text = _tavily_search(query, max_results=8)
    if text:
        return text

    all_results: dict[str, list] = {}
    for item in items:
        try:
            all_results[item] = _ddg_search(f"{item} {aspect}", max_results=3)
        except Exception:
            all_results[item] = []

    lines = [f"Comparison — {aspect.upper()}", "─" * 40]
    for item in items:
        lines.append(f"\n▸ {item}")
        for r in all_results.get(item, [])[:2]:
            if r.get("snippet"):
                lines.append(f"  • {r['snippet']}")
            if r.get("url"):
                lines.append(f"    {r['url']}")
    return "\n".join(lines)


# ── Public entry point ─────────────────────────────────────────────────────────

def web_search(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    query  = params.get("query", "").strip()
    mode   = params.get("mode",  "search").lower().strip()
    items  = params.get("items", [])
    aspect = params.get("aspect", "general").strip() or "general"

    if not query and not items:
        return "Please provide a search query."

    if items and mode not in ("compare",):
        mode = "compare"

    if player:
        player.write_log(f"[Search:{mode}] {query or ', '.join(items)}")

    print(f"[WebSearch] 🔍 mode={mode!r}  query={query!r}")

    try:
        if mode == "compare" and items:
            return _compare(items, aspect)
        if mode == "news":
            return _news(query)
        if mode == "research":
            return _research(query)
        if mode == "price":
            return _price(query)
        return _search(query)

    except Exception as e:
        print(f"[WebSearch] ❌ All backends failed: {e}")
        return f"Search failed: {e}"
