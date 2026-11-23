"""Convert the latest raw OSM JSON into a flat CSV (unknown values stay empty)."""
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
EXPORT_DIR = ROOT / "data" / "exports"


def flatten(el: dict) -> dict:
    t = el.get("tags", {})
    center = el.get("center", {})
    lat = el.get("lat", center.get("lat"))
    lon = el.get("lon", center.get("lon"))
    parts = [t.get(k) for k in ("addr:housenumber", "addr:street", "addr:suburb", "addr:city")]
    return {
        "osm_type": el["type"],
        "osm_id": str(el["id"]),
        "name": (t.get("name") or "").strip() or None,
        "name_en": t.get("name:en"),
        "category": t.get("amenity"),
        "cuisine": t.get("cuisine"),
        "latitude": lat,
        "longitude": lon,
        "street": t.get("addr:street"),
        "suburb": t.get("addr:suburb"),
        "city": t.get("addr:city"),
        "postcode": t.get("addr:postcode"),
        "address_derived": ", ".join(p for p in parts if p) or None,
        "phone": t.get("phone") or t.get("contact:phone"),
        "website": t.get("website") or t.get("contact:website"),
        "opening_hours": t.get("opening_hours"),
        "osm_url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        "osm_tags_json": json.dumps(t, ensure_ascii=False),
    }


def main(raw: Path | None = None) -> Path:
    raw = raw or max(RAW_DIR.glob("osm_lahore_*.json"))
    elements = json.loads(raw.read_text(encoding="utf-8"))["elements"]
    df = pd.DataFrame(flatten(e) for e in elements)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = EXPORT_DIR / "lahore_restaurants.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")  # BOM so Excel reads Urdu
    print(f"{len(df)} rows | named: {df['name'].notna().sum()} | "
          f"with coords: {df['latitude'].notna().sum()}")
    print(df["category"].value_counts().to_string())
    print(f"Wrote {out}")
    return out


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else None)
