#!/usr/bin/env python3
"""AllEasyWay collector + dashboard builder.
Crawls public pages, merges into a cumulative dataset (never deletes history),
and regenerates the dashboard HTML.

  python3 -I aew_sync.py --data data.json --out dashboard.html [--deep]

Accuracy model: every post carries a strictly increasing `ad_id`. Posts are keyed
by ad_id, so re-running is idempotent. A post seen once is kept forever, so a
later deletion on the site never rewrites history.
"""
import re, json, os, sys, time, argparse, collections, datetime as dt
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

BASE = 'https://alleasyway.com'
TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))          # Asia/Colombo
UA = 'Mozilla/5.0 (compatible; AllEasyWayDashboard/1.0; owner analytics)'
FIELDS = ["id","ad_id","title","main_category","sub_category","item_type","brand","model",
          "is_reel","city","postal_code","price","price_currency_code","created_at","description"]
PAT = {f: re.compile(r'"%s":(?:"((?:[^"\\]|\\.)*)"|(null|true|false|-?\d+(?:\.\d+)?))' % f) for f in FIELDS}
CATEGORIES = ["Vehicles","Electronics","Real Estate","Jobs and Career","Fashion","Events","Other","Collection"]
INDEX_PATHS = ["", "mix", "mix?view=recent", "explorer", "reels", "communaute"] + \
              ["c/" + urllib.parse.quote(c) for c in CATEGORIES]

def get(path):
    try:
        req = urllib.request.Request(BASE + '/' + path, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=50) as r:
            return r.read().decode('utf-8', 'replace')
    except Exception:
        return ''

def unesc(s):
    if s is None: return ''
    return json.loads('"%s"' % s) if '\\' in s else s

def parse(html):
    s = html.replace('\\"', '"'); out = []
    for m in re.finditer(r'"ad_id":(\d+)', s):
        start = s.rfind('{"id":"', max(0, m.start() - 6000), m.start())
        if start < 0: continue
        chunk = s[start:m.start() + 6000]
        d = {}
        for f in FIELDS:
            mm = PAT[f].search(chunk)
            if mm: d[f] = unesc(mm.group(1)) if mm.group(1) is not None else mm.group(2)
        if d.get('id') and d.get('created_at') and d.get('ad_id'):
            out.append(d)
    return out

def kind_of(r):
    if r.get('is_reel') == 'true': return 'reel'
    if r.get('main_category') == 'Communauté': return 'community'
    return 'listing'

def norm(r):
    br = r.get('brand') or ''
    return {
        'ad_id': int(r['ad_id']), 'uid': r['id'], 'kind': kind_of(r),
        'title': (r.get('title') or '').strip(),
        'brand': '' if br == 'Other' else br, 'model': (r.get('model') or '').strip(),
        'type': r.get('item_type') or '', 'cat': r.get('main_category') or '',
        'sub': r.get('sub_category') or '', 'city': r.get('city') or '',
        'price': None if r.get('price') in (None, '', 'null') else float(r['price']),
        'cur': r.get('price_currency_code') or '', 'ts': r['created_at'],
    }

def local_date(ts):
    return dt.datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(TZ).strftime('%Y-%m-%d')

def local_time(ts):
    return dt.datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(TZ).strftime('%H:%M')

def crawl(known_titles, deep=False):
    found = {}
    def absorb(html):
        for r in parse(html):
            try: found[int(r['ad_id'])] = norm(r)
            except Exception: pass

    with ThreadPoolExecutor(max_workers=4) as ex:
        for html in ex.map(get, INDEX_PATHS): absorb(html)
    print(f"  index+categories -> {len(found)} records", flush=True)

    # search sweep: vocabulary from known titles widens recall beyond the 300/page cap
    words = collections.Counter()
    for t in known_titles:
        for w in re.findall(r'[\w஀-௿඀-෿]{3,}', t.lower()): words[w] += 1
    for r in found.values():
        for w in re.findall(r'[\w஀-௿඀-෿]{3,}', r['title'].lower()): words[w] += 1
    terms = [w for w, _ in words.most_common(900 if deep else 120)]

    def q(t): return get('recherche?q=' + urllib.parse.quote(t))
    for i in range(0, len(terms), 40):
        before = len(found)
        with ThreadPoolExecutor(max_workers=4) as ex:
            for html in ex.map(q, terms[i:i + 40]): absorb(html)
        print(f"  search {min(i+40,len(terms))}/{len(terms)} -> {len(found)} (+{len(found)-before})", flush=True)
        time.sleep(0.3)
    return found

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', required=True); ap.add_argument('--out', required=True)
    ap.add_argument('--deep', action='store_true'); ap.add_argument('--no-crawl', action='store_true')
    a = ap.parse_args()

    store = json.load(open(a.data, encoding='utf-8')) if os.path.exists(a.data) else {'posts': {}, 'runs': []}
    posts = {int(k): v for k, v in store['posts'].items()}
    before = len(posts)

    if not a.no_crawl:
        found = crawl([p['title'] for p in posts.values()][:4000], deep=a.deep)
        for aid, rec in found.items():
            posts.setdefault(aid, rec)          # first sighting wins: history is never rewritten
    added = len(posts) - before

    ids = sorted(posts)
    run = {'at': dt.datetime.now(dt.timezone.utc).astimezone(TZ).isoformat(timespec='seconds'),
           'total': len(posts), 'added': added,
           'max_ad_id': ids[-1] if ids else 0,
           'coverage': round(100 * len(ids) / (ids[-1] - ids[0] + 1), 1) if len(ids) > 1 else 0}
    store = {'posts': {str(k): v for k, v in posts.items()}, 'runs': (store.get('runs', []) + [run])[-60:]}
    json.dump(store, open(a.data, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f"  dataset: {len(posts)} posts (+{added}), coverage {run['coverage']}%", flush=True)

    build(posts, store['runs'], a.out)
    print("  wrote", a.out, round(os.path.getsize(a.out)/1024), "KB", flush=True)

def build(posts, runs, out):
    days = collections.defaultdict(lambda: {'listing': 0, 'reel': 0, 'community': 0})
    bydate = collections.defaultdict(list)
    for aid in sorted(posts):
        p = posts[aid]; d = local_date(p['ts'])
        days[d][p['kind']] += 1
        bydate[d].append([p['ad_id'], local_time(p['ts']), p['kind'][0], p['title'], p['brand'],
                          p['model'], p['type'], p['cat'], p['sub'], p['city'],
                          '' if p['price'] is None else p['price'], p['cur'], p['uid']])
    series = [{'d': d, 'l': v['listing'], 'r': v['reel'], 'c': v['community']} for d, v in sorted(days.items())]
    # keep full detail for every day, so any date stays browsable
    detail = {d: bydate[d] for d in sorted(bydate)}
    payload = {'series': series, 'posts': detail, 'runs': runs[-14:]}
    tpl = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard_template.html'), encoding='utf-8').read()
    open(out, 'w', encoding='utf-8').write(
        tpl.replace('__PAYLOAD__', json.dumps(payload, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')))

if __name__ == '__main__':
    main()
