"""Discover Lahore food places directly on Google Maps, in small resumable batches.

    python -m lahore_restaurants.discover --batch 15 --reviews 10        # one batch of ~15 NEW places
    python -m lahore_restaurants.discover --batch 15 --batches 4          # four batches in a row
    python -m lahore_restaurants.discover status

How it works: a queue of searches ("restaurants in Gulberg, Lahore", ...) is stored in SQLite. Each batch
takes the next unfinished search, scrolls its result list, skips every place already stored, opens up to
--batch NEW places, saves their details (+ reviews), and stops. The next batch continues from there.
A search is marked done once its result list is exhausted. New places that match an existing OSM record
(<=150 m, similar name) are attached to that record instead of creating a duplicate.
Same safety rules as maps.py: headed browser, slow pauses, stops on CAPTCHA, never evades it.
"""
import argparse
import json
import random
import re
import sys
import time

from .db import connect, now
from .maps import (Blocked, accept_consent, check_blocked, haversine_m, open_context, pause,
                   place_key, read_place, read_reviews, save, sim, txt, Throttle)
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright
from urllib.parse import quote

LAHORE_BOX = (31.25, 31.75, 74.00, 74.65)  # lat_min, lat_max, lon_min, lon_max
AREAS = [
    "Gulberg", "MM Alam Road", "Liberty Market", "DHA Phase 1", "DHA Phase 2", "DHA Phase 3", "DHA Phase 4",
    "DHA Phase 5", "DHA Phase 6", "DHA Phase 7", "DHA Phase 8", "Cantt", "Cavalry Ground", "Johar Town",
    "Model Town", "Bahria Town", "Wapda Town", "Iqbal Town", "Allama Iqbal Town", "Faisal Town", "Garden Town",
    "Township", "Samanabad", "Shadman", "Defence Road", "Raiwind Road", "Ferozepur Road", "Canal Road",
    "Thokar Niaz Baig", "Valencia Town", "Askari", "Mall Road", "Anarkali", "Gawalmandi", "Walled City",
    "Fort Road Food Street", "Muslim Town", "Mozang", "Ichra", "Shalimar", "Harbanspura", "Jail Road",
    "Kot Lakhpat", "Sabzazar", "Gulshan-e-Ravi", "Baghbanpura", "Data Darbar", "Lake City", "Bedian Road",
    "Lahore Ring Road", "Kalma Chowk", "Qurtaba Chowk", "Barkat Market", "Fortress Stadium", "Packages Mall",
    "Emporium Mall", "Nishtar Colony", "Sanda", "Chung", "Multan Road", "GT Road", "Shahdara", "Walton",
    "Cavalry Ground", "Gulistan-e-Jauhar", "Eden", "Park View", "Bahria Orchard", "Sundar", "Pindi Road",
]
TERMS = ["restaurants", "cafes", "fast food"]
FOOD = re.compile(r"rest|caf|bake|food|pizza|burger|grill|bbq|barbe|tea|coffee|ice cream|dessert|sweet|juice|"
                  r"shawarma|karahi|biryani|halwa|kitchen|dhaba|diner|eatery|canteen|takeaway|take-out|"
                  r"buffet|steak|sandwich|chicken|noodle|chinese|thai|italian|meal|snack|bar |paratha|"
                  r"nihari|haleem|kebab|tikka|donut|waffle|shake|lounge|banquet|hotel", re.I)


def seed(con):
    if con.execute("SELECT COUNT(*) FROM discovery_queries").fetchone()[0]:
        return
    for a in dict.fromkeys(AREAS):
        for t in TERMS:
            con.execute("INSERT OR IGNORE INTO discovery_queries (query) VALUES (?)", (f"{t} in {a}, Lahore",))
    con.commit()


def known(con, key) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM maps_places WHERE place_key=? UNION ALL "
        "SELECT 1 FROM restaurants WHERE osm_type='gmaps' AND osm_id=? UNION ALL "
        "SELECT 1 FROM discovery_skipped WHERE place_key=? LIMIT 1", (key, key, key)).fetchone())


def in_lahore(lat, lon) -> bool:
    return LAHORE_BOX[0] <= lat <= LAHORE_BOX[1] and LAHORE_BOX[2] <= lon <= LAHORE_BOX[3]


def collect_new(page, con, want, max_scrolls=30):
    """Scroll the result feed; return (new_places, reached_end). Each place: dict(key, href, label, lat, lon)."""
    feed = page.locator("div[role='feed']")
    seen, new, end = set(), [], False
    for _ in range(max_scrolls):
        for a in page.locator("a.hfpxzc").all():
            href = a.get_attribute("href") or ""
            key = place_key(href)
            m = re.search(r"!3d(-?[\d.]+)!4d(-?[\d.]+)", href)
            if not key or not m or key in seen:
                continue
            seen.add(key)
            lat, lon = float(m.group(1)), float(m.group(2))
            if not in_lahore(lat, lon):
                con.execute("INSERT OR IGNORE INTO discovery_skipped VALUES (?,?,?,?,?)",
                            (key, a.get_attribute("aria-label"), None, "outside Lahore", now()))
                continue
            if not known(con, key):
                new.append({"key": key, "href": href, "label": a.get_attribute("aria-label"), "lat": lat, "lon": lon})
        con.commit()
        if page.get_by_text("You've reached the end of the list").count():
            end = True
        if len(new) >= want or end:
            break
        if feed.count():
            feed.first.evaluate("e => e.scrollBy(0, e.scrollHeight)")
        else:
            break
        page.wait_for_timeout(2200)
    return new, end


def link_or_create(con, d, data):
    """Attach to a nearby un-enriched OSM record with a similar name, else create a 'gmaps' restaurant."""
    dlat, dlon = 0.0015, 0.0015  # ~150 m box
    cands = con.execute(
        """SELECT r.* FROM restaurants r WHERE r.latitude BETWEEN ? AND ? AND r.longitude BETWEEN ? AND ?
           AND r.osm_type != 'gmaps'
           AND NOT EXISTS (SELECT 1 FROM maps_places m WHERE m.restaurant_id=r.id AND m.status='ok')""",
        (d["lat"] - dlat, d["lat"] + dlat, d["lon"] - dlon, d["lon"] + dlon)).fetchall()
    for r in cands:
        tags = json.loads(r["osm_tags_json"] or "{}")
        n = r["name_en"] or tags.get("brand:en") or tags.get("brand") or r["name"] or ""
        label = data.get("maps_name") or d["label"] or ""
        if n.isascii() and (sim(n, label) >= 0.7 or n.lower() in label.lower() or label.lower() in n.lower()):
            return r["id"], f"linked to OSM {r['osm_type']}/{r['osm_id']}"
        if not n.isascii() and haversine_m(r["latitude"], r["longitude"], d["lat"], d["lon"]) <= 60:
            return r["id"], f"linked to OSM {r['osm_type']}/{r['osm_id']} by position"
    cur = con.execute(
        "INSERT INTO restaurants (osm_type, osm_id, name, category, latitude, longitude, osm_url, osm_tags_json, "
        "first_seen_at, last_seen_at, is_active) VALUES ('gmaps', ?, ?, ?, ?, ?, ?, '{}', ?, ?, 1)",
        (d["key"], data.get("maps_name") or d["label"], data.get("maps_category"), d["lat"], d["lon"],
         data.get("place_url"), now(), now()))
    return cur.lastrowid, "new (Google Maps discovery)"


def run_batch(page, con, batch, reviews, run_id, throttle) -> bool:
    """Process one batch. Returns False when nothing is left to do or Google blocked us."""
    q = con.execute("SELECT * FROM discovery_queries WHERE status != 'done' ORDER BY id LIMIT 1").fetchone()
    if not q:
        print("All discovery searches are done.")
        return False
    print(f"\nSearch: {q['query']}  (new found so far: {q['new_found']})")
    page.goto(f"https://www.google.com/maps/search/{quote(q['query'])}?hl=en", wait_until="domcontentloaded")
    accept_consent(page)
    check_blocked(page)
    try:
        page.wait_for_selector("a.hfpxzc, h1.DUwDvf", timeout=20000)
    except PWTimeout:
        con.execute("UPDATE discovery_queries SET status='done', last_run_at=? WHERE id=?", (now(), q["id"]))
        con.commit()
        print("  no results -> marked done")
        return True
    if page.locator("h1.DUwDvf").count() and not page.locator("a.hfpxzc").count():
        con.execute("UPDATE discovery_queries SET status='done', last_run_at=? WHERE id=?", (now(), q["id"]))
        con.commit()
        print("  search opened a single place; skipping")
        return True
    new, end = collect_new(page, con, batch)
    todo = new[:batch]
    print(f"  {len(new)} new places in view (end of list: {end}); processing {len(todo)}")
    saved = 0
    for i, d in enumerate(todo, 1):
        try:
            page.goto(d["href"] if d["href"].startswith("http") else "https://www.google.com" + d["href"],
                      wait_until="domcontentloaded")
            check_blocked(page)
            page.wait_for_selector("h1.DUwDvf", timeout=15000)
            data = read_place(page)
            cat = data.get("maps_category") or ""
            if cat and not FOOD.search(cat):
                con.execute("INSERT OR IGNORE INTO discovery_skipped VALUES (?,?,?,?,?)",
                            (d["key"], data.get("maps_name"), cat, "not food", now()))
                con.commit()
                print(f"  [{i}/{len(todo)}] skip (not food): {data.get('maps_name')} [{cat}]")
            else:
                rid, how = link_or_create(con, d, data)
                revs = read_reviews(page, reviews) if reviews else []
                data["place_key"] = d["key"]
                save(con, rid, run_id, "ok", how, data, revs)
                saved += 1
                print(f"  [{i}/{len(todo)}] {data.get('maps_name')} {data.get('rating')}* "
                      f"({data.get('review_count')}) +{len(revs)} reviews -- {how}")
        except Blocked as e:
            print(f"STOPPED: Google asked for a verification ({e}). Resolve it in the open window or retry later.")
            return False
        except Exception as e:
            print(f"  [{i}/{len(todo)}] error on {d['label']}: {str(e)[:100]}")
            if not throttle.fail():
                return False
            continue
        if not throttle.ok():
            print(f"Reached --max-places cap; stopping for this run.")
            return False
    finished = end and len(new) <= batch
    con.execute("UPDATE discovery_queries SET status=?, new_found=new_found+?, last_run_at=? WHERE id=?",
                ("done" if finished else "active", saved, now(), q["id"]))
    con.commit()
    if finished:
        print("  search exhausted -> done")
    return True


def status(con):
    for st, n in con.execute("SELECT status, COUNT(*) FROM discovery_queries GROUP BY status"):
        print(f"searches {st}: {n}")
    print("restaurants:", con.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0],
          "| from Google discovery:", con.execute("SELECT COUNT(*) FROM restaurants WHERE osm_type='gmaps'").fetchone()[0],
          "| with Maps data:", con.execute("SELECT COUNT(DISTINCT restaurant_id) FROM maps_places WHERE status='ok'").fetchone()[0],
          "| reviews:", con.execute("SELECT COUNT(*) FROM maps_reviews").fetchone()[0])


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "status"])
    ap.add_argument("--batch", type=int, default=15, help="new places to save per batch (10-20 suggested)")
    ap.add_argument("--batches", type=int, default=1)
    ap.add_argument("--reviews", type=int, default=0)
    ap.add_argument("--max-places", type=int, default=100, help="hard cap of places per run (rate-limit safety)")
    a = ap.parse_args()
    con = connect()
    seed(con)
    if a.cmd == "status":
        return status(con)
    run_id = now()
    throttle = Throttle(max_places=a.max_places)
    with sync_playwright() as p:
        ctx = open_context(p, headed=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for b in range(a.batches):
            if not run_batch(page, con, a.batch, a.reviews, run_id, throttle):
                break
            if b < a.batches - 1:
                t = random.uniform(60, 120)
                print(f"Cooling down {t:.0f}s before the next batch...")
                time.sleep(t)
        ctx.close()
    status(con)


if __name__ == "__main__":
    main()
