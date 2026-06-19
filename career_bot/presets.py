"""
career_bot/presets.py
=====================
Preset configuration: serialization, hydration, and file-system storage.

A "preset" is a JSON file in  data/presets/  that stores the user's tuning
parameters for a career run (skill priorities, stat targets, distance, etc.).

Two representations
-------------------
serialized  – minimal, what gets written to disk.  Only user-editable fields.
hydrated    – full runtime dict passed to the strategy.  Adds fixed defaults
              for scoring weights, thresholds, and internal tuning values that
              are not exposed in the UI.

Extending / forking notes
--------------------------
- To add a new user-facing setting: add it to serialize_preset() and expose it
  in the UI.  If it needs a default for the strategy, also add it to hydrate_preset().
- # Scenario 4 = "Make a New Track!" (the only scenario the bot currently runs).
MANT_SCENARIO_ID = 4 is "Make a New Track!" — currently the only supported
  scenario.  To add another scenario, register its strategy class in runner.py's
  STRATEGIES dict and add a new scenario_id constant here.
"""

import json
import re
from pathlib import Path


# Keys that exist in old preset files but are no longer used.
EXCLUDED_KEYS = {
    "facility_period_configs",
    "facility_ratios",
    "learn_skill_list",
    "learn_skill_blacklist",
    "mandatory_skill_list",
    "learn_skill_threshold",
    "learn_skill_only_user_provided",
}

# Legacy field-name aliases from older versions of the preset format.
# serialize_preset() rewrites these keys so saved files stay forward-compatible.
RENAMES = {
    "race_list": "extra_race_list",
    "extraWeight": "extra_weight",
    "scoreValue": "score_value",
    "baseScore": "base_score",
    "statValueMultiplier": "stat_value_multiplier",
    "witSpecialMultiplier": "wit_special_multiplier",
    "cureAsapConditions": "cure_asap_conditions",
}

MANT_SCENARIO_ID = 4


# ── Helpers ────────────────────────────────────────────────────────────────

def slugify(value):
    text = re.sub(r"[^a-zA-Z0-9._ -]+", "", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    return text or "preset"


def split_csv(value):
    if isinstance(value, list):
        return value
    return [part.strip() for part in str(value or "").split(",") if part.strip()]



def as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_race_list(value):
    result = []
    for item in value if isinstance(value, list) else []:
        race_id = as_int(item, None)
        if race_id is not None:
            result.append(race_id)
    return result


# ── Serialization / hydration ──────────────────────────────────────────────

def serialize_preset(raw):
    data = dict(raw or {})
    serialized = {}

    serialized["name"] = slugify(data.get("name") or "preset")
    serialized["running_style"] = as_int(data.get("running_style"), 1)


    serialized["extra_race_list"] = normalize_race_list(data.get("extra_race_list", data.get("race_list", [])))
    serialized["target_distance"] = as_int(data.get("target_distance"), 0)

    # Race solver settings
    solver_mode = str(data.get("race_solver_mode") or "manual").strip().lower()
    serialized["race_solver_mode"] = solver_mode if solver_mode in ("off", "auto", "manual") else "manual"
    serialized["solver_max_races_in_row"] = max(1, min(5, as_int(data.get("solver_max_races_in_row"), 2)))
    serialized["solver_include_op"] = bool(data.get("solver_include_op") or False)
    serialized["solver_allow_summer"] = bool(data.get("solver_allow_summer") or False)
    serialized["solver_apt_floor"] = max(1, min(8, as_int(data.get("solver_apt_floor"), 6)))
    # manual_locks: {turn_str: program_id | "train"}
    raw_locks = data.get("solver_manual_locks") or {}
    serialized["solver_manual_locks"] = {str(k): v for k, v in raw_locks.items()} if isinstance(raw_locks, dict) else {}

    mode = str(data.get("skill_optimizer_mode") or "team_trials").strip()
    serialized["skill_optimizer_mode"] = mode if mode in ("score", "team_trials") else "team_trials"

    raw_priority = data.get("stat_priority")
    if isinstance(raw_priority, list) and len(raw_priority) == 5:
        serialized["stat_priority"] = [as_int(v, i) for i, v in enumerate(raw_priority)]
    else:
        serialized["stat_priority"] = [0, 1, 2, 3, 4]

    raw_ideal = data.get("stat_ideal_targets")
    if isinstance(raw_ideal, list) and len(raw_ideal) == 5:
        serialized["stat_ideal_targets"] = [as_int(v, 0) for v in raw_ideal]
    else:
        serialized["stat_ideal_targets"] = [0, 0, 0, 0, 0]

    raw_min = data.get("stat_min_targets")
    if isinstance(raw_min, list) and len(raw_min) == 5:
        serialized["stat_min_targets"] = [as_int(v, 0) for v in raw_min]
    else:
        serialized["stat_min_targets"] = [0, 0, 0, 0, 0]

    return serialized

def hydrate_preset(raw):
    """
    Build the full runtime preset dict from raw user data.

    All values set here are internal scoring weights and thresholds that are
    not exposed in the UI.  Tweak them here if you want to adjust the bot's
    decision-making defaults globally.

    stat_priority, stat_ideal_targets, and stat_min_targets come from the
    user's preset and are passed through from serialize_preset().
    """
    data = serialize_preset(raw)

    data["scenario_id"] = MANT_SCENARIO_ID
    data["scenario"] = MANT_SCENARIO_ID
    data["cure_asap_conditions"] = ["Migraine", "Night Owl", "Skin Outbreak", "Slacker", "Slow Metabolism", "(Practice poor isn't worth a turn to cure)"]
    data["expect_attribute"] = [9999, 9999, 9999, 9999, 9999]
    data["score_value"] = [[0.11, 0.1, 0.006, 0.09], [0.11, 0.1, 0.006, 0.09], [0.11, 0.1, 0.006, 0.09], [0.03, 0.05, 0.006, 0.09], [0, 0, 0.006, 0]]
    data["base_score"] = [0, 0, 0, 0, 0]
    data["stat_value_multiplier"] = [0.01, 0.01, 0.01, 0.01, 0.01, 0.005]
    data["extra_weight"] = [[0, 0, 0, 0, 0]] * 4
    data["npc_score_value"] = [[0.05, 0.05, 0.05], [0.05, 0.05, 0.05], [0.05, 0.05, 0.05], [0.03, 0.05, 0.05], [0, 0, 0.05]]
    data["special_training"] = [0.095, 0.095, 0.095, 0.095, 0]
    data["spirit_explosion"] = [[0.16, 0.16, 0.16, 0.06, 0.11]] * 5
    data["wit_special_multiplier"] = [1.57, 1.37]
    data["compensate_failure"] = True
    data["summer_score_threshold"] = 0.34
    data["motivation_threshold_year1"] = 3
    data["motivation_threshold_year2"] = 4
    data["motivation_threshold_year3"] = 4
    data["prioritize_recreation"] = False
    data["pal_thresholds"] = []
    data["pal_friendship_score"] = [0.08, 0.057, 0.018]
    data["pal_card_multiplier"] = 0.1
    data["rest_threshold"] = 48
    data["mant_config"] = {}

    return data

# ── File-system storage ────────────────────────────────────────────────────

class PresetStore:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.preset_dir = self.base_dir / "data" / "presets"

    def ensure(self):
        self.preset_dir.mkdir(parents=True, exist_ok=True)

    def read_all(self):
        self.ensure()
        loaded = {}
        for path in self._source_files():
            try:
                data = hydrate_preset(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            loaded[data["name"]] = data
        return sorted(loaded.values(), key=lambda item: item["name"].lower())

    def _source_files(self):
        return sorted(self.preset_dir.glob("*.json"))

    def load(self, name):
        """Load and hydrate a single preset by name. Returns None if not found."""
        for path in self._source_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if slugify(data.get("name") or path.stem) == slugify(name):
                    return hydrate_preset(data)
            except Exception:
                continue
        return None

    def save(self, preset):
        self.ensure()
        serialized = serialize_preset(preset)
        name = serialized["name"]
        path = self.preset_dir / f"{name}.json"
        path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2), encoding="utf-8")
        return serialized

    def delete(self, name):
        for path in self._source_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if slugify(data.get("name") or path.stem) == slugify(name):
                    path.unlink()
                    return True
            except Exception:
                continue
        return False
