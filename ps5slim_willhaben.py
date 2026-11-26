#!/usr/bin/env python3
"""
PS5 Slim watcher for Willhaben.at  (separate from the PS5 Pro watcher)

Sends an instant ntfy push with the ad link when a NEW PS5 Slim ad
priced BELOW 400 EUR appears.

  python ps5slim_willhaben.py                 loop forever (home PC / Pi)
  python ps5slim_willhaben.py --once          one round, then exit
  python ps5slim_willhaben.py --duration 240  loop ~240 s, then exit (GitHub Actions)

Needs:  pip install requests   and the env var / secret NTFY_TOPIC
"""
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import quote

import requests

# ----------------------------- CONFIG ---------------------------------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "change-me-to-a-long-random-string")
CHECK_INTERVAL = 30            # seconds between rounds when looping forever

KEYWORDS = ["ps5 slim", "playstation 5 slim", "playstation5 slim", "ps 5 slim"]
MAX_PRICE = 400                # alert only if price is BELOW this (EUR)
MIN_PRICE = 150                # ignore bait / accessory ads below this
MAX_AGE_MIN = 8                # alert only for ads published in the last N minutes
BAD_WORDS = ["suche", "defekt", "bastler", "ersatzteil", "tausch", "hülle", "huelle",
             "standfuß", "standfuss", "laufwerk", "ankauf", "kaufe", "reparatur",
             "nur controller", "nur spiel", "pro"]   # "pro" = PS5 Pro ads (other script)
# ---------------------------------------------------------------------

URL = ("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz"
       "?keyword={kw}&sort=1&rows=30")          # sort=1 -> newest first
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.5",
}
DEADLINE = None


def find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, key)
            if r is not None:
                return r
    return None


def fetch_ads(keyword):
    r = requests.get(URL.format(kw=quote(keyword)), headers=HEADERS, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("page format not recognised")
    ads, now = [], time.time()
    for ad in find_key(json.loads(m.group(1)), "advertSummary") or []:
        try:
            attrs = {a["name"]: a.get("values", []) for a in ad["attributes"]["attribute"]}
            first = lambda k: (attrs.get(k) or [None])[0]
            published = first("PUBLISHED")                  # epoch milliseconds
            ads.append({
                "id": str(ad.get("id")),
                "title": ad.get("description") or first("HEADING") or "",
                "price": float(str(first("PRICE") or "0").replace(",", ".")),
                "url": "https://www.willhaben.at/iad/" + (first("SEO_URL") or "").lstrip("/"),
                "age_min": (now - int(published) / 1000) / 60 if published else None,
            })
        except Exception:
            continue
    return ads


def notify(title, message, url):
    try:
        requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers={"Title": title.encode("utf-8"), "Priority": "urgent",
                               "Tags": "video_game,rotating_light", "Click": url},
                      timeout=10)
    except Exception as e:
        print("  ntfy failed:", e)
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{message}" with title "{title}" sound name "Glass"'])
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", title, f"{message}\n{url}"])
        elif sys.platform == "win32":
            import winsound
            for _ in range(5):
                winsound.Beep(1200, 300)
    except Exception:
        pass


def check_round(seen):
    ts = datetime.now().strftime("%H:%M:%S")
    ads, errors = {}, []
    for kw in KEYWORDS:
        if DEADLINE and time.time() > DEADLINE:
            break
        try:
            for ad in fetch_ads(kw):
                ads[ad["id"]] = ad
        except Exception as e:
            errors.append(f"'{kw}': {e}")
        time.sleep(random.uniform(0.3, 1.0))
    if errors:
        print(f"[{ts}] Willhaben: UNKNOWN - {'; '.join(errors)}", flush=True)
    new = 0
    for ad in ads.values():
        t = ad["title"].lower()
        compact = re.sub(r"[\s\-_.]", "", t)               # "PS 5 Slim" -> "ps5slim"
        if "ps5slim" not in compact and "playstation5slim" not in compact:
            continue
        if any(re.search(r"\b" + re.escape(w) + (r"\b" if w == "pro" else ""), t)
               for w in BAD_WORDS):
            continue
        if not (MIN_PRICE <= ad["price"] < MAX_PRICE):
            continue
        if ad["age_min"] is not None and ad["age_min"] > MAX_AGE_MIN:
            continue
        if ad["id"] in seen:
            continue
        seen.add(ad["id"])
        new += 1
        notify(f"New PS5 Slim ad: EUR {ad['price']:.0f}", f"{ad['title']}\n{ad['url']}", ad["url"])
    if ads:
        print(f"[{ts}] Willhaben: {len(ads)} ads read, {new} new match(es)", flush=True)


def main():
    global DEADLINE
    once = "--once" in sys.argv
    duration = int(sys.argv[sys.argv.index("--duration") + 1]) if "--duration" in sys.argv else None
    DEADLINE = time.time() + duration if duration else None
    interval = 30 if duration else CHECK_INTERVAL
    seen = set()
    print(f"Watching Willhaben for PS5 Slim below EUR {MAX_PRICE} every ~{interval}s.")
    while True:
        start = time.time()
        check_round(seen)
        if once:
            return
        wait = max(1, interval - (time.time() - start))
        if DEADLINE and time.time() + wait >= DEADLINE:
            return
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
