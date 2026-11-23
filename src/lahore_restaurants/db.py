"""SQLite schema + helpers. SQLite is the source of truth (see PLAN.md)."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "db" / "restaurants.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id INTEGER PRIMARY KEY,
    osm_type TEXT NOT NULL, osm_id TEXT NOT NULL,
    name TEXT, name_en TEXT, category TEXT, cuisine TEXT,
    latitude REAL, longitude REAL,
    street TEXT, suburb TEXT, city TEXT, postcode TEXT, address_derived TEXT,
    phone TEXT, website TEXT, opening_hours TEXT,
    osm_url TEXT, osm_tags_json TEXT,
    first_seen_at TEXT, last_seen_at TEXT, is_active INTEGER DEFAULT 1,
    UNIQUE (osm_type, osm_id)
);
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY, source TEXT, raw_file TEXT, area_name TEXT,
    started_at TEXT, finished_at TEXT,
    raw_count INTEGER, inserted_count INTEGER, updated_count INTEGER, error_message TEXT
);
-- One row per (restaurant, scrape run): history is kept so weekly/monthly runs can be compared.
CREATE TABLE IF NOT EXISTS maps_places (
    id INTEGER PRIMARY KEY,
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
    scrape_run TEXT NOT NULL,
    status TEXT NOT NULL,               -- ok | not_found | ambiguous | error
    place_key TEXT, place_url TEXT, maps_name TEXT, maps_category TEXT, address TEXT,
    phone TEXT, website TEXT, price_level TEXT,
    rating REAL, review_count INTEGER,
    hours_text TEXT, permanently_closed INTEGER,
    note TEXT, scraped_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_maps_places_rest ON maps_places(restaurant_id, scraped_at);
CREATE TABLE IF NOT EXISTS maps_reviews (
    id INTEGER PRIMARY KEY,
    maps_place_id INTEGER NOT NULL REFERENCES maps_places(id),
    restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
    review_key TEXT NOT NULL,           -- hash of text+rating+relative date, for de-dup across runs
    rating INTEGER, relative_date TEXT, review_text TEXT, owner_response TEXT,
    scraped_at TEXT NOT NULL,
    UNIQUE (restaurant_id, review_key)
);
-- Discovery: searches run directly on Google Maps ("restaurants in Gulberg, Lahore"), resumable.
CREATE TABLE IF NOT EXISTS discovery_queries (
    id INTEGER PRIMARY KEY, query TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | active | done
    new_found INTEGER DEFAULT 0, last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS discovery_skipped (   -- places seen but not food / outside Lahore
    place_key TEXT PRIMARY KEY, name TEXT, category TEXT, reason TEXT, seen_at TEXT
);
CREATE TABLE IF NOT EXISTS research_notes (
    id INTEGER PRIMARY KEY, restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
    source_name TEXT, source_url TEXT, note_type TEXT, note_text TEXT,
    observed_at TEXT, confidence TEXT, created_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(maps_places)")]
    if "place_key" not in cols:  # migrate DBs created before discovery existed
        import re
        con.execute("ALTER TABLE maps_places ADD COLUMN place_key TEXT")
        for pid, url in con.execute("SELECT id, place_url FROM maps_places").fetchall():
            m = re.search(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)", url or "")
            if m:
                con.execute("UPDATE maps_places SET place_key=? WHERE id=?", (m.group(1), pid))
        con.commit()
    con.execute("CREATE INDEX IF NOT EXISTS ix_maps_places_key ON maps_places(place_key)")
    return con
