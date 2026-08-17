#!/usr/bin/env python3
"""
Reads the published IBJA benchmark rates and writes docs/gold.json.

Design rules:
  - Never write a file we are not sure about. A holiday, a site change or a
    garbled number must leave the previous file untouched and exit non-zero.
  - Two independent gates: an absolute move gate against the last published
    figure, and a ratio gate that checks each purity against 999. The ratio
    gate is what catches a column re-order, which a move gate would miss.
  - No secrets, no tokens, no user data. One GET, nothing sent.
"""

import json
import os
import re
import sys
import time
import urllib.request

SRC = "https://ibjarates.com/"
OUT = os.path.join(os.path.dirname(__file__), "..", "docs", "gold.json")
RAW = os.path.join(os.path.dirname(__file__), "..", "raw.html")

UA = ("Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126 Mobile Safari/537.36")

# purity -> fraction of 999 that the figure should sit at
PURITY = {
    "999": 1.000,
    "995": 0.995,
    "916": 0.916,
    "750": 0.750,
    "585": 0.585,
}
RATIO_TOL = 0.02        # 2 percentage points either side of the expected ratio
MOVE_TOL = 0.15         # 15% against the previously published figure
FLOOR, CEIL = 1000.0, 100000.0   # rupees per gram, 999 gold, sanity bounds


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "replace")


def label(html: str, ident: str):
    """Pull the text out of an ASP.NET label span by id, e.g. lblGold999_AM."""
    m = re.search(
        r'id\s*=\s*["\']' + re.escape(ident) + r'["\'][^>]*>([^<]*)<',
        html, re.I)
    if not m:
        return None
    txt = m.group(1)
    txt = txt.replace(",", "").replace("\u20b9", "").replace("&nbsp;", "").strip()
    m2 = re.search(r"\d+(?:\.\d+)?", txt)
    return float(m2.group(0)) if m2 else None


def pick_session(html: str):
    """Prefer PM if it is published, else AM. Returns (session, rates dict)."""
    for sess in ("PM", "AM"):
        rates = {}
        for p in PURITY:
            v = label(html, f"lblGold{p}_{sess}")
            if v is None or v <= 0:
                rates = {}
                break
            rates[p] = v
        if rates:
            s = label(html, f"lblSilver999_{sess}")
            if s and s > 0:
                rates["silver999"] = s
            return sess, rates
    return None, {}


def published_stamp(html: str, sess: str) -> int:
    """Epoch seconds for the session's publication moment, using the date shown
    on the page. IBJA publishes at about 12:10 and 18:10 IST. Falls back to now."""
    import calendar
    m = re.search(r"(\d{2})[-/](\d{2})[-/](\d{4})", html)
    hour_ist = 18 if sess == "PM" else 12
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            # IST = UTC+5:30, so 12:10 IST is 06:40 UTC and 18:10 IST is 12:40 UTC
            return calendar.timegm((y, mo, d, hour_ist - 6, 40, 0, 0, 1, 0))
        except Exception:
            pass
    return int(time.time())


def load_previous():
    try:
        with open(OUT, "r") as f:
            return json.load(f)
    except Exception:
        return None


def die(msg: str):
    sys.stderr.write("REFUSED: " + msg + "\n")
    sys.exit(1)


def main():
    debug = os.environ.get("KEEP_RAW") == "1"
    try:
        html = fetch(SRC)
    except Exception as e:
        die(f"could not read the source: {e}")

    if debug:
        with open(RAW, "w") as f:
            f.write(html)

    sess, rates = pick_session(html)
    if not rates:
        die("no complete set of purities found on the page "
            "(holiday, or the page layout changed)")

    base = rates["999"]
    if not (FLOOR <= base <= CEIL):
        die(f"999 rate {base} is outside the sane band {FLOOR}-{CEIL}")

    # ratio gate - catches a column re-order or a swapped label
    for p, expect in PURITY.items():
        got = rates[p] / base
        if abs(got - expect) > RATIO_TOL:
            die(f"purity {p} sits at {got:.4f} of 999, expected ~{expect} "
                f"- the columns look wrong, not the market")

    # move gate - against whatever we last published
    prev = load_previous()
    if prev:
        for p in PURITY:
            old = prev.get("rates", {}).get(p)
            if old:
                if abs(rates[p] - old) / old > MOVE_TOL:
                    die(f"purity {p} moved from {old} to {rates[p]}, "
                        f"more than {int(MOVE_TOL*100)}% - not publishing")

    payload = {
        "schema": 1,
        "source": "IBJA",
        "session": sess,
        "published": published_stamp(html, sess),
        "fetched": int(time.time()),
        "currency": "INR",
        "unit": "gram",
        "silver_unit": "kg",
        "rates": {k: round(v, 2) for k, v in rates.items()},
    }

    if prev and prev.get("rates") == payload["rates"] \
            and prev.get("session") == payload["session"]:
        print("unchanged - nothing to commit")
        return

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    print("wrote", json.dumps(payload["rates"]), payload["session"])


if __name__ == "__main__":
    main()
