"""
kw-research — a small tool that actually searches the internet for keywords.

You give it a business / products and a city. It asks Google, YouTube and
DuckDuckGo live for what people search, then scores and groups the results.

    python3 app.py        ->  http://localhost:8100

Why the results are trustworthy
-------------------------------
Every keyword comes back with the *evidence*: which autocomplete endpoints
returned it, and how high it sat in the suggestion list. Google's suggestions
are ordered by popularity, so a term that appears from many different query
stems, high in the list, is genuinely in demand. That is a real popularity
signal, not a guess.

There is no official search volume here — Google does not publish it and
inventing it would be lying. Demand is a 0-100 score built from those signals,
and every row is exported so you can check volumes against Keyword Planner.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import analysis
import competitor as cmpmod

ROOT = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = os.path.join(ROOT, "history")
os.makedirs(HISTORY_DIR, exist_ok=True)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MARKETS = {
    "in": ("in", "en"), "in_hi": ("in", "hi"), "us": ("us", "en"),
    "uk": ("uk", "en"), "ae": ("ae", "en"), "au": ("au", "en"),
    "ca": ("ca", "en"), "global": ("", "en"),
}

# Places Google happily suggests that are not your market. Without this, an
# India brief fills up with "...cost texas", "...nyc", "...kerala".
LABEL = {"google": "Google", "youtube": "YouTube", "ddg": "DuckDuckGo"}

_FOREIGN_PLACES = set("""
usa us america canada uk england london australia sydney melbourne dubai uae sharjah
singapore malaysia pakistan karachi lahore bangladesh dhaka nepal kathmandu sri lanka
philippines indonesia thailand vietnam china japan korea germany france spain italy
netherlands ireland scotland wales south africa nigeria kenya ghana brazil mexico
texas california florida newyork nyc chicago houston dallas austin seattle boston
atlanta miami denver phoenix sanjose sandiego losangeles lasvegas philadelphia
toronto vancouver montreal ottawa manchester birmingham leeds glasgow dublin
melbourne brisbane perth adelaide auckland wellington doha riyadh jeddah muscat
kuwait bahrain abudhabi delhi mumbai bangalore bengaluru hyderabad chennai kolkata
pune ahmedabad jaipur lucknow surat indore bhopal nagpur patna kanpur thane
kerala karnataka goa punjab haryana gujarat maharashtra telangana odisha assam
""".split())

_MULTIWORD_PLACES = ["new york", "los angeles", "san francisco", "las vegas", "hong kong",
                     "sri lanka", "new zealand", "south africa", "abu dhabi", "saudi arabia",
                     "united states", "united kingdom", "san jose", "san diego"]


def _foreign_geo(kw: str, allow: str = "") -> bool:
    if any(m in kw for m in _MULTIWORD_PLACES):
        return not (allow and allow in kw)
    toks = set(re.findall(r"[a-z]+", kw))
    hits = toks & _FOREIGN_PLACES
    if not hits:
        return False
    return not (allow and allow in kw)


_cache: dict[str, list[str]] = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Live data sources
# ---------------------------------------------------------------------------
def _get(url: str, timeout=8) -> str | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return raw.decode("utf-8", "ignore")
    except Exception:
        return None


def google_suggest(q: str, gl: str, hl: str) -> list[str]:
    """Google autocomplete. Ordered by real search popularity."""
    key = f"g|{gl}|{hl}|{q}"
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    url = ("https://suggestqueries.google.com/complete/search?client=firefox"
           f"&hl={hl}&gl={gl}&q={urllib.parse.quote(q)}")
    raw = _get(url)
    out = []
    if raw:
        try:
            data = json.loads(raw)
            out = [s.strip() for s in data[1] if isinstance(s, str)]
        except Exception:
            out = []
    with _cache_lock:
        _cache[key] = out
    return out


def youtube_suggest(q: str, gl: str, hl: str) -> list[str]:
    key = f"y|{gl}|{hl}|{q}"
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    url = ("https://suggestqueries.google.com/complete/search?client=firefox&ds=yt"
           f"&hl={hl}&gl={gl}&q={urllib.parse.quote(q)}")
    raw = _get(url)
    out = []
    if raw:
        try:
            data = json.loads(raw)
            out = [s.strip() for s in data[1] if isinstance(s, str)]
        except Exception:
            out = []
    with _cache_lock:
        _cache[key] = out
    return out


def ddg_suggest(q: str) -> list[str]:
    raw = _get("https://duckduckgo.com/ac/?q=" + urllib.parse.quote(q))
    if not raw:
        return []
    try:
        return [d["phrase"] for d in json.loads(raw) if isinstance(d, dict) and "phrase" in d]
    except Exception:
        return []


def bing_serp(q: str, market: str = "in") -> list[dict]:
    """
    Bing's RSS output. The HTML search page serves junk to server IPs, but the
    RSS endpoint returns the real result set, so this is the primary source.
    """
    import html as _h
    # NOTE: any extra parameter (cc=, setmkt=, mkt=) makes Bing's RSS endpoint
    # return a random unrelated cached feed, so the URL must stay exactly this.
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(q)
           + "&format=rss&count=15")
    raw = _get(url, timeout=10)
    if not raw or "<item>" not in raw:
        return []
    out = []
    for item in re.findall(r"<item>(.*?)</item>", raw, re.S)[1:]:
        t = re.search(r"<title>(.*?)</title>", item, re.S)
        l = re.search(r"<link>(.*?)</link>", item, re.S)
        if not t or not l:
            continue
        title = _h.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip()
        link = _h.unescape(l.group(1)).strip()
        dom = urllib.parse.urlparse(link).netloc.replace("www.", "")
        if not dom or "bing.com" in dom or not title:
            continue
        out.append({"domain": dom, "title": title, "url": link})
        if len(out) >= 10:
            break

    # Bing sometimes mixes in unrelated cached results. Drop any single result
    # that shares no vocabulary with the query, then reject the whole feed if
    # too little survives (caller will fall back to another engine).
    GENERIC = {"best", "top", "cheap", "cheapest", "cost", "price", "prices", "fees",
               "near", "online", "buy", "review", "reviews", "india", "indian",
               "the", "and", "for", "with", "you", "your", "how", "what", "why",
               "does", "can", "get", "good", "new"}
    q_tokens = {t for t in re.findall(r"[a-z0-9]+", q.lower())
                if len(t) > 2 and t not in GENERIC}
    if q_tokens:
        need = 2 if len(q_tokens) >= 3 else 1
        scored = []
        for r in out:
            toks = {t for t in re.findall(r"[a-z0-9]+",
                                          (r["title"] + " " + r["url"]).lower())}
            scored.append((len(q_tokens & toks), r))
        kept = [r for n, r in scored if n >= need]
        # No "relax to 1 token" fallback: it let unrelated pages define the
        # difficulty score, which is worse than returning nothing.
        return kept if len(kept) >= 4 else []
    return out


def ddg_serp(q: str) -> list[dict]:
    """DuckDuckGo HTML endpoint — fallback; may be rate-limited from servers."""
    raw = _get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q), timeout=9)
    if not raw or "result__a" not in raw:
        return []
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        title = re.sub(r"\s+", " ", title).strip()
        url = href
        if "uddg=" in href:
            url = urllib.parse.unquote(href.split("uddg=", 1)[1].split("&")[0])
        dom = urllib.parse.urlparse(url).netloc.replace("www.", "")
        if dom and title:
            out.append({"domain": dom, "title": title, "url": url})
        if len(out) >= 10:
            break
    return out


def _html_unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)


# ---------------------------------------------------------------------------
# Research cache (public-site essentials: speed + fewer upstream requests)
# ---------------------------------------------------------------------------
RESEARCH_CACHE = {}
RESEARCH_TTL = int(os.environ.get("CACHE_TTL", "21600"))   # 6 hours
CACHE_MAX = int(os.environ.get("CACHE_MAX", "300"))
_cache_lock2 = threading.Lock()


def cache_key(payload: dict) -> str:
    seeds = sorted({x.strip().lower() for x in (payload.get("seeds") or []) if x.strip()})
    return "|".join([",".join(seeds), (payload.get("city") or "").strip().lower(),
                     payload.get("market") or "in", "deep" if payload.get("deep", True) else "fast"])


def cache_get(key: str):
    with _cache_lock2:
        item = RESEARCH_CACHE.get(key)
        if not item:
            return None
        if time.time() - item["at"] > RESEARCH_TTL:
            RESEARCH_CACHE.pop(key, None)
            return None
        return item["data"]


def cache_put(key: str, data: dict):
    with _cache_lock2:
        if len(RESEARCH_CACHE) >= CACHE_MAX:
            oldest = sorted(RESEARCH_CACHE.items(), key=lambda kv: kv[1]["at"])[:40]
            for k, _ in oldest:
                RESEARCH_CACHE.pop(k, None)
        RESEARCH_CACHE[key] = {"at": time.time(), "data": data}


SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()


def serpapi_serp(q, market="in"):
    """
    Optional. If SERPAPI_KEY is set, use SerpApi instead of scraping: no
    rate-limit bans and a licensed data source rather than scraping, which is
    the right choice for a public website.
    """
    if not SERPAPI_KEY:
        return []
    gl = {"in": "in", "in_hi": "in", "us": "us", "uk": "uk", "ae": "ae",
          "au": "au", "ca": "ca"}.get(market, "in")
    url = ("https://serpapi.com/search.json?engine=google&num=10&hl=en"
           f"&gl={gl}&q={urllib.parse.quote(q)}&api_key={SERPAPI_KEY}")
    raw = _get(url, timeout=20)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for r in (data.get("organic_results") or [])[:10]:
        link = r.get("link") or ""
        dom = urllib.parse.urlparse(link).netloc.replace("www.", "")
        if dom:
            out.append({"domain": dom, "title": r.get("title", ""), "url": link})
    return out


def serp(q: str, market: str = "in") -> list[dict]:
    """SerpApi (if configured) -> Bing RSS -> DuckDuckGo HTML."""
    key = f"s|{market}|{q}"
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    rows = serpapi_serp(q, market)
    if not rows:
        rows = bing_serp(q, market)
    if not rows:                       # transient junk feed -- one retry
        rows = bing_serp(q, market)
    if not rows:
        rows = ddg_serp(q)
    with _cache_lock:
        _cache[key] = rows
    return rows


# ---------------------------------------------------------------------------
# Research plan
# ---------------------------------------------------------------------------
QUESTION_PREFIXES = ["how to ", "how much ", "how long ", "what is ", "why ", "when ",
                     "where ", "can i ", "does ", "is ", "do i need ", "who "]
MODIFIER_PREFIXES = ["best ", "cheap ", "top ", "affordable ", "cost of ", "price of "]
SUFFIXES = ["", " price", " cost", " near me", " for", " vs", " online", " reviews",
            " shop", " clinic", " service", " course", " in ", " without", " after",
            " at home", " for kids", " for adults", " package", " charges", " fees",
            " doctor", " centre", " center", " treatment", " options"]

INTENT_WORDS = {
    "buy": ["buy", "price", "cost", "fee", "fees", "rate", "charges", "cheap", "book",
            "order", "shop", "hire", "package", "offer", "discount", "quote"],
    "compare": ["best", "top", "vs", "versus", "review", "reviews", "alternative",
                "compare", "which"],
    "local": ["near me", "nearby", "in ", "open now", "timing", "address", "phone"],
    "learn": ["how", "what", "why", "when", "where", "guide", "meaning", "benefits",
              "side effects", "does", "can", "is ", "vs"],
}


def build_queries(seeds: list[str], city: str, deep: bool = True) -> list[tuple[str, str]]:
    """(query, source) pairs to fire at autocomplete."""
    qs: list[tuple[str, str]] = []
    city = (city or "").strip().lower()
    for si, seed in enumerate(seeds):
        s = seed.strip().lower()
        if not s:
            continue
        qs.append((s, "google"))
        for suf in SUFFIXES[1:]:
            qs.append((s + suf, "google"))
        if city:
            qs.append((f"{s} in {city}", "google"))
            qs.append((f"{city} {s}", "google"))
            qs.append((f"{s} near {city}", "google"))
        for pre in QUESTION_PREFIXES:
            qs.append((pre + s, "google"))
        for pre in MODIFIER_PREFIXES:
            qs.append((pre + s, "google"))
        # alphabet sweep is the highest-yield trick: only for the first seed(s)
        if deep and si < 2:
            for c in "abcdefghijklmnopqrstuvwxyz":
                qs.append((f"{s} {c}", "google"))
        for suf in ["", " price", " near me", " for"]:
            qs.append((s + suf, "youtube"))
        for suf in ["", " price", " near me"]:
            qs.append((s + suf, "ddg"))
    # dedupe on (query, source) -- the same phrase must still be asked of
    # Google AND YouTube AND DuckDuckGo, they return different suggestions
    seen, out = set(), []
    for q in qs:
        if q in seen:
            continue
        seen.add(q)
        out.append(q)
    return out


def run_research(business: str, seeds: list[str], city: str, market: str,
                 deep: bool = True) -> dict:
    t0 = time.time()
    gl, hl = MARKETS.get(market, ("in", "en"))
    seeds = [s.strip() for s in seeds if s and s.strip()]
    if not seeds and business:
        seeds = [p.strip() for p in re.split(r"[,/|]| and ", business) if p.strip()][:4]
    seeds = seeds[:4]
    if not seeds:
        return {"ok": False, "error": "Tell me what you sell (one service or product per line)."}

    queries = build_queries(seeds, city, deep)
    # keep the live request count sane
    queries = queries[:320]

    hits = defaultdict(lambda: {"count": 0, "pos": [], "sources": set(), "seeds": set(),
                                "queries": set()})

    def worker(item):
        q, source = item
        if source == "google":
            sugg = google_suggest(q, gl, hl)
        elif source == "youtube":
            sugg = youtube_suggest(q, gl, hl)
        else:
            sugg = ddg_suggest(q)
        return q, source, sugg

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(worker, queries))

    allow = (city or "").strip().lower()
    dropped_geo = 0

    seed_lookup = {s.lower(): s for s in seeds}
    for q, source, sugg in results:
        for pos, kw in enumerate(sugg):
            k = kw.lower().strip()
            if not k or len(k) > 90:
                continue
            if _foreign_geo(k, allow):
                dropped_geo += 1
                continue
            h = hits[k]
            h["count"] += 1
            h["pos"].append(pos)
            h["sources"].add(source)
            h["queries"].add(q)
            for s in seed_lookup:
                if s in k:
                    h["seeds"].add(seed_lookup[s])

    if not hits:
        return {"ok": False, "error": "No live data came back. Check the connection and retry."}

    # ---- SERP check: which keywords are already being targeted by ranking pages
    serp_targets = seeds[:2]
    serp_data = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut = {s: pool.submit(serp, (s + (" " + city if city else "")).strip(), market)
               for s in serp_targets}
        for s, f in fut.items():
            serp_data[s] = f.result()

    serp_domains = defaultdict(lambda: {"count": 0, "titles": []})
    serp_text = ""
    for s, rows in serp_data.items():
        for r in rows:
            serp_domains[r["domain"]]["count"] += 1
            if len(serp_domains[r["domain"]]["titles"]) < 2:
                serp_domains[r["domain"]]["titles"].append(r["title"])
            serp_text += " " + r["title"].lower()

    # ---- scoring
    max_count = max(h["count"] for h in hits.values()) or 1
    rows = []
    for kw, h in hits.items():
        avg_pos = sum(h["pos"]) / len(h["pos"])
        coverage = math.log(h["count"] + 1) / math.log(max_count + 1)
        position = 1.0 - min(avg_pos, 10) / 10.0
        diversity = len(h["sources"]) / 3.0
        serp_bonus = 0.12 if kw.replace(" ", "") in serp_text.replace(" ", "") else 0.0
        demand = (0.55 * coverage + 0.25 * position + 0.08 * diversity + serp_bonus) * 100
        demand = round(min(99.0, demand), 1)

        intent = "learn"
        if any(w in kw for w in INTENT_WORDS["buy"]):
            intent = "buy"
        elif any(w in kw for w in INTENT_WORDS["compare"]):
            intent = "compare"
        if city and city.lower() in kw:
            intent = "local"
        elif "near me" in kw:
            intent = "local"

        rows.append({
            "keyword": kw,
            "demand": demand,
            "intent": intent,
            "sources": sorted(h["sources"]),
            "evidence": h["count"],
            "avg_position": round(avg_pos, 1),
            "seed": (sorted(h["seeds"]) or [""])[0],
            "serp_hit": bool(serp_bonus),
        })

    rows.sort(key=lambda r: -r["demand"])

    # Guaranteed local targets: the exact "service + your city" pairs. They are
    # built, not scraped -- if autocomplete did not return them they still are
    # the pages a local business must own. Flagged so you can tell the difference.
    local_targets = []
    if city:
        c = city.strip().lower()
        for s_ in seeds:
            for kw in (f"{s_} in {c}", f"{s_} {c}", f"best {s_} in {c}",
                       f"{s_} {c} price", f"{s_} near me"):
                hit = next((r for r in rows if r["keyword"] == kw), None)
                local_targets.append({
                    "keyword": kw,
                    "demand": hit["demand"] if hit else None,
                    "evidence": hit["evidence"] if hit else 0,
                    "intent": "local",
                    "source": "found in autocomplete" if hit else "built for your city",
                })
        local_targets.sort(key=lambda r: (-(r["demand"] or 0), r["keyword"]))

    questions = [r for r in rows if re.match(
        r"^(how|what|why|when|where|which|can|does|is|are|do|should)\b", r["keyword"])]
    local = [r for r in rows if city and (city.lower() in r["keyword"] or "near me" in r["keyword"])]

    competitors = sorted(
        [{"domain": d, "rankings": v["count"], "titles": v["titles"]}
         for d, v in serp_domains.items()],
        key=lambda x: -x["rankings"])[:10]

    return {
        "ok": True,
        "meta": {
            "business": business,
            "seeds": seeds,
            "city": city,
            "market": market,
            "queries_run": len(queries),
            "keywords_found": len(rows),
            "seconds": round(time.time() - t0, 1),
            "sources": ["Google autocomplete", "YouTube autocomplete",
                        "DuckDuckGo autocomplete", "DuckDuckGo results"],
            "dropped_foreign": dropped_geo,
        },
        "keywords": rows,
        "questions": questions[:40],
        "local": local[:40],
        "local_targets": local_targets[:24],
        "competitors": competitors,
        "seeds_detail": [
            {"seed": s, "results": serp_data.get(s, [])[:8]} for s in serp_targets
        ],
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Per-visitor sessions
# ---------------------------------------------------------------------------
# The local version kept results in one global dict. On a website that means two
# visitors overwrite each other's research, so results are now scoped to a
# session cookie and resolved per request (each request runs in its own thread).
import secrets
import threading
from collections.abc import MutableMapping

_ctx = threading.local()
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "400"))


class _SessionProxy(MutableMapping):
    """Looks like a dict, but always points at the current visitor's data."""

    def _d(self):
        d = getattr(_ctx, "session", None)
        if d is None:
            d = _ctx.session = {}
        return d

    def __getitem__(self, k):
        return self._d()[k]

    def __setitem__(self, k, v):
        self._d()[k] = v

    def __delitem__(self, k):
        del self._d()[k]

    def __iter__(self):
        return iter(self._d())

    def __len__(self):
        return len(self._d())

    def clear(self):
        self._d().clear()

    def update(self, *a, **kw):
        self._d().update(*a, **kw)


LAST = _SessionProxy()


def session_for(sid):
    with SESSIONS_LOCK:
        s = SESSIONS.get(sid)
        if s is None:
            if len(SESSIONS) >= MAX_SESSIONS:          # drop the oldest
                oldest = sorted(SESSIONS.items(),
                                key=lambda kv: kv[1].get("_seen", 0))[:50]
                for k, _ in oldest:
                    SESSIONS.pop(k, None)
            s = SESSIONS[sid] = {}
        s["_seen"] = time.time()
        return s


def ensure_session_loaded() -> None:
    """Recover a visitor's last research after a restart / cold start."""
    if LAST.get("keywords"):
        return
    folder = os.path.join(HISTORY_DIR, getattr(_ctx, "sid", "anon"))
    if not os.path.isdir(folder):
        return
    files = sorted((f for f in os.listdir(folder) if f.endswith(".json")), reverse=True)
    if not files:
        return
    try:
        with open(os.path.join(folder, files[0]), encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return
    j.pop("id", None)
    j.pop("when", None)
    j.pop("label", None)
    LAST.update(j)
    if LAST.get("keywords"):
        LAST["topics"] = analysis.build_topics(LAST["keywords"])


def score_difficulty(keywords: list[str]) -> dict:
    """Fetch the live SERP for each keyword and score how hard it is to win."""
    ensure_session_loaded()
    keywords = [k for k in keywords if k][:25]

    market = LAST.get("meta", {}).get("market", "in")

    def one(kw):
        try:
            results = serp(kw, market)
        except Exception:
            results = []
        return kw, analysis.score_difficulty(kw, results)

    out = {}
    with ThreadPoolExecutor(max_workers=3) as pool:   # gentle: avoid rate limits
        for kw, d in pool.map(one, keywords):
            out[kw] = d
    if LAST.get("keywords"):
        analysis.add_scores(LAST["keywords"], out)
    return {"ok": True, "checked": len(out), "results": out}


def build_briefs(count: int = 15) -> dict:
    ensure_session_loaded()
    if not LAST.get("keywords"):
        return {"ok": False, "error": "Run a research first."}
    rows = sorted(LAST["keywords"],
                  key=lambda r: -(r.get("opportunity")
                                  if r.get("opportunity") is not None else r.get("demand") or 0))
    qs = LAST.get("questions") or []
    briefs = [analysis.build_brief(r, qs, LAST["meta"].get("city", ""),
                                   LAST["meta"].get("business", ""))
              for r in rows[:max(1, min(count, 50))]]
    LAST["briefs"] = briefs
    return {"ok": True, "briefs": briefs}


def save_history() -> None:
    if not LAST.get("keywords"):
        return
    import datetime
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    label = LAST["meta"].get("business") or ", ".join(LAST["meta"].get("seeds", []))
    city = LAST["meta"].get("city", "")
    sid = getattr(_ctx, "sid", "anon")
    folder = os.path.join(HISTORY_DIR, sid)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{ts}.json")
    slim = {
        "id": ts,
        "label": f"{label}{' · ' + city if city else ''}",
        "when": datetime.datetime.now().strftime("%d %b %Y, %H:%M"),
        "meta": LAST.get("meta"),
        "keywords": LAST.get("keywords"),
        "questions": LAST.get("questions"),
        "local": LAST.get("local"),
        "local_targets": LAST.get("local_targets"),
        "competitors": LAST.get("competitors"),
        "seeds_detail": LAST.get("seeds_detail"),
        "briefs": LAST.get("briefs"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False)


def export_xlsx(path: str) -> str:
    ensure_session_loaded()
    from xlsx_writer import Workbook
    d = LAST
    if not d:
        raise RuntimeError("run a research first")
    m = d["meta"]
    wb = Workbook()
    wb.props["title"] = f"Keywords — {m.get('business') or ', '.join(m['seeds'])}"

    s = wb.add_sheet("Best Keywords", freeze="A2", autofilter=True,
                     widths={"A": 5, "B": 46, "C": 10, "D": 11, "E": 10, "F": 26, "G": 30})
    s.row([f"KEYWORDS — {m.get('business') or ', '.join(m['seeds'])}".upper()], styles=["title"])
    s.row([f"Market: {m.get('market')}   City: {m.get('city') or '—'}   "
           f"Seeds: {', '.join(m['seeds'])}   "
           f"{m['keywords_found']} keywords from {m['queries_run']} live queries "
           f"({m['seconds']}s)"], styles=["muted"])
    s.blank()
    s.header(["#", "Keyword", "Demand", "Intent", "Evidence", "Found in", "Seed"])
    for i, r in enumerate(d["keywords"], 1):
        style = ("green_int" if r["demand"] >= 70 else
                 "amber_int" if r["demand"] >= 45 else "int")
        s.row([i, r["keyword"], r["demand"], r["intent"], r["evidence"],
               ", ".join(LABEL.get(x, x) for x in r["sources"]), r["seed"]],
              styles=["int", "text", style, "text", "int", "text", "text"])

    if d.get("keywords") and any(r.get("difficulty") is not None for r in d["keywords"]):
        op = wb.add_sheet("Opportunity", autofilter=True,
                          widths={"A": 46, "B": 10, "C": 11, "D": 11, "E": 12, "F": 70})
        op.header(["Keyword", "Demand", "Difficulty", "Level", "Opportunity", "Why it scores that way"])
        ranked = sorted(d["keywords"], key=lambda r: -(r.get("opportunity") or 0))
        for r in ranked:
            if r.get("opportunity") is None:
                continue
            st = ("green_int" if r["opportunity"] >= 65 else
                  "amber_int" if r["opportunity"] >= 45 else "int")
            op.row([r["keyword"], r["demand"], r["difficulty"], r["level"],
                    r["opportunity"], "; ".join(r.get("difficulty_reasons", []))],
                   styles=["text", "int", "int", "text", st, "wrap"])

    topics = analysis.build_topics(d["keywords"])
    t = wb.add_sheet("Topics", widths={"A": 30, "B": 10, "C": 12, "D": 13, "E": 13,
                                       "F": 14, "G": 70})
    t.header(["Topic (seed)", "Keywords", "Avg demand", "Main intent",
              "Best opportunity", "Avg difficulty", "Top keywords"])
    for x in topics:
        t.row([x["topic"], x["count"], x["avg_demand"], x["top_intent"],
               x["best_opportunity"] if x["best_opportunity"] is not None else "—",
               x["avg_difficulty"] if x["avg_difficulty"] is not None else "—",
               ", ".join(x["top_keywords"])],
              styles=["bold", "int", "num", "text", "int", "int", "wrap"])

    if d.get("briefs"):
        b = wb.add_sheet("Content briefs", widths={"A": 34, "B": 60, "C": 34, "D": 30,
                                                   "E": 60, "F": 60, "G": 10})
        b.header(["Keyword", "SEO title", "H1", "URL slug", "Outline", "Questions to answer",
                  "Words"])
        for x in d["briefs"]:
            b.row([x["keyword"], x["seo_title"], x["h1"], x["url_slug"],
                   "\n".join(f"{i}. {s}" for i, s in enumerate(x["outline"], 1)),
                   "\n".join(x["questions_to_answer"]), x["word_count_target"]],
                  styles=["text", "text", "text", "text", "wrap", "wrap", "int"])

    q = wb.add_sheet("Questions", autofilter=True, widths={"A": 70, "B": 10, "C": 10})
    q.header(["Question people type", "Demand", "Evidence"])
    for r in d["questions"]:
        q.row([r["keyword"], r["demand"], r["evidence"]], styles=["text", "int", "int"])

    if d["local"]:
        l = wb.add_sheet("Local", autofilter=True, widths={"A": 70, "B": 10, "C": 10})
        l.header(["Local keyword", "Demand", "Evidence"])
        for r in d["local"]:
            l.row([r["keyword"], r["demand"], r["evidence"]], styles=["text", "int", "int"])

    c = wb.add_sheet("Competitors", widths={"A": 30, "B": 12, "C": 100})
    c.header(["Domain ranking for your seeds", "Results", "Page titles"])
    for r in d["competitors"]:
        c.row([r["domain"], r["rankings"], " | ".join(r["titles"])],
              styles=["text", "int", "wrap"])

    h = wb.add_sheet("How to read", widths={"A": 3, "B": 26, "C": 95})
    h.row(["", "HOW THIS WAS BUILT", ""], styles=["default", "title", "title"])
    h.blank()
    for k, v in [
        ("Where the keywords come from",
         "Live Google autocomplete, YouTube autocomplete and DuckDuckGo — the queries people "
         "actually type, right now."),
        ("Demand score",
         "0-100 built from three real signals: how many different query stems returned the "
         "keyword, how high it ranked in Google's suggestion list, and whether a ranking page "
         "targets it. Google never publishes absolute volume, so this is a popularity ranking — "
         "not a traffic forecast."),
        ("Evidence",
         "How many distinct autocomplete requests returned this keyword. Higher = more certain."),
        ("Intent",
         "buy = ready to purchase, compare = evaluating options, local = city/near-me, "
         "learn = research stage."),
        ("Next step",
         "Paste this sheet's keywords into Google Keyword Planner for absolute volumes, or "
         "start with the top 20 — they are already proven demand."),
    ]:
        h.row(["", k, v], styles=["default", "bold", "wrap"])
    wb.save(path)
    return path


def export_csv() -> str:
    ensure_session_loaded()
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["keyword", "demand", "intent", "evidence", "sources", "seed", "serp_hit"])
    for r in LAST.get("keywords", []):
        w.writerow([r["keyword"], r["demand"], r["intent"], r["evidence"],
                    ";".join(r["sources"]), r["seed"], int(r["serp_hit"])])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Deployment controls: optional password, per-IP rate limit, session cookie
# ---------------------------------------------------------------------------
AUTH_USER = os.environ.get("AUTH_USER", "").strip()
AUTH_PASS = os.environ.get("AUTH_PASS", "").strip()
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "25"))        # research runs / hour / IP
RATE_WINDOW = 3600
_hits = {}
_hits_lock = threading.Lock()


def rate_ok(ip: str) -> bool:
    now = time.time()
    with _hits_lock:
        arr = [t for t in _hits.get(ip, []) if now - t < RATE_WINDOW]
        if len(arr) >= RATE_LIMIT:
            _hits[ip] = arr
            return False
        arr.append(now)
        _hits[ip] = arr
        return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def log_error(self, *a):
        pass

    def handle_one_request(self):
        # Load balancers and previews probe with odd requests; never let a
        # broken pipe or malformed probe spam the logs.
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    # ---- auth ----
    def _authorised(self) -> bool:
        if not (AUTH_USER and AUTH_PASS):
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        import base64
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return secrets.compare_digest(user, AUTH_USER) and \
            secrets.compare_digest(pw, AUTH_PASS)

    def _deny(self):
        self._send(401, "Authentication required", "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="Keyword Research"'})

    # ---- session ----
    def _bind_session(self):
        cookie = self.headers.get("Cookie", "")
        sid = ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "kw_sid" and v:
                sid = v
        if not sid or len(sid) < 8:
            sid = secrets.token_hex(9)
            self._new_cookie = sid
        else:
            self._new_cookie = None
        _ctx.sid = sid
        _ctx.session = session_for(sid)
        return sid

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "_new_cookie", None):
            self.send_header("Set-Cookie",
                             f"kw_sid={self._new_cookie}; Path=/; Max-Age=2592000; "
                             "SameSite=Lax")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/health":
            return self._send(200, json.dumps({"ok": True, "sessions": len(SESSIONS),
                                               "version": "1.0"}),
                              "application/json; charset=utf-8")
        if not self._authorised():
            return self._deny()
        self._bind_session()
        if p in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if p == "/api/history":
            items = []
            folder = os.path.join(HISTORY_DIR, getattr(_ctx, "sid", "anon"))
            files = (sorted(os.listdir(folder), reverse=True)[:30]
                     if os.path.isdir(folder) else [])
            for fn in files:
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(folder, fn), encoding="utf-8") as f:
                        j = json.load(f)
                    items.append({"id": j.get("id", fn[:-5]), "label": j.get("label", fn),
                                  "when": j.get("when", ""),
                                  "keywords": len(j.get("keywords") or [])})
                except Exception:
                    continue
            return self._send(200, json.dumps({"ok": True, "items": items}),
                              "application/json; charset=utf-8")
        if p == "/api/history/load":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            hid = (qs.get("id") or [""])[0]
            path = os.path.join(HISTORY_DIR, getattr(_ctx, "sid", "anon"),
                                f"{hid}.json")
            if not os.path.exists(path):
                return self._send(404, "{}", "application/json")
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
            LAST.clear()
            LAST.update({k: v for k, v in j.items() if k not in ("id", "when", "label")})
            return self._send(200, json.dumps({"ok": True, **j}),
                              "application/json; charset=utf-8")
        if p == "/api/export.csv":
            if not LAST:
                return self._send(400, "{}", "application/json")
            return self._send(200, export_csv(), "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="keywords.csv"'})
        if p == "/api/export.xlsx":
            if not LAST:
                return self._send(400, "{}", "application/json")
            path = os.path.join(ROOT, "keywords.xlsx")
            export_xlsx(path)
            with open(path, "rb") as f:
                body = f.read()
            return self._send(200, body,
                              "application/vnd.openxmlformats-officedocument."
                              "spreadsheetml.sheet",
                              {"Content-Disposition": 'attachment; filename="keywords.xlsx"'})
        return self._send(404, "{}", "application/json")

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if not self._authorised():
            return self._deny()
        self._bind_session()
        if p == "/api/research":
            ip = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or self.client_address[0])
            if not rate_ok(ip):
                return self._send(429, json.dumps({
                    "ok": False,
                    "error": f"Rate limit: {RATE_LIMIT} searches per hour. Try again later."}),
                    "application/json; charset=utf-8")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}

        if p == "/api/difficulty":
            try:
                out = score_difficulty(body.get("keywords") or [])
            except Exception:
                traceback.print_exc()
                out = {"ok": False, "error": "difficulty check failed"}
            return self._send(200, json.dumps(out), "application/json; charset=utf-8")

        if p == "/api/briefs":
            return self._send(200, json.dumps(build_briefs(int(body.get("count") or 15))),
                              "application/json; charset=utf-8")

        if p == "/api/competitor":
            try:
                ensure_session_loaded()
                out = cmpmod.mine(body.get("domain", ""))
                if out.get("ok") and LAST.get("keywords"):
                    ours = [r["keyword"] for r in LAST["keywords"]]
                    diff = cmpmod.compare_with_own(out["keywords"], ours)
                    out["gaps"] = diff["gaps"][:60]
                    out["shared_count"] = len(diff["shared"])
                else:
                    out["gaps"] = out.get("keywords", [])
            except Exception:
                traceback.print_exc()
                out = {"ok": False, "error": "could not read that site"}
            return self._send(200, json.dumps(out), "application/json; charset=utf-8")

        if p == "/api/export-selected.csv":
            import csv as _csv
            import io as _io
            keys = set(body.get("keywords") or [])
            buf = _io.StringIO()
            w = _csv.writer(buf)
            w.writerow(["keyword", "demand", "opportunity", "difficulty", "intent",
                        "evidence", "sources", "seed"])
            for r in LAST.get("keywords", []):
                if r["keyword"] in keys:
                    w.writerow([r["keyword"], r["demand"], r.get("opportunity", ""),
                                r.get("difficulty", ""), r["intent"], r["evidence"],
                                ";".join(r["sources"]), r["seed"]])
            return self._send(200, buf.getvalue(), "text/csv; charset=utf-8",
                              {"Content-Disposition":
                               'attachment; filename="selected-keywords.csv"'})

        if p != "/api/research":
            return self._send(404, "{}", "application/json")

        # ---- input limits: this endpoint is public ----
        if n > 64_000:
            return self._send(413, json.dumps({"ok": False, "error": "Payload too large."}),
                              "application/json; charset=utf-8")
        seeds = [str(s).strip()[:60] for s in (body.get("seeds") or []) if str(s).strip()]
        body["seeds"] = seeds[:4]
        body["city"] = str(body.get("city") or "").strip()[:40]
        body["market"] = str(body.get("market") or "in")[:10]
        if not body["seeds"]:
            return self._send(400, json.dumps({"ok": False, "error": "Tell me what you sell."}),
                              "application/json; charset=utf-8")

        key = cache_key(body)
        cached = cache_get(key)
        if cached:
            payload = dict(cached)
            payload["cached"] = True
            return self._send(200, json.dumps(payload, ensure_ascii=False),
                              "application/json; charset=utf-8")
        try:
            out = run_research(
                business=body.get("business", ""),
                seeds=body.get("seeds") or [],
                city=body.get("city", ""),
                market=body.get("market", "in"),
                deep=bool(body.get("deep", True)),
            )
            if out.get("ok"):
                LAST.clear()
                LAST.update(out)
                LAST["topics"] = analysis.build_topics(LAST["keywords"])
                cache_put(key, {k: v for k, v in out.items()})
                try:
                    save_history()
                except Exception:
                    traceback.print_exc()
            self._send(200, json.dumps(out, ensure_ascii=False),
                       "application/json; charset=utf-8")
        except Exception:
            traceback.print_exc()
            self._send(500, json.dumps({"ok": False, "error": "research failed"}),
                       "application/json; charset=utf-8")


def main():
    port = int(os.environ.get("PORT", 8100))
    host = os.environ.get("HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    auth = "ON" if (AUTH_USER and AUTH_PASS) else "off"
    print(f"keyword research tool -> http://{host}:{port}  (password: {auth}, "
          f"serpapi: {'on' if SERPAPI_KEY else 'off'}, rate limit: {RATE_LIMIT}/h)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
