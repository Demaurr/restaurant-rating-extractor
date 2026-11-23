"""Import a raw OSM JSON file into SQLite (idempotent upsert on osm_type+osm_id)."""
import json
import sys
from pathlib import Path

from .db import connect, now
from .to_csv import RAW_DIR, flatten

COLS = ["name", "name_en", "category", "cuisine", "latitude", "longitude", "street", "suburb",
        "city", "postcode", "address_derived", "phone", "website", "opening_hours",
        "osm_url", "osm_tags_json"]


def import_file(raw: Path) -> None:
    started = now()
    elements = json.loads(raw.read_text(encoding="utf-8"))["elements"]
    con = connect()
    ins = upd = 0
    for el in elements:
        r = flatten(el)
        exists = con.execute("SELECT 1 FROM restaurants WHERE osm_type=? AND osm_id=?",
                             (r["osm_type"], r["osm_id"])).fetchone()
        if exists:
            sets = ", ".join(f"{c}=?" for c in COLS)
            con.execute(f"UPDATE restaurants SET {sets}, last_seen_at=?, is_active=1 "
                        "WHERE osm_type=? AND osm_id=?",
                        [r[c] for c in COLS] + [now(), r["osm_type"], r["osm_id"]])
            upd += 1
        else:
            con.execute(
                f"INSERT INTO restaurants (osm_type, osm_id, {', '.join(COLS)}, first_seen_at, last_seen_at) "
                f"VALUES (?, ?, {', '.join('?' * len(COLS))}, ?, ?)",
                [r["osm_type"], r["osm_id"]] + [r[c] for c in COLS] + [now(), now()])
            ins += 1
    con.execute("INSERT INTO import_runs (source, raw_file, area_name, started_at, finished_at, "
                "raw_count, inserted_count, updated_count) VALUES (?,?,?,?,?,?,?,?)",
                ("openstreetmap_overpass", raw.name, "Lahore", started, now(), len(elements), ins, upd))
    con.commit()
    print(f"Imported {raw.name}: {len(elements)} raw, {ins} inserted, {upd} updated")


if __name__ == "__main__":
    import_file(Path(sys.argv[1]) if len(sys.argv) > 1 else max(RAW_DIR.glob("osm_lahore_*.json")))
