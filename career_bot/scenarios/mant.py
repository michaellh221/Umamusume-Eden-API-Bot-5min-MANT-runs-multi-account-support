"""
career_bot/scenarios/mant.py
============================
MantStrategy – decision engine for the "Make a New Track!" (MANT) scenario
(scenario_id = 4).

Decision priority (next_decision)
----------------------------------
1. Career finished?          → "finish"
2. Pending event?            → "event" (handle it, auto-pick best choice)
3. Character state == 3?     → "finish" (goal evaluation screen)
4. Race already in progress? → "race_progress" (resume)
5. Forced race (mandatory)?  → "race"
6. Optional race (planner)?  → "race" (unless G2/G3 skipped for better train)
7. Otherwise                 → best training command via _best_command()

Scoring (_score_command)
------------------------
Each available training command gets a composite score based on:
- Stat gains weighted by stat_priority and score_value from the preset.
- Support card partner bonuses (friendship, special training triggers).
- Deficit multiplier: under-target stats get a score boost.
- Mood / motivation multiplier.
- Summer camp bonus turns are scored differently (higher partner weight).

Extending / forking notes
--------------------------
- BAD_EFFECT_NAMES maps game effect IDs to English names used in cure logic.
- SUMMER_CAMP_TURNS / SUMMER_CONSERVE_TURNS control the special summer calendar.
- To tune scoring, adjust score_value / stat_priority in the preset JSON rather
  than editing weights here.  For structural changes, override _score_command().
"""

from career_bot.events import EventManager
from career_bot.scenarios.base import Decision, ScenarioStrategy


# Maps command_id → stat index [Speed=0, Stamina=1, Power=2, Guts=3, Wit=4].
# Index 30 = Wit (alternative command_id used in summer camp).
STAT_TARGETS = {
    1: 0,
    2: 1,
    3: 2,
    4: 3,
    5: 4,
    30: 5,
}

# command_id → stat index for both regular (1xx) and summer-camp (6xx) training.
TRAINING_COMMANDS = {101: 0, 105: 1, 102: 2, 103: 3, 106: 4, 601: 0, 602: 1, 603: 2, 604: 3, 605: 4}
TRAINING_NAMES = ["Speed", "Stamina", "Power", "Guts", "Wit"]
# Summer camp turns: all training gives bonus partner effects.
# CONSERVE turns: save HP going into camp / before next camp block.
SUMMER_CAMP_TURNS = {36, 37, 38, 39, 40, 60, 61, 62, 63, 64}
SUMMER_CONSERVE_TURNS = {35, 36, 59, 60}
SUMMER_CONSERVE_ENERGY = 60
ENERGY_FAST_MEDIC = 80
ENERGY_MEDIC_GENERAL = 85
DECK_PARTNERS = {1, 2, 3, 4, 5, 6}
# Maps bad-condition effect_id → display name used in cure-asap logic.
BAD_EFFECT_NAMES = {
    1: "Night Owl",
    2: "Slacker",
    3: "Skin Outbreak",
    4: "Slow Metabolism",
    5: "Migraine",
    6: "Practice Poor",
}


# ── Strategy class ─────────────────────────────────────────────────────────
class MantStrategy(ScenarioStrategy):
    scenario_id = 4

    def __init__(self, race_planner=None):
        self.race_planner = race_planner
        self.event_manager = None
        if self.race_planner and self.race_planner.base_dir:
            self.event_manager = EventManager(self.race_planner.base_dir)
        # Populated by _best_command; read by runner for the diagnostics trace panel.
        self._last_trace = {"reason": "", "rows": []}
        # Stored each call so choose_from_event can weight event scoring by priorities.
        self._current_preset = None

    def next_decision(self, state, preset):
        """
        Inspect the current game state and return the next Decision.
        Called once per turn by CareerRunner.
        Returns a Decision(action_type, payload, reason_string).
        """
        self._current_preset = preset  # expose for choose_from_event
        data = state.get("data") or {}
        chara = data.get("chara_info") or {}
        home = data.get("home_info") or {}
        if "single_mode_finish_common" in data:
            return Decision("finish", {"current_turn": chara["turn"]}, "finished")
        events = data.get("unchecked_event_array") or []
        if events:
            event = events[0] or {}
            choice = self._choice(event)
            payload = {"event_id": event.get("event_id"), "chara_id": event.get("chara_id", 0), "choice_number": choice, "current_turn": chara["turn"]}
            if choice is None:
                payload = {"event_id": event.get("event_id"), "_event": event, "_current_turn": chara["turn"]}
            return Decision("event", payload, "event")
        if chara.get("state") == 3:
            return Decision("finish", {"current_turn": chara["turn"]}, "ready to finish")
        race = data.get("race_start_info")
        playing_state = (chara.get("playing_state") or 0)
        if playing_state == 3:
            return Decision("race_progress", {"current_turn": chara["turn"], "phase": "start", "race_start_info": race, "chara_info": chara}, "resume race start")
        if playing_state == 5:
            return Decision("finish", {"current_turn": chara["turn"]}, "goal failed / career end")     
        if race and race.get("program_id") and playing_state in (2, 4):
            return Decision("race_progress", {"current_turn": chara["turn"], "phase": "start", "race_start_info": race, "chara_info": chara}, "race start")
        if self.race_planner:
            forced_program_id = self.race_planner.forced_program(state)
            if forced_program_id:
                return Decision("race", {"program_id": forced_program_id, "current_turn": chara["turn"], "_strategy": self}, self.race_planner.label(forced_program_id))
            program_id = self.race_planner.choose(state, preset)
            if program_id:
                # G1 races are always mandatory; G2/G3 may be skipped for high-value training
                if self._is_g1_race(program_id) or not self._should_skip_race_for_training(data, chara, preset):
                    return Decision("race", {"program_id": program_id, "current_turn": chara["turn"], "_strategy": self}, self.race_planner.label(program_id))
                # Fall through to training instead of the G2/G3 race
        command = self._best_command(data, chara, preset)
        if command:
            command_type = command.get("command_type", 1)
            command_id = command.get("command_id")
            command_group_id = command.get("command_group_id", 0)
            reason = self._command_reason(command)
            if command_type == 3:
                command_group_id = command_id
                command_id = 0
            return Decision("command", {
                "command_type": command_type,
                "command_id": command_id,
                "command_group_id": command_group_id,
                "select_id": command.get("select_id", 0),
                "current_turn": chara["turn"],
                "current_vital": chara.get("vital", 0),
            }, reason)
        return Decision("idle", {}, "no action")

    def _choice(self, event):
        choices = ((event.get("event_contents_info") or {}).get("choice_array") or [])
        if not choices:
            return 0
        if len(choices) > 1:
            return None
        return 0

    def choice_from_rewards(self, rewards, event):
        choices = ((event.get("event_contents_info") or {}).get("choice_array") or [])
        if not choices:
            return 0
        if not rewards:
            return choices[0].get("select_index", 1)
        best_index = 0
        best_score = None
        for i, reward in enumerate(rewards):
            score = self._reward_score(reward)
            if best_score is None or score > best_score:
                best_score = score
                best_index = i
        if best_index < len(choices):
            return choices[best_index].get("select_index", best_index + 1)
        return choices[0].get("select_index", 1)

    def _reward_score(self, reward):
        score = 0.0
        for item in reward.get("params_inc_dec_info_array") or reward.get("effected_parameter_array") or []:
            target = STAT_TARGETS.get(item.get("target_type"))
            value = float(item.get("value") or 0)
            if target is None:
                if item.get("target_type") == 10:
                    score += value * 0.03
                continue
            score += value * (0.02 if target < 5 else 0.01)
        score += float(reward.get("skill_point") or 0) * 0.01
        score += float(reward.get("vital") or 0) * 0.03
        return score

    def _best_command(self, data, chara, preset):
        commands = (data.get("home_info") or {}).get("command_info_array") or []
        enabled = [cmd for cmd in commands if cmd.get("is_enable", 1)]
        rest = self._rest_command(enabled)
        recreation = self._recreation_command(enabled)
        medic = self._medic_command(enabled)
        training = [cmd for cmd in enabled if cmd.get("command_type") == 1 and cmd.get("command_id") in TRAINING_COMMANDS]
        turn = int(chara.get("turn") or 0)
        vital = int(chara.get("vital") or 0)
        motivation = int(chara.get("motivation") or 3)
        bad_status = self._has_curable_bad_status(chara, preset)
        if not training:
            if medic and bad_status and vital <= ENERGY_MEDIC_GENERAL:
                return medic
            return rest or recreation
        scored = [(self._score_command(cmd, data, chara, preset), cmd) for cmd in training]
        if 48 < turn <= 72:
            stat_keys = ["speed", "stamina", "power", "guts", "wiz"]
            highest_idx = max(range(5), key=lambda idx: int(chara.get(stat_keys[idx]) or 0))
            scored = [(score * 0.95 if TRAINING_COMMANDS.get(cmd.get("command_id"), 0) == highest_idx and score > 0 else score, cmd) for score, cmd in scored]
        best_score, best = max(scored, key=lambda row: row[0])
        # Store trace for diagnostics panel (read by runner after next_decision)
        _label = {1: "Speed", 2: "Stamina", 3: "Power", 4: "Guts", 5: "Wit"}
        self._last_trace = {
            "reason": self._command_reason(best),
            "rows": sorted([
                {
                    "facility": _label.get(cmd.get("command_id"), str(cmd.get("command_id", "?"))),
                    "score": round(float(sc), 2),
                    "failure": int(cmd.get("failure_rate") or 0),
                    "gain": self._command_raw_gain(cmd),
                }
                for sc, cmd in scored
            ], key=lambda r: -r["score"])
        }
        rest_threshold = int(preset.get("rest_threshold") or 48)
        failure = int(best.get("failure_rate") or 0)
        if medic and bad_status and vital <= ENERGY_FAST_MEDIC:
            return medic
        if medic and bad_status and vital <= ENERGY_MEDIC_GENERAL:
            return medic
        if turn in SUMMER_CAMP_TURNS and recreation and (vital <= rest_threshold or failure >= 35 or best_score < 0):
            return recreation
        if self._should_recreate(recreation, preset, turn, motivation, vital, best_score):
            return recreation
        if rest and (vital <= rest_threshold or failure >= 35 or best_score < 0):
            return rest
        conserve = self._summer_conserve_command(enabled, turn, vital, best_score, preset, rest, recreation)
        if conserve:
            return conserve
        return best

    def _is_g1_race(self, program_id):
        """Return True if the race program is a G1 (race_instance_id starts with '1')."""
        if not self.race_planner or not program_id:
            return False
        info = (self.race_planner.program or {}).get(int(program_id or 0)) or {}
        race_inst = str(info.get("race_instance_id") or "")
        return race_inst.startswith("1")

    def _command_raw_gain(self, cmd):
        """Sum of raw stat point gains (target_type 1-5) from a training command."""
        total = 0
        for item in (cmd or {}).get("params_inc_dec_info_array") or []:
            tt = item.get("target_type")
            if tt in (1, 2, 3, 4, 5):
                total += int(item.get("value") or 0)
        return total

    def _should_skip_race_for_training(self, data, chara, preset):
        """Return True if the best training command's projected gain (with items) exceeds 70 pts.

        We check raw stat gain against a threshold of 55; with an Empowering Megaphone
        (~1.3x) and Ankle Weights (~1.15x) that projects to roughly 70+ stat points.
        """
        commands = (data.get("home_info") or {}).get("command_info_array") or []
        training = [
            cmd for cmd in commands
            if cmd.get("is_enable", 1)
            and int(cmd.get("command_type") or 0) == 1
            and cmd.get("command_id") in TRAINING_COMMANDS
        ]
        if not training:
            return False
        best_gain = max(self._command_raw_gain(cmd) for cmd in training)
        # ~55 raw ≈ 70 with Empowering Megaphone + Ankle Weights
        SKIP_RACE_RAW_THRESHOLD = 55
        return best_gain >= SKIP_RACE_RAW_THRESHOLD

    def _rest_command(self, commands):
        for cmd in commands:
            if cmd.get("command_type") == 7 and cmd.get("command_id") == 701:
                return cmd
        return None

    def _recreation_command(self, commands):
        for cmd in commands:
            if cmd.get("command_type") == 3:
                return cmd
        return None

    def _medic_command(self, commands):
        for cmd in commands:
            if cmd.get("command_type") == 8 and cmd.get("command_id") == 801:
                return cmd
        return None

    def _enabled_training(self, commands, command_id):
        for cmd in commands:
            if cmd.get("command_type") == 1 and cmd.get("command_id") == command_id:
                return cmd
        return None

    def _enabled_training_idx(self, commands, idx):
        for cmd in commands:
            if cmd.get("command_type") == 1 and TRAINING_COMMANDS.get(cmd.get("command_id")) == idx:
                return cmd
        return None

    def _summer_conserve_command(self, enabled, turn, vital, best_score, preset, rest, recreation):
        if turn not in SUMMER_CONSERVE_TURNS:
            return None
        if best_score >= float(preset.get("summer_score_threshold") or 0.34):
            return None
        if vital < SUMMER_CONSERVE_ENERGY:
            if turn in SUMMER_CAMP_TURNS and recreation:
                return recreation
            return rest
        return self._enabled_training_idx(enabled, 4)

    def _has_curable_bad_status(self, chara, preset):
        wanted = self._cure_condition_names(preset)
        if not wanted:
            return False
        for effect_id in chara.get("chara_effect_id_array") or []:
            try:
                effect_id = int(effect_id)
            except (TypeError, ValueError):
                continue
            name = BAD_EFFECT_NAMES.get(effect_id)
            if name and self._condition_key(name) in wanted:
                return True
        return False

    def _cure_condition_names(self, preset):
        result = set()
        names = preset.get("cure_asap_conditions") or []
        if isinstance(names, str):
            names = names.split(",")
        for name in names:
            key = self._condition_key(name)
            if key:
                result.add(key)
        return result

    def _condition_key(self, name):
        text = str(name or "").strip()
        if not text or text.startswith("("):
            return ""
        return "".join(ch.lower() for ch in text if ch.isalnum())

    def _command_reason(self, command):
        command_type = command.get("command_type")
        command_id = command.get("command_id")
        if command_id in TRAINING_COMMANDS:
            return f"training {TRAINING_NAMES[TRAINING_COMMANDS.get(command_id, 0)]} {command_id}"
        if command_type == 7 and command_id == 701:
            return f"rest {command_id}"
        if command_type == 3:
            return f"recreation {command_id}"
        if command_type == 8 and command_id == 801:
            return "medic 801"
        return f"command {command_type}:{command_id}"

    def _score_command(self, command, data, chara, preset):
        turn = int(chara.get("turn") or 0)
        weights = self._period_row(preset.get("score_value"), turn, [0.11, 0.10, 0.006, 0.09])
        base = preset.get("base_score") or [0, 0, 0, 0, 0]
        targets = preset.get("expect_attribute") or [9999, 9999, 9999, 9999, 9999]
        idx = TRAINING_COMMANDS.get(command.get("command_id"), 0)
        score = float(base[idx] if idx < len(base) else 0)
        w_lv1 = float(weights[0] if len(weights) > 0 else 0.11)
        w_lv2 = float(weights[1] if len(weights) > 1 else 0.10)
        w_energy = float(weights[2] if len(weights) > 2 else 0.006)
        w_hint = float(weights[3] if len(weights) > 3 else 0.09)
        stat_mult = preset.get("stat_value_multiplier") or [0.01, 0.01, 0.01, 0.01, 0.01, 0.005]
        bonds = self._bond_map(chara)
        partners = command.get("training_partner_array") or []
        hints = set(command.get("tips_event_partner_array") or [])
        pal_count = 0
        hint_count = 0
        for partner_id in partners:
            bond = bonds.get(partner_id, 0)
            if partner_id in hints:
                hint_count += 1

            if bond >= 80:
                continue

            time_decay = max(0.0, (72 - turn) / 72.0)
            efficiency_boost = 1.0 + (bond / 80.0) * 0.5 if bond >= 60 else 1.0
            
            weight = time_decay * efficiency_boost

            if partner_id not in DECK_PARTNERS:
                yield_val = self._npc_score(bond, turn, preset)
                score += yield_val * weight
                continue

            if partner_id == 6:
                pal_count += 1
                yield_val = self._pal_score(bond, preset)
                score += yield_val * weight
                continue

            ratio = min(1.0, bond / 80.0)
            yield_val = w_lv1 + (w_lv2 - w_lv1) * ratio
            score += yield_val * weight
        if hint_count:
            score += w_hint
        for item in command.get("params_inc_dec_info_array") or []:
            value = float(item.get("value") or 0)
            if item.get("target_type") == 10:
                energy_score = value * w_energy
                if int(chara.get("vital") or 0) >= 80 and value < 0:
                    energy_score *= 0.9
                score += energy_score
                continue
            target = STAT_TARGETS.get(item.get("target_type"))
            if target is None:
                continue
            if target == 5:
                continue
            
            stat_gain_score = value * float(stat_mult[target] if target < len(stat_mult) else 0.01)
            cap = float(targets[target] if target < len(targets) else 9999)
            if cap > 0 and target < 5:
                current = self._current_stat(chara, target)
                ratio = current / cap
                if ratio > 1.0:
                    stat_gain_score *= 0.0
                elif ratio > 0.97:
                    stat_gain_score *= 0.35 - ((ratio - 0.97) / 0.03) * 0.25
                elif ratio > 0.94:
                    stat_gain_score *= 0.55 - ((ratio - 0.94) / 0.03) * 0.20
                elif ratio > 0.90:
                    stat_gain_score *= 0.75 - ((ratio - 0.90) / 0.04) * 0.20
                elif ratio > 0.86:
                    stat_gain_score *= 0.85 - ((ratio - 0.86) / 0.04) * 0.10
                elif ratio > 0.82:
                    stat_gain_score *= 0.91 - ((ratio - 0.82) / 0.04) * 0.06
                elif ratio > 0.78:
                    stat_gain_score *= 0.95 - ((ratio - 0.78) / 0.04) * 0.04
                elif ratio > 0.74:
                    stat_gain_score *= 0.98 - ((ratio - 0.74) / 0.04) * 0.03
                elif ratio > 0.70:
                    stat_gain_score *= 1.00 - ((ratio - 0.70) / 0.04) * 0.02
            score += stat_gain_score
        if pal_count:
            score *= 1.0 + max(0.0, min(1.0, float(preset.get("pal_card_multiplier") or 0.1)))
        if preset.get("compensate_failure", True):
            score *= max(0.0, 1.0 - (float(command.get("failure_rate") or 0) / 50.0))
        if idx == 4:
            vital = int(chara.get("vital") or 0)
            max_vital = int(chara.get("max_vital") or 100)
            gain = 0
            for item in command.get("params_inc_dec_info_array") or []:
                if item.get("target_type") == 10:
                    gain = float(item.get("value") or 0)
                    break
            if vital >= max_vital or (gain > 0 and vital + gain > max_vital):
                score *= 0.35 if turn > 72 else 0.75
            elif vital < 85:
                score *= 1.03
        extra = self._extra_weight(idx, turn, preset)
        if extra == -1:
            return -999.0
        score *= max(0.0, min(2.0, 1.0 + extra))

        if turn < 60:
            deck_mults = preset.get("_deck_multipliers")
            if deck_mults and len(deck_mults) > idx:
                score *= float(deck_mults[idx])

        stat_priority = preset.get("stat_priority")
        if isinstance(stat_priority, list) and idx < 5:
            try:
                rank = stat_priority.index(idx)
                priority_mults = [1.25, 1.12, 1.00, 0.90, 0.80]
                score *= priority_mults[rank] if rank < len(priority_mults) else 1.0
            except ValueError:
                pass

        # Target-based deficit multiplier: boost lagging stats, de-prioritize finished ones
        if idx < 5:
            current_val = self._current_stat(chara, idx)
            ideal_targets = preset.get("stat_ideal_targets") or []
            min_targets = preset.get("stat_min_targets") or []
            ideal = int(ideal_targets[idx]) if idx < len(ideal_targets) else 0
            minimum = int(min_targets[idx]) if idx < len(min_targets) else 0

            if minimum > 0 and current_val < minimum:
                # Below minimum — strong proportional boost (up to 1.6x)
                deficit = 1.0 - (current_val / minimum)
                score *= 1.0 + 0.6 * deficit
            elif ideal > 0 and current_val < ideal:
                # Below ideal — moderate proportional boost (up to 1.3x)
                ref = minimum if minimum > 0 else 0
                deficit = (ideal - current_val) / (ideal - ref) if ideal > ref else 0.0
                score *= 1.0 + 0.3 * deficit
            elif ideal > 0 and current_val >= ideal:
                # Already at ideal — de-prioritize so other stats catch up
                score *= 0.85

        return score

    def _current_stat(self, chara, target):
        keys = ["speed", "stamina", "power", "guts", "wiz", "skill_point"]
        return float(chara.get(keys[target], 0) or 0)

    def _team_command(self, data, command_id):
        team_data = data.get("team_data_set") or {}
        for cmd in team_data.get("command_info_array") or []:
            if cmd.get("command_id") == command_id:
                return cmd
        return None

    def _bond_map(self, chara):
        result = {}
        for row in chara.get("evaluation_info_array") or []:
            result[row.get("target_id", 0)] = row.get("evaluation", 0)
        return result

    def _npc_score(self, bond, turn, preset):
        if bond >= 80:
            return 0.0
        row = self._period_row(preset.get("npc_score_value"), turn, [0.05, 0.05, 0.05])
        v1 = float(row[0] if len(row) > 0 else 0.05)
        v2 = float(row[1] if len(row) > 1 else v1)
        ratio = min(1.0, bond / 80.0)
        return v1 + (v2 - v1) * ratio

    def _pal_score(self, bond, preset):
        if bond >= 80:
            return 0.0
        scores = preset.get("pal_friendship_score") or [0.08, 0.057, 0.018]
        v1 = float(scores[0] if len(scores) > 0 else 0.08)
        v2 = float(scores[1] if len(scores) > 1 else v1)
        ratio = min(1.0, bond / 80.0)
        return v1 + (v2 - v1) * ratio

    def _period_index(self, turn):
        if turn <= 24:
            return 0
        if turn <= 48:
            return 1
        if turn <= 60:
            return 2
        if turn <= 72:
            return 3
        return 4

    def _period_row(self, rows, turn, fallback):
        if not isinstance(rows, list) or not rows:
            return fallback
        idx = min(self._period_index(turn), len(rows) - 1)
        row = rows[idx]
        return row if isinstance(row, list) else fallback

    def _extra_weight(self, idx, turn, preset):
        rows = preset.get("extra_weight") or [[0, 0, 0, 0, 0]] * 4
        if turn <= 24:
            row_idx = 0
        elif turn <= 48:
            row_idx = 1
        elif turn in SUMMER_CAMP_TURNS and len(rows) >= 4:
            row_idx = 3
        else:
            row_idx = 2
        if row_idx >= len(rows) or not isinstance(rows[row_idx], list) or idx >= len(rows[row_idx]):
            return 0.0
        return float(rows[row_idx][idx] or 0)

    def _mood_threshold(self, turn, preset):
        if turn <= 36:
            return int(preset.get("motivation_threshold_year1") or 3)
        if turn <= 60:
            return int(preset.get("motivation_threshold_year2") or 4)
        return int(preset.get("motivation_threshold_year3") or 4)

    def _should_recreate(self, recreation, preset, turn, motivation, vital, best_score):
        if not recreation:
            return False
        if turn in SUMMER_CAMP_TURNS:
            return False
        if motivation < self._mood_threshold(turn, preset) and vital < 90 and best_score <= 0.3:
            return True
        if not preset.get("prioritize_recreation"):
            return False
        thresholds = preset.get("pal_thresholds") or []
        if not thresholds:
            return False
        stage = int(preset.get("_pal_event_stage") or 0)
        if stage >= len(thresholds):
            stage = 0
        row = thresholds[stage]
        if not isinstance(row, list) or len(row) < 2:
            return False
        mood_ok = motivation <= int(row[0])
        energy_ok = vital <= int(row[1])
        score_ok = True
        if len(row) > 2:
            score_ok = best_score <= float(row[2])
        return mood_ok and energy_ok and score_ok

    def choose_from_event(self, event, current_turn):
        if self.event_manager:
            stat_priority = getattr(self._current_preset, "stat_priority", None)
            return self.event_manager.choose(event, stat_priority=stat_priority)
        return 1
