#!/usr/bin/env python3
"""Materialize the morning pulse into feed.xml + pulse-status.json via the ntfy relay.

Why a relay: the cloud pulse run cannot push to GitHub (sandbox egress blocks
api.github.com — verified 2026-07-21) and GitHub Actions cannot read the pulse
artifact (claude.ai serves content only to authenticated sessions). ntfy.sh is
reachable from BOTH sides: the cloud run POSTs a compact JSON payload to a
topic right after publishing (spec step 6.5) — preceded by one payload per
recent item-day it finds missing from the live feed.xml, so a relay that was
dropped (ntfy 429s happen) heals the next morning — and this script, run on
GitHub Actions shortly after, polls that topic, merges every payload in the
window, and writes durable same-origin files the curriculum page's news
widget reads:

  feed.xml           RSS; item format identical to pulse/build-pulse.py's
                     generator (guid pulse-YYYY-MM-DD-idx, pubDate 13:15 GMT).
                     Today's items are merged into the existing feed history.
  pulse-status.json  {edition_date, quiet, items_today, checked_at, source} —
                     lets the widget distinguish "ran today, quiet" from
                     "today's run hasn't landed". checked_at updates on every
                     successful poll even when no payload arrived.

Relay payload (posted as the ntfy message body):
  {"kind":"pulse-relay-v1","edition_date":"YYYY-MM-DD","quiet":bool,
   "items":[{"head","link","tag","module","summary","why"}, ...]}

Payload authentication: if the PULSE_RELAY_SECRET env var is set (repo Actions
secret), payloads must carry sig = sha256(secret + ":" + canonical_json) where
canonical_json is json.dumps of the payload minus its sig key with sort_keys
and (',',':') separators. Unsigned/bad-sig payloads are ignored. When the env
var is absent, sig is not enforced (bootstrap mode) — the relay topic name is
visible in this public repo, so set the secret to shut out spoofed payloads.

Stdlib only. Fail-closed: on poll failure exit nonzero touching nothing.
"""
import json, re, urllib.request, datetime, os, sys, hashlib

TOPIC = 'pulse-feed-jkl-x8e2rv7q'
POLL = 'https://ntfy.sh/%s/json?poll=1&since=13h' % TOPIC
ARTIFACT = 'https://claude.ai/code/artifact/fda1c4f0-0e51-48cd-b2f3-1ea5c9e6fb12'
SITE = 'https://m7tn6mm64d-sudo.github.io/curriculum/'
REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
FEED_PATH = os.path.join(REPO_ROOT, 'feed.xml')
STATUS_PATH = os.path.join(REPO_ROOT, 'pulse-status.json')
MAX_ITEMS = 60


def _xesc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _txt(v, limit=1200):
    """Coerce a payload field to safe text: None -> '', lone surrogates replaced,
    XML-1.0-illegal control chars stripped, trimmed, length-capped."""
    s = '' if v is None else str(v)
    s = s.encode('utf-8', 'replace').decode('utf-8')
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
    return s.strip()[:limit]


def write_atomic(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _rfc822(datestr):
    d = datetime.datetime.strptime(datestr, '%Y-%m-%d')
    return d.strftime('%a, %d %b %Y 13:15:00 GMT')


def poll_relays():
    """Return every valid pulse-relay-v1 payload in the window, oldest first.

    All of them, not just the newest: the cloud run re-sends earlier days that
    are missing from feed.xml (backfill) just before today's relay, and each
    must land. Merging is idempotent, so re-processing a payload on a later
    poll is harmless.

    Test hook: if PULSE_RELAY_FILE is set, read the payload (or a JSON list of
    payloads) from that file instead of polling — lets the item-merge path be
    exercised without posting fabricated items to the live topic.
    """
    stub = os.environ.get('PULSE_RELAY_FILE')
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
        t = env.get('time', 0)
        try:
            payload = json.loads(env.get('message', ''))
        except Exception:
            payload = None
        if payload is not None and valid(payload):
            found.append((t, payload))
        elif isinstance(env.get('attachment'), dict) and env['attachment'].get('url'):
            # ntfy silently converts >4KB bodies into attachments; recover them
            try:
                areq = urllib.request.Request(env['attachment']['url'], headers={'User-Agent': 'pulse-pull-action'})
                with urllib.request.urlopen(areq, timeout=30) as r:
                    payload = json.loads(r.read(65536).decode('utf-8', 'replace'))
            except Exception as e:
                print('skipping unreadable relay attachment (%s)' % e)
                continue
            if valid(payload):
                found.append((t, payload))
            else:
                print('skipping relay attachment with invalid payload (kind/date/sig)')
    found.sort(key=lambda x: x[0])
    return [p for _, p in found]


def valid(payload):
    return isinstance(payload, dict) and payload.get('kind') == 'pulse-relay-v1' \
        and re.match(r'^\d{4}-\d{2}-\d{2}$', str(payload.get('edition_date', ''))) \
        and sig_ok(payload)


def sig_ok(payload):
    secret = os.environ.get('PULSE_RELAY_SECRET', '')
    if not secret:
        return True  # bootstrap mode: enforcement starts when the repo secret exists
    body = {k: v for k, v in payload.items() if k != 'sig'}
    canon = json.dumps(body, sort_keys=True, separators=(',', ':'))
    want = hashlib.sha256((secret + ':' + canon).encode()).hexdigest()
    ok = payload.get('sig') == want
    if not ok:
        print('rejecting payload for %s: bad or missing sig' % payload.get('edition_date'))
    return ok


def item_xml(date, idx, it):
    head = _txt(it.get('head'), 300) or 'Pulse item'
    link = _txt(it.get('link'), 1000)
    if not re.match(r'^https?://', link):
        link = ARTIFACT  # never emit relative/None/javascript: links
    tag = _txt(it.get('tag'), 10).upper()
    tag = tag if tag in ('ACT', 'WATCH') else ''
    module = _txt(it.get('module'), 4)[:1].upper()
    mod = (' · Module ' + module) if module in 'ABCDEFGHI' and module else ''
    desc = '<p>%s</p>' % _xesc(_txt(it.get('summary')))
    why = _txt(it.get('why'))
    if why:
        desc += '<p><b>Why it matters:</b> %s</p>' % _xesc(why)
    if tag:
        desc += '<p><i>%s%s</i></p>' % (tag, mod)
    return ('<item><title>%s</title><link>%s</link><guid isPermaLink="false">pulse-%s-%d</guid>'
            '<pubDate>%s</pubDate><description><![CDATA[%s]]></description></item>'
            % (_xesc(head), _xesc(link), date, idx, _rfc822(date), desc))


def existing_items():
    """Parse (guid_date, xml_block) pairs out of the current feed.xml, if any."""
    if not os.path.exists(FEED_PATH):
        return []
    txt = open(FEED_PATH).read()
    out = []
    for block in re.findall(r'<item>.*?</item>', txt, re.S):
        gm = re.search(r'<guid[^>]*>pulse-(\d{4}-\d{2}-\d{2})-\d+</guid>', block)
        if gm:
            out.append((gm.group(1), block))
    return out


payloads = poll_relays()  # raises on transport failure -> Action turns red, files untouched

old_status = {}
if os.path.exists(STATUS_PATH):
    try:
        old_status = json.load(open(STATUS_PATH))
    except Exception:
        old_status = {}

now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

if not payloads:
    # Poll worked but no fresh relay message. Stamp the check at most once per
    # UTC day so the poller doesn't commit an identical heartbeat every run.
    if str(old_status.get('checked_at', ''))[:10] == now[:10]:
        print('no relay payload in window · heartbeat already stamped today — no write')
        sys.exit(0)
    status = dict(old_status)
    status.update({'checked_at': now, 'source': 'relay-poll-empty'})
    status.setdefault('edition_date', '')
    write_atomic(STATUS_PATH, json.dumps(status) + '\n')
    print('no relay payload in window · status stamped %s (edition still %s)'
          % (now, status.get('edition_date') or 'unknown'))
    sys.exit(0)

items = existing_items()
for payload in payloads:  # oldest first, so a later relay for the same date wins
    date = payload['edition_date']
    items = [(date, item_xml(date, i, it)) for i, it in enumerate(payload.get('items') or [])] \
        + [(d, b) for d, b in items if d != date]
merged = [b for d, b in items]


def _order(block):  # date descending, item index ascending within a day
    gm = re.search(r'<guid[^>]*>pulse-(\d{4}-\d{2}-\d{2})-(\d+)</guid>', block)
    return (gm.group(1), -int(gm.group(2)))


merged.sort(key=_order, reverse=True)
merged = merged[:MAX_ITEMS]

# keep the job-watch items (guid jobs-...) that pull_jobs.py writes into the same feed,
# interleaved by date; pulse items sort ahead of job items on the same day
_jobs = re.findall(r'<item>(?:(?!</item>).)*?<guid[^>]*>jobs-\d{4}-\d{2}-\d{2}-\d+</guid>.*?</item>', open(FEED_PATH).read(), re.S) if os.path.exists(FEED_PATH) else []


def _order_all(block):
    gm = re.search(r'<guid[^>]*>(pulse|jobs)-(\d{4}-\d{2}-\d{2})-(\d+)</guid>', block)
    return (gm.group(2), 1 if gm.group(1) == 'pulse' else 0, -int(gm.group(3))) if gm else ('0000-00-00', 0, 0)


merged = sorted(merged + _jobs, key=_order_all, reverse=True)

feed = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        '<title>The Pulse — Health-System AI Daily</title>'
        '<link>' + SITE + '</link>'
        '<atom:link href="' + SITE + 'feed.xml" rel="self" type="application/rss+xml"/>'
        '<description>0–3 items a day a health-system clinical-AI leader would be embarrassed to miss. De-hyped by contract; quiet days produce no items. Also carries the daily job watch (items titled New job / Job closed / Job reposted).</description>'
        '<language>en-us</language><ttl>180</ttl>' + ''.join(merged) + '</channel></rss>\n')

# the status line tracks the newest edition; a backfill of an older day never moves it backward
newest = max(payloads, key=lambda p: p['edition_date'])  # max() keeps the first of equal dates
newest = [p for p in payloads if p['edition_date'] == newest['edition_date']][-1]
if newest['edition_date'] >= str(old_status.get('edition_date', '')):
    status = {
        'edition_date': newest['edition_date'],
        'quiet': bool(newest.get('quiet')),
        'items_today': len(newest.get('items') or []),
        'checked_at': now,
        'source': 'relay',
    }
else:
    status = dict(old_status)
    status.update({'checked_at': now, 'source': 'relay-backfill'})

write_atomic(FEED_PATH, feed)
write_atomic(STATUS_PATH, json.dumps(status) + '\n')
print('feed.xml: %d bytes · %d items · merged %d payload(s) for %s · status edition %s (quiet=%s)'
      % (len(feed), len(merged), len(payloads), ', '.join(p['edition_date'] for p in payloads),
         status.get('edition_date'), status.get('quiet')))
