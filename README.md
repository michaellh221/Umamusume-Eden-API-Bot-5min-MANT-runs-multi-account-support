# Umamusume Sweepy

An automation bot for **Uma Musume Pretty Derby (PC / Steam)** that runs
"Make a New Track!" (MANT, scenario 4) careers automatically via a local web UI.

---

## Requirements

| Dependency | Notes |
|---|---|
| Python 3.10+ | Tested on 3.13 |
| Node.js 18+ | Used to generate Steam auth tickets |
| Uma Musume Pretty Derby | PC version (Steam), must be installed |
| Windows | Hardware fingerprinting uses the Windows registry |

---

## Installation

**1. Clone the repo**

```
git clone https://github.com/your-username/umamusume-sweepy.git
cd umamusume-sweepy
```

**2. Install Python dependencies**

```
pip install -r requirements.txt
```

**3. Install Node dependencies**

```
npm install
```

**4. Set your master.mdb path**

Open `settings.json` and set `master_mdb_path` to your local game database:

```json
{
  "master_data": {
    "master_mdb_path": "C:\\Users\\YourName\\AppData\\LocalLow\\Cygames\\Umamusume\\master\\master.mdb"
  }
}
```

**5. Launch the game**, then start the server:

```
python main.py
```

**6. Open the UI** at **http://127.0.0.1:1616**

On first run the bot captures Steam auth credentials from the running game process and saves them to `uma_runtime/auth_cache.json`. Subsequent runs reuse the cache and skip the game launch check.

---

## Features

### Skill Optimizer

Before starting a run you can choose between two optimization modes per preset:

**Team Trials (Consistency)** — scores skills using `SV × consistency²`, where consistency
measures how reliably the skill activates across timing windows, race breadth, and scenario
conditions. Gold skills get a bonus for consistent activation; stamina-recovery skills and
volatile race-condition skills (e.g. rotation-locked passives) are penalized. This mode
maximises expected Team Trial contribution.

**Score (Grade Value)** — scores skills by raw `grade_value` from the master data, plus flat
bonuses for matching the preset's running style and distance. Use this when you want the highest
possible career score rather than TT consistency.

The mode is stored per-preset, so you can have separate presets for TT grinding and score pushing.

### Skill Auto-Buy

At the end of a career the bot runs a 0/1 knapsack optimizer over all available skill hints
within the SP budget, picking the combination with the highest total score under the chosen mode.

Style and distance skills that match the preset's running style and target distance are
auto-bought immediately when they appear as hints, before the optimizer runs.

### Event System

The bot uses a stat-diff database (`data/event_outcomes.json`) to score every event choice.
Each choice is weighted by the stat gains it produces, scaled against the current `stat_priority`
order from the preset. The highest-scoring choice is selected automatically.

### Preset System

Presets are JSON files in `data/presets/` created and edited through the UI's **Config** tab.

| Field | Description |
|---|---|
| `running_style` | Running style (1=Pace Setter, 2=Front Runner, 3=Late Charger, 4=Last Spurt) |
| `target_distance` | Preferred race distance in metres (0 = no preference) |
| `skill_optimizer_mode` | `"team_trials"` or `"score"` |
| `stat_priority` | Drag-to-reorder weight for `[Speed, Stamina, Power, Guts, Wit]` |
| `stat_ideal_targets` | Target stat values — deficit boosts training scoring |
| `stat_min_targets` | Hard minimum targets — stronger boost than ideal |
| `extra_race_list` | Additional race program IDs beyond the default schedule |

### Loop Mode

Enable **LOOP: ON** in the UI to run careers back-to-back automatically. Fan gains from
each career are recorded to `uma_runtime/fan_stats.json` and displayed in the Stats tab.
You can also set a stop-after-N-careers limit.

### Item Management

The bot manages Megaphones, Whistles, and Anklets during Summer Half-Anniversary turns,
coordinating the use order to hit the maximum stat bonus. TP recovery items (potions and
food) are consumed automatically when a training's TP drops below the configured threshold.

### Give Up

A **GIVE UP** button in the UI aborts the current career cleanly, triggering the in-game
give-up flow rather than killing the process mid-run.

---

## Project Structure

```
umamusume-sweepy/
├── main.py                    # FastAPI server — entry point
├── requirements.txt
├── settings.json              # Local config (master.mdb path, delays, TP recovery)
│
├── uma_api/
│   └── client.py              # Encrypted msgpack API client + Steam auth
│
├── career_bot/
│   ├── runner.py              # CareerRunner — main automation loop
│   ├── presets.py             # Preset serialization / hydration / storage
│   ├── items.py               # Item tables + MantItemManager
│   ├── skills.py              # SkillBuyer — skill optimizer + auto-buy
│   ├── races.py               # RacePlanner — race schedule logic
│   ├── events.py              # EventManager — scored event choice selection
│   ├── delay.py               # Human-like timing helpers
│   ├── master_data.py         # Game master data loader
│   ├── report.py              # Per-career run report builder
│   └── scenarios/
│       ├── base.py            # ScenarioStrategy base class + Decision type
│       └── mant.py            # MantStrategy — MANT decision engine
│
├── public/
│   ├── index.html             # Single-page UI shell
│   ├── app.js                 # All frontend logic
│   └── styles.css             # All styles
│
├── data/
│   ├── skill_data.json        # Skill grade values and costs
│   ├── skills_all.json        # Full skill condition groups (UmaTools)
│   ├── skills_green.json      # Stamina-recovery skill classification (UmaTools)
│   ├── event_outcomes.json    # Stat-diff database for event choices
│   ├── chara_list.json
│   ├── race_map.json
│   ├── factor_map.json
│   ├── support_list.json
│   └── presets/               # User presets (created by UI)
│
└── uma_runtime/
    ├── auth_cache.json        # Cached Steam credentials (auto-generated)
    └── bot_logs/              # Per-career JSON logs
```

---

## How a Career Run Works

```
Browser UI
  └─ POST /api/career/run
       └─ main.py: start_career_from_request()
            ├─ UmaClient.pre_single_mode()         # announce intent to server
            ├─ UmaClient.start_career()            # single_mode_free/start
            └─ CareerRunner.start(client, preset)
                 └─ background thread loop:
                      ├─ MantStrategy.next_decision(state, preset)
                      │    → Decision(action, payload, reason)
                      ├─ execute via UmaClient (train / race / event / rest / item)
                      ├─ update snapshot (polled by UI every 2 s)
                      └─ repeat until finished or stop_requested
                           └─ SkillBuyer.buy_end_of_career()
                                └─ knapsack optimizer → purchase sequence
```

---

## Extending

### Adding a new scenario

1. Create `career_bot/scenarios/your_scenario.py` subclassing `ScenarioStrategy`.
2. Implement `next_decision(state, preset) -> Decision`.
3. Register it in `career_bot/runner.py`:
   ```python
   STRATEGIES = {4: MantStrategy, 5: YourStrategy}
   ```
4. Add the scenario ID constant in `presets.py` and wire it in `hydrate_preset()`.

### Adding a new API call

In `uma_api/client.py`, add a method on `UmaClient`:

```python
def your_call(self, arg1, arg2):
    return self.call('endpoint/name', {'field1': arg1, 'field2': arg2})
```

`self.call()` handles encryption, SID rolling, error raising, and tracing.

### Adding a new REST endpoint

```python
@app.get("/api/your/endpoint")
async def your_endpoint():
    return {"success": True, "data": ...}
```

Then call it from `app.js` via `apiJson('/api/your/endpoint')`.
