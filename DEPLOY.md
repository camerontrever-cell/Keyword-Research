# Deploy as a public website

You upload the files to GitHub, connect the repo to a host, and get a public link.
Everything needed is already in this folder.

## Files that matter

```
app.py            server + live search sources
analysis.py       difficulty, opportunity, topics, briefs
competitor.py     sitemap mining
xlsx_writer.py    Excel writer (no dependencies)
index.html        the whole UI
Dockerfile        how the host builds it
render.yaml       Render config (optional, Render can auto-detect)
.env.example      every setting you can change
.gitignore        keeps runtime junk out of your repo
```

`history/`, `exports/` and `__pycache__/` are git-ignored — don't commit them.

---

## Step 1 — Put it on GitHub

**In the browser (no git needed):**
1. github.com → **New repository** → name it `keyword-research` → **Public** → Create.
2. On the empty repo page click **uploading an existing file**.
3. Drag in these 9 files: `app.py`, `analysis.py`, `competitor.py`, `xlsx_writer.py`,
   `index.html`, `Dockerfile`, `render.yaml`, `.env.example`, `.gitignore`, plus
   `README.md` and `DEPLOY.md` if you want them visible.
4. Commit.

> `.gitignore` starts with a dot — your file picker may hide it. Enable "show hidden files"
> or GitHub's own "Add file → Create new file" and paste its contents.

**Or with git:**
```bash
cd kw-research
git init && git add . && git commit -m "keyword research tool"
git branch -M main
git remote add origin https://github.com/<you>/keyword-research.git
git push -u origin main
```

---

## Step 2 — Get the public link (Render, free)

1. Sign up at **render.com** with your GitHub account.
2. **New → Web Service** → pick your repo.
3. Settings it should auto-detect or you set:
   - **Language / Runtime:** Docker
   - **Instance type:** Free
   - **Health check path:** `/health`
4. **Environment variables** (Advanced → Add):

   | Key | Value | Why |
   |---|---|---|
   | `RATE_LIMIT` | `25` | searches per hour, per visitor IP |
   | `SERPAPI_KEY` | *(your key, optional but recommended)* | real Google results instead of scraping |
   | `AUTH_USER` | *(optional)* | set only if you want a password prompt |
   | `AUTH_PASS` | *(optional)* | use a long random string |
   | `CACHE_TTL` | `21600` | seconds a repeated search is served from memory |

5. **Create Web Service** → first build takes ~2 minutes.
6. You get `https://keyword-research-xxxx.onrender.com`. **That's your public link.**

**Test it immediately:**
```bash
curl https://YOUR-LINK.onrender.com/health
# {"ok": true, "sessions": 0, "version": "1.0"}
```
Then open the link and press **Try an example**.

### Free-tier realities (know these before you share the link)

| Reality | What it means for you |
|---|---|
| **Sleeps after 15 min idle** | First visit after a quiet period takes ~50 seconds to wake. Paid plan removes it. |
| **512 MB RAM, one instance** | Fine for tens of visitors a day. Session cap is 400. |
| **Ephemeral disk** | A restart wipes `history/`. Searches that were already run come back via an in-memory cache only while the instance lives. Visitors just re-run their search. |
| **Shared datacenter IP** | Google/DDG will rate-limit the *scraper*. This is the big one — see Step 3. |

---

## Step 3 — Make the live data survive public traffic (important)

Right now SERP lookups use Bing RSS, then DuckDuckGo. From a datacenter IP, under many
visitors, those get throttled and keywords start showing **"not checked"**.

Fix: get a [SerpApi](https://serpapi.com) key (100 searches/month free, then ~$50/mo for
5,000) and add it as `SERPAPI_KEY`. The app switches to it automatically — no code change:

```python
rows = serpapi_serp(q, market)   # if SERPAPI_KEY is set
if not rows: rows = bing_serp()  # else scrape
```

Autocomplete (the keyword discovery itself) keeps working either way, because it is a
public endpoint that does not block servers the way SERPs do.

If you'd rather not pay yet: publish with `RATE_LIMIT=10` so you get few enough requests
that throttling rarely triggers, and accept that difficulty checks sometimes say "not checked".

---

## Step 4 — Before you share the link publicly

- [ ] `/health` returns 200
- [ ] **Try an example** works end to end (keywords → difficulty → briefs → Excel)
- [ ] You've decided: open to everyone, or password-protected (`AUTH_USER`/`AUTH_PASS`)
- [ ] `RATE_LIMIT` matches how much upstream traffic you can afford
- [ ] One sentence somewhere saying demand is a 0–100 popularity score, not Google volume
      (already in the page footer)
- [ ] You know the free-tier limits above, so the first slow load doesn't surprise visitors

---

## Optional: your own domain

Render → your service → **Settings → Custom Domains** → add `keywords.yourdomain.com` →
create the CNAME it shows at your registrar. Free SSL is automatic.

---

## Alternative hosts (same repo, same Dockerfile)

| Host | Free tier | Notes |
|---|---|---|
| **Render** | Yes, sleeps | Easiest. `render.yaml` included. |
| **Fly.io** | Limited free allowance | `fly launch` → `fly deploy`; better if you want no sleep. |
| **Railway** | Trial credit | Very fast setup, then usage-based. |
| **Koyeb** | Yes, small instance | Good sleep behaviour. |
| **Hugging Face Spaces** | Yes | Use the Docker SDK; public by default. |

### Fly.io version
```bash
fly launch --dockerfile Dockerfile    # accept the defaults
fly secrets set SERPAPI_KEY=xx RATE_LIMIT=25
fly deploy
fly open
```

---

## Updating the site later

Edit a file on GitHub → commit. Render redeploys automatically in ~1–2 minutes.
Or locally: `git add . && git commit -m "..." && git push`.

---

## If something breaks

| Symptom | Cause | Fix |
|---|---|---|
| 502 / blank page for ~1 min | free tier waking up | wait, or move to a paid plan |
| "No live data came back" | upstream throttled / blocked | set `SERPAPI_KEY`, lower `RATE_LIMIT` |
| Difficulty mostly "not checked" | SERP IP throttling | same as above |
| Everyone's past searches empty | restart wiped disk (free tier) | expected; paid disk keeps it |
| 429 "Rate limit" | `RATE_LIMIT` reached for that IP | raise it, or add a login |
| Excel downloads empty | no search run in that session | run a search, then export |
| Build fails | a file wasn't uploaded | repo must contain `app.py` and `index.html` at the root |

---

## What this costs to run

| Item | Cost |
|---|---|
| Render free tier | ₹0 |
| Render Starter (no sleeping) | ~$7/month |
| SerpApi (5,000 SERP lookups) | ~$50/month |
| Domain (optional) | ~₹800/year |

Minimum viable public site: **₹0/month**, with sleeping and occasional "not checked".
Comfortable: **~$57/month** (always-on + SERP API).
