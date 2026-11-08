#!/usr/bin/env python3
"""AllEasyWay collector + dashboard builder.

Collects every public post on alleasyway.com into a cumulative dataset and
regenerates the dashboard. Run once a day, just after midnight Sri Lanka time:

    python3 collector.py --data data.json --out index.html

Sources, in the order they are used:

  /api/feed      JSON, 30 per page, offset paging  -> listings and product reels
  /api/reels     JSON, 30 per page, offset paging  -> every reel, standalone ones included
  /communaute    RSC page payload                  -> the latest community posts
  /sitemap.xml   the site's own inventory          -> anything the three above missed,
                                                      fetched one post at a time

The sitemap pass is what makes the count trustworthy: whatever the feeds skip is
still listed there, so the run can both fill the gap and report what it could not
resolve. `coverage` in each run entry is the share of sitemap posts we hold.

Accuracy model: posts are keyed by the site's sequential `ad_id`, so re-running is
idempotent and a post seen once is kept forever - a later deletion on the site can
never rewrite a day that has already been counted. Fields other than the timestamp
are refreshed when a post is seen again, so corrections on the site reach us too.

A run that cannot reach the site writes nothing and exits non-zero, so a failed
crawl can never be mistaken for a quiet day.
"""
import re, json, os, sys, time, argparse, collections, datetime as dt
import urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

BASE = 'https://alleasyway.com'
TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))          # Asia/Colombo
UA = 'Mozilla/5.0 (compatible; AllEasyWayDashboard/2.0; owner analytics)'
PAGE = 30                      # the API's own cap, whatever limit we ask for
WORKERS = 6
MAX_SINGLE_FETCHES = 900       # ceiling on the per-post sitemap pass, per run

stats = collections.Counter()


# ---------------------------------------------------------------- fetching

def fetch(path, rsc=False):
    """Return the body of BASE/path, or None if it could not be fetched."""
    headers = {'User-Agent': UA}
    if rsc:
        headers['RSC'] = '1'
    for attempt in range(3):
        try:
            req = urllib.request.Request(BASE + '/' + path, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                stats['ok'] += 1
                return r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            if e.code == 404:
                stats['notfound'] += 1
                return None
            if attempt == 2:
                stats['fail'] += 1
                return None
        except Exception:
            if attempt == 2:
                stats['fail'] += 1
                return None
        time.sleep(1.5 * (attempt + 1))
    return None


def fetch_json(path):
    body = fetch(path)
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        stats['badjson'] += 1
        return None


def rsc_records(body):
    """Pull post objects out of a React server-component payload.

    The payload is a series of JSON string chunks; decoding them and then
    decoding each object properly is what keeps quotes, newlines and non-Latin
    titles intact - the old regex scraper mangled all three.
    """
    if body is None:
        return {}
    if 'self.__next_f' in body:
        chunks = re.finditer(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', body)
        try:
            body = ''.join(json.loads(m.group(1)) for m in chunks)
        except ValueError:
            return {}
    dec, out = json.JSONDecoder(), {}
    for m in re.finditer(r'\{"id":"', body):
        try:
            obj, _ = dec.raw_decode(body, m.start())
        except ValueError:
            continue
        if isinstance(obj, dict) and 'ad_id' in obj and obj.get('created_at'):
            out[int(obj['ad_id'])] = obj
    return out


# ---------------------------------------------------------------- shaping

def iso(ts):
    """Parse the site's timestamps on any Python 3.8+.

    They carry 1 to 6 fractional digits, and datetime.fromisoformat only
    accepts 3 or 6 before Python 3.11.
    """
    ts = ts.strip().replace('Z', '+00:00')
    m = re.match(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?([+-]\d\d:?\d\d)?$', ts)
    if not m:
        raise ValueError('unparsable timestamp: %r' % ts)
    frac = (m.group(2) or '0')[:6].ljust(6, '0')
    off = (m.group(3) or '+00:00').replace(':', '')
    off = off[:3] + ':' + off[3:]
    return dt.datetime.fromisoformat('%s.%s%s' % (m.group(1), frac, off))


def text(v):
    """The site sends missing values as null, '' or the string 'null'."""
    if v is None:
        return ''
    v = str(v).strip()
    return '' if v.lower() in ('null', 'none', 'undefined') else v


def kind_of(r):
    if r.get('is_reel') is True or text(r.get('main_category')) == 'Reel':
        return 'reel'
    if text(r.get('main_category')) == 'Communauté':
        return 'community'
    return 'listing'


def norm(r):
    price = r.get('price')
    if price in (None, '', 'null'):
        price = r.get('price_amount')
    try:
        price = None if price in (None, '', 'null') else float(price)
    except (TypeError, ValueError):
        price = None
    brand = text(r.get('brand'))
    return {
        'uid': r['id'],
        'kind': kind_of(r),
        'title': text(r.get('title')) or text(r.get('other_title')) or text(r.get('event_title')),
        'brand': '' if brand == 'Other' else brand,
        'model': text(r.get('model')),
        'type': text(r.get('item_type')) or text(r.get('other_type')),
        'cat': text(r.get('main_category')),
        'sub': text(r.get('sub_category')),
        'city': text(r.get('city')),
        'cc': text(r.get('country_code')).upper(),
        'price': price,
        'cur': text(r.get('price_currency_code')) or text(r.get('price_currency')),
        'ts': r['created_at'],
    }


# ---------------------------------------------------------------- sources

def page_source(name, path, found, log):
    """Walk an offset-paged JSON endpoint to its end."""
    offset, pages, before = 0, 0, len(found)
    while True:
        d = fetch_json('%s?limit=%d&offset=%d' % (path, PAGE, offset))
        if d is None:
            log('  %s: page at offset %d failed' % (name, offset))
            stats['pagefail'] += 1
            break
        posts = d.get('posts') or []
        for p in posts:
            try:
                found[int(p['ad_id'])] = norm(p)
            except (KeyError, TypeError, ValueError):
                stats['skipped'] += 1
        pages += 1
        offset += PAGE
        if not posts or not d.get('hasMore'):
            break
        if pages > 600:
            log('  %s: stopped at the 600-page guard' % name)
            break
        if pages % 25 == 0:
            log('  %s: %d pages, %d posts held' % (name, pages, len(found)))
    log('  %s: %d pages -> +%d posts' % (name, pages, len(found) - before))
    return pages


def sitemap_ids(log):
    """Every annonce/reel the site publishes, as {uuid: url-kind}."""
    body = fetch('sitemap.xml')
    if body is None:
        log('  sitemap: unavailable')
        return {}
    ids = dict((u, k) for k, u in
               re.findall(r'<loc>%s/(annonce|reel)/([0-9a-f-]{36})</loc>' % re.escape(BASE), body))
    log('  sitemap: %d posts listed' % len(ids))
    return ids


def crawl(log):
    found = {}
    page_source('feed', 'api/feed', found, log)
    page_source('reels', 'api/reels', found, log)

    community = rsc_records(fetch('communaute', rsc=True))
    for aid, r in community.items():
        found.setdefault(aid, norm(r))
    log('  communaute: %d posts' % len(community))

    if stats['ok'] == 0:
        return found, {}, 0

    # Whatever the feeds missed is still in the sitemap; fetch those one by one.
    listed = sitemap_ids(log)
    have = set(r['uid'] for r in found.values())
    missing = [(u, k) for u, k in listed.items() if u not in have]
    todo = missing[:MAX_SINGLE_FETCHES]
    if missing:
        log('  sitemap: %d not in the feeds, fetching %d' % (len(missing), len(todo)))

    def one(item):
        uid, url_kind = item
        return uid, rsc_records(fetch('%s/%s' % (url_kind, uid), rsc=True))

    resolved = 0
    for i in range(0, len(todo), 60):
        batch = todo[i:i + 60]
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            for uid, recs in ex.map(one, batch):
                for aid, r in recs.items():
                    if r['id'] == uid:
                        found[aid] = norm(r)
                        resolved += 1
        if i and i % 300 == 0:
            log('  sitemap: %d/%d fetched' % (i, len(todo)))
        time.sleep(0.3)
    if todo:
        log('  sitemap: resolved %d of %d fetched' % (resolved, len(todo)))
    return found, listed, len(missing) - resolved


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--data', required=True, help='cumulative dataset (read and written)')
    ap.add_argument('--out', required=True, help='dashboard HTML to generate')
    ap.add_argument('--no-crawl', action='store_true',
                    help='rebuild the HTML from existing data, touching no network and logging no run')
    a = ap.parse_args()

    def log(msg):
        print(msg, flush=True)

    store = {'posts': {}, 'runs': []}
    if os.path.exists(a.data):
        with open(a.data, encoding='utf-8') as f:
            store = json.load(f)
    posts = dict((int(k), v) for k, v in store['posts'].items())

    if a.no_crawl:
        build(posts, store.get('runs', []), a.out)
        log('  rebuilt %s from %d stored posts (no run recorded)' % (a.out, len(posts)))
        return

    t0 = time.time()
    found, listed, unresolved = crawl(log)

    if stats['ok'] == 0 or not found:
        log('  ABORT: the site could not be reached (%d failures) - nothing written'
            % stats['fail'])
        sys.exit(1)
    reqs = stats['ok'] + stats['fail']
    if stats['fail'] > reqs * 0.2:
        log('  ABORT: %d of %d requests failed - nothing written' % (stats['fail'], reqs))
        sys.exit(1)

    added = 0
    for aid, rec in found.items():
        old = posts.get(aid)
        if old is None:
            rec['seen'] = dt.datetime.now(dt.timezone.utc).astimezone(TZ).strftime('%Y-%m-%d')
            posts[aid] = rec
            added += 1
        else:
            # First sighting wins for the timestamp; everything else is refreshed,
            # so corrections on the site reach records collected by older versions.
            rec['seen'] = old.get('seen', '')
            rec['ts'] = old.get('ts') or rec['ts']
            posts[aid] = rec

    held = set(r['uid'] for r in posts.values())
    coverage = round(100 * len(set(listed) & held) / len(listed), 1) if listed else 0.0
    run = {
        'at': dt.datetime.now(dt.timezone.utc).astimezone(TZ).isoformat(timespec='seconds'),
        'total': len(posts), 'added': added, 'live': len(found),
        'sitemap': len(listed), 'unresolved': unresolved, 'coverage': coverage,
        'secs': round(time.time() - t0),
    }
    store = {'version': 2,
             'posts': dict((str(k), v) for k, v in sorted(posts.items())),
             'runs': (store.get('runs', []) + [run])[-60:]}
    with open(a.data, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False)

    log('  dataset: %d posts (+%d), %d live on site, sitemap coverage %s%%, %d unresolved, %ds'
        % (len(posts), added, len(found), coverage, unresolved, run['secs']))
    build(posts, store['runs'], a.out)
    log('  wrote %s (%d KB)' % (a.out, round(os.path.getsize(a.out) / 1024)))


def build(posts, runs, out):
    """Render the dashboard for completed days only - today is always excluded.

    A day is only counted once it is over in Sri Lanka, so the newest bar can
    never be a half-finished day that looks like a drop in posting.
    """
    today = dt.datetime.now(dt.timezone.utc).astimezone(TZ).date()
    cutoff = today.strftime('%Y-%m-%d')

    days = collections.defaultdict(lambda: {'listing': 0, 'reel': 0, 'community': 0})
    bydate = collections.defaultdict(list)
    skipped = 0
    for aid in sorted(posts):
        p = posts[aid]
        try:
            when = iso(p['ts']).astimezone(TZ)
        except ValueError:
            skipped += 1
            continue
        d = when.strftime('%Y-%m-%d')
        if d >= cutoff:                      # today and anything later: not a complete day
            continue
        days[d][p['kind']] += 1
        bydate[d].append([aid, when.strftime('%H:%M'), p['kind'][0], p['title'], p['brand'],
                          p['model'], p['type'], p['cat'], p['sub'], p['city'],
                          '' if p['price'] is None else p['price'], p['cur'], p['uid'],
                          p.get('cc', '')])

    series = [{'d': d, 'l': v['listing'], 'r': v['reel'], 'c': v['community']}
              for d, v in sorted(days.items())]
    payload = {'series': series, 'posts': dict(sorted(bydate.items())),
               'runs': runs[-14:], 'through': series[-1]['d'] if series else '',
               'pending': sum(1 for p in posts.values() if _safe_day(p['ts']) >= cutoff)}
    if skipped:
        print('  note: %d posts had an unreadable timestamp and were left out' % skipped,
              flush=True)

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, 'dashboard_template.html'), encoding='utf-8') as f:
        tpl = f.read()
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    with open(out, 'w', encoding='utf-8', newline='\n') as f:
        f.write(tpl.replace('__PAYLOAD__', body))


def _safe_day(ts):
    try:
        return iso(ts).astimezone(TZ).strftime('%Y-%m-%d')
    except ValueError:
        return ''


if __name__ == '__main__':
    main()
