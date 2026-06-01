# Bus Charging Scheduler

A Streamlit app that schedules electric bus charging on the Bengaluru–Kochi route.

## Live App

[Hosted on Streamlit Community Cloud] - https://bus-charging-scheduler-o9tzjvvkpoyaacwmyagv69.streamlit.app/

## Running locally

```bash
git clone <your-repo>
cd bus-charging-scheduler
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501 in your browser.

## How to change a weight

Weights live in each scenario JSON file - one place, no code change required.

Example: bump operator fairness for scenario 4:

```json
// scenarios/scenario_4.json
"weights": {
  "individual": 1.0,
  "operator": 2.0,   // ← change this
  "overall": 1.0
}
```

Reload the app; the scheduler immediately uses the new values.

## How to add a new rule

**Step 1** - Add the weight to your scenario JSON:

```json
"weights": {
  "individual": 1.0,
  "operator": 1.0,
  "overall": 1.0,
  "priority": 0.5   // ← new weight key
}
```

**Step 2** - Parse the new weight in `run_scheduler()` inside `scheduler/engine.py`:

```python
weights = Weights(
    individual=w.get("individual", 1.0),
    operator=w.get("operator", 1.0),
    overall=w.get("overall", 1.0),
    priority=w.get("priority", 0.0),   # ← add field
)
```

**Step 3** - Add the field to the `Weights` dataclass:

```python
@dataclass
class Weights:
    individual: float = 1.0
    operator: float = 1.0
    overall: float = 1.0
    priority: float = 0.0   # ← new field
```

**Step 4** - Add the penalty term in `_score_plan()`:

```python
# ── Priority: penalise non-priority buses relative to priority buses
priority_penalty = 0.0 if bus.priority else 1.0

return (weights.individual * individual_penalty
      + weights.operator   * operator_penalty
      + weights.overall    * overall_penalty
      + weights.priority   * priority_penalty)   # ← new term
```

That's the entire change. The engine core doesn't move

## How to add a new scenario

1. Copy any existing `scenarios/scenario_N.json`
2. Edit the `buses` array with your departure schedule
3. Optionally override `weights`, `stations_config`, `physics`, or `route`
4. Save as `scenarios/scenario_6.json` -it auto-appears in the dropdown

