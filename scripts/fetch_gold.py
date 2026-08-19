#!/usr/bin/env python3
"""
Publishes the IBJA benchmark rates as two static JSON files.

    docs/gold.json         metals.dev where it can, the scrape where it cannot
    docs/gold_scrape.json  the scrape alone, always, whatever gold.json did

Two files rather than one because the scrape is the fallback, and a fallback
that is never exercised is not a fallback. The second file costs nothing --
the page is read on every run regardless -- and it lets the app be pointed at
the scrape alone to prove it still works, without touching the file everybody
else is reading.

Sources
-------
metals.dev serves IBJA's own published 999 gold and 999 silver figures as an
authority feed, in rupees per gram. IBJA does not publish 916, 750 and 585
independently; it derives them from 999 by the purity fraction, so one figure
gives all of them. It carries no IBJA platinum and no AM/PM label, so both of
those still come from the page.

ibjarates.com carries the full table: five gold purities, silver, platinum,
and an AM and a PM pane, newest row first, every cell tagged with a data-label
naming the metal. The parse is by name rather than by column position and so
survives a column being added or moved.

Figures are rupees per 10 grams for gold and platinum, and per kilogram for
silver. That is how IBJA publishes them and how the newspaper prints them.
They are carried through unconverted; the app does its own arithmetic, and a
converted figure is one more place for a mistake to hide.

Design rules
------------
  - Never write a file we are not sure about. A holiday, a site change or a
    garbled number leaves the previous file exactly where it is.
  - A day with nothing published is not a failure. It exits zero. A red cross
    on a day IBJA was shut teaches you to ignore red crosses.
  - Every number passes the same three gates whichever source produced it: a
    sane absolute band, a ratio check of each purity against 999, and a move
    check against the last figure published. A source is never trusted on the
    strength of being the expensive one.
  - The API quota is metered here, in a file, not hoped about. The counter is
    incremented before the call, not after, so a call that times out still
    counts -- it was still spent.
  - No user data anywhere near this. One GET to each source, nothing sent but
    the key, and the key never reaches a log or either output file.
"""

import calendar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

SRC = "https://ibjarates.com/"
API = "https://api.metals.dev/v1/latest"

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(HERE, "..", "docs")
OUT = os.path.join(DOCS, "gold.json")
OUT_SCRAPE = os.path.join(DOCS, "gold_scrape.json")
QUOTA = os.path.join(DOCS, "quota.json")
RAW = os.path.join(HERE, "..", "raw.html")

UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Mobile Safari/537.36")

# purity -> the fraction of the 999 figure it should sit at
PURITY = {
    "999": 1.000,
    "995": 0.996,   # IBJA computes 995 at 0.996 of 999, not 0.995
    "916": 0.916,
    "750": 0.750,
    "585": 0.585,
}
RATIO_TOL = 0.02        # 2 points either side of the expected ratio
MOVE_TOL = 0.15         # 15% against the previously published figure
FLOOR, CEIL = 10000.0, 1000000.0   # rupees per 10g of 999 gold, sanity bounds

# How far the two sources may disagree on 999 before the API is disbelieved.
# They agreed to four significant figures on the days this was written, so a
# gap this wide means one of them is reading something else entirely.
CROSS_TOL = 0.02

# The free tier is 100 calls a month. The ceiling sits below it on purpose:
# the remainder is the allowance for calls made by hand from the metals.dev
# dashboard, which the meter here cannot see but the quota still counts.
QUOTA_CAP = 80

API_TRIES = 2           # each attempt is a spent call, so few
SCRAPE_TRIES = 3        # free, so more
BACKOFF = (5, 20)

IST_OFFSET = 19800      # +5:30 in seconds


class Refused(Exception):
    """A source produced something we will not publish. Not a crash."""


def warn(msg):
    sys.stderr.write("%s\n" % msg)


# ----------------------------------------------------------------- transport

def get(url, timeout=45, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def retry(label, tries, fn, on_attempt=None):
    """Runs fn up to `tries` times. Returns its value, or None if all failed.

    on_attempt fires before each attempt rather than after a success, because
    for the metered source the cost is incurred by asking, not by getting an
    answer.
    """
    last = None
    for i in range(tries):
        if on_attempt:
            on_attempt()
        try:
            return fn()
        except Exception as e:          # noqa: BLE001 -- any failure retries
            last = e
            warn("%s attempt %d/%d failed: %s" % (label, i + 1, tries, e))
            if i + 1 < tries:
                time.sleep(BACKOFF[min(i, len(BACKOFF) - 1)])
    warn("%s gave up: %s" % (label, last))
    return None


# --------------------------------------------------------------------- clock

def ist_now():
    return time.gmtime(time.time() + IST_OFFSET)


def clock_session():
    """(date, session) for the newest IBJA session that can exist right now.

    Used only when the page could not be read, so there is no published label
    to copy. IBJA publishes at about 12:10 and 18:10 IST, Monday to Saturday.
    Before the morning publication the newest session is the previous working
    day's PM, so walk back over Sunday rather than claiming a session that has
    not happened.
    """
    now = ist_now()
    minutes = now.tm_hour * 60 + now.tm_min
    if minutes >= 18 * 60 + 10:
        return time.strftime("%d/%m/%Y", now), "PM"
    if minutes >= 12 * 60 + 10:
        return time.strftime("%d/%m/%Y", now), "AM"
    back = time.time() + IST_OFFSET - 86400
    while time.gmtime(back).tm_wday == 6:      # Sunday
        back -= 86400
    return time.strftime("%d/%m/%Y", time.gmtime(back)), "PM"


def published_stamp(date, sess):
    """Epoch seconds for the session's publication moment. IST is UTC+5:30."""
    try:
        d, mo, y = (int(x) for x in date.split("/"))
        hour_ist = 18 if sess == "PM" else 12
        return calendar.timegm((y, mo, d, hour_ist - 6, 40, 0, 0, 1, 0))
    except Exception:
        return int(time.time())


# --------------------------------------------------------------------- quota

def quota_read():
    month = time.strftime("%Y-%m", ist_now())
    try:
        with open(QUOTA) as f:
            q = json.load(f)
        if q.get("month") == month:
            return q
    except Exception:
        pass
    return {"month": month, "calls": 0, "cap": QUOTA_CAP}


def quota_write(q):
    q["cap"] = QUOTA_CAP
    q["updated"] = int(time.time())
    os.makedirs(DOCS, exist_ok=True)
    with open(QUOTA, "w") as f:
        json.dump(q, f, indent=2, sort_keys=True)
        f.write("\n")


# ----------------------------------------------------------------- the scrape

def pane(html, tab_id):
    """The markup of one tab pane, from its id to the end of its table."""
    i = html.find('id="%s"' % tab_id)
    if i < 0:
        return ""
    j = html.find("</table>", i)
    return html[i:j] if j > 0 else html[i:]


def rows(block):
    """Every <tr> in the block, as a list of (data-label, text) pairs."""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", block, re.S | re.I):
        cells = re.findall(
            r'<td[^>]*data-label\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</td>',
            tr, re.S | re.I)
        if cells:
            out.append([(k.strip(), re.sub(r"<[^>]+>", "", v))
                        for k, v in cells])
    return out


def number(text):
    t = text.replace(",", "").replace("\u20b9", "").replace("&nbsp;", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def read_session(html, tab_id):
    """(date_string, rates) from the newest row of one pane, or (None, {})."""
    for row in rows(pane(html, tab_id)):
        by = {k: v for k, v in row}
        date = None
        for k, v in row:
            m = re.search(r"\d{2}/\d{2}/\d{4}", v)
            if m:
                date = m.group(0)
                break
        if not date:
            continue
        rates = {}
        ok = True
        for p in PURITY:
            v = number(by.get("Gold %s" % p, ""))
            if v is None or v <= 0:
                ok = False
                break
            rates[p] = v
        if not ok:
            continue
        s = number(by.get("Silver 999", ""))
        if s and s > 0:
            rates["silver999"] = s
        pt = number(by.get("Platinum 999", ""))
        if pt and pt > 0:
            rates["platinum999"] = pt
        return date, rates
    return None, {}


def as_date(s):
    """dd/mm/yyyy to a sortable tuple. Comparing these as strings almost works
    and then quietly stops: "01/09/2026" sorts below "30/08/2026"."""
    try:
        d, m, y = (int(x) for x in s.split("/"))
        return (y, m, d)
    except Exception:
        return (0, 0, 0)


def read_today(html):
    """(date, session, rates) from the current day's table, or (None, None, {}).

    The Previous Dates tables are named literally: a day appears there only
    once it has closed, so reading them alone leaves the feed permanently one
    publication behind. Today's figures sit in their own table.

    That table's id carries its own state -- TodayRatesTableDataNo before
    IBJA publishes, TodayRatesTableDataYes after -- so the element being
    looked for is not the element that appears once there is something to
    read. Both are accepted here. Its cells are ASP label spans rather than
    data-label attributes, so it is read by label id, one per purity per
    session.
    """
    block = ""
    for ident in ("TodayRatesTableDataYes", "TodayRatesTableDataNo"):
        b = pane(html, ident)
        if b:
            block = b
            break
    if not block:
        return None, None, {}

    def cell(purity, sess):
        m = re.search(
            r'id\s*=\s*["\']lbl%s_%s["\'][^>]*>([^<]*)<' % (purity, sess),
            block, re.I)
        return number(m.group(1)) if m else None

    for sess in ("PM", "AM"):
        rates = {}
        for p in PURITY:
            v = cell("Gold" + p, sess)
            if v is None or v <= 0:
                rates = {}
                break
            rates[p] = v
        if not rates:
            continue
        for key, label in (("silver999", "Silver999"),
                           ("platinum999", "Platinum999")):
            v = cell(label, sess)
            if v and v > 0:
                rates[key] = v
        date = time.strftime("%d/%m/%Y", ist_now())
        return date, sess, rates

    return None, None, {}


def pick_session(html):
    """Today's figures if they are published, else the newest closed day."""
    t_date, t_sess, t_rates = read_today(html)
    if t_rates:
        return t_sess, t_date, t_rates

    am_date, am = read_session(html, "tab-am")
    pm_date, pm = read_session(html, "tab-pm")
    if pm and (not am or as_date(pm_date) >= as_date(am_date)):
        return "PM", pm_date, pm
    if am:
        return "AM", am_date, am
    return None, None, {}


def scrape(debug=False):
    """(session, date, rates) from the page, or None if it gave us nothing."""
    def once():
        html = get(SRC)
        if debug:
            with open(RAW, "w") as f:
                f.write(html)
        sess, date, rates = pick_session(html)
        if not rates:
            raise Refused("no complete set of purities on the page")
        return sess, date, rates

    return retry("scrape", SCRAPE_TRIES, once)


# -------------------------------------------------------------------- the API

def find_number(obj, *names):
    """The first numeric value under any of `names`, at any depth.

    By name rather than by path, because a response shape can gain a wrapper
    without the figure we want moving anywhere meaningful. A KeyError three
    levels deep is a worse failure than a slightly loose search.
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names and isinstance(v, (int, float)):
                return float(v)
        for v in obj.values():
            found = find_number(v, *names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_number(v, *names)
            if found is not None:
                return found
    return None


def api_rates(key, quota):
    """Gold and silver from metals.dev as a rates dict, or None.

    The quota counter moves before the request is sent. A call that times out
    was still a call, and a meter that only counts successes is a meter that
    lets the quota run out.
    """
    if not key:
        warn("no METALS_DEV_KEY set, using the scrape alone")
        return None
    if quota["calls"] >= QUOTA_CAP:
        warn("quota ceiling %d reached for %s, using the scrape alone"
             % (QUOTA_CAP, quota["month"]))
        return None

    url = "%s?api_key=%s&currency=INR&unit=g" % (API, key)

    def spend():
        quota["calls"] += 1

    def once():
        if quota["calls"] > QUOTA_CAP:
            raise Refused("quota ceiling reached mid-retry")
        try:
            body = get(url, timeout=30, headers={"Accept": "application/json"})
        except urllib.error.HTTPError as e:
            # The key never goes near the message. A 401 or 429 is fully
            # described by its own number.
            raise Refused("metals.dev returned HTTP %d" % e.code)
        data = json.loads(body)
        status = str(data.get("status", "success")).lower()
        if status not in ("success", "ok"):
            raise Refused("metals.dev status %s" % status)

        gold_g = find_number(data, "ibja_gold")
        silver_g = find_number(data, "ibja_silver")
        if not gold_g or gold_g <= 0:
            raise Refused("no ibja_gold in the response")

        base = gold_g * 10.0                     # per gram -> per 10 grams
        rates = {p: base * f for p, f in PURITY.items()}
        if silver_g and silver_g > 0:
            rates["silver999"] = silver_g * 1000.0   # per gram -> per kg
        return rates

    return retry("metals.dev", API_TRIES, once, on_attempt=spend)


# ----------------------------------------------------------------- the gates

def validate(rates, prev):
    base = rates.get("999")
    if not base or not (FLOOR <= base <= CEIL):
        raise Refused("999 rate %s is outside the sane band %s-%s"
                      % (base, FLOOR, CEIL))

    # ratio gate -- catches a re-ordered or mislabelled column
    for p, expect in PURITY.items():
        if p not in rates:
            raise Refused("purity %s missing" % p)
        got = rates[p] / base
        if abs(got - expect) > RATIO_TOL:
            raise Refused("purity %s sits at %.4f of 999, expected ~%s -- the "
                          "columns look wrong, not the market" % (p, got, expect))

    # move gate -- against whatever we last published
    if prev:
        for p in PURITY:
            old = prev.get("rates", {}).get(p)
            if old and abs(rates[p] - old) / old > MOVE_TOL:
                raise Refused("purity %s moved from %s to %s, more than %d%%"
                              % (p, old, rates[p], int(MOVE_TOL * 100)))


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def payload(source, sess, date, rates):
    return {
        "schema": 1,
        "source": source,
        "session": sess,
        "rate_date": date,
        "published": published_stamp(date, sess),
        "fetched": int(time.time()),
        "currency": "INR",
        "gold_unit": "10g",
        "silver_unit": "kg",
        "platinum_unit": "10g",
        "rates": {k: round(v, 2) for k, v in rates.items()},
    }


def same(prev, new):
    return bool(prev) and all(
        prev.get(k) == new.get(k) for k in ("rates", "session", "rate_date"))


def write(path, prev, new, label):
    if same(prev, new):
        print("%s unchanged" % label)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(new, f, indent=2, sort_keys=True)
        f.write("\n")
    print("%s wrote %s %s via %s %s"
          % (label, new["rate_date"], new["session"], new["source"],
             json.dumps(new["rates"])))


# ----------------------------------------------------------------------- main

def main():
    debug = os.environ.get("KEEP_RAW") == "1"
    key = os.environ.get("METALS_DEV_KEY", "").strip()

    prev = load(OUT)
    prev_scrape = load(OUT_SCRAPE)
    quota = quota_read()

    got = scrape(debug=debug)
    scrape_payload = None
    if got:
        s_sess, s_date, s_rates = got
        try:
            validate(s_rates, prev_scrape)
            scrape_payload = payload("IBJA-scrape", s_sess, s_date, s_rates)
            write(OUT_SCRAPE, prev_scrape, scrape_payload, "gold_scrape.json")
        except Refused as e:
            warn("scrape refused: %s" % e)
            got = None

    api = api_rates(key, quota)
    quota_write(quota)

    chosen = None

    if api:
        # Platinum is not in the API. Carry the scrape's, or failing that the
        # last one published, rather than dropping a metal off the screen for
        # the sake of the source that is otherwise better.
        pt = None
        if got:
            pt = s_rates.get("platinum999")
        if pt is None and prev:
            pt = prev.get("rates", {}).get("platinum999")
        if pt:
            api["platinum999"] = pt
        if "silver999" not in api:
            fallback_silver = (s_rates.get("silver999") if got else None) or \
                (prev or {}).get("rates", {}).get("silver999")
            if fallback_silver:
                api["silver999"] = fallback_silver

        # Cross-check. Two independent sources agreeing is the strongest gate
        # available here, and it costs nothing because both were read anyway.
        if got:
            gap = abs(api["999"] - s_rates["999"]) / s_rates["999"]
            if gap > CROSS_TOL:
                warn("sources disagree on 999 by %.2f%% (api %s, scrape %s) -- "
                     "using the scrape" % (gap * 100, api["999"], s_rates["999"]))
                api = None

    if api:
        # The API carries no session label and no date of its own. Copy the
        # page's when we have it, since both are reading the same publication.
        a_date, a_sess = (s_date, s_sess) if got else clock_session()
        try:
            validate(api, prev)
            chosen = payload("metals.dev", a_sess, a_date, api)
        except Refused as e:
            warn("metals.dev refused: %s" % e)

    if chosen is None and scrape_payload is not None:
        chosen = dict(scrape_payload)

    if chosen is None:
        # A holiday, a Sunday, an outage at both ends. The previous file is
        # still correct -- it is the last thing IBJA published -- so leave it
        # alone and do not colour the run red for it.
        print("nothing publishable this run, previous file left in place")
        return

    write(OUT, prev, chosen, "gold.json")


if __name__ == "__main__":
    main()
