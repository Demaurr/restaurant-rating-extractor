[out:json][timeout:90];

// Lahore District (OSM relation 16117666, admin_level 6). Area id = 3600000000 + relation id.
// Chosen after listing admin boundaries in the Lahore bbox on 2026-11-19; see PLAN.md 4.2.
area(id:3616117666)->.searchArea;

(
  nwr["amenity"="restaurant"](area.searchArea);
  nwr["amenity"="cafe"](area.searchArea);
  nwr["amenity"="fast_food"](area.searchArea);
);

out center tags;
