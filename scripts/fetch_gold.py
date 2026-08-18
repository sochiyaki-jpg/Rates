#!/usr/bin/env python3
"""
Reads the published IBJA benchmark rates and writes docs/gold.json.

Source: the "Previous Dates Rate" tables on ibjarates.com, which carry an AM
and a PM pane, each listing 999, 995, 916, 750 and 585 gold plus silver 999
and platinum 999, newest row first. Every cell is tagged with a data-label
naming the metal, so the parse is by name rather than by column position and
survives a column being added or moved.

Figures are rupees per 10 grams for gold and platinum, and per kilogram for
silver, which is how IBJA publishes them and how the newspaper prints them.
They are passed through unchanged rather than converted; the app does its own
arithmetic and a converted figure is one more thing that can be wrong.

Design rules:
  - Never write a file we are not sure about. A holiday, a site change or a
    garbled number must leave the previous file untouched and exit non-zero.
  - Two independent gates: an absolute move gate against the last published
    figure, and a ratio gate that checks each purity against 999. The ratio
    gate is what catches a re-ordered or mislabelled column.
  - No secrets, no tokens, no user data. One GET, nothing sent.
"""

import calendar
import json
import os
import re
import sys
import time
import urllib.request

SRC = "https://ibjarates.com/"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "docs", "gold.json")
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


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def pane(html: str, tab_id: str) -> str:
    """The markup of one tab pane, from its id to the end of its table."""
    i = html.find('id="%s"' % tab_id)
    if i < 0:
        return ""
    j = html.find("</table>", i)
    return html[i:j] if j > 0 else html[i:]


def rows(block: str):
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


def number(text: str):
    t = text.replace(",", "").replace("\u20b9", "").replace("&nbsp;", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def read_session(html: str, tab_id: str):
    """(date_string, rates dict) from the newest row of one pane, or (None, {})."""
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


def read_today(html: str):
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

    Everything this returns still goes through the same gates, and anything
    it cannot supply falls back to the closed days, so a further change to
    this markup costs a day rather than a wrong number.
    """
    block = ""
    for ident in ("TodayRatesTableDataYes", "TodayRatesTableDataNo"):
        b = pane(html, ident)
        if b:
            block = b
            break
    if not block:
        return None, None, {}

    def cell(purity: str, sess: str):
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
        for key, label in (("silver999", "Silver999"), ("platinum999", "Platinum999")):
            v = cell(label, sess)
            if v and v > 0:
                rates[key] = v
        # The page is written for an Indian audience in IST; the runner is UTC.
        date = time.strftime("%d/%m/%Y", time.gmtime(time.time() + 19800))
        return date, sess, rates

    return None, None, {}


def pick_session(html: str):
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


def published_stamp(date: str, sess: str) -> int:
    """Epoch seconds for the session's publication moment. IBJA publishes at
    about 12:10 and 18:10 IST; IST is UTC+5:30."""
    try:
        d, mo, y = (int(x) for x in date.split("/"))
        hour_ist = 18 if sess == "PM" else 12
        return calendar.timegm((y, mo, d, hour_ist - 6, 40, 0, 0, 1, 0))
    except Exception:
        return int(time.time())


def load_previous():
    try:
        with open(OUT) as f:
            return json.load(f)
    except Exception:
        return None


def die(msg: str):
    sys.stderr.write("REFUSED: " + msg + "\n")
    sys.exit(1)


def validate(rates, prev):
    base = rates["999"]
    if not (FLOOR <= base <= CEIL):
        die("999 rate %s is outside the sane band %s-%s" % (base, FLOOR, CEIL))

    # ratio gate - catches a re-ordered or mislabelled column
    for p, expect in PURITY.items():
        got = rates[p] / base
        if abs(got - expect) > RATIO_TOL:
            die("purity %s sits at %.4f of 999, expected ~%s - the columns "
                "look wrong, not the market" % (p, got, expect))

    # move gate - against whatever we last published
    if prev:
        for p in PURITY:
            old = prev.get("rates", {}).get(p)
            if old and abs(rates[p] - old) / old > MOVE_TOL:
                die("purity %s moved from %s to %s, more than %d%% - not "
                    "publishing" % (p, old, rates[p], int(MOVE_TOL * 100)))


def build(html):
    sess, date, rates = pick_session(html)
    if not rates:
        die("no complete set of purities found (holiday, or the page changed)")
    prev = load_previous()
    validate(rates, prev)
    return prev, {
        "schema": 1,
        "source": "IBJA",
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


def main():
    debug = os.environ.get("KEEP_RAW") == "1"
    try:
        html = fetch(SRC)
    except Exception as e:
        die("could not read the source: %s" % e)

    if debug:
        with open(RAW, "w") as f:
            f.write(html)

    prev, payload = build(html)

    if prev and prev.get("rates") == payload["rates"] \
            and prev.get("session") == payload["session"] \
            and prev.get("rate_date") == payload["rate_date"]:
        print("unchanged - nothing to commit")
        return

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    print("wrote", payload["rate_date"], payload["session"],
          json.dumps(payload["rates"]))


if __name__ == "__main__":
    main()
