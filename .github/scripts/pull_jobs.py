#!/usr/bin/env python3
"""Materialize the daily job watch into jobs.json + job items in feed.xml via the ntfy relay.

Same rail as pull_pulse.py: the cloud job-watch run cannot push to GitHub, so
it POSTs a signed JSON payload to an ntfy topic, and this script (run by the
pulse-pull Action) polls the topic and writes durable same-origin files:

  jobs.json   the running job list the curriculum page's Jobs tab reads, plus
              last_run (the newest run's date, counts and quiet flag) and
              checked_at (stamped on every successful poll that changed
              something, at most once a UTC day when nothing arrived).
  feed.xml    one RSS item per new posting and per status change, guid
              jobs-YYYY-MM-DD-idx. Pulse items (guid pulse-...) are kept
              untouched; pull_pulse.py likewise keeps job items.

Relay payload (posted as the ntfy message body, or as an attachment when ntfy
converts a body over 4KB):
  {"kind":"jobs-relay-v1","run_date":"YYYY-MM-DD","quiet":bool,
   "new":[{"id","title","employer","location","employer_type","tier","comp",
           "url","posted","why","fit"}, ...],
   "updates":[{"id","status","note"}, ...],
   "sig":"..."}

Payload authentication matches pull_pulse.py: when PULSE_RELAY_SECRET is set,
sig = sha256(secret + ":" + canonical_json(payload minus sig)).

Stdlib only. Fail-closed: on poll failure exit nonzero touching nothing.
"""
import json, re, urllib.request, datetime, os, sys, hashlib

TOPIC = 'jobs-feed-jkl-83f9874466'
POLL = 'https://ntfy.sh/%s/json?poll=1&since=13h' % TOPIC
SITE = 'https://m7tn6mm64d-sudo.github.io/curriculum/'
REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
JOBS_PATH = os.path.join(REPO_ROOT, 'jobs.json')
FEED_PATH = os.path.join(REPO_ROOT, 'feed.xml')
MAX_JOB_ITEMS = 40
TIERS = ('pursue', 'stretch', 'fallback', 'side', 'watch')
TYPES = ('public', 'nonprofit', 'for-profit', 'contract', 'unknown')
STATUSES = ('open', 'closed', 'reposted', 'changed')


def _xesc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _txt(v, limit=600):
    s = '' if v is None else str(v)
    s = s.encode('utf-8', 'replace').decode('utf-8')
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
    return s.strip()[:limit]


def _url(v):
    u = _txt(v, 1000)
    return u if re.match(r'^https?://', u) else ''


def _slug(*parts):
    s = '-'.join(_txt(p, 120).lower() for p in parts if p)
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')[:80] or 'job'


def write_atomic(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sig_ok(payload):
    secret = os.environ.get('PULSE_RELAY_SECRET', '')
    if not secret:
        return True
    body = {k: v for k, v in payload.items() if k != 'sig'}
    canon = json.dumps(body, sort_keys=True, separators=(',', ':'))
    ok = payload.get('sig') == hashlib.sha256((secret + ':' + canon).encode()).hexdigest()
    if not ok:
        print('rejecting jobs payload for %s: bad or missing sig' % payload.get('run_date'))
    return ok


def valid(p):
    return isinstance(p, dict) and p.get('kind') == 'jobs-relay-v1' \
        and re.match(r'^\d{4}-\d{2}-\d{2}$', str(p.get('run_date', ''))) and sig_ok(p)


def poll_relays():
    stub = os.environ.get('JOBS_RELAY_FILE')
    if stub:
        loaded = json.load(open(stub))
        return [p for p in (loaded if isinstance(loaded, list) else [loaded]) if valid(p)]
    req = urllib.request.Request(POLL, headers={'User-Agent': 'pulse-pull-action'})
    with urllib.request.urlopen(req, timeout=30) as r:
        lines = r.read().decode('utf-8', 'replace').strip().split('\n')
    found = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            env = json.loads(ln)
        except Exception:
            continue
        payload = None
        try:
            payload = json.loads(env.get('message', ''))
        except Exception:
            payload = None
        if payload is None and isinstance(env.get('attachment'), dict) and env['attachment'].get('url'):
            try:
                areq = urllib.request.Request(env['attachment']['url'], headers={'User-Agent': 'pulse-pull-action'})
                with urllib.request.urlopen(areq, timeout=30) as r:
                    payload = json.loads(r.read(262144).decode('utf-8', 'replace'))
            except Exception as e:
                print('skipping unreadable jobs attachment (%s)' % e)
                continue
        if payload is not None and valid(payload):
            found.append((env.get('time', 0), payload))
    found.sort(key=lambda x: x[0])
    return [p for _, p in found]


def clean_job(j, run_date):
    title = _txt(j.get('title'), 200)
    employer = _txt(j.get('employer'), 160)
    if not title or not employer:
        return None
    tier = _txt(j.get('tier'), 20).lower()
    etype = _txt(j.get('employer_type'), 20).lower()
    return {
        'id': _txt(j.get('id'), 80) or _slug(employer, title),
        'title': title,
        'employer': employer,
        'location': _txt(j.get('location'), 160),
        'employer_type': etype if etype in TYPES else 'unknown',
        'tier': tier if tier in TIERS else 'watch',
        'comp': _txt(j.get('comp'), 160),
        'url': _url(j.get('url')),
        'posted': _txt(j.get('posted'), 60),
        'fit': _txt(j.get('fit') or j.get('why'), 500),
        'gap': _txt(j.get('gap'), 300),
        'status': 'open',
        'first_seen': run_date,
        'last_seen': run_date,
        'note': '',
    }


def load_jobs():
    if os.path.exists(JOBS_PATH):
        try:
            return json.load(open(JOBS_PATH))
        except Exception:
            pass
    return {'jobs': [], 'last_run': {}, 'checked_at': ''}


def job_item_xml(date, idx, kind, j):
    label = {'new': 'New job', 'closed': 'Job closed', 'reposted': 'Job reposted', 'changed': 'Job update'}.get(kind, 'Job')
    title = '%s · %s — %s' % (label, j['title'], j['employer'])
    link = j.get('url') or SITE + '#jobs'
    bits = [b for b in (j.get('location'), j.get('comp'), {'public': 'Public employer', 'nonprofit': 'Nonprofit employer',
            'for-profit': 'For-profit employer', 'contract': 'Contract work'}.get(j.get('employer_type'), '')) if b]
    desc = '<p>%s</p>' % _xesc(' · '.join(bits)) if bits else ''
    if kind == 'new' and j.get('fit'):
        desc += '<p><b>Why it fits:</b> %s</p>' % _xesc(j['fit'])
    if kind == 'new' and j.get('gap'):
        desc += '<p><b>Gap:</b> %s</p>' % _xesc(j['gap'])
    if kind != 'new' and j.get('note'):
        desc += '<p>%s</p>' % _xesc(j['note'])
    desc += '<p><i>JOBS · %s</i></p>' % _xesc(j.get('tier', 'watch').upper())
    pub = datetime.datetime.strptime(date, '%Y-%m-%d').strftime('%a, %d %b %Y 14:00:00 GMT')
    return ('<item><title>%s</title><link>%s</link><guid isPermaLink="false">jobs-%s-%d</guid>'
            '<pubDate>%s</pubDate><category>Jobs</category><description><![CDATA[%s]]></description></item>'
            % (_xesc(title), _xesc(link), date, idx, pub, desc))


def rewrite_feed(new_blocks_by_date):
    """Replace job items for the given dates, keep every other item, cap job items."""
    txt = open(FEED_PATH).read() if os.path.exists(FEED_PATH) else ''
    head_m = re.search(r'^(.*?<channel>.*?)(?=<item>|</channel>)', txt, re.S)
    if not head_m:
        print('feed.xml missing or unparseable; job items not written to RSS')
        return False
    head = head_m.group(1)
    blocks = re.findall(r'<item>.*?</item>', txt, re.S)

    def key(b):
        m = re.search(r'<guid[^>]*>(pulse|jobs)-(\d{4}-\d{2}-\d{2})-(\d+)</guid>', b)
        return (m.group(2), 1 if m.group(1) == 'pulse' else 0, -int(m.group(3))) if m else ('0000-00-00', 0, 0)

    kept = [b for b in blocks if not any(('<guid isPermaLink="false">jobs-%s-' % d) in b for d in new_blocks_by_date)]
    for d, bl in new_blocks_by_date.items():
        kept.extend(bl)
    jobs_blocks = sorted([b for b in kept if '>jobs-' in b], key=key, reverse=True)[:MAX_JOB_ITEMS]
    other = [b for b in kept if '>jobs-' not in b]
    allb = sorted(other + jobs_blocks, key=key, reverse=True)
    write_atomic(FEED_PATH, head + ''.join(allb) + '</channel></rss>\n')
    return True


payloads = poll_relays()  # raises on transport failure -> step fails, files untouched
data = load_jobs()
now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

if not payloads:
    if str(data.get('checked_at', ''))[:10] == now[:10]:
        print('no jobs payload in window · heartbeat already stamped today — no write')
        sys.exit(0)
    data['checked_at'] = now
    write_atomic(JOBS_PATH, json.dumps(data, indent=1, ensure_ascii=False) + '\n')
    print('no jobs payload in window · checked_at stamped')
    sys.exit(0)

jobs = data.get('jobs') or []
by_id = {j['id']: j for j in jobs}
by_url = {j['url']: j for j in jobs if j.get('url')}
feed_blocks = {}
for p in payloads:  # oldest first
    d = p['run_date']
    blocks = []
    for raw in (p.get('new') or [])[:15]:
        j = clean_job(raw, d)
        if not j:
            continue
        old = by_id.get(j['id']) or (by_url.get(j['url']) if j['url'] else None)
        if old:  # already known: refresh; announce a repost, or re-emit when re-processing the same day's payload
            was_closed = old.get('status') == 'closed'
            if old.get('first_seen') == d and not was_closed:
                blocks.append(job_item_xml(d, len(blocks), 'new', old))
                continue
            for k in ('location', 'comp', 'url', 'posted', 'fit', 'gap', 'tier', 'employer_type'):
                if j.get(k):
                    old[k] = j[k]
            old['last_seen'] = d
            if was_closed or (old.get('status') == 'reposted' and old.get('status_date') == d):
                old['status'] = 'reposted'
                old['status_date'] = d
                old['note'] = 'Reposted %s' % d
                blocks.append(job_item_xml(d, len(blocks), 'reposted', old))
            continue
        jobs.append(j)
        by_id[j['id']] = j
        if j['url']:
            by_url[j['url']] = j
        blocks.append(job_item_xml(d, len(blocks), 'new', j))
    for u in (p.get('updates') or [])[:30]:
        jid = _txt(u.get('id'), 80)
        st = _txt(u.get('status'), 20).lower()
        j = by_id.get(jid)
        if not j or st not in STATUSES:
            continue
        note = _txt(u.get('note'), 300)
        changed = st != j.get('status') or bool(note and note != j.get('note'))
        if changed:
            j['status_date'] = d
        j['status'] = st
        j['note'] = note or j.get('note', '')
        j['last_seen'] = d
        if st in ('closed', 'reposted', 'changed') and j.get('status_date') == d:
            blocks.append(job_item_xml(d, len(blocks), st, j))
    feed_blocks[d] = blocks
    if d >= str((data.get('last_run') or {}).get('date', '')):
        data['last_run'] = {'date': d, 'quiet': bool(p.get('quiet')) and not blocks,
                            'new': len(p.get('new') or []), 'updates': len(p.get('updates') or []),
                            'items': len(blocks)}

data['jobs'] = jobs
data['checked_at'] = now
write_atomic(JOBS_PATH, json.dumps(data, indent=1, ensure_ascii=False) + '\n')
wrote = rewrite_feed({d: b for d, b in feed_blocks.items()})
print('jobs.json: %d jobs · merged %d payload(s) for %s · feed items %s'
      % (len(jobs), len(payloads), ', '.join(p['run_date'] for p in payloads),
         {d: len(b) for d, b in feed_blocks.items()} if wrote else 'not written'))
