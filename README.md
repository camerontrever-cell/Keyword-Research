# Keyword Research

One screen. Type what you sell → it searches the internet live → keywords, topics,
difficulty, content briefs, competitor gaps, Excel export.

```bash
python3 app.py        # http://localhost:8100
```

No accounts, no API keys, no dependencies (Python standard library only).

## What it does

For each product/service you enter it fires ~150–250 live queries at **Google
Autocomplete**, **YouTube Autocomplete** and **DuckDuckGo Autocomplete**, then scores
and groups what comes back.

Query stems: the seed, 24 suffixes (price, cost, near me, vs, without, at home, package,
charges…), the seed + your city in 3 word orders, 12 question prefixes, 6 modifiers, and
an a–z alphabet sweep for the first two seeds. Foreign-city variants are filtered out.

## Tabs

| Tab | What's in it |
|---|---|
| **Best keywords** | Sortable, filterable list. Demand, opportunity, difficulty, intent, evidence. Click a keyword to open its live SERP. |
| **Topics** | The list grouped by product/service, with counts, intent mix and the best opportunity per topic. |
| **Content briefs** | For your top keywords: SEO title, H1, URL slug, meta description, outline and the questions the page must answer. Also exported to Excel. |
| **Questions** | The how/what/why phrasing, ready for FAQ or blog briefs. |
| **Local** | The city + service pages you must own, plus every near-me keyword found live. |
| **Competitors** | Paste a competitor domain → it reads their sitemap and page titles and shows the phrases they target, flagging the ones you missed. |

## The three scores

**Demand (0–100)** — Google publishes no volume, so this is built from real autocomplete
signals: how many different searches returned the keyword (55%), how high it ranked in
Google's suggestion list (25%), agreement across Google/YouTube/DuckDuckGo (8%), and
whether a page already ranks for it (12%). `Evidence` = the number of searches that
returned it.

**Difficulty (0–100)** — fetched live per keyword: how many big sites (marketplaces,
directories, authorities) are in the top 10, whether pages match the exact phrase, whether
forums rank (a weak SERP you can beat), and how thin the result set is. Every score carries
its reasons — hover the tag. If the SERP can't be fetched, it says **not checked** instead
of inventing a number.

**Opportunity** — `0.62 × demand + 0.38 × (100 − difficulty)`. Demand you can actually
capture. This is the default sort.

## Extras

- **Hide junk / hide brand terms** — drops "free, pdf, jobs, courses…" and competitor brand
  searches.
- **Select rows → Export selected** — CSV of just the ones you want.
- **Past searches** — every run is saved to `history/`; pick one from the dropdown to reload.

## Deploying it publicly

See **DEPLOY.md** — upload to GitHub, connect to Render, get a public URL. The app already
has password protection, per-visitor sessions, rate limiting, a research cache, a `/health`
endpoint and a Dockerfile.

```bash
# run it locally the same way the host does
PORT=8100 python3 app.py
```

## Files

```
app.py          server + live data sources + research plan
analysis.py     difficulty, opportunity, topics, content briefs
competitor.py   sitemap mining and gap comparison
xlsx_writer.py  dependency-free XLSX writer
index.html      the whole UI
history/        saved runs (per visitor, git-ignored)
Dockerfile      production build
DEPLOY.md       public deployment guide
.env.example    all configuration options
```

## Honest limits

- Demand is a **popularity ranking, not a traffic forecast**. Export to Keyword Planner if
  you need absolute monthly volume.
- Difficulty uses Bing's index and reflects the global SERP; for local intent include the
  city in the keyword (the Local tab gives you those exact phrases).
- Search engines rate-limit server IPs, so a few difficulty checks may fail. It runs best
  on your own machine, and failed checks are always labelled as such — never guessed.
