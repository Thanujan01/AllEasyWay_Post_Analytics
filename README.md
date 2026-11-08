# AllEasyWay Post Analytics

Daily posting monitor for [alleasyway.com](https://alleasyway.com) — listings, reels and
community posts per day, in Sri Lanka time, filterable by country, with per-post
drill-down.

**Live dashboard:** https://thanujan01.github.io/AllEasyWay_Post_Analytics/

## Whole days only

The collector runs once a day, just after midnight Sri Lanka time, and the dashboard
shows days that have **finished**. On the 8th the newest day on the page is the 7th.

Today's posts are still collected and stored — they are simply held back until the day
is over, so no bar on the chart is ever a half-counted day that looks like a slump.

## How it works

`collector.py` reads four sources and merges them into `data.json`:

| Source | What it gives |
|---|---|
| `/api/feed` | Listings and product reels, 30 per page, offset paging |
| `/api/reels` | Every reel, including standalone ones the feed leaves out |
| `/communaute` | The latest community posts |
| `/sitemap.xml` | The site's own inventory — anything the feeds missed, fetched one post at a time |

The sitemap pass is what makes the numbers trustworthy. Whatever the feeds skip is still
listed there, so each run both fills the gap and reports what it could not resolve. The
`coverage` figure in each run entry is the share of sitemap posts the dataset holds, not
a guess from ad-number gaps.

Posts are keyed by the site's own sequential ad number, so:

- re-running is idempotent — a post already recorded is never duplicated;
- first sighting wins for the timestamp, so a post counted once stays in history even if
  the seller later deletes it, and a finished day's count can never be rewritten;
- every other field is refreshed when a post is seen again, so corrections on the site
  reach records that were collected earlier.

A run that cannot reach the site writes nothing and exits non-zero. A failed crawl can
never be mistaken for a quiet day.

It then regenerates `index.html` from `dashboard_template.html`. The dashboard is a
single self-contained file: no server, no build step, no dependencies.

## Running it yourself

Python 3.8+ and the standard library — nothing to install.

```bash
python3 collector.py --data data.json --out index.html
```

| Flag | Effect |
|---|---|
| `--no-crawl` | Rebuilds the HTML from existing `data.json` without touching the network, and records no run |

A full run takes about eight minutes. The template must sit next to `collector.py` — the
script loads it from its own directory.

## Automatic refresh

`.github/workflows/refresh.yml` runs the collector daily at 18:50 UTC (= 00:20
Asia/Colombo) and commits the updated `data.json` and `index.html`. GitHub Pages serves
the result from the `main` branch root.

Two things to know about GitHub's scheduler: runs on a cron can be delayed by several
minutes under load, and scheduled workflows are disabled automatically after 60 days with
no commits to the repository. A manual run from the Actions tab re-arms it.

## Files

| File | Role |
|---|---|
| `collector.py` | Collector and dashboard builder |
| `dashboard_template.html` | Page shell; `__PAYLOAD__` is replaced with the data |
| `data.json` | Cumulative dataset — the memory between runs. Do not delete. |
| `index.html` | Generated dashboard, served by GitHub Pages |

## Note on data

`data.json` holds every post the collector has seen: titles, categories, prices, cities,
countries and ad numbers. In a public repository all of that is publicly readable, and
some sellers put their phone number in the post title. That is fine for public listings
from your own site — but if anything in it should not be public, make the repository
private and serve the dashboard elsewhere.

The collector reads `/api/feed` and `/api/reels`, which `robots.txt` puts off-limits to
general crawlers. It is run by the site's owner against their own site, one page at a
time with retries and pauses; anyone else should not point it at alleasyway.com.
