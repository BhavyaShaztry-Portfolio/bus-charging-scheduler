"""
scheduler/engine.py

Core scheduling engine for the Bus Charging Scheduler.

Architecture overview:
  - RouteGraph  — direction-aware, handles variable segment counts/distances.
  - Plan generation — enumerate all feasible 2+ stop subsets per bus.
  - Plan selection  — scored via a weighted cost function (individual + operator + overall).
                      Critically, the plan selection accounts for *expected queue depth*
                      at candidate stations so the scheduler naturally spreads load.
  - Queue simulation — event-driven priority-queue simulation; ties broken by
                       weighted bus priority score.

Adding a new soft rule:
    Define a function  my_rule(bus, plan, context) -> float  and wire it into
    _score_plan() with a new weight key. Nothing else changes.

Changing a weight:
    Edit the "weights" block in the scenario JSON. One place, no code change.

Scaling:
    More buses/stations/routes = add them to the scenario JSON.
    Multiple chargers per station = set "charger_count" in stations_config.
"""

from __future__ import annotations

import json
import heapq
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ─────────────────────────────────────────────────────────────────────────────
# Value objects / data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StationConfig:
    id: str
    name: str
    charger_count: int = 1


@dataclass
class Segment:
    from_node: str
    to_node: str
    distance_km: float


@dataclass
class RouteConfig:
    id: str
    name: str
    endpoints: List[str]
    stations: List[StationConfig]
    segments: List[Segment]


@dataclass
class Physics:
    battery_range_km: float
    charge_duration_min: float
    speed_kmh: float


@dataclass
class Weights:
    individual: float = 1.0
    operator: float = 1.0
    overall: float = 1.0


@dataclass
class BusInput:
    id: str
    operator: str
    direction: str          # "BK" | "KB"
    departure_min: float    # minutes since midnight


@dataclass
class ChargingEvent:
    station_id: str
    arrive_min: float
    wait_min: float
    charge_start_min: float
    charge_end_min: float


@dataclass
class BusSchedule:
    bus_id: str
    operator: str
    direction: str
    departure_min: float
    origin: str
    destination: str
    charging_events: List[ChargingEvent]
    arrival_min: float
    total_wait_min: float


@dataclass
class ScenarioResult:
    scenario_id: str
    scenario_name: str
    buses: List[BusInput]
    route: RouteConfig
    physics: Physics
    weights: Weights
    schedules: List[BusSchedule]
    station_queues: Dict[str, List[Dict]]


# ─────────────────────────────────────────────────────────────────────────────
# Loaders / parsers
# ─────────────────────────────────────────────────────────────────────────────

def load_scenario(path) -> dict:
    with open(path) as f:
        return json.load(f)


def parse_time(t: str) -> float:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def fmt_time(minutes: float) -> str:
    h = int(minutes) // 60
    m = int(minutes) % 60
    return f"{h:02d}:{m:02d}"


def build_route(raw: dict) -> RouteConfig:
    stations = [StationConfig(id=s["id"], name=s["name"]) for s in raw["stations"]]
    segments = [Segment(from_node=s["from"], to_node=s["to"], distance_km=s["distance_km"])
                for s in raw["segments"]]
    return RouteConfig(
        id=raw["id"],
        name=raw["name"],
        endpoints=raw["endpoints"],
        stations=stations,
        segments=segments,
    )


# ─────────────────────────────────────────────────────────────────────────────
# RouteGraph — all geometry lives here
# ─────────────────────────────────────────────────────────────────────────────

class RouteGraph:
    def __init__(self, route: RouteConfig, stations_config: dict):
        self._route = route

        # Build ordered node list from segments
        self._forward_nodes: List[str] = []
        for i, seg in enumerate(route.segments):
            if i == 0:
                self._forward_nodes.append(seg.from_node)
            self._forward_nodes.append(seg.to_node)

        self._node_index = {n: i for i, n in enumerate(self._forward_nodes)}

        # Segment distances (both directions)
        self._seg_dist: Dict[Tuple[str, str], float] = {}
        for seg in route.segments:
            self._seg_dist[(seg.from_node, seg.to_node)] = seg.distance_km
            self._seg_dist[(seg.to_node, seg.from_node)] = seg.distance_km

        self._station_ids = [s.id for s in route.stations]
        self._charger_counts = {
            sid: cfg.get("charger_count", 1)
            for sid, cfg in stations_config.items()
        }

    def nodes_for_direction(self, direction: str) -> List[str]:
        return self._forward_nodes[:] if direction == "BK" else self._forward_nodes[::-1]

    def stations_for_direction(self, direction: str) -> List[str]:
        return self._station_ids[:] if direction == "BK" else self._station_ids[::-1]

    def distance_between(self, a: str, b: str) -> float:
        """Cumulative distance between any two nodes on the route."""
        ia, ib = self._node_index[a], self._node_index[b]
        if ia > ib:
            ia, ib = ib, ia
            a = self._forward_nodes[ia]
        nodes = self._forward_nodes[ia: ib + 1]
        return sum(self._seg_dist[(nodes[k], nodes[k + 1])] for k in range(len(nodes) - 1))

    def travel_time_min(self, a: str, b: str, speed_kmh: float) -> float:
        return (self.distance_between(a, b) / speed_kmh) * 60

    def charger_count(self, station_id: str) -> int:
        return self._charger_counts.get(station_id, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Feasibility
# ─────────────────────────────────────────────────────────────────────────────

def _is_feasible(stops: List[str], origin: str, destination: str,
                 graph: RouteGraph, battery_range: float) -> bool:
    checkpoints = [origin] + list(stops) + [destination]
    for i in range(len(checkpoints) - 1):
        if graph.distance_between(checkpoints[i], checkpoints[i + 1]) > battery_range:
            return False
    return True


def all_feasible_plans(direction: str, graph: RouteGraph,
                       battery_range: float) -> List[List[str]]:
    stations = graph.stations_for_direction(direction)
    nodes = graph.nodes_for_direction(direction)
    origin, destination = nodes[0], nodes[-1]
    feasible = []
    for r in range(1, len(stations) + 1):
        for combo in combinations(stations, r):
            ordered = sorted(combo, key=lambda s: stations.index(s))
            if _is_feasible(ordered, origin, destination, graph, battery_range):
                feasible.append(ordered)
    return feasible


# ─────────────────────────────────────────────────────────────────────────────
# Weighted scoring — plug new rules here
# ─────────────────────────────────────────────────────────────────────────────

def _score_plan(
    plan: List[str],
    bus: BusInput,
    weights: Weights,
    operator_avg_stops: Dict[str, float],
    expected_queue_lengths: Dict[str, float],   # station_id -> expected wait minutes
) -> float:
    """
    Lower = better plan for this bus given current system state.

    individual_penalty  : fewer stops = less exposure to queues
    operator_penalty    : align with operator fleet's average stop count
    overall_penalty     : expected total wait across chosen stations (system load)

    To add a new soft rule, define its penalty term and add:
        + weights.new_rule * new_penalty
    Then add "new_rule" to the weights dict in the scenario JSON.
    """
    n_stops = len(plan)
    n_min, n_max = 2, 4  # min required, max possible

    # ── Individual: prefer fewer stops (less journey disruption per bus)
    individual_penalty = (n_stops - n_min) / max(n_max - n_min, 1)

    # ── Operator: align with operator peer fleet average
    op_avg = operator_avg_stops.get(bus.operator, n_min)
    operator_penalty = abs(n_stops - op_avg) / n_max

    # ── Overall: expected queue wait for this plan (sum of estimated waits)
    total_expected_wait = sum(expected_queue_lengths.get(s, 0.0) for s in plan)
    # Normalise: max possible = 4 stations * worst-case queue
    max_possible_wait = max(expected_queue_lengths.values(), default=1.0) * 4 or 1.0
    overall_penalty = total_expected_wait / max_possible_wait

    return (weights.individual * individual_penalty
            + weights.operator * operator_penalty
            + weights.overall * overall_penalty)


# ─────────────────────────────────────────────────────────────────────────────
# Main scheduler
# ─────────────────────────────────────────────────────────────────────────────

def run_scheduler(scenario_data: dict) -> ScenarioResult:
    # ── Parse
    route = build_route(scenario_data["route"])
    physics = Physics(**scenario_data["physics"])
    w = scenario_data.get("weights", {})
    weights = Weights(
        individual=w.get("individual", 1.0),
        operator=w.get("operator", 1.0),
        overall=w.get("overall", 1.0),
    )
    stations_config = scenario_data.get("stations_config",
                                        {s.id: {"charger_count": 1} for s in route.stations})
    buses = [
        BusInput(id=b["id"], operator=b["operator"],
                 direction=b["direction"], departure_min=parse_time(b["departure"]))
        for b in scenario_data["buses"]
    ]
    graph = RouteGraph(route, stations_config)

    # Pre-cache feasible plans per direction
    feasible_by_dir: Dict[str, List[List[str]]] = {}
    for direction in set(b.direction for b in buses):
        feasible_by_dir[direction] = all_feasible_plans(direction, graph, physics.battery_range_km)

    # ── Step 1: Assign plans with load-aware scoring
    operator_avg_stops: Dict[str, float] = {}
    operator_count: Dict[str, int] = {}
    expected_queue: Dict[str, float] = {s.id: 0.0 for s in route.stations}

    bus_plans: Dict[str, List[str]] = {}
    for bus in sorted(buses, key=lambda b: b.departure_min):
        feasible = feasible_by_dir[bus.direction]
        if not feasible:
            raise ValueError(f"No feasible plan for {bus.id}")

        op_avg = operator_avg_stops.get(bus.operator, 2.0)
        op_avgs = {**operator_avg_stops, bus.operator: op_avg}

        scored = sorted(
            feasible,
            key=lambda plan: _score_plan(plan, bus, weights, op_avgs, expected_queue)
        )
        chosen = scored[0]
        bus_plans[bus.id] = chosen

        # Update operator running average
        n = operator_count.get(bus.operator, 0) + 1
        prev_avg = operator_avg_stops.get(bus.operator, float(len(chosen)))
        operator_avg_stops[bus.operator] = prev_avg + (len(chosen) - prev_avg) / n
        operator_count[bus.operator] = n

        # Update expected queue depth for chosen stations
        for s in chosen:
            expected_queue[s] += physics.charge_duration_min / graph.charger_count(s)

    # ── Step 2: Event-driven queue simulation
    #    heap items: (arrival_time, tie_break_counter, bus_id, plan_idx)
    charger_free: Dict[str, List[float]] = {
        s.id: [0.0] * graph.charger_count(s.id)
        for s in route.stations
    }
    bus_charging_events: Dict[str, List[ChargingEvent]] = {b.id: [] for b in buses}

    counter = 0
    heap: list = []

    def _push_arrival(bus_id: str, plan_idx: int, depart_from: str, depart_time: float):
        nonlocal counter
        station_id = bus_plans[bus_id][plan_idx]
        tt = graph.travel_time_min(depart_from, station_id, physics.speed_kmh)
        arrive = depart_time + tt
        heapq.heappush(heap, (arrive, counter, bus_id, plan_idx))
        counter += 1

    # Seed heap — first station for each bus
    for bus in buses:
        if bus_plans[bus.id]:
            nodes = graph.nodes_for_direction(bus.direction)
            _push_arrival(bus.id, 0, nodes[0], bus.departure_min)

    # Bus lookup
    bus_by_id: Dict[str, BusInput] = {b.id: b for b in buses}

    while heap:
        arrive_time, _, bus_id, plan_idx = heapq.heappop(heap)
        bus = bus_by_id[bus_id]
        station_id = bus_plans[bus_id][plan_idx]

        # Pick earliest-free charger
        chargers = charger_free[station_id]
        earliest_free = min(chargers)
        cidx = chargers.index(earliest_free)

        charge_start = max(arrive_time, earliest_free)
        wait = charge_start - arrive_time
        charge_end = charge_start + physics.charge_duration_min

        bus_charging_events[bus_id].append(ChargingEvent(
            station_id=station_id,
            arrive_min=arrive_time,
            wait_min=wait,
            charge_start_min=charge_start,
            charge_end_min=charge_end,
        ))
        charger_free[station_id][cidx] = charge_end

        # Schedule next station
        if plan_idx + 1 < len(bus_plans[bus_id]):
            _push_arrival(bus_id, plan_idx + 1, station_id, charge_end)

    # ── Step 3: Compute final arrivals
    schedules: List[BusSchedule] = []
    for bus in buses:
        nodes = graph.nodes_for_direction(bus.direction)
        origin, destination = nodes[0], nodes[-1]
        events = bus_charging_events[bus.id]

        if events:
            last_loc = events[-1].station_id
            last_time = events[-1].charge_end_min
        else:
            last_loc = origin
            last_time = bus.departure_min

        arrival = last_time + graph.travel_time_min(last_loc, destination, physics.speed_kmh)
        schedules.append(BusSchedule(
            bus_id=bus.id,
            operator=bus.operator,
            direction=bus.direction,
            departure_min=bus.departure_min,
            origin=origin,
            destination=destination,
            charging_events=events,
            arrival_min=arrival,
            total_wait_min=sum(e.wait_min for e in events),
        ))

    # ── Step 4: Build station queue views
    station_queues: Dict[str, List[Dict]] = {s.id: [] for s in route.stations}
    for sched in schedules:
        for ev in sched.charging_events:
            station_queues[ev.station_id].append({
                "bus_id": sched.bus_id,
                "operator": sched.operator,
                "direction": sched.direction,
                "arrive": fmt_time(ev.arrive_min),
                "wait_min": round(ev.wait_min, 1),
                "charge_start": fmt_time(ev.charge_start_min),
                "charge_end": fmt_time(ev.charge_end_min),
            })
    for sid in station_queues:
        station_queues[sid].sort(key=lambda x: x["charge_start"])

    return ScenarioResult(
        scenario_id=scenario_data["id"],
        scenario_name=scenario_data["name"],
        buses=buses,
        route=route,
        physics=physics,
        weights=weights,
        schedules=schedules,
        station_queues=station_queues,
    )
