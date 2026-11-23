"""Lahore Restaurant Intelligence dashboard.  Run:  streamlit run app/streamlit_app.py"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from lahore_restaurants.db import DB_PATH, connect  # noqa: E402
from lahore_restaurants.discover import AREAS  # noqa: E402

ACCENT, GOLD, TEAL, MUTED = "#ff6b35", "#ffc857", "#2ec4b6", "#8a94a6"
SEQ = ["#2b2d42", "#5c4d7d", "#b5446e", ACCENT, GOLD]
st.set_page_config(page_title="Lahore Restaurant Intelligence", page_icon="🍽️", layout="wide")

st.markdown(f"""
<style>
.block-container {{padding-top: 1.4rem; max-width: 1400px;}}
h1, h2, h3 {{letter-spacing: -0.02em;}}
.hero {{background: linear-gradient(120deg,#1d2733 0%,#2a1f2d 55%,#3a2118 100%); border:1px solid #2b3442;
        border-radius: 18px; padding: 22px 26px; margin-bottom: 14px;}}
.hero h1 {{margin:0; font-size: 1.9rem;}} .hero p {{margin:.25rem 0 0; color:{MUTED};}}
.kpis {{display:grid; grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); gap:12px; margin: 6px 0 14px;}}
.kpi {{background:#171d26; border:1px solid #263041; border-radius:14px; padding:14px 16px;}}
.kpi .l {{color:{MUTED}; font-size:.78rem; text-transform:uppercase; letter-spacing:.06em;}}
.kpi .v {{font-size:1.6rem; font-weight:700; line-height:1.15;}} .kpi .s {{color:{MUTED}; font-size:.78rem;}}
.kpi.accent .v {{color:{ACCENT};}} .kpi.gold .v {{color:{GOLD};}} .kpi.teal .v {{color:{TEAL};}}
.review {{background:#171d26; border:1px solid #263041; border-radius:12px; padding:12px 14px; margin-bottom:10px;}}
.review .m {{color:{MUTED}; font-size:.8rem;}} .chip {{display:inline-block; padding:2px 10px; border-radius:99px;
        background:#222b38; color:#cbd3df; font-size:.78rem; margin:0 6px 6px 0;}}
div[data-baseweb="tab-list"] {{gap: 4px; flex-wrap: wrap;}}
button[data-baseweb="tab"] {{border-radius: 10px 10px 0 0; padding: 8px 14px;}}
@media (max-width: 700px) {{ .hero h1 {{font-size:1.4rem}} .kpi .v {{font-size:1.4rem}} }}
</style>""", unsafe_allow_html=True)


def kpi(label, value, sub="", cls=""):
    return f'<div class="kpi {cls}"><div class="l">{label}</div><div class="v">{value}</div><div class="s">{sub}</div></div>'


def style(fig, h=340):
    fig.update_layout(template="plotly_dark", height=h, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      margin=dict(l=8, r=8, t=36, b=8), font=dict(color="#cbd3df"),
                      legend=dict(orientation="h", y=-0.2))
    return fig


AREA_RE = [(a, re.compile(re.escape(a.replace(" Road", "")), re.I)) for a in sorted(set(AREAS), key=len, reverse=True)]


def area_of(addr):
    if addr is None or pd.isna(addr) or not str(addr).strip():
        return None
    addr = str(addr)
    m = re.search(r"DHA\s*Phase\s*(\d+)", addr, re.I)
    if m:
        return f"DHA Phase {m.group(1)}"
    for name, rx in AREA_RE:
        if rx.search(addr):
            return "Gulberg" if name in ("MM Alam Road",) else name
    return "Other / unknown"


@st.cache_data(ttl=60, show_spinner=False)
def load():
    con = connect()
    q = lambda sql: pd.read_sql_query(sql, con)
    places = q("""
        SELECT r.id, COALESCE(m.maps_name, r.name) AS name, r.name AS osm_name, r.osm_type AS source, r.category AS osm_category,
               r.latitude AS lat, r.longitude AS lon, r.cuisine, r.suburb,
               m.maps_category, m.address, m.phone, m.website, m.price_level, m.rating, m.review_count,
               m.hours_text, m.permanently_closed, m.place_url, m.scraped_at, m.note, m.status AS maps_status,
               r.osm_url
        FROM restaurants r
        LEFT JOIN maps_places m ON m.id = (SELECT MAX(id) FROM maps_places WHERE restaurant_id=r.id AND status='ok')
        WHERE r.is_active=1""")
    places["source"] = places["source"].map(lambda s: "Google Maps" if s == "gmaps" else "OpenStreetMap")
    places["enriched"] = places["rating"].notna() | places["place_url"].notna()
    for c, p in (("address", "Address: "), ("phone", "Phone: "), ("price_level", "Price: ")):
        places[c] = places[c].astype("string").str.replace(p, "", regex=False).str.strip()
    places["category"] = places["maps_category"].fillna(places["osm_category"].str.replace("_", " ").str.title())
    places["area"] = places["address"].map(area_of)
    reviews = q("""SELECT v.*, COALESCE(m.maps_name, r.name) AS place FROM maps_reviews v
                   JOIN restaurants r ON r.id=v.restaurant_id
                   LEFT JOIN maps_places m ON m.id=v.maps_place_id""")
    attempts = q("""SELECT m.restaurant_id, r.name, m.status, m.note, m.scraped_at, r.latitude, r.longitude
                    FROM maps_places m JOIN restaurants r ON r.id=m.restaurant_id
                    WHERE m.status IN ('ambiguous','not_found','error')
                      AND m.id=(SELECT MAX(id) FROM maps_places WHERE restaurant_id=m.restaurant_id)""")
    history = q("SELECT restaurant_id, scraped_at, rating, review_count FROM maps_places WHERE status='ok'")
    imports = q("SELECT * FROM import_runs ORDER BY id DESC")
    queries = q("SELECT * FROM discovery_queries")
    skipped = q("SELECT * FROM discovery_skipped")
    return places, reviews, attempts, history, imports, queries, skipped


if not DB_PATH.exists():
    st.error("No database yet. Run the collector and importer first.")
    st.stop()
places, reviews, attempts, history, imports, queries, skipped = load()
enr = places[places["rating"].notna()].copy()
C = enr["rating"].mean() if len(enr) else 0
M = 200  # prior weight for the Bayesian "weighted rating"
enr["score"] = (enr["review_count"].fillna(0) / (enr["review_count"].fillna(0) + M)) * enr["rating"] + (M / (enr["review_count"].fillna(0) + M)) * C

places["score"] = places["id"].map(enr.set_index("id")["score"])

last = pd.to_datetime(places["scraped_at"]).max()
st.markdown(f"""<div class="hero"><h1>🍽️ Lahore Restaurant Intelligence</h1>
<p>OpenStreetMap directory enriched with Google Maps data · {len(places):,} places ·
last Maps update: {last.strftime('%d %b %Y %H:%M') if pd.notna(last) else 'never'}</p></div>""", unsafe_allow_html=True)

with st.sidebar:
    st.header("Global filters")
    srcs = st.multiselect("Source", sorted(places["source"].unique()), default=sorted(places["source"].unique()))
    cats = st.multiselect("Category", sorted(places["category"].dropna().unique()))
    areas = st.multiselect("Area", sorted(places["area"].dropna().unique()))
    only_enriched = st.toggle("Only places with Google Maps data", value=False)
    st.caption("Filters apply to Overview, Explorer, Map and Analysis.")
    if st.button("🔄 Reload data"):
        st.cache_data.clear()
        st.rerun()

f = places[places["source"].isin(srcs)]
if cats:
    f = f[f["category"].isin(cats)]
if areas:
    f = f[f["area"].isin(areas)]
if only_enriched:
    f = f[f["rating"].notna()]
fe = f[f["rating"].notna()]
fid = set(f["id"])

tab_over, tab_explore, tab_map, tab_an, tab_place, tab_quality, tab_pipe = st.tabs(
    ["📊 Overview", "🔎 Explorer", "🗺️ Map", "📈 Analysis", "🏪 Place detail", "🧹 Data quality", "⚙️ Pipeline"])

# ---------------------------------------------------------------- Overview
with tab_over:
    tot_rev = int(fe["review_count"].fillna(0).sum())
    st.markdown('<div class="kpis">' + "".join([
        kpi("Places", f"{len(f):,}", f"{(f['source']=='Google Maps').sum()} found via Google Maps", "accent"),
        kpi("With Maps data", f"{len(fe):,}", f"{len(fe)/max(len(f),1):.0%} of filtered", "teal"),
        kpi("Avg rating", f"{fe['rating'].mean():.2f}★" if len(fe) else "–", f"median {fe['rating'].median():.1f}" if len(fe) else "", "gold"),
        kpi("Google reviews", f"{tot_rev:,}", "sum of public counts"),
        kpi("Reviews stored", f"{int(reviews['restaurant_id'].isin(fid).sum()):,}", "text samples in DB"),
        kpi("Permanently closed", f"{int(fe['permanently_closed'].fillna(0).sum())}", "flagged on Maps"),
    ]) + "</div>", unsafe_allow_html=True)

    c1, c2 = st.columns(2)
    with c1:
        d = f["category"].fillna("Unknown").value_counts().head(12).reset_index()
        d.columns = ["category", "places"]
        fig = px.bar(d.iloc[::-1], x="places", y="category", orientation="h", title="Places by category",
                     color="places", color_continuous_scale=SEQ)
        st.plotly_chart(style(fig).update_coloraxes(showscale=False), use_container_width=True)
    with c2:
        if len(fe):
            fig = px.histogram(fe, x="rating", nbins=20, title="Rating distribution", color_discrete_sequence=[ACCENT])
            fig.add_vline(x=fe["rating"].mean(), line_dash="dash", line_color=GOLD,
                          annotation_text=f"avg {fe['rating'].mean():.2f}")
            st.plotly_chart(style(fig), use_container_width=True)
        else:
            st.info("No Maps ratings yet - run the enrichment/discovery commands.")
    c3, c4 = st.columns(2)
    with c3:
        prog = pd.DataFrame({"state": ["Enriched", "Pending"], "n": [int(f["rating"].notna().sum()), int(f["rating"].isna().sum())]})
        fig = px.pie(prog, names="state", values="n", hole=.62, title="Enrichment progress",
                     color="state", color_discrete_map={"Enriched": TEAL, "Pending": "#2b3442"})
        st.plotly_chart(style(fig), use_container_width=True)
    with c4:
        d = fe["area"].value_counts().head(12).reset_index()
        d.columns = ["area", "places"]
        if len(d):
            fig = px.bar(d.iloc[::-1], x="places", y="area", orientation="h", title="Top areas (enriched places)",
                         color_discrete_sequence=[GOLD])
            st.plotly_chart(style(fig), use_container_width=True)
    if len(fe):
        st.subheader("Top rated (confidence-weighted)")
        top = fe.sort_values("score", ascending=False).head(10)[["name", "category", "area", "rating", "review_count", "score"]]
        st.dataframe(top, hide_index=True, width="stretch",
                     column_config={"rating": st.column_config.NumberColumn("Rating", format="%.1f ★"),
                                    "review_count": st.column_config.NumberColumn("Reviews", format="%d"),
                                    "score": st.column_config.ProgressColumn("Weighted score", min_value=1, max_value=5, format="%.2f")})

# ---------------------------------------------------------------- Explorer
with tab_explore:
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    text = c1.text_input("Search name / address / cuisine", placeholder="e.g. bundu khan, gulberg, bbq")
    rmin = c2.slider("Min rating", 0.0, 5.0, 0.0, 0.1)
    vmin = c3.number_input("Min reviews", 0, 100000, 0, step=50)
    sort = c4.selectbox("Sort by", ["Weighted score", "Rating", "Reviews", "Name"])
    e = f.copy()
    if text:
        blob = (e["name"].fillna("") + " " + e["address"].fillna("") + " " + e["cuisine"].fillna("") + " " + e["category"].fillna("")).str.lower()
        e = e[blob.str.contains(re.escape(text.lower()))]
    if rmin:
        e = e[e["rating"] >= rmin]
    if vmin:
        e = e[e["review_count"].fillna(0) >= vmin]
    key = {"Weighted score": ("score", False), "Rating": ("rating", False), "Reviews": ("review_count", False), "Name": ("name", True)}[sort]
    e = e.sort_values(key[0], ascending=key[1], na_position="last")
    e["gmaps"] = e.apply(lambda r: r["place_url"] or f"https://www.google.com/maps/search/?api=1&query={quote(str(r['name']) + ' Lahore')}", axis=1)
    st.caption(f"{len(e):,} places")
    show = e[["name", "category", "area", "rating", "review_count", "price_level", "address", "phone", "website", "source", "gmaps"]]
    st.dataframe(show, hide_index=True, width="stretch", height=560, column_config={
        "rating": st.column_config.NumberColumn("Rating", format="%.1f ★"),
        "review_count": st.column_config.NumberColumn("Reviews", format="%d"),
        "gmaps": st.column_config.LinkColumn("Maps", display_text="open"),
        "website": st.column_config.LinkColumn("Website", display_text="visit")})
    st.download_button("⬇️ Download CSV", show.to_csv(index=False).encode("utf-8-sig"), "lahore_places_filtered.csv", "text/csv")

# ---------------------------------------------------------------- Map
with tab_map:
    m = f.dropna(subset=["lat", "lon"]).copy()
    mode = st.radio("Colour by", ["Rating", "Source", "Category"], horizontal=True)
    m["size"] = (m["review_count"].fillna(0).clip(upper=10000) ** 0.5 / 4 + 5)
    m["label"] = m["name"] + " · " + m["rating"].map(lambda x: f"{x:.1f}★" if pd.notna(x) else "no rating")
    kw = dict(lat="lat", lon="lon", size="size", hover_name="label", hover_data={"lat": False, "lon": False, "size": False, "area": True, "category": True},
              zoom=10.6, center=dict(lat=31.50, lon=74.35), height=640, size_max=18)
    if mode == "Rating":
        fig = px.scatter_map(m, color="rating", color_continuous_scale=["#5c4d7d", ACCENT, GOLD], range_color=(3, 5), **kw)
    else:
        fig = px.scatter_map(m, color="source" if mode == "Source" else "category", **kw)
    fig.update_layout(map_style="carto-darkmatter", margin=dict(l=0, r=0, t=0, b=0), paper_bgcolor="rgba(0,0,0,0)")
    st.plotly_chart(fig, use_container_width=True)
    st.caption("Bubble size = number of Google reviews. Grey/unrated points have not been enriched yet.")

# ---------------------------------------------------------------- Analysis
with tab_an:
    if not len(fe):
        st.info("Analysis needs places with Maps data.")
    else:
        c1, c2 = st.columns(2)
        with c1:
            fig = px.scatter(fe, x="review_count", y="rating", hover_name="name", color="category", log_x=True,
                             title="Popularity vs rating (reviews on log scale)")
            st.plotly_chart(style(fig, 380), use_container_width=True)
        with c2:
            ag = (fe.groupby("area").agg(places=("id", "count"), avg=("rating", "mean"), reviews=("review_count", "sum")).reset_index())
            ag = ag[ag["places"] >= 2].sort_values("avg")
            fig = px.bar(ag, x="avg", y="area", orientation="h", color="places", color_continuous_scale=SEQ,
                         title="Average rating by area (≥2 places)", range_x=[min(3, ag["avg"].min() - .1) if len(ag) else 3, 5])
            st.plotly_chart(style(fig, 380), use_container_width=True)
        c3, c4 = st.columns(2)
        with c3:
            cat = fe[fe["category"].isin(fe["category"].value_counts().head(8).index)]
            fig = px.box(cat, x="category", y="rating", color="category", title="Rating spread by category",
                         color_discrete_sequence=px.colors.qualitative.Set2)
            st.plotly_chart(style(fig).update_layout(showlegend=False), use_container_width=True)
        with c4:
            pl = fe["price_level"].dropna()
            if len(pl):
                d = pl.value_counts().reset_index()
                d.columns = ["price", "n"]
                st.plotly_chart(style(px.bar(d, x="price", y="n", title="Price level", color_discrete_sequence=[TEAL])), use_container_width=True)
            else:
                d = fe.assign(bucket=pd.cut(fe["review_count"].fillna(0), [-1, 50, 200, 1000, 5000, 1e9], labels=["<50", "50-200", "200-1k", "1k-5k", "5k+"]))
                d = d["bucket"].value_counts().sort_index().reset_index()
                d.columns = ["reviews", "n"]
                st.plotly_chart(style(px.bar(d, x="reviews", y="n", title="Places by review volume", color_discrete_sequence=[TEAL])), use_container_width=True)
        c5, c6 = st.columns(2)
        with c5:
            st.subheader("🏆 Hidden gems")
            st.caption("High rating (≥4.5) but relatively few reviews (<500)")
            g = fe[(fe["rating"] >= 4.5) & (fe["review_count"].fillna(0) < 500)].sort_values("rating", ascending=False)
            st.dataframe(g[["name", "area", "rating", "review_count"]].head(10), hide_index=True, width="stretch")
        with c6:
            st.subheader("⚠️ Under-performers")
            st.caption("Rating ≤3.6 with ≥200 reviews")
            b = fe[(fe["rating"] <= 3.6) & (fe["review_count"].fillna(0) >= 200)].sort_values("rating")
            st.dataframe(b[["name", "area", "rating", "review_count"]].head(10), hide_index=True, width="stretch")

        st.subheader("Review text insights")
        rv = reviews[reviews["restaurant_id"].isin(fid)]
        if len(rv):
            a, b = st.columns(2)
            with a:
                d = rv["rating"].value_counts().sort_index().reset_index()
                d.columns = ["stars", "n"]
                st.plotly_chart(style(px.bar(d, x="stars", y="n", title=f"Stored review ratings (n={len(rv)})",
                                             color_discrete_sequence=[GOLD])), width="stretch")
            with b:
                themes = {"Food/taste": r"food|taste|delicious|flavou?r|yummy|tasty|fresh|portion",
                          "Service/staff": r"service|staff|waiter|rude|friendly|manager|slow",
                          "Price/value": r"price|expensive|cheap|value|overpriced|costly|worth",
                          "Ambience": r"ambien|atmosphere|decor|view|music|clean|seating|rooftop",
                          "Hygiene": r"hygien|dirty|clean|fly|flies|smell"}
                txt = rv["review_text"].fillna("").str.lower()
                rows = [{"theme": k, "mentions": int(txt.str.contains(v).sum()),
                         "avg stars": rv.loc[txt.str.contains(v), "rating"].mean()} for k, v in themes.items()]
                td = pd.DataFrame(rows)
                st.plotly_chart(style(px.bar(td, x="theme", y="mentions", color="avg stars", color_continuous_scale=["#b5446e", GOLD, TEAL],
                                             title="What reviewers talk about (colour = avg stars)")), width="stretch")
            st.caption(f"Owner response rate in stored reviews: {rv['owner_response'].notna().mean():.0%}. "
                       "Keyword themes are a rough signal, not sentiment analysis.")

# ---------------------------------------------------------------- Place detail
with tab_place:
    options = places[places["rating"].notna()].sort_values("name")
    if options.empty:
        st.info("No enriched places yet.")
    else:
        pick = st.selectbox("Choose a place", options["id"], format_func=lambda i: f"{options.set_index('id').loc[i, 'name']}  ·  {options.set_index('id').loc[i, 'area']}")
        p = places.set_index("id").loc[pick]
        a, b = st.columns([3, 2])
        with a:
            st.subheader(p["name"])
            chips = [x for x in [p["category"], p["area"], p["price_level"], "Closed" if p["permanently_closed"] == 1 else None] if pd.notna(x) and x]
            st.markdown("".join(f'<span class="chip">{c}</span>' for c in chips), unsafe_allow_html=True)
            st.markdown(f"**{p['rating']}★** from **{int(p['review_count']) if pd.notna(p['review_count']) else '–'}** Google reviews")
            for lab, v in (("📍 Address", p["address"]), ("📞 Phone", p["phone"]), ("🌐 Website", p["website"]), ("🕒 Hours", p["hours_text"])):
                if v is not None and pd.notna(v):
                    st.write(f"{lab}: {v}")
            st.link_button("Open in Google Maps", p["place_url"]) if pd.notna(p["place_url"]) else None
            if p["osm_url"] and str(p["osm_url"]).startswith("https://www.openstreetmap"):
                st.link_button("OpenStreetMap record", p["osm_url"])
            st.caption(f"Source: {p['source']} · {p['note'] or ''} · scraped {p['scraped_at']}")
        with b:
            h = history[history["restaurant_id"] == pick].sort_values("scraped_at")
            if len(h) > 1:
                fig = go.Figure(go.Scatter(x=h["scraped_at"], y=h["rating"], mode="lines+markers", line=dict(color=ACCENT)))
                st.plotly_chart(style(fig.update_layout(title="Rating over time"), 260), use_container_width=True)
            else:
                st.info("Only one snapshot so far - run again next month to see trends.")
            if pd.notna(p["lat"]):
                st.map(pd.DataFrame({"lat": [p["lat"]], "lon": [p["lon"]]}), zoom=14, height=220)
        rv = reviews[reviews["restaurant_id"] == pick]
        st.subheader(f"Stored reviews ({len(rv)})")
        for _, r in rv.iterrows():
            stars = "★" * int(r["rating"] or 0) + "☆" * (5 - int(r["rating"] or 0))
            resp = f"<div class='m'>↳ Owner: {r['owner_response']}</div>" if r["owner_response"] else ""
            st.markdown(f"<div class='review'><div class='m'>{stars} · {r['relative_date'] or ''}</div>{r['review_text'] or '<i>(rating only)</i>'}{resp}</div>", unsafe_allow_html=True)

# ---------------------------------------------------------------- Data quality
with tab_quality:
    named = places["osm_name"].notna().sum()
    st.markdown('<div class="kpis">' + "".join([
        kpi("Never attempted", f"{int((places['rating'].isna() & ~places['id'].isin(attempts['restaurant_id'])).sum()):,}", "waiting for enrichment"),
        kpi("Ambiguous", f"{(attempts['status']=='ambiguous').sum()}", "need a human decision", "gold"),
        kpi("Not found / errors", f"{(attempts['status']!='ambiguous').sum()}", "retry later", "accent"),
        kpi("OSM without name", f"{int(places[places['source']=='OpenStreetMap']['osm_name'].isna().sum())}", "cannot be searched"),
        kpi("Missing phone", f"{int(fe['phone'].isna().sum())}", f"of {len(fe)} enriched"),
        kpi("Missing website", f"{int(fe['website'].isna().sum())}", f"of {len(fe)} enriched"),
    ]) + "</div>", unsafe_allow_html=True)
    st.subheader("Ambiguous matches - needs review")
    st.caption("The note lists the nearest Google Maps candidates (distance from the OSM point, name similarity). "
               "Open the search to decide by eye.")
    if len(attempts):
        t = attempts.copy()
        t["search"] = t.apply(lambda r: f"https://www.google.com/maps/search/{quote(str(r['name']))}/@{r['latitude']},{r['longitude']},17z", axis=1)
        st.dataframe(t[["name", "status", "note", "scraped_at", "search"]], hide_index=True, width="stretch", height=420,
                     column_config={"search": st.column_config.LinkColumn("Check on Maps", display_text="open"),
                                    "note": st.column_config.TextColumn(width="large")})
    else:
        st.success("Nothing to review.")

# ---------------------------------------------------------------- Pipeline
with tab_pipe:
    if len(queries):
        done = (queries["status"] == "done").sum()
        st.markdown('<div class="kpis">' + "".join([
            kpi("Discovery searches", f"{len(queries)}", "area × category"),
            kpi("Done", f"{done}", f"{done/len(queries):.0%}", "teal"),
            kpi("In progress", f"{(queries['status']=='active').sum()}", "", "gold"),
            kpi("Pending", f"{(queries['status']=='pending').sum()}", ""),
            kpi("Skipped places", f"{len(skipped)}", "non-food / outside Lahore"),
            kpi("New via discovery", f"{int(queries['new_found'].sum())}", "", "accent"),
        ]) + "</div>", unsafe_allow_html=True)
        st.progress(done / len(queries), text="Discovery queue")
        st.dataframe(queries.sort_values("last_run_at", ascending=False), hide_index=True, width="stretch", height=260)
    st.subheader("OSM import history")
    st.dataframe(imports, hide_index=True, width="stretch")
    st.subheader("Run commands (from the src folder)")
    st.code("python -m lahore_restaurants.discover --batch 15 --reviews 10 --batches 4\n"
            "python -m lahore_restaurants.maps run --limit 20 --reviews 10\n"
            "python -m lahore_restaurants.discover status", language="bash")
    st.caption("Data © OpenStreetMap contributors (ODbL). Google Maps data is for internal research only.")
