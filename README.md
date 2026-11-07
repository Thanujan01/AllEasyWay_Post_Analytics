# AllEasyWay Post Analytics

Daily posting monitor for [alleasyway.com](https://alleasyway.com) — listings, reels and
community posts per day, in Sri Lanka time, with per-post drill-down.

**Live dashboard:** https://thanujan01.github.io/AllEasyWay_Post_Analytics/

## How it works

`collector.py` crawls the site's public pages (index, categories, reels, community, plus a
search sweep that widens recall past the 300-per-page cap), extracts each post, and merges
what it finds into `data.json`.

Posts are keyed by the site's own sequential ad number, so:

- re-running is idempotent — a post already recorded is never duplicated;
- first sighting wins — a post counted once stays in history even if the seller later
  deletes it, so the daily counts cannot be rewritten after the fact.

It then regenerates `index.html` from `dashboard_template.html`. The dashboard is a single
self-contained file: no server, no build step, no dependencies.

## Running it yourself

Python 3.8+ and the standard library — nothing to install.

```bash
python3 collector.py --data data.json --out index.html
```

| Flag | Effect |
|---|---|
| `--deep` | Widens the search sweep to 900 terms instead of 120. Slower, better coverage. |
| `--no-crawl` | Rebuilds the HTML from existing `data.json` without touching the network. |

The template must sit next to `collector.py` — the script loads it from its own directory.

## Automatic refresh

`.github/workflows/refresh.yml` runs the collector twice daily at 06:30 and 18:30 UTC
(= 12:00 and 00:00 Asia/Colombo) and commits the updated `data.json` and `index.html`.
GitHub Pages serves the result from the `main` branch root.

Two things to know about GitHub's scheduler: runs on a cron can be delayed by several
minutes under load, and scheduled workflows are disabled automatically after 60 days with
no commits to the repository. A manual run from the Actions tab re-arms it.

## Files

| File | Role |
|---|---|
| `collector.py` | Crawler and dashboard builder |
| `dashboard_template.html` | Page shell; `__PAYLOAD__` is replaced with the data |
| `data.json` | Cumulative dataset — the memory between runs. Do not delete. |
| `index.html` | Generated dashboard, served by GitHub Pages |

## Note on data

`data.json` holds every post the crawler has seen: titles, categories, prices, cities and
ad numbers. In a public repository all of that is publicly readable. That is fine for
public listings from your own site — but if anything in it should not be public, make the
repository private and serve the dashboard elsewhere.
