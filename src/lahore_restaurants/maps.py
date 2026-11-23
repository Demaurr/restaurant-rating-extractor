"""Google Maps enrichment through a persistent, logged-in browser profile.

Usage (run from the project's src/ folder):
    python -m lahore_restaurants.maps import-cookies cookies.json   # one-time (Google blocks
                                                            # sign-in inside automated browsers)
    python -m lahore_restaurants.maps run --limit 20        # enrich 20 restaurants
    python -m lahore_restaurants.maps run --limit 20 --reviews 30
    python -m lahore_restaurants.maps stats

Design rules:
  * You sign in yourself, once, in a real browser window. The session lives in data/browser_profile
    (git-ignored). Use a dedicated Google account, not a personal one.
  * Slow and polite: random 6-14 s pauses, small batches, resumable (restaurants with a recent
    snapshot are skipped), every run is stored as a new snapshot so month-to-month changes show up.
  * On a CAPTCHA / "unusual traffic" page the run STOPS. It never tries to solve or evade it:
    solve it by hand in the open window, or wait and resume later.
  * Reviewer names/profiles are deliberately NOT stored - only rating, relative date and text.
"""
import argparse
import hashlib
import random
import re
import time
from difflib import SequenceMatcher
from urllib.parse import quote

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from .db import ROOT, connect, now

PROFILE_DIR = ROOT / "data" / "browser_profile"


MAX_DISTANCE_M = 250        # fuzzy name match must be this close to the OSM point
MAX_DISTANCE_M_EXACT = 600  # near-exact name match (sim >= 0.85) may be a bit farther (OSM points are loose)


def place_key(url):
    mm = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", url or "")
    return mm.group(1) if mm else None


def haversine_m(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 12742000 * asin(sqrt(a))


class Blocked(Exception):
    """Google asked for a CAPTCHA / unusual-traffic check."""


def open_context(p, headed: bool):
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    kwargs = dict(user_data_dir=str(PROFILE_DIR), headless=not headed, locale="en-US",
                  viewport={"width": 1366, "height": 900})
    try:
        return p.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception:  # Chrome not installed -> bundled Chromium (python -m playwright install chromium)
        return p.chromium.launch_persistent_context(**kwargs)


def pause(lo=6.0, hi=14.0):
    time.sleep(random.uniform(lo, hi))


class Throttle:
    """Polite pacing: jittered delay per place, longer rest every N places, exponential backoff on failures.

    Failures (timeouts, missing page elements) are the usual symptom of Google rate limiting, so they slow us
    down progressively (30s, 60s, 120s ... capped at 10 min) and abort after `max_failures` in a row.
    """
    def __init__(self, lo=8.0, hi=16.0, rest_every=10, rest=(45, 90), max_failures=3, max_places=100):
        self.lo, self.hi, self.rest_every, self.rest = lo, hi, rest_every, rest
        self.max_failures, self.max_places = max_failures, max_places
        self.count = self.failures = 0

    def ok(self):
        self.count += 1
        self.failures = 0
        if self.count >= self.max_places:
            return False
        if self.count % self.rest_every == 0:
            t = random.uniform(*self.rest)
            print(f"  ...resting {t:.0f}s to stay under rate limits")
            time.sleep(t)
        else:
            time.sleep(random.uniform(self.lo, self.hi))
        return True

    def fail(self):
        self.failures += 1
        if self.failures >= self.max_failures:
            print(f"STOPPED: {self.failures} failures in a row - likely rate limited. Wait an hour+ and resume.")
            return False
        t = min(600, 30 * 2 ** (self.failures - 1)) * random.uniform(1.0, 1.3)
        print(f"  ...backing off {t:.0f}s after failure {self.failures}")
        time.sleep(t)
        return True


def check_blocked(page):
    if "/sorry/" in page.url or page.locator("text=unusual traffic").count():
        raise Blocked(page.url)


def accept_consent(page):
    for label in ("Accept all", "I agree", "Reject all"):
        btn = page.get_by_role("button", name=label)
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(1500)
            return


def txt(page, selector):
    loc = page.locator(selector)
    return loc.first.inner_text().strip() if loc.count() else None


def attr(page, selector, name):
    loc = page.locator(selector)
    return loc.first.get_attribute(name) if loc.count() else None


def sim(a, b):
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def find_place(page, r):
    """Search near the OSM coordinates; return ('ok'|'not_found'|'ambiguous', note)."""
    import json
    tags = json.loads(r["osm_tags_json"] or "{}")
    name = r["name_en"] or tags.get("brand:en") or tags.get("brand") or r["name"]
    if not name.isascii() and tags.get("brand") and tags["brand"].isascii():
        name = tags["brand"]
    q = quote(name)
    loc = f"@{r['latitude']},{r['longitude']},17z" if r["latitude"] else ""
    page.goto(f"https://www.google.com/maps/search/{q}/{loc}?hl=en", wait_until="domcontentloaded")
    accept_consent(page)
    check_blocked(page)
    try:
        page.wait_for_selector("h1.DUwDvf, a.hfpxzc, div:has-text('Google Maps can')", timeout=15000)
    except PWTimeout:
        return "not_found", "timeout waiting for results"
    if page.locator("h1.DUwDvf").count():          # Maps jumped straight to one place: validate it too
        label = txt(page, "h1.DUwDvf")
        s = sim(name, label)
        if name.lower() in (label or "").lower() or (label or "").lower() in name.lower():
            s = max(s, 0.85)
        if not name.isascii() or s < 0.7:
            return "ambiguous", f"direct landing on '{label}' sim={s:.2f} (rejected)"
        return "ok", f"direct '{label}' sim={s:.2f}"
    links = page.locator("a.hfpxzc")
    if not links.count():
        return "not_found", "no results"
    # Score every result by name similarity AND distance from the OSM point (place URLs carry !3d<lat>!4d<lng>).
    best_href, best_score, best_note = None, 0.0, ""
    seen = []
    for i in range(links.count()):
        label, href = links.nth(i).get_attribute("aria-label"), links.nth(i).get_attribute("href")
        m = re.search(r"!3d(-?[\d.]+)!4d(-?[\d.]+)", href or "")
        if not m or not r["latitude"]:
            continue
        dist = haversine_m(r["latitude"], r["longitude"], float(m.group(1)), float(m.group(2)))
        s = sim(name, label)
        if name.lower() in (label or "").lower() or (label or "").lower() in name.lower():
            s = max(s, 0.85)
        seen.append((dist, s, label))
        if not name.isascii():  # Urdu-only OSM name: Maps label is English, so trust only a very close point
            s = 0.7 if dist <= 80 else 0.0
        limit = MAX_DISTANCE_M_EXACT if s >= 0.85 else MAX_DISTANCE_M
        if dist > limit or s < 0.7:
            continue
        score = s - dist / (limit * 4)
        if score > best_score:
            best_href, best_score, best_note = href, score, f"'{label}' sim={s:.2f} dist={dist:.0f}m"
    if not best_href:
        near = sorted(seen)[:3]
        cands = "; ".join(f"{l} ({d:.0f}m, sim {x:.2f})" for d, x, l in near)
        return "ambiguous", f"no match; nearest: {cands}" if cands else "no candidates with coordinates"
    page.goto(best_href if best_href.startswith("http") else "https://www.google.com" + best_href,
              wait_until="domcontentloaded")
    check_blocked(page)
    page.wait_for_selector("h1.DUwDvf", timeout=15000)
    return "ok", best_note


def read_place(page) -> dict:
    page.wait_for_timeout(1500)
    rating = txt(page, "div.F7nice span[aria-hidden='true']")
    count_label = attr(page, "div.F7nice span[aria-label*='review']", "aria-label") or ""
    m = re.search(r"([\d,]+)", count_label)
    if not m:  # fallback: "4.1 (1,234)" in the rating block text
        m = re.search(r"\(([\d,]+)\)", txt(page, "div.F7nice") or "")
    if not m:
        m = re.search(r"([\d,]+)\s+reviews", page.locator("body").inner_text())
    hours = attr(page, "[data-item-id='oh'] [aria-label], div[aria-label*='Hours']", "aria-label")
    price = attr(page, "span[aria-label^='Price']", "aria-label")
    body = page.locator("body").inner_text()
    return {
        "place_key": place_key(page.url),
        "place_url": page.url,
        "maps_name": txt(page, "h1.DUwDvf"),
        "maps_category": txt(page, "button.DkEaL"),
        "address": attr(page, "button[data-item-id='address']", "aria-label"),
        "phone": attr(page, "button[data-item-id^='phone']", "aria-label"),
        "website": attr(page, "a[data-item-id='authority']", "href"),
        "price_level": price,
        "rating": float(rating) if rating and re.fullmatch(r"\d(\.\d)?", rating) else None,
        "review_count": int(m.group(1).replace(",", "")) if m else None,
        "hours_text": hours,
        "permanently_closed": int("Permanently closed" in body),
    }


def read_reviews(page, want: int) -> list[dict]:
    tab = page.get_by_role("tab", name=re.compile("Reviews"))
    if not tab.count():
        return []
    tab.first.click()
    page.wait_for_timeout(2000)
    sort = page.get_by_role("button", name=re.compile("Sort", re.I))
    if sort.count():
        sort.first.click()
        page.wait_for_timeout(600)
        newest = page.get_by_role("menuitemradio", name="Newest")
        if newest.count():
            newest.first.click()
            page.wait_for_timeout(1500)
    cards = page.locator("div.jftiEf[data-review-id]")
    stalls = 0
    while cards.count() < want and stalls < 3:
        before = cards.count()
        cards.last.scroll_into_view_if_needed() if before else None
        page.mouse.move(400, 500)
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(random.randint(1200, 2200))
        stalls = stalls + 1 if cards.count() == before else 0
    for b in page.locator("div.jftiEf button.w8nwRe").all():  # expand "More"
        try:
            b.click(timeout=500)
        except Exception:
            pass
    out = []
    for i in range(min(cards.count(), want)):
        c = cards.nth(i)
        rt = c.locator("span.kvMYJc").first.get_attribute("aria-label") if c.locator("span.kvMYJc").count() else ""
        rm = re.search(r"(\d)", rt or "")
        text = c.locator("span.wiI7pd").first.inner_text().strip() if c.locator("span.wiI7pd").count() else ""
        when = c.locator("span.rsqaWe").first.inner_text().strip() if c.locator("span.rsqaWe").count() else ""
        resp = c.locator("div.CDe7pd").first.inner_text().strip() if c.locator("div.CDe7pd").count() else None
        key = hashlib.sha1(f"{rm.group(1) if rm else ''}|{text}".encode()).hexdigest()
        out.append({"review_key": key, "rating": int(rm.group(1)) if rm else None,
                    "relative_date": when, "review_text": text or None, "owner_response": resp})
    return out


def save(con, rid, run, status, note, data=None, reviews=()):
    data = data or {}
    cols = ["place_key", "place_url", "maps_name", "maps_category", "address", "phone", "website",
            "price_level", "rating", "review_count", "hours_text", "permanently_closed"]
    cur = con.execute(
        f"INSERT INTO maps_places (restaurant_id, scrape_run, status, note, scraped_at, {', '.join(cols)}) "
        f"VALUES (?,?,?,?,?,{', '.join('?' * len(cols))})",
        [rid, run, status, note, now()] + [data.get(c) for c in cols])
    for rv in reviews:
        con.execute("INSERT OR IGNORE INTO maps_reviews (maps_place_id, restaurant_id, review_key, rating, "
                    "relative_date, review_text, owner_response, scraped_at) VALUES (?,?,?,?,?,?,?,?)",
                    (cur.lastrowid, rid, rv["review_key"], rv["rating"], rv["relative_date"],
                     rv["review_text"], rv["owner_response"], now()))
    con.commit()


def cmd_login(_):
    with sync_playwright() as p:
        ctx = open_context(p, headed=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/")
        input("Sign in to the dedicated Google account in the window, then press Enter here...")
        ctx.close()
    print(f"Session saved in {PROFILE_DIR}")


SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}


def load_cookie_file(path) -> list[dict]:
    """Read a Cookie-Editor / EditThisCookie JSON export or a Netscape cookies.txt file."""
    import json
    from pathlib import Path
    raw = Path(path).read_text(encoding="utf-8").strip()
    cookies = []
    if raw.startswith("["):
        for c in json.loads(raw):
            ck = {"name": c["name"], "value": c["value"], "domain": c["domain"],
                  "path": c.get("path", "/"), "secure": bool(c.get("secure", True)),
                  "httpOnly": bool(c.get("httpOnly", False)),
                  "sameSite": SAMESITE.get(str(c.get("sameSite", "lax")).lower(), "Lax")}
            exp = c.get("expirationDate", c.get("expires"))
            if exp and not c.get("session"):
                ck["expires"] = float(exp)
            cookies.append(ck)
    else:  # Netscape format: domain, subdomains, path, secure, expiry, name, value
        for line in raw.splitlines():
            line = line.removeprefix("#HttpOnly_")
            parts = line.split("\t")
            if line.startswith("#") or len(parts) < 7:
                continue
            d, _, path_, secure, exp, name, value = parts[:7]
            ck = {"name": name, "value": value, "domain": d, "path": path_,
                  "secure": secure.upper() == "TRUE", "sameSite": "Lax"}
            if exp.isdigit() and int(exp) > 0:
                ck["expires"] = float(exp)
            cookies.append(ck)
    return [c for c in cookies if "google." in c["domain"] or "youtube." in c["domain"]]


def cmd_import_cookies(a):
    cookies = load_cookie_file(a.file)
    if not cookies:
        raise SystemExit("No google.com cookies found in that file.")
    with sync_playwright() as p:
        ctx = open_context(p, headed=False)
        ctx.add_cookies(cookies)
        page = ctx.new_page()
        page.goto("https://www.google.com/maps?hl=en", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        signed_in = page.get_by_role("link", name=re.compile("Sign in")).count() == 0
        ctx.close()
    print(f"Imported {len(cookies)} cookies into {PROFILE_DIR}")
    print("Looks signed in." if signed_in else
          "Maps still shows 'Sign in' - the cookies were rejected/expired. Re-export while logged in.")
    print("Now DELETE the cookie file: it is full access to that Google account.")


def cmd_run(a):
    con = connect()
    rows = con.execute(
        """SELECT * FROM restaurants r
           WHERE r.name IS NOT NULL AND r.latitude IS NOT NULL AND r.is_active = 1
             AND NOT EXISTS (SELECT 1 FROM maps_places m WHERE m.restaurant_id = r.id
                             AND m.status IN ('ok','not_found','ambiguous')
                             AND m.scraped_at > datetime('now', ?))
           ORDER BY (r.suburb IS NULL), r.id LIMIT ?""",
        (f"-{a.refresh_days} days", a.limit)).fetchall()
    print(f"{len(rows)} restaurants to enrich")
    run = now()
    throttle = Throttle(max_places=a.max_places)
    with sync_playwright() as p:
        ctx = open_context(p, headed=not a.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for n, r in enumerate(rows, 1):
            try:
                status, note = find_place(page, r)
                if status == "ok":
                    data = read_place(page)
                    revs = read_reviews(page, a.reviews) if a.reviews else []
                    save(con, r["id"], run, "ok", note, data, revs)
                    print(f"[{n}/{len(rows)}] {r['name']} -> {data['maps_name']} "
                          f"{data['rating']}* ({data['review_count']}) +{len(revs)} reviews")
                else:
                    save(con, r["id"], run, status, note)
                    print(f"[{n}/{len(rows)}] {r['name']} -> {status}: {note}")
            except Blocked as e:
                print(f"STOPPED: Google asked for a verification ({e}). Resolve it manually "
                      f"(in the open window) or retry later. Progress is saved.")
                break
            except Exception as e:  # keep going; log the failure
                save(con, r["id"], run, "error", str(e)[:300])
                print(f"[{n}/{len(rows)}] {r['name']} -> error: {str(e)[:120]}")
                if not throttle.fail():
                    break
                continue
            if not throttle.ok():
                print(f"Reached --max-places {a.max_places}; stopping for this run.")
                break
        ctx.close()


def cmd_stats(_):
    con = connect()
    for q in ("SELECT COUNT(*) FROM restaurants", "SELECT COUNT(DISTINCT restaurant_id) FROM maps_places WHERE status='ok'",
              "SELECT COUNT(*) FROM maps_reviews"):
        print(q, "->", con.execute(q).fetchone()[0])


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(required=True)
    sub.add_parser("login").set_defaults(fn=cmd_login)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    ic = sub.add_parser("import-cookies", help="load cookies exported from your own browser")
    ic.add_argument("file")
    ic.set_defaults(fn=cmd_import_cookies)
    rp = sub.add_parser("run")
    rp.add_argument("--limit", type=int, default=20)
    rp.add_argument("--reviews", type=int, default=0, help="max reviews per place (0 = skip)")
    rp.add_argument("--max-places", type=int, default=100, help="hard cap per run (rate-limit safety)")
    rp.add_argument("--refresh-days", type=int, default=30)
    rp.add_argument("--headless", action="store_true",
                    help="NOT recommended: Google serves headless browsers a stripped page (no review counts/reviews)")
    rp.set_defaults(fn=cmd_run)
    args = ap.parse_args()
    args.fn(args)
