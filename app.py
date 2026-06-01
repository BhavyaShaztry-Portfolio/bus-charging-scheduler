"""
app.py — Bus Charging Scheduler
"""

import streamlit as st
import pandas as pd
from pathlib import Path
from scheduler.engine import load_scenario, run_scheduler, fmt_time

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Bus Charging Scheduler",
    page_icon="⚡",
    layout="wide",
)

st.markdown("""
<style>
[data-testid="stAppViewContainer"] { background:#0d1117; color:#e6edf3; }
[data-testid="stSidebar"] { background:#161b22; }
h1,h2,h3,h4 { color:#e6edf3; }
div[data-testid="metric-container"] {
    background:#161b22; border:1px solid #30363d;
    border-radius:8px; padding:12px 16px;
}
.badge {
    display:inline-block; padding:2px 8px; border-radius:4px;
    font-size:0.78em; font-weight:600; margin-right:4px;
}
.bk  { background:#1f3a5c; color:#58a6ff; }
.kb  { background:#1f3b2a; color:#3fb950; }
.kpn      { background:#1a2d4a; color:#79c0ff; }
.freshbus { background:#1a3328; color:#56d364; }
.flixbus  { background:#3d2a0e; color:#f0883e; }
</style>
""", unsafe_allow_html=True)

# ── Load scenarios ─────────────────────────────────────────────────────────────
SCENARIO_DIR = Path(__file__).parent / "scenarios"
scenarios_raw = {}
for f in sorted(SCENARIO_DIR.glob("scenario_*.json")):
    d = load_scenario(f)
    scenarios_raw[d["id"]] = d

# ── Header ─────────────────────────────────────────────────────────────────────
st.title("⚡ Bus Charging Scheduler")
st.caption("Bengaluru ↔ Kochi  ·  540 km  ·  4 charging stations  ·  Python + Streamlit")
st.divider()

# ── Scenario picker ────────────────────────────────────────────────────────────
selected_id = st.selectbox(
    "Select scenario",
    options=list(scenarios_raw.keys()),
    format_func=lambda sid: scenarios_raw[sid]["name"],
)
raw = scenarios_raw[selected_id]

with st.spinner("Running scheduler…"):
    result = run_scheduler(raw)

# ── Helpers ────────────────────────────────────────────────────────────────────
OP_EMOJI   = {"kpn": "🔵", "freshbus": "🟢", "flixbus": "🟠"}
OP_CLASS   = {"kpn": "kpn", "freshbus": "freshbus", "flixbus": "flixbus"}
DIR_LABELS = {"BK": "BLR → KCH", "KB": "KCH → BLR"}

FWD = ["Bengaluru", "A", "B", "C", "D", "Kochi"]
SEG = {"Bengaluru-A": 100, "A-B": 120, "B-C": 100, "C-D": 120, "D-Kochi": 100}

def seg_dist(a, b):
    if FWD.index(a) > FWD.index(b):
        a, b = b, a
    total = 0
    for k in range(FWD.index(a), FWD.index(b)):
        total += SEG[f"{FWD[k]}-{FWD[k+1]}"]
    return total

def badge(text, cls):
    return f'<span class="badge {cls}">{text}</span>'

# ── Tabs ───────────────────────────────────────────────────────────────────────
tab_input, tab_buses, tab_stations = st.tabs([
    "📋 Scenario Input",
    "🚌 Per-Bus Timetable",
    "🔌 Per-Station Queue",
])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Scenario input
# ═══════════════════════════════════════════════════════════════════════════════
with tab_input:
    st.subheader(raw["name"])
    st.markdown(f"*{raw['description']}*")
    st.markdown("")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("**Route segments**")
        for seg in raw["route"]["segments"]:
            st.markdown(f"- {seg['from']} → {seg['to']}: **{seg['distance_km']} km**")
    with col2:
        st.markdown("**Physics**")
        p = raw["physics"]
        st.markdown(f"- Battery range: **{p['battery_range_km']} km**")
        st.markdown(f"- Charge time: **{p['charge_duration_min']} min** (to full)")
        st.markdown(f"- Speed: **{p['speed_kmh']} km/h**")
    with col3:
        st.markdown("**Weights**")
        w = raw["weights"]
        st.markdown(f"- Individual: **{w['individual']}**")
        st.markdown(f"- Operator: **{w['operator']}**")
        st.markdown(f"- Overall: **{w['overall']}**")

    st.divider()
    st.subheader("Departure schedule")

    rows = [
        {
            "Bus ID": b["id"],
            "Operator": f"{OP_EMOJI.get(b['operator'],'⚪')} {b['operator']}",
            "Direction": DIR_LABELS[b["direction"]],
            "Departure": b["departure"],
        }
        for b in raw["buses"]
    ]
    df_all = pd.DataFrame(rows)

    ca, cb = st.columns(2)
    with ca:
        st.markdown("**Bengaluru → Kochi**")
        bk = df_all[df_all["Direction"] == "BLR → KCH"].reset_index(drop=True)
        st.dataframe(bk, use_container_width=True, hide_index=True)
    with cb:
        st.markdown("**Kochi → Bengaluru**")
        kb = df_all[df_all["Direction"] == "KCH → BLR"].reset_index(drop=True)
        st.dataframe(kb, use_container_width=True, hide_index=True)

    with st.expander("Raw JSON"):
        st.json(raw)

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Per-bus timetable
# ═══════════════════════════════════════════════════════════════════════════════
with tab_buses:
    st.subheader("Per-Bus Timetable")

    waits    = [s.total_wait_min for s in result.schedules]
    journeys = [s.arrival_min - s.departure_min for s in result.schedules]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Buses scheduled",    len(result.schedules))
    m2.metric("Avg wait / bus",     f"{sum(waits)/len(waits):.1f} min")
    m3.metric("Max wait / bus",     f"{max(waits):.0f} min")
    m4.metric("Avg journey time",   f"{sum(journeys)/len(journeys):.0f} min")

    st.divider()

    direction_filter = st.radio(
        "Show direction", ["All", "Bengaluru → Kochi", "Kochi → Bengaluru"],
        horizontal=True
    )

    for sched in sorted(result.schedules, key=lambda s: s.departure_min):
        dir_label = "Bengaluru → Kochi" if sched.direction == "BK" else "Kochi → Bengaluru"
        if direction_filter != "All" and dir_label != direction_filter:
            continue

        journey = sched.arrival_min - sched.departure_min
        label = (
            f"{OP_EMOJI.get(sched.operator,'⚪')} **{sched.bus_id}** "
            f"· {sched.operator} · {dir_label} "
            f"· Dep {fmt_time(sched.departure_min)} → Arr {fmt_time(sched.arrival_min)} "
            f"({journey:.0f} min, {sched.total_wait_min:.0f} min wait)"
        )
        with st.expander(label):
            # Timeline rows
            rows = [{"Event": "🚀 Depart", "Location": sched.origin,
                     "Time": fmt_time(sched.departure_min), "Wait (min)": "—", "Charge ends": "—"}]
            for ev in sched.charging_events:
                rows.append({
                    "Event": "⚡ Charge",
                    "Location": f"Station {ev.station_id}",
                    "Time": fmt_time(ev.arrive_min),
                    "Wait (min)": f"{ev.wait_min:.0f}",
                    "Charge ends": fmt_time(ev.charge_end_min),
                })
            rows.append({"Event": "🏁 Arrive", "Location": sched.destination,
                         "Time": fmt_time(sched.arrival_min), "Wait (min)": "—", "Charge ends": "—"})
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            # Range check
            checkpoints = [sched.origin] + [e.station_id for e in sched.charging_events] + [sched.destination]
            range_rows = []
            for i in range(len(checkpoints) - 1):
                d = seg_dist(checkpoints[i], checkpoints[i + 1])
                ok = "✅" if d <= result.physics.battery_range_km else "❌ VIOLATION"
                range_rows.append({
                    "Leg": f"{checkpoints[i]} → {checkpoints[i+1]}",
                    "Distance (km)": d,
                    "Status": ok,
                })
            st.dataframe(pd.DataFrame(range_rows), use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Per-station queue
# ═══════════════════════════════════════════════════════════════════════════════
with tab_stations:
    st.subheader("Per-Station Charging Queue")
    st.caption("Sorted by charge-start time. Shows the order the scheduler assigned each bus to a charger.")

    station_ids = [s.id for s in result.route.stations]
    cols = st.columns(len(station_ids))

    for col, sid in zip(cols, station_ids):
        with col:
            queue = result.station_queues[sid]
            chargers = raw["stations_config"][sid].get("charger_count", 1)
            st.markdown(f"### Station {sid}")
            st.caption(f"{len(queue)} buses · {chargers} charger{'s' if chargers > 1 else ''}")
            if not queue:
                st.info("No buses used this station.")
                continue
            for rank, entry in enumerate(queue, 1):
                op_em = OP_EMOJI.get(entry["operator"], "⚪")
                arrow = "→" if entry["direction"] == "BK" else "←"
                wait_str = f"⏳ {entry['wait_min']:.0f} min wait" if entry["wait_min"] > 0 else "✅ no wait"
                st.markdown(
                    f"**{rank}.** {op_em} `{entry['bus_id']}` {arrow}  \n"
                    f"Arr **{entry['arrive']}** · Start **{entry['charge_start']}** · End **{entry['charge_end']}**  \n"
                    f"{wait_str}"
                )
                if rank < len(queue):
                    st.markdown("---")

    st.divider()
    st.subheader("Station Utilisation")
    util_rows = []
    for sid in station_ids:
        q = result.station_queues[sid]
        total_wait = sum(e["wait_min"] for e in q)
        util_rows.append({
            "Station": f"Station {sid}",
            "Buses charged": len(q),
            "Buses that waited": sum(1 for e in q if e["wait_min"] > 0),
            "Total wait (min)": round(total_wait, 1),
            "Avg wait (min)": round(total_wait / len(q), 1) if q else 0,
        })
    st.dataframe(pd.DataFrame(util_rows), use_container_width=True, hide_index=True)
