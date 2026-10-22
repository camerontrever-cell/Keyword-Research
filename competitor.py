"""
competitor.py — what keywords is that competitor actually targeting?

Walks a competitor's sitemap (including sitemap-index and .gz sitemaps), turns
their URLs into keyword phrases, and reads page titles for the top few pages,
because a title usually states the phrase the page is chasing.

This is the "look at what already works" step: a competitor who publishes a page
for "clear aligners cost in jodhpur" has already decided that keyword matters.
"""

from __future__ import annotations

import gzip
import html as _html
import io
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SITEMAP_PATHS = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                 "/wp-sitemap.xml", "/sitemap.xml.gz", "/sitemap-pages.xml"]

# URL segments that are site furniture, not content
SKIP_SEGMENTS = {"tag", "tags", "author", "category", "categories", "page", "feed",
                 "search", "cart", "checkout", "account", "login", "privacy",
                 "terms", "refund", "policy", "disclaimer", "sitemap", "amp",
                 "comment-page", "wp-content", "wp-json", "cdn", "assets", "static"}

SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js", ".pdf",
            ".xml", ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".zip")

NUMERIC = re.compile(r"^\d+$")

# Pages that exist on every site and target no keyword
NOT_TARGETS = {"about", "about us", "contact", "contact us", "gallery", "clinic gallery",
               "faq", "faqs", "reviews", "patient reviews", "testimonials", "privacy",
               "privacy policy", "terms", "terms conditions", "home", "blog", "index",
               "thank you", "sitemap", "disclaimer", "refund policy", "our team", "team",
               "careers", "login", "sign up", "cart", "checkout"}


def _is_target(kw: str) -> bool:
    k = re.sub(r"[^a-z ]", " ", kw.lower()).strip()
    k = re.sub(r"\s+", " ", k)
    if k in NOT_TARGETS:
        return False
    for t in NOT_TARGETS:
        if len(t) > 4 and k.startswith(t + " "):
            return False
    if len(k.split()) == 1 and len(k) < 5:
        return False
    return True


def _fetch(url: str, timeout=12) -> bytes | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw
    except Exception:
        return None


def _text(url: str) -> str | None:
    raw = _fetch(url)
    if not raw:
        return None
    for enc in ("utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return None


def _locs(xml: str) -> list[str]:
    return [m.strip() for m in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.S | re.I)]


def normalise_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).split("/")[0]
    return d


def find_sitemaps(domain: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, sitemap_urls_tried)."""
    base = "https://" + normalise_domain(domain)
    tried, pages = [], []
    index_url = None

    for p in SITEMAP_PATHS:
        url = base + p
        tried.append(url)
        xml = _text(url)
        if not xml:
            continue
        if "<sitemapindex" in xml.lower():
            index_url = url
            childs = _locs(xml)[:10]
            for c in childs:
                tried.append(c)
                cxml = _text(c)
                if cxml:
                    pages.extend(_locs(cxml))
            if pages:
                break
        elif "<urlset" in xml.lower():
            pages.extend(_locs(xml))
            if pages:
                break

    # de-dupe, keep order
    seen, out = set(), []
    for u in pages:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out, tried


def url_to_keyword(url: str) -> str | None:
    path = urllib.parse.urlparse(url).path
    if any(path.lower().endswith(e) for e in SKIP_EXT):
        return None
    segs = [s for s in path.split("/") if s and not NUMERIC.match(s)]
    if not segs:
        return None
    if any(s.lower() in SKIP_SEGMENTS for s in segs):
        return None
    seg = segs[-1]
    seg = urllib.parse.unquote(seg)
    seg = re.sub(r"\.(html?|php|aspx?)$", "", seg, flags=re.I)
    kw = re.sub(r"[-_+]+", " ", seg)
    kw = re.sub(r"[^A-Za-z\u0900-\u097F\s]", " ", kw)
    kw = re.sub(r"\b\d{4,}\b", " ", kw)
    kw = re.sub(r"\s+", " ", kw).strip().lower()
    words = kw.split()
    if not (1 <= len(words) <= 10) or len(kw) < 4:
        return None
    if len(words) == 1 and len(kw) < 7:
        return None
    return kw


def page_title(url: str) -> str | None:
    html = _text(url)
    if not html:
        return None
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not m:
        return None
    t = re.sub(r"<[^>]+>", "", m.group(1))
    t = _html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def mine(domain: str, title_limit: int = 18) -> dict:
    d = normalise_domain(domain)
    if not d or "." not in d:
        return {"ok": False, "error": "Enter a domain like clinicx.in"}

    pages, tried = find_sitemaps(d)
    if not pages:
        return {"ok": False,
                "error": f"No sitemap found at {d}. Try the exact domain, or paste a page URL.",
                "tried": tried[:6]}

    # keep content-looking URLs only
    content = []
    for u in pages:
        p = urllib.parse.urlparse(u).path.lower()
        if any(p.endswith(e) for e in SKIP_EXT):
            continue
        if any(seg in p.split("/") for seg in SKIP_SEGMENTS):
            continue
        content.append(u)

    # titles tell us the phrase each page is chasing
    sample = content[:title_limit]
    titles = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for u, t in zip(sample, pool.map(page_title, sample)):
            if t:
                titles[u] = t

    rows, seen = [], set()
    for u in content:
        kw = url_to_keyword(u)
        title = titles.get(u)
        # prefer the title when it is short and phrase-like, else the slug
        cand = kw
        if title:
            clean = re.sub(r"\s*[|\-–—:]\s*[^|\-–:–—]{0,25}$", "", title)
            clean = clean.replace("&amp;", " and ")
            clean = re.sub(r"\bamp\b", "and", clean)
            clean = re.sub(r"[^\w\s\u0900-\u097F]", " ", clean).strip().lower()
            clean = re.sub(r"\s+", " ", clean)
            if 1 <= len(clean.split()) <= 9:
                cand = clean
        if not cand or cand in seen or not _is_target(cand):
            continue
        seen.add(cand)
        rows.append({"keyword": cand, "url": u, "title": title or "", "slug": kw or ""})

    return {
        "ok": True,
        "domain": d,
        "sitemap_urls": len(tried),
        "pages_found": len(pages),
        "titles_read": len(titles),
        "keywords": rows[:120],
    }


def compare_with_own(competitor_rows: list[dict], our_keywords: list[str]) -> dict:
    """
    Which of their targeted phrases did you miss? Token-overlap matching, so
    "implant cost jodhpur" matches "dental implant cost in jodhpur".
    """
    ours = [set(re.findall(r"[a-z0-9]+", k.lower())) for k in our_keywords]
    ours_sets = [s - {"in", "the", "a", "for", "near", "me", "best", "of"} for s in ours]
    gaps, shared = [], []
    for r in competitor_rows:
        t = set(re.findall(r"[a-z0-9]+", r["keyword"].lower()))
        t -= {"in", "the", "a", "for", "near", "me", "best", "of"}
        if not t:
            continue
        hit = any(t <= s or (len(t & s) / max(1, len(t))) >= 0.75 for s in ours_sets)
        (shared if hit else gaps).append(r)
    return {"gaps": gaps, "shared": shared}
