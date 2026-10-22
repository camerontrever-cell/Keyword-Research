"""
analysis.py — difficulty, opportunity, topics and content briefs.

Everything here is derived from data the tool actually fetched: the SERP a
keyword returns, the demand score from autocomplete signals, and the keywords
themselves. No black boxes — each difficulty score carries the reasons for it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

# Domains that are structurally hard to outrank for a small business.
AUTHORITY = {
    "wikipedia.org", "youtube.com", "facebook.com", "instagram.com", "linkedin.com",
    "healthline.com", "webmd.com", "mayoclinic.org", "nih.gov", "who.int", "cdc.gov",
    "forbes.com", "nerdwallet.com", "investopedia.com", "britannica.com", "medium.com",
    "quora.com", "reddit.com", "pinterest.com", "twitter.com", "x.com", "tiktok.com",
    "businessinsider.com", "economictimes.indiatimes.com", "timesofindia.indiatimes.com",
    "thehindu.com", "ndtv.com", "hindustantimes.com", "gov.in", "nic.in",
}
MARKETPLACE = {
    "amazon.in", "amazon.com", "flipkart.com", "etsy.com", "ebay.com", "myntra.com",
    "nykaa.com", "1mg.com", "pharmeasy.in", "netmeds.com", "indiamart.com",
    "urbancompany.com", "olx.in", "quikr.com", "99acres.com", "magicbricks.com",
    "naukri.com", "indeed.com", "makemytrip.com", "booking.com", "agoda.com",
    "zomato.com", "swiggy.com", "bigbasket.com", "jiomart.com", "meesho.com",
    "ajio.com", "tatacliq.com", "croma.com", "reliancedigital.in",
}
LOCAL_DIRECTORY = {
    "justdial.com", "practo.com", "sulekha.com", "lybrate.com", "drlogy.com",
    "credihealth.com", "medindia.net", "yellowpages.co.in", "indiacom.com",
    "google.com", "maps.google.com", "yelp.com", "tripadvisor.com", "asklaila.com",
    "grotal.com", "bizidex.com", "cybo.com", "vymaps.com",
}
UGC = {"reddit.com", "quora.com", "youtube.com", "stackexchange.com", "stackoverflow.com",
       "forum.", "blogspot.com", "wordpress.com", "medium.com"}

# Words that never carry topical meaning for titles/outlines
FILLER = set("""a an the and or of for to in on at with from by is are was were be been
best top cheap affordable near me online buy price cost prices costs fee fees rate rates
charges review reviews vs versus which what how why when where can does do i my you your
it its india indian new 2025 2026""".split())


def _base(domain: str) -> str:
    return domain.lower().replace("www.", "").strip()


def _match(domain: str, table: set) -> bool:
    d = _base(domain)
    for t in table:
        if t in d:
            return True
    return False


def classify_domain(domain: str) -> str:
    if _match(domain, LOCAL_DIRECTORY):
        return "directory"
    if _match(domain, MARKETPLACE):
        return "marketplace"
    if _match(domain, AUTHORITY):
        return "authority"
    if _match(domain, UGC):
        return "ugc"
    return "independent"


def keyword_tokens(kw: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", kw.lower()) if t not in FILLER and len(t) > 2}


def score_difficulty(keyword: str, results: list[dict]) -> dict:
    """
    Difficulty 0-100 from the actual top-10 you would have to beat.

    Signals, and why they matter:
      - directories/aggregators (Practo, Justdial) already own most local
        commercial queries in India, and they are hard to displace.
      - an exact-phrase page in the top 3 means Google is satisfied by a
        dedicated page -- you need a better one, not a mention.
      - forums/UGC in the top 10 is the opposite: Google is settling for
        discussion, which means weak commercial content exists to beat.
      - a short result set means a thin niche (easier) and also less demand.
    """
    if not results:
        return {"difficulty": None, "level": "unknown", "reasons": ["no SERP data"],
                "top": []}

    rows = results[:10]
    counts = Counter(classify_domain(r["domain"]) for r in rows)
    toks = keyword_tokens(keyword)

    exact = 0
    for r in rows[:3]:
        t = keyword_tokens(r.get("title", ""))
        if toks and toks <= t:
            exact += 1

    score = 26.0
    reasons = []

    hard_hits = counts["directory"] + counts["marketplace"] + counts["authority"]
    if hard_hits:
        score += min(38, hard_hits * 6.5)
        named = []
        for r in rows:
            if classify_domain(r["domain"]) in ("directory", "marketplace", "authority"):
                d = _base(r["domain"]).split(".")[0]
                if d not in named:
                    named.append(d)
        reasons.append(f"{hard_hits} big site(s) in the top 10 ({', '.join(named[:3])}) — "
                       "you must beat their domain strength")

    if exact:
        score += 12 * exact
        reasons.append(f"{exact} page(s) in the top 3 match your exact phrase — "
                       "you need a more complete page, not a mention")

    if counts["ugc"]:
        score -= 9
        reasons.append("forums/video rank here — Google is settling for discussion, "
                       "so a solid page can break in")

    indep = counts["independent"]
    if indep >= 4:
        score -= 6
        reasons.append(f"{indep} small/independent sites rank — the SERP is not locked up")

    if len(rows) < 6:
        score -= 7
        reasons.append("few results returned — thin niche, low competition")

    score = max(5, min(95, round(score)))
    level = "easy" if score < 40 else "medium" if score < 65 else "hard"
    if not reasons:
        reasons.append("mixed SERP with no strong pattern")

    return {
        "difficulty": score,
        "level": level,
        "reasons": reasons,
        "top": [{"domain": r["domain"], "type": classify_domain(r["domain"]),
                 "title": r.get("title", "")} for r in rows[:5]],
    }


def opportunity(demand: float | None, difficulty: int | None) -> int | None:
    """Demand you can actually capture: high demand + low difficulty."""
    if demand is None or difficulty is None:
        return None
    return round(0.62 * demand + 0.38 * (100 - difficulty))


def add_scores(keywords: list[dict], diffs: dict) -> list[dict]:
    for r in keywords:
        d = diffs.get(r["keyword"])
        if not d:
            continue
        r["difficulty"] = d["difficulty"]
        r["level"] = d["level"]
        r["difficulty_reasons"] = d["reasons"]
        r["serp_top"] = d["top"]
        r["opportunity"] = opportunity(r.get("demand"), d["difficulty"])
    return keywords


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------
def build_topics(keywords: list[dict]) -> list[dict]:
    """
    Group by the seed that produced them. Deliberately not fuzzy clustering:
    with 3-4 seeds the user already thinks in these buckets, and a topic they
    cannot predict is worse than no topic at all.
    """
    groups = defaultdict(list)
    for r in keywords:
        groups[r.get("seed") or "other"].append(r)

    out = []
    for name, rows in groups.items():
        rows.sort(key=lambda r: -(r.get("opportunity") or r.get("demand") or 0))
        intents = Counter(r["intent"] for r in rows)
        scored = [r for r in rows if r.get("opportunity") is not None]
        out.append({
            "topic": name,
            "count": len(rows),
            "avg_demand": round(sum(r["demand"] for r in rows) / len(rows), 1),
            "top_intent": intents.most_common(1)[0][0],
            "best_opportunity": (max((r["opportunity"] for r in scored), default=None)),
            "avg_difficulty": (round(sum(r["difficulty"] for r in scored) / len(scored))
                               if scored else None),
            "top_keywords": [r["keyword"] for r in rows[:6]],
            "easy_wins": [r["keyword"] for r in rows
                          if r.get("level") == "easy"][:5],
        })
    out.sort(key=lambda t: -t["count"])
    return out


# ---------------------------------------------------------------------------
# Content briefs
# ---------------------------------------------------------------------------
def _title_case(s: str) -> str:
    small = {"a", "an", "the", "in", "of", "for", "and", "or", "to", "on", "at", "vs"}
    words = s.split()
    out = []
    for i, w in enumerate(words):
        out.append(w.capitalize() if (i == 0 or w.lower() not in small) else w.lower())
    return " ".join(out)


def _related_questions(kw: str, questions: list[dict], limit=6) -> list[str]:
    toks = keyword_tokens(kw)
    scored = []
    for q in questions:
        qt = keyword_tokens(q["keyword"])
        if not qt:
            continue
        overlap = len(toks & qt)
        if overlap:
            scored.append((overlap, q["demand"], q["keyword"]))
    scored.sort(reverse=True)
    return [q for _, _, q in scored[:limit]]


def build_brief(row: dict, questions: list[dict], city: str = "",
                business: str = "") -> dict:
    kw = row["keyword"]
    intent = row.get("intent", "learn")
    # never append a city to a "near me" phrase -- it already localises itself
    already_local = ("near me" in kw) or (city and city.lower() in kw)
    loc = f" in {_title_case(city)}" if (city and not already_local) else ""
    K = _title_case(kw)

    if intent == "buy":
        title = f"{K}{loc}: Price, Options & What to Expect"
        h1 = f"{K}{loc}"
        sections = ["Short answer: typical price range, above the fold",
                    "Price table by option / brand / size",
                    "What changes the price",
                    "The 3 options compared",
                    "Process and timeline (step by step)",
                    "How to choose / red flags",
                    "FAQ"]
        meta = f"What {kw} costs{loc or ' today'}, what drives the price, and how to choose."
    elif intent == "compare":
        title = f"{K}: Differences, Costs & Which to Choose{loc}"
        h1 = f"{K}"
        sections = ["Quick verdict box (2-3 lines)",
                    "Side-by-side comparison table",
                    "Cost comparison",
                    "Results, comfort and duration",
                    "Who should pick which",
                    "FAQ"]
        meta = f"{kw} compared on cost, results and suitability{loc}."
    elif intent == "local":
        title = f"{K} — Prices, Reviews & Booking"
        h1 = f"{K}"
        sections = ["Who it is for and what is included",
                    "Location, hours and how to reach",
                    "Price list",
                    "Before / after or results proof",
                    "Reviews and credentials",
                    "Book or call CTA",
                    "FAQ"]
        meta = f"{kw} — prices, timings and booking."
    else:
        title = f"{K}: A Complete Guide"
        h1 = f"{K}"
        sections = ["Answer the question in the first 2 sentences",
                    "What it is / how it works",
                    "Benefits",
                    "Risks, limits or side effects",
                    "Who it is right for",
                    "Cost and next step",
                    "FAQ"]
        meta = f"{kw} explained simply — what it is, how it works, what it costs."

    return {
        "keyword": kw,
        "intent": intent,
        "demand": row.get("demand"),
        "difficulty": row.get("difficulty"),
        "opportunity": row.get("opportunity"),
        "title": title[:60],
        "seo_title": title,
        "h1": h1,
        "url_slug": "/" + re.sub(r"[^a-z0-9]+", "-", kw).strip("-"),
        "meta_description": meta[:155],
        "outline": sections,
        "questions_to_answer": _related_questions(kw, questions),
        "word_count_target": {"buy": 1200, "compare": 1400, "local": 900,
                              "learn": 1500}.get(intent, 1200),
        "business": business,
    }
