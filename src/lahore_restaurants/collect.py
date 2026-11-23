"""Fetch Lahore food businesses from Overpass and save the raw response unchanged."""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
QUERY_FILE = ROOT / "queries" / "lahore_restaurants.overpassql"
RAW_DIR = ROOT / "data" / "raw"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def collect() -> Path:
    query = QUERY_FILE.read_text(encoding="utf-8")
    resp = None
    for attempt, url in enumerate(OVERPASS_URLS * 2):
        if attempt:
            time.sleep(10)
        try:
            resp = requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": "lahore-restaurant-research/0.1"},
                timeout=120,
            )
            resp.raise_for_status()
            break
        except requests.RequestException as e:
            print(f"{url} failed: {e}")
            resp = None
    if resp is None:
        raise SystemExit("All Overpass attempts failed; try again later.")
    if not resp.json().get("elements"):
        raise SystemExit(
            "Overpass returned 0 elements (server overload or wrong area selector); "
            "nothing saved. Check the boundary in Overpass Turbo and retry later."
        )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f"osm_lahore_{stamp}.json"
    out.write_text(resp.text, encoding="utf-8")
    print(f"Saved {len(resp.json().get('elements', []))} elements -> {out}")
    return out


if __name__ == "__main__":
    collect()
