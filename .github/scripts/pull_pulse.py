#!/usr/bin/env python3
"""Materialize the morning pulse into feed.xml + pulse-status.json via the ntfy relay.

Why a relay: the cloud pulse run cannot push to GitHub (sandbox egress blocks
api.github.com — verified 2026-07-21) and GitHub Actions cannot read the pulse
artifact (claude.ai serves content only to authenticated sessions). ntfy.sh is
reachable from BOTH sides: the cloud run POSTs a compact JSON payload to a
topic right after publishing (spec step 6.6), and this script — run on GitHub
Actions shortly after — polls that topic and writes durable same-origin files
the curriculum page's news widget reads:

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
REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
FEED_PATH = os.path.join(REPO_ROOT, 'feed.xml')
STATUS_PATH = os.path.join(REPO_ROOT, 'pulse-status.json')
MAX_ITEMS = 60


def _xesc(t):
    return t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _rfc822(datestr):
    d = datetime.datetime.strptime(datestr, '%Y-%m-%d')
    return d.strftime('%a, %d %b %Y 13:15:00 GMT')


def poll_relay():
    """Return the newest pulse-relay-v1 payload from the topic, or None.

    Test hook: if PULSE_RELAY_FILE is set, read the payload from that file
    instead of polling — lets the item-merge path be exercised without
    posting fabricated items to the live topic.
    """
    stub = os.environ.get('PULSE_RELAY_FILE')
    if stub:
        payload = json.load(open(stub))
        return payload if valid(payload) else None
    req = urllib.request.Request(POLL, headers={'User-Agent': 'pulse-pull-action'})
    with urllib.request.urlopen(req, timeout=30) as r:
        lines = r.read().decode('utf-8', 'replace').strip().split('\n')
    best, best_t = None, -1
    for ln in lines:
        if not ln.strip():
            continue
        try:
            env = json.loads(ln)
            payload = json.loads(env.get('message', ''))
        except Exception:
            continue
        if valid(payload):
            t = env.get('time', 0)
            if t > best_t:
                best, best_t = payload, t
    return best


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
    head = str(it.get('head', '')).strip() or 'Pulse item'
    link = str(it.get('link', '')).strip() or ARTIFACT
    tag = str(it.get('tag', '')).strip().upper()
    tag = tag if tag in ('ACT', 'WATCH') else ''
    module = str(it.get('module', '')).strip()[:1].upper()
    mod = (' · Module ' + module) if module in 'ABCDEFGHI' and module else ''
    desc = '<p>%s</p>' % _xesc(str(it.get('summary', '')).strip())
    why = str(it.get('why', '')).strip()
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
        gm = re.search(r'pulse-(\d{4}-\d{2}-\d{2})-\d+', block)
        if gm:
            out.append((gm.group(1), block))
    return out


payload = poll_relay()  # raises on transport failure -> Action turns red, files untouched

old_status = {}
if os.path.exists(STATUS_PATH):
    try:
        old_status = json.load(open(STATUS_PATH))
    except Exception:
        old_status = {}

now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

if payload is None:
    # Poll worked but no fresh relay message: stamp the check, keep the rest.
    status = dict(old_status)
    status.update({'checked_at': now, 'source': 'relay-poll-empty'})
    status.setdefault('edition_date', '')
    open(STATUS_PATH, 'w').write(json.dumps(status) + '\n')
    print('no relay payload in window · status stamped %s (edition still %s)'
          % (now, status.get('edition_date') or 'unknown'))
    sys.exit(0)

date = payload['edition_date']
new_blocks = [item_xml(date, i, it) for i, it in enumerate(payload.get('items') or [])]

merged = new_blocks + [b for d, b in existing_items() if d != date]


def _order(block):  # date descending, item index ascending within a day
    gm = re.search(r'pulse-(\d{4}-\d{2}-\d{2})-(\d+)', block)
    return (gm.group(1), -int(gm.group(2)))


merged.sort(key=_order, reverse=True)
merged = merged[:MAX_ITEMS]

feed = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        '<title>The Pulse — Health-System AI Daily</title>'
        '<link>' + ARTIFACT + '</link>'
        '<description>0–3 items a day a health-system clinical-AI leader would be embarrassed to miss. De-hyped by contract; quiet days produce no items.</description>'
        '<language>en-us</language><ttl>180</ttl>' + ''.join(merged) + '</channel></rss>\n')

status = {
    'edition_date': date,
    'quiet': bool(payload.get('quiet')),
    'items_today': len(payload.get('items') or []),
    'checked_at': now,
    'source': 'relay',
}

open(FEED_PATH, 'w').write(feed)
open(STATUS_PATH, 'w').write(json.dumps(status) + '\n')
print('feed.xml: %d bytes · %d items (%d new for %s, quiet=%s)'
      % (len(feed), len(merged), len(new_blocks), date, status['quiet']))
