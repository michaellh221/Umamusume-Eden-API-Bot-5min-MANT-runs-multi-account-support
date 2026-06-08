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
}

# Legacy field-name aliases from older versions of the preset format.
# serialize_preset() rewrites these keys so saved files stay forward-compatible.
RENAMES = {
    "race_list": "extra_race_list",
    "skill_priority_list": "learn_skill_list",
    "skill_blacklist": "learn_skill_blacklist",
    "blacklistedSkills": "learn_skill_blacklist",
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


def normalize_skill_list(value):
    rows = value if isinstance(value, list) else []
    result = []
    for row in rows:
        if isinstance(row, list):
            parts = []
            for item in row:
                parts.extend(split_csv(item))
        else:
            parts = split_csv(row)
        if parts:
            result.append(parts)
    return result


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
    serialized["learn_skill_list"] = normalize_skill_list(data.get("learn_skill_list"))

    blacklist = []
    blacklist.extend(split_csv(data.get("blacklistedSkills")))
    blacklist.extend(split_csv(data.get("skill_blacklist")))
    blacklist.extend(split_csv(data.get("learn_skill_blacklist")))
    serialized["learn_skill_blacklist"] = list(dict.fromkeys(blacklist))

    serialized["extra_race_list"] = normalize_race_list(data.get("extra_race_list", data.get("race_list", [])))
    serialized["learn_skill_threshold"] = as_int(data.get("learn_skill_threshold"), 888)
    serialized["target_distance"] = as_int(data.get("target_distance"), 0)
    serialized["auto_buy_override_threshold"] = bool(data.get("auto_buy_override_threshold", True))

    raw_mandatory = data.get("mandatory_skill_list")
    if isinstance(raw_mandatory, list):
        serialized["mandatory_skill_list"] = [str(s) for s in raw_mandatory if s]
    else:
        serialized["mandatory_skill_list"] = []

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
def hydrate_preset(raw):
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
    data["manual_purchase_at_end"] = False
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

    def read_one(self, name):
        wanted = str(name or "").strip().lower()
        for preset in self.read_all():
            if preset["name"].lower() == wanted:
                return preset
        return None

    def write(self, preset):
        self.ensure()
        serialized_data = serialize_preset(preset)
        path = self.preset_dir / f"{slugify(serialized_data['name'])}.json"
        path.write_text(json.dumps(serialized_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return hydrate_preset(serialized_data)

    def delete(self, name):
        path = self.preset_dir / f"{slugify(name)}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def _source_files(self):
        if self.preset_dir.exists():
            return list(self.preset_dir.glob("*.json"))
        return []
