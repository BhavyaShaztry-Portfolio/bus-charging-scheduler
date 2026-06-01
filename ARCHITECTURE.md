# Architecture

## Scheduler approach: Weighted event-driven simulation

### Why this approach?

The problem has two separable sub-problems:
1. **Plan selection** — which charging stations should each bus use?
2. **Queue arbitration** — when multiple buses want the same charger, who goes first?

I chose an **event-driven priority-queue simulation** with a **weighted cost function** because:

- It naturally handles shared resources (chargers) without complex constraint solving.
- Weights are first-class inputs, not coefficients buried in code.
- New rules are additive — define a penalty term, wire it in, done.
- It scales linearly with buses × stops, not exponentially like a full combinatorial solver.

Alternatives considered:
- **Greedy FCFS**: fast but ignores operator fairness and produces poor load distribution.
- **ILP/OR-Tools**: correct but overkill; adding a new rule means reformulating constraints, not adding a function.
- **Genetic algorithm**: interesting but non-deterministic; harder to explain in an interview.

### The two-phase architecture

**Phase 1 — Plan selection (offline)**

For each bus, enumerate all feasible 2–4 stop subsets. Score each using:

```
cost = w_individual * individual_penalty
     + w_operator   * operator_penalty
     + w_overall    * overall_penalty
```

Where:
- `individual_penalty` = normalised stop count (fewer stops = less wait exposure)
- `operator_penalty` = deviation from operator fleet average stop count (fairness across peers)
- `overall_penalty` = sum of *expected* queue depth at candidate stations (load spreading)

The load estimate feeds back as buses are assigned plans, so later buses naturally shift to less congested stations.

**Phase 2 — Queue simulation (online)**

A min-heap of `(arrival_time, tie_break, bus_id, plan_idx)` events drives the simulation. On each pop:

1. Find the earliest-free charger at this station.
2. Compute wait = max(0, charger_free_time - arrival_time).
3. Record the charging event, advance the charger's free time, schedule the next station.

This gives exact arrival times and wait times for every bus.

---

## Data structure design

The scenario JSON is the unit of truth. It contains everything the scheduler needs:

```json
{
  "route": {
    "segments": [{ "from": "X", "to": "Y", "distance_km": 100 }]
  },
  "physics": { "battery_range_km": 240, "charge_duration_min": 25, "speed_kmh": 60 },
  "stations_config": { "A": { "charger_count": 1 } },
  "weights": { "individual": 1.0, "operator": 1.0, "overall": 1.0 },
  "buses": [{ "id": "...", "operator": "...", "direction": "BK", "departure": "19:00" }]
}
```

The `RouteGraph` class derives everything (station order, distances, travel times) from the segments array. There is no hardcoded topology.

---

## Anticipated future changes — and how the design handles each

### 1. Different segment distances / a new segment added to the route

**Handled by data alone.** The route is described entirely by the `segments` array in JSON. Add a segment, change a distance — `RouteGraph` recomputes everything. Zero code changes.

### 2. More charging stations (e.g., add station E between D and Kochi)

**Handled by data alone.** Add the segment split in `segments`, add the station to `stations`, add its config to `stations_config`. The feasibility enumerator and queue simulator pick it up automatically.

### 3. Multiple chargers per station

**Already supported.** `stations_config.X.charger_count` is live today. Set it to any integer; the simulation tracks each charger independently.

### 4. New route entirely (e.g., Chennai–Mysuru)

**Handled by data alone.** Each scenario carries its own `route` block. Create a new scenario file with a different route; no code change.

### 5. More buses / operators

**Handled by data alone.** Add entries to the `buses` array. The operator scoring term generalises to any operator name without code changes.

### 6. A new soft rule (e.g., time-of-day electricity cost)

**One function + one weight.** Add `"electricity_cost_weight"` to the weights block. Define a `electricity_cost_penalty(plan, arrival_times)` function. Add one line to `_score_plan()`. Nothing else changes.

### 7. A new hard rule (e.g., driver shift — bus can't leave before 20:00)

**One filter.** Add a `_is_compliant(bus, plan)` check before scoring in plan selection. The engine core is unchanged.

### 8. Priority buses (skip the queue)

**One weight + one rule.** Add `"priority": true` to a bus in the scenario JSON. Add `priority_penalty = 0 if bus.priority else 1` in `_score_plan()`. The bus gets a score of zero for the queue priority term and always wins ties.

### 9. Stations shared across multiple routes

**One architecture extension.** Move `charger_free` state into a shared `StationState` registry keyed by `station_id`. Each route's simulation reads from the same registry. No change to the scenario format.

### 10. Variable charging times (e.g., 50 kW vs 150 kW chargers)

**One field.** Add `"charge_duration_min"` to each station in `stations_config` (override the global physics value). The simulation reads station-level duration if present, global otherwise.

### 11. Scenarios stored in a database rather than JSON files

**One loader swap.** `load_scenario()` is a one-function seam. Swap it to read from a DB; everything downstream is unchanged.

### 12. Buses that don't start with a full charge

**One field.** Add `"initial_range_km"` to a bus in the JSON. Pass it into the feasibility check as the range available before the first charge. No structural change.

---

## How to change a weight (concrete example)

Scenario 4 has `operator = 2.0`. To increase it to 3.0:

```json
// scenarios/scenario_4.json
"weights": {
  "individual": 1.0,
  "operator": 3.0,   // ← was 2.0
  "overall": 1.0
}
```

Reload the app. The scheduler re-runs with the new weight. No code touched.

---

## How to add a new rule (concrete example)

**Business rule**: penalise plans that use station B during the 20:00–21:00 peak window.

**Step 1** — Add weight to scenario JSON:
```json
"weights": { ..., "peak_avoidance": 1.5 }
```

**Step 2** — Add field to `Weights` dataclass:
```python
@dataclass
class Weights:
    individual: float = 1.0
    operator: float = 1.0
    overall: float = 1.0
    peak_avoidance: float = 0.0   # ← new
```

**Step 3** — Parse in `run_scheduler()`:
```python
weights = Weights(
    ...
    peak_avoidance=w.get("peak_avoidance", 0.0),
)
```

**Step 4** — Add term in `_score_plan()`:
```python
# ── Peak avoidance: penalise using station B during 20:00–21:00
peak_stations = {"B"}
peak_window = (20 * 60, 21 * 60)
estimated_arrival_at_B = bus.departure_min + 100 / physics.speed_kmh * 60  # rough
in_peak = peak_window[0] <= estimated_arrival_at_B <= peak_window[1]
peak_penalty = sum(1 for s in plan if s in peak_stations and in_peak) / len(peak_stations)

return (weights.individual * individual_penalty
      + weights.operator   * operator_penalty
      + weights.overall    * overall_penalty
      + weights.peak_avoidance * peak_penalty)  # ← new
```

Total diff: ~10 lines across 2 files, no engine rewrite.

---

## Assumptions

1. **Speed is uniform** — no traffic, no variation. One `speed_kmh` value per scenario.
2. **Buses start with a full charge** — Bengaluru and Kochi provide a full charge before departure.
3. **Charging is always to full** — partial charges are not modelled.
4. **Buses follow route order** — no skipping or backtracking.
5. **Charger queues are FCFS within a time step** — when two buses arrive at the same minute, lower heap tie-break counter (earlier-registered) goes first. In practice this is resolved by the weighted plan selection upstream.
6. **Time is continuous float minutes** — no discretisation, no rounding in the simulation.
7. **Kochi/Bengaluru endpoints are not scheduling stations** — as specified.
8. **The minimum feasible stop count is 2** — verified: 540 km total, 240 km range, at least 2 charges required.
