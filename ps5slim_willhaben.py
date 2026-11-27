#!/usr/bin/env python3
"""
PS5 Slim watcher (separate from the PS5 Pro watcher)

1) Sony PlayStation Direct AT: stock alerts for PS5 Slim, new and
   "Generalüberholt" (certified refurbished), with Disc and Digital.
2) Willhaben.at: new PS5 Slim ads priced BELOW 400 EUR.

  python ps5slim_willhaben.py                 loop forever (home PC / Pi)
  python ps5slim_willhaben.py --once          one round, then exit
  python ps5slim_willhaben.py --duration 240  loop ~240 s, then exit (GitHub Actions)

Needs:  pip install requests   and the env var / secret NTFY_TOPIC
"""
import html as htmllib
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
RENOTIFY_AFTER = 15 * 60       # re-alert every 15 min while still in stock

# Sony Direct Austria (stock alerts)
SONY_PAGES = {
    "Sony Direct AT PS5 Slim Disc (new)":
        "https://direct.playstation.com/de-at/buy-consoles/playstation5-console-model-group-slim",
    "Sony Direct AT PS5 Slim Digital (new)":
        "https://direct.playstation.com/de-at/buy-consoles/playstation5-digital-edition-console-model-group-slim",
    "Sony Direct AT PS5 Digital 825 GB (new)":
        "https://direct.playstation.com/de-at/buy-consoles/playstation5-digital-edition-console-825-gb",
    "Sony Direct AT refurbished PS5 Slim Disc":
        "https://direct.playstation.com/de-at/buy-consoles/certified-refurbished-playstation5-console-model-group-slim",
    "Sony Direct AT refurbished PS5 Slim Digital":
        "https://direct.playstation.com/de-at/buy-consoles/certified-refurbished-playstation5-digital-edition-console-model-group-slim",
}
SONY_MAX_PRICE = 700           # ignore anything priced above this (EUR)

# Willhaben ads
WILLHABEN_ENABLED = True
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
STRONG_OUT = [r"nicht zum kauf erhältlich", r"derzeit nicht vorrätig", r"nicht vorrätig",
              r"ausverkauft", r"sold out", r"out of stock", r"currently unavailable",
              r"keine lieferung möglich", r"schau bald wieder vorbei"]
WEAK_OUT = [r"zurzeit nicht verf", r"derzeit nicht verf", r"nicht verfügbar"]
IN_PATTERNS = [r"in den warenkorb", r"add to cart", r"jetzt kaufen", r"in den einkaufswagen"]
DEADLINE = None


# ------------------------------ helpers --------------------------------
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


def parse_jsonld(page):
    avail, price = None, None
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                            page, re.S | re.I):
        try:
            data = json.loads(block)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                if "availability" in node:
                    avail = str(node["availability"])
                    price = node.get("price", price)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return avail, price


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


# --------------------------- Sony Direct stock -------------------------
def check_stock(url):
    """Return (status, detail): IN_STOCK / OUT / UNKNOWN."""
    r = requests.get(url, headers=HEADERS, timeout=10)
    if r.status_code in (403, 429, 503):
        return "UNKNOWN", f"blocked/rate-limited (HTTP {r.status_code})"
    if r.status_code != 200:
        return "UNKNOWN", f"HTTP {r.status_code}"
    text = htmllib.unescape(r.text).lower()
    notice = bool(re.search(r"nicht zum kauf erhältlich", text))

    avail, price = parse_jsonld(r.text)
    if avail:
        note = " | page says 'Nicht zum Kauf erhältlich'" if notice else ""
        if "instock" in avail.lower() or "limitedavailability" in avail.lower():
            try:
                if price and float(str(price).replace(",", ".")) > SONY_MAX_PRICE:
                    return "OUT", f"in stock but price {price} > limit"
            except ValueError:
                pass
            conflict = " - CONFLICT, verify by hand!" if notice else ""
            return "IN_STOCK", f"schema.org: {avail}, price {price}{note}{conflict}"
        return "OUT", f"schema.org: {avail}{note}"

    strong = next((p for p in STRONG_OUT if re.search(p, text)), None)
    weak = next((p for p in WEAK_OUT if re.search(p, text)), None)
    buy = next((p for p in IN_PATTERNS if re.search(p, text)), None)
    if strong or weak:
        extra = f"; buy-button text also present ('{buy}')" if buy else ""
        return "OUT", f"out-of-stock text found ('{strong or weak}'){extra}"
    if buy:
        return "IN_STOCK", "buy button text found (verify manually!)"
    return "UNKNOWN", "could not determine (page may load content via JavaScript)"


def check_sony(last_alert):
    for name, url in SONY_PAGES.items():
        if DEADLINE and time.time() > DEADLINE:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        try:
            status, detail = check_stock(url)
        except Exception as e:
            status, detail = "UNKNOWN", f"error: {e}"
        print(f"[{ts}] {name}: {status} - {detail}", flush=True)
        if status == "IN_STOCK" and time.time() - last_alert.get(url, 0) > RENOTIFY_AFTER:
            last_alert[url] = time.time()
            notify(f"PS5 Slim available: {name}", f"{detail}\n{url}", url)
        time.sleep(random.uniform(0.3, 1.0))


# ------------------------------ Willhaben ------------------------------
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


def check_willhaben(seen):
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
    if not ads:
        return
    n_title = n_word = n_price = n_age = n_new = 0
    for ad in sorted(ads.values(), key=lambda a: a["age_min"] if a["age_min"] is not None else 1e9):
        t = ad["title"].lower()
        compact = re.sub(r"[\s\-_.]", "", t)               # "PS 5 Slim" -> "ps5slim"
        if "ps5slim" not in compact and "playstation5slim" not in compact:
            continue
        n_title += 1
        if any(re.search(r"\b" + re.escape(w) + (r"\b" if w == "pro" else ""), t) for w in BAD_WORDS):
            continue
        n_word += 1
        if not (MIN_PRICE <= ad["price"] < MAX_PRICE):
            continue
        n_price += 1
        if ad["age_min"] is not None and ad["age_min"] > MAX_AGE_MIN:
            continue
        n_age += 1
        if ad["id"] in seen:
            continue
        seen.add(ad["id"])
        n_new += 1
        notify(f"New PS5 Slim ad: EUR {ad['price']:.0f}", f"{ad['title']}\n{ad['url']}", ad["url"])
    print(f"[{ts}] Willhaben: {len(ads)} ads | Slim titles {n_title} | ok words {n_word} | "
          f"price {MIN_PRICE}-{MAX_PRICE} {n_price} | fresh {n_age} | NEW {n_new}", flush=True)
    newest = sorted((a for a in ads.values() if a["age_min"] is not None),
                    key=lambda a: a["age_min"])[:2]
    for a in newest:
        print(f"    newest: '{a['title'][:50]}' EUR {a['price']:.0f}, {a['age_min']:.0f} min old", flush=True)


# -------------------------------- main ---------------------------------
def main():
    global DEADLINE
    once = "--once" in sys.argv
    duration = int(sys.argv[sys.argv.index("--duration") + 1]) if "--duration" in sys.argv else None
    DEADLINE = time.time() + duration if duration else None
    interval = 30 if duration else CHECK_INTERVAL
    seen, last_alert = set(), {}
    print(f"Watching {len(SONY_PAGES)} Sony pages + Willhaben (Slim < EUR {MAX_PRICE}) every ~{interval}s.")
    while True:
        start = time.time()
        check_sony(last_alert)
        if WILLHABEN_ENABLED and not (DEADLINE and time.time() > DEADLINE):
            check_willhaben(seen)
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
