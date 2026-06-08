# Umamusume Sweepy

An automation bot for **Uma Musume Pretty Derby (PC / Steam)** that runs
"Make a New Track!" (MANT, scenario 4) careers automatically via a local
web UI.

---

## Requirements

| Dependency | Notes |
|---|---|
| Python 3.10+ | Tested on 3.13 |
| Node.js 18+ | Used to generate Steam auth tickets |
| Uma Musume Pretty Derby | PC version (Steam), must be running |
| Windows | Hardware fingerprinting uses the Windows registry |

Install Python dependencies:

```
pip install -r requirements.txt
```

Node dependencies are installed automatically on first run (`npm install`).

---

## Running

1. Launch the game and get to the title screen (so the process is alive).
2. Start the server:

```
python main.py
```

3. Open **http://127.0.0.1:1616** in your browser.

The server injects a Frida hook into the game process on startup to
capture auth credentials. Once captured, they are saved to
`data/accounts/<account>.json` and reused on subsequent runs.

---

## Project structure

```
umamusume-sweepy/
├── main.py                    # FastAPI server — entry point
├── requirements.txt
│
├── uma_api/
│   └── client.py              # Encrypted msgpack API client + Steam auth
│
├── career_bot/
│   ├── runner.py              # CareerRunner — main automation loop
│   ├── presets.py             # Preset serialization / hydration / storage
│   ├── items.py               # Item tables + MantItemManager
│   ├── skills.py              # SkillBuyer — skill purchase logic
│   ├── races.py               # RacePlanner — race schedule logic
│   ├── events.py              # EventManager — event choice lookup
│   ├── delay.py               # Human-like timing helpers (dna_sleep etc.)
│   ├── master_data.py         # Game master data loader
│   ├── report.py              # Per-career run report builder
│   └── scenarios/
│       ├── base.py            # ScenarioStrategy base class + Decision type
│       └── mant.py            # MantStrategy — MANT decision engine
│
├── public/
│   ├── index.html             # Single-page UI shell
│   ├── app.js                 # All frontend logic (one IIFE)
│   └── styles.css             # All styles
│
└── data/                      # Runtime data (created on first run)
    ├── accounts/              # Saved account configs (JSON)
    ├── presets/               # User presets (JSON)
    └── fan_stats.json         # Per-career fan gain history
```

---

## How a career run works

```
Browser UI
  └─ POST /api/career/run
       └─ main.py: start_career_from_request()
            ├─ UmaClient.pre_single_mode()         # announce intent to server
            ├─ UmaClient.start_career()            # single_mode_free/start
            └─ CareerRunner.start(client, preset, result)
                 └─ background thread loop:
                      ├─ MantStrategy.next_decision(state, preset)
                      │    → Decision(action, payload, reason)
                      ├─ execute via UmaClient (train / race / event / rest)
                      ├─ update snapshot (polled by UI every 2 s)
                      └─ repeat until finished or stop_requested
```

When **LOOP: ON** is enabled, `manage_career_loop()` in `main.py` manages
the outer loop: it automatically starts a new career after each finish,
recording fan gains to `data/fan_stats.json` each time.

---

## Presets

Presets live in `data/presets/*.json` and are created/edited through the
UI's **Config** tab. Key fields:

| Field | Description |
|---|---|
| `learn_skill_list` | Skill priority rows — first affordable match per row is bought |
| `mandatory_skill_list` | Always bought if affordable, regardless of list position |
| `learn_skill_blacklist` | Never buy these skill IDs |
| `learn_skill_threshold` | SP cost below which auto-buy triggers for style/distance skills |
| `stat_priority` | Weight order `[Speed, Stamina, Power, Guts, Wit]` |
| `stat_ideal_targets` | Target values — deficit boosts scoring for under-target stats |
| `stat_min_targets` | Hard minimum — stronger boost than ideal targets |
| `target_distance` | Preferred race distance (0 = any) |
| `extra_race_list` | Additional race program IDs beyond the default schedule |

---

## Parent types (Legacy Select)

The game distinguishes two parent types:

| UI tab | API field | Notes |
|---|---|---|
| Veteran Umamusume | `succession_trained_chara_id_1/2` | Your own trained charas |
| Guests | `rental_succession_trained_chara` | Borrowed from another player (max 1) |

Guest parents show a purple **GUEST** badge in the grid and are routed to
the rental slot automatically — selecting one as a veteran parent was the
original cause of `result_code: 500` on career start.

---

## Forking / extending

### Adding a new scenario

1. Create `career_bot/scenarios/your_scenario.py` subclassing `ScenarioStrategy`.
2. Implement `next_decision(state, preset) -> Decision`.
3. Register it in `career_bot/runner.py`:
   ```python
   STRATEGIES = {
       4: MantStrategy,
       5: YourStrategy,
   }
   ```
4. Add the scenario ID constant in `presets.py` and wire it in `hydrate_preset()`.

### Adding a new API call to the game

In `uma_api/client.py`, add a method on `UmaClient`:

```python
def your_call(self, arg1, arg2):
    return self.call('endpoint/name', {'field1': arg1, 'field2': arg2})
```

`self.call()` handles encryption, SID rolling, error raising, and tracing.

### Adding a new REST endpoint (backend → UI)

Add a FastAPI route in `main.py` following the existing pattern:

```python
@app.get("/api/your/endpoint")
async def your_endpoint():
    return {"success": True, "data": ...}
```

Then call it from `app.js` via `apiJson('/api/your/endpoint')`.

---

## Runtime output

All runtime files go under `uma_runtime/` (sibling of the repo root).
Set the `UMA_RUNTIME_DIR` environment variable to redirect them:

```
uma_runtime/
├── trace_logs/api_payloads/   # JSONL trace of every API request/response
└── reports/                   # Per-career run reports
```
