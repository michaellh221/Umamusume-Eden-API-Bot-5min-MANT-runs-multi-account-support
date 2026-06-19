"""
career_bot/skills.py
====================
SkillBuyer – end-of-career skill optimizer.

All SP is hoarded during the run.  On the finish trigger (after TS Climax
Race 3), the optimizer selects the best combination of available skills
within the accumulated SP budget using a grouped 0/1 knapsack algorithm.

Skill scoring (Team Trials consistency model)
---------------------------------------------
  Ported from UmaTools/js/team-trials-optimizer.js.

  Expected value = SV × consistency²
    Gold SV = 12 (fires = 1200 TT pts),  White SV = 5 (fires = 500 TT pts)
    consistency = timingScore×0.45 + breadthScore×0.3 + scenarioScore×0.25
                  (combined across condition groups, penalised for situational triggers)

  Penalties (applied before knapsack):
    Green / recovery skills (effect type 9):  -0.05 consistency, ×0.88 expected
    Volatile race-condition skills (weather/season/rotation):
                                              -0.22 consistency, ×0.80 expected
  Consistent-gold bonus (gold, consistency≥0.58, no volatile):
    consistency += 0.06,  expected += 0.14

Tag mappings (skill_data.json)
------------------------------
  Running style: 101=nige  102=senko  103=sashi  104=oikomi
  Distance:      201=short 202=mile   203=medium  204=long

Data files used
---------------
  data/skill_data.json  – name, rarity, cost, grade_value, tags, group_id
  data/skills_all.json  – condition groups + effect types (UmaTools export)
"""

import json
import re
from pathlib import Path

# ── Running style → skill tag ──────────────────────────────────────────────
STYLE_TAG_MAP = {1: 101, 2: 102, 3: 103, 4: 104}
# ── Target distance → skill tag ────────────────────────────────────────────
DISTANCE_TAG_MAP = {1: 201, 2: 202, 3: 203, 4: 204}
ALL_DISTANCE_TAGS = set(DISTANCE_TAG_MAP.values())  # {201, 202, 203, 204}
ALL_STYLE_TAGS    = set(STYLE_TAG_MAP.values())      # {101, 102, 103, 104}

MARK_WHITE_CIRCLE  = "○"
MARK_DOUBLE_CIRCLE = "◎"
MARK_X             = "×"
MARK_LARGE_CIRCLE  = "◯"
MOJI_WHITE_CIRCLE  = "○"
MOJI_LARGE_CIRCLE  = "◯"
MOJI_DOUBLE_CIRCLE = "◎"
MOJI_X             = "×"

# ── Team Trials scoring constants (from UmaTools DEFAULT_WEIGHTS) ──────────
TT_SV_GOLD                   = 12     # gold fires → 1200 TT pts
TT_SV_WHITE                  = 5      # white fires → 500 TT pts
TT_GOLD_MIN_CONSISTENCY      = 0.58
TT_GOLD_CONSISTENCY_BONUS    = 0.06
TT_GOLD_EXPECTED_BONUS       = 0.14
TT_GREEN_CONSISTENCY_PENALTY = 0.05
TT_GREEN_EXPECTED_PENALTY    = 0.12
TT_VOLATILE_CONSIST_PENALTY  = 0.22
TT_VOLATILE_EXPECTED_PENALTY = 0.20


# ── Text normalisation helpers ──────────────────────────────────────────────
def norm(text):
    return re.sub(r'[^a-z0-9]+', '', str(text or '').lower())


def strip_mark(text):
    if not text:
        return ""
    for m in [MARK_WHITE_CIRCLE, MARK_DOUBLE_CIRCLE, MARK_X, MARK_LARGE_CIRCLE,
              MOJI_WHITE_CIRCLE, MOJI_DOUBLE_CIRCLE, MOJI_X, MOJI_LARGE_CIRCLE]:
        text = text.replace(m, "")
    return text.strip()


# ── Team Trials consistency scoring functions ───────────────────────────────
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _cond_text(g):
    """Join condition + precondition for a condition-group dict."""
    c = g.get("condition") or ""
    p = g.get("precondition") or ""
    return " & ".join(x for x in [c, p] if x)


def _cmp_count(t):
    return len(re.findall(r"==|>=|<=|>|<", str(t or "")))


def _range_cov(t, key, max_val):
    """Fraction of [1..max_val] covered by comparators for `key` in text t."""
    s = str(t or "").lower()
    if not s:
        return None
    lo, hi, ok = 1, max_val, False
    eq = re.search(rf"{key}\s*==\s*(-?\d+)", s, re.I)
    if eq:
        v = int(eq.group(1))
        lo = hi = v
        ok = True
    ge = re.search(rf"{key}\s*>=\s*(-?\d+)", s, re.I)
    if ge:
        lo = max(lo, int(ge.group(1)))
        ok = True
    gt = re.search(rf"{key}\s*>\s*(-?\d+)", s, re.I)
    if gt:
        lo = max(lo, int(gt.group(1)) + 1)
        ok = True
    le = re.search(rf"{key}\s*<=\s*(-?\d+)", s, re.I)
    if le:
        hi = min(hi, int(le.group(1)))
        ok = True
    lt = re.search(rf"{key}\s*<\s*(-?\d+)", s, re.I)
    if lt:
        hi = min(hi, int(lt.group(1)) - 1)
        ok = True
    if not ok:
        return None
    if hi < lo:
        return 0.0
    return _clamp((hi - lo + 1) / max_val, 0.0, 1.0)


def _timing_score(t):
    t = str(t or "").lower()
    if not t:
        return 0.62
    if re.search(r"always\s*==\s*1", t):
        return 0.98
    if re.search(r"is_lastspurt|is_finalcorner|is_last_straight", t):
        return 0.76 if "_random" in t else 0.88
    if re.search(r"phase_random|phase_[a-z_]*random|corner_random|straight_random"
                 r"|distance_rate_after_random", t):
        return 0.62
    if re.search(r"phase\s*==\s*[1234]", t):
        return 0.76
    if "distance_rate" in t:
        cov = _range_cov(t, "distance_rate", 100)
        return 0.82 if (cov is not None and cov <= 0.2) else 0.72
    if "corner" in t:
        return 0.75
    return 0.68


def _breadth_score(t):
    t = str(t or "").lower()
    if not t:
        return 0.65
    parts = []
    for key, mx in [("order", 18), ("order_rate", 100), ("distance_rate", 100)]:
        v = _range_cov(t, key, mx)
        if v is not None:
            parts.append(v)
    m = re.search(r"near_count\s*>=\s*(\d+)", t)
    if m:
        parts.append(_clamp((10 - int(m.group(1)) + 1) / 10, 0.1, 1.0))
    if parts:
        b = sum(parts) / len(parts)
    elif re.search(r"always\s*==\s*1", t):
        b = 0.96
    else:
        b = 0.72
    if re.search(r"order\s*==\s*1", t):
        b = min(b, 0.18)
    if re.search(r"order\s*<=\s*5", t):
        b = max(b, 0.52)
    c = _cmp_count(t)
    if c >= 4:
        b -= min(0.2, (c - 3) * 0.05)
    return _clamp(b, 0.05, 1.0)


def _scenario_score(t):
    t = str(t or "").lower()
    if not t:
        return 0.70
    s = 0.95
    if re.search(r"blocked_side_continuetime|blocked_front_continuetime|blocked_front", t): s -= 0.22
    if re.search(r"is_overtake", t):                                                        s -= 0.18
    if re.search(r"change_order_onetime|change_order_up_end_after|change_order_up_middle", t): s -= 0.14
    if re.search(r"is_move_lane", t):                                                       s -= 0.10
    if re.search(r"is_surrounded|temptation_count|is_temptation", t):                      s -= 0.20
    if re.search(r"popularity|post_number", t):                                             s -= 0.12
    if re.search(r"is_activate_other_skill_detail|is_activate_any_skill|activate_count_", t): s -= 0.09
    if re.search(r"order\s*==\s*1", t):                                                    s -= 0.16
    if re.search(r"near_count\s*>=\s*[34]", t):                                            s -= 0.09
    if re.search(r"always\s*==\s*1", t):                                                   s += 0.04
    return _clamp(s, 0.05, 1.0)


def _has_volatile_race_condition(t):
    t = str(t or "").lower()
    return bool(re.search(
        r"(track_id|ground_condition|weather|season|rotation)\s*(==|!=|>=|<=|>|<)", t))



def _score_tt_consistency(condition_groups, running_style_value=None):
    """
    Port of scoreSkillConsistency() from team-trials-optimizer.js.
    Returns (consistency: float, has_volatile_race: bool).
    """
    gs = condition_groups or []
    g_scores = []
    volatile = False

    if not gs:
        g_scores.append(0.58)

    for g in gs:
        t = _cond_text(g)
        if not t:
            continue
        ts = _timing_score(t)
        bs = _breadth_score(t)
        ss = _scenario_score(t)

        strict = 0
        if re.search(r"order\s*==\s*1", t):                       strict += 2
        if re.search(r"blocked_|is_overtake|change_order_onetime", t): strict += 2
        if re.search(r"phase_random|corner_random|straight_random", t): strict += 1
        if _cmp_count(t) >= 5:                                     strict += 1
        pen = min(0.24, strict * 0.04)
        gs0 = _clamp(ts * 0.45 + bs * 0.3 + ss * 0.25 - pen, 0.05, 0.99)
        g_scores.append(gs0)

        # Fixed-setup deterministic bonus
        if (re.search(r"(distance_type|ground_type|running_style)\s*(==|!=|>=|<=|>|<)", t)
                and not _has_volatile_race_condition(t)
                and not re.search(
                    r"_random|blocked_|is_overtake|change_order|temptation|popularity|post_number", t)):
            g_scores.append(max(0.72, gs0))

        if _has_volatile_race_condition(t):
            volatile = True

    # Combine via miss-probability product
    miss = 1.0
    for v in g_scores:
        miss *= 1.0 - min(0.97, v * 0.9)
    c = (1.0 - miss) if g_scores else 0.58

    # Multi-group bonus
    if len(gs) > 1:
        c += min(0.08, (len(gs) - 1) * 0.03)

    c = _clamp(c, 0.05, 0.99)

    # Running-style match bonus: +0.06 if skill condition references our style
    if running_style_value:
        all_cond = " ".join(_cond_text(g) for g in gs).lower()
        sv = str(running_style_value)
        pos_match   = bool(re.search(rf"running_style\s*==\s*{sv}", all_cond))
        neg_exclude = bool(re.search(rf"running_style\s*!=\s*{sv}", all_cond))
        pos_other   = bool(re.search(r"running_style\s*==\s*\d", all_cond)) and not pos_match
        has_style   = bool(re.search(r"running_style\s*(==|!=)", all_cond))
        if has_style and pos_match and not neg_exclude:
            c += 0.06
        elif has_style and not pos_other and not neg_exclude:
            c += 0.06
        c = _clamp(c, 0.05, 0.99)

    return c, volatile


# ── SkillBuyer ──────────────────────────────────────────────────────────────
class SkillBuyer:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.skill_names = {}
        self.skill_rarities = {}
        self.skill_costs = {}
        self.skill_grade_values = {}
        self.skill_id_exists = set()
        self.group_to_skill_ids = {}
        self.skill_to_group_id = {}
        self.skill_tags = {}
        # TT condition-group data (from skills_all.json)
        self.skill_condition_groups = {}   # skill_id → list[dict]
        self.green_skill_ids: set   = set()  # IDs from skills_green.json
        self.failed_this_turn = {}
        self.current_turn = None
        self.last_candidates = []
        self.last_selected = []
        self.last_attempt = []
        self.last_result = {}
        self.recover_after_error = False
        self.attempt_events = []
        self._load()

    def _load(self):
        path = self.base_dir / "data" / "skill_data.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.skill_names = {}
            self.skill_rarities = {}
            self.skill_costs = {}
            self.skill_grade_values = {}
            self.skill_to_group_id = {}
            self.skill_tags = {}
            for raw_id, raw_info in data.items():
                skill_id = int(raw_id)
                if isinstance(raw_info, dict):
                    self.skill_names[skill_id] = raw_info.get("name") or str(skill_id)
                    self.skill_rarities[skill_id] = int(raw_info.get("rarity") or 0)
                    self.skill_costs[skill_id] = int(raw_info.get("need_skill_point") or 0)
                    self.skill_grade_values[skill_id] = int(raw_info.get("grade_value") or 0)
                    group_id = int(raw_info.get("group_id") or 0)
                    if group_id:
                        self.skill_to_group_id[skill_id] = group_id
                    tags = raw_info.get("tags")
                    self.skill_tags[skill_id] = tags if isinstance(tags, list) else []
                else:
                    self.skill_names[skill_id] = raw_info
        except Exception:
            return
        self.skill_id_exists = set(self.skill_names)
        self.group_to_skill_ids = {}
        for skill_id in self.skill_names:
            group_id = self.skill_to_group_id.get(skill_id) or (skill_id if skill_id < 100000 else skill_id // 10)
            self.skill_to_group_id[skill_id] = group_id
            self.group_to_skill_ids.setdefault(group_id, []).append(skill_id)

        for group_id, ids in self.group_to_skill_ids.items():
            children = [sid for sid in ids if sid >= 100000]
            if children:
                self.group_to_skill_ids[group_id] = sorted(children, key=self._tier_sort_key)
            else:
                self.group_to_skill_ids[group_id] = sorted(ids, key=self._tier_sort_key)

        # Load condition-group data from skills_all.json (UmaTools export)
        uma_path = self.base_dir / "data" / "skills_all.json"
        if uma_path.exists():
            try:
                uma_raw = json.loads(uma_path.read_text(encoding="utf-8"))
                items = uma_raw if isinstance(uma_raw, list) else list(uma_raw.values())
                for item in items:
                    sid = int(item.get("id") or 0)
                    if not sid:
                        continue
                    cgs = item.get("condition_groups") or []
                    if not cgs:
                        gene = item.get("gene_version") or {}
                        cgs = gene.get("condition_groups") or []
                    if cgs:
                        self.skill_condition_groups[sid] = cgs
            except Exception:
                pass

        # Load green skill IDs from skills_green.json (UmaTools classification).
        # Green skills = stamina recovery skills shown with a green icon in-game.
        # We use UmaTools' curated list rather than checking effect type==9 directly,
        # because many speed/accel skills also carry a secondary recovery effect and
        # would be wrongly penalised if we only checked for effect type 9.
        green_path = self.base_dir / "data" / "skills_green.json"
        if green_path.exists():
            try:
                green_raw = json.loads(green_path.read_text(encoding="utf-8"))
                g_items = green_raw if isinstance(green_raw, list) else list(green_raw.values())
                for item in g_items:
                    gid = int(item.get("id") or 0)
                    if gid:
                        self.green_skill_ids.add(gid)
            except Exception:
                pass

    def _tier_sort_key(self, skill_id):
        grade_value = int(self.skill_grade_values.get(skill_id) or 0)
        return (
            int(self.skill_rarities.get(skill_id) or 99),
            1 if grade_value <= 0 else 0,
            grade_value if grade_value > 0 else 999999,
            int(skill_id),
        )

    def _tier_ids(self, group_id, rarity):
        ids = [
            sid for sid in self.group_to_skill_ids.get(group_id, [])
            if self.skill_rarities.get(sid, 0) == rarity and self.skill_grade_values.get(sid, 0) > 0
        ]
        return sorted(ids, key=self._tier_sort_key)

    def _resolve_buyable_tier(self, group_id, rarity, owned_skill_ids):
        tiers = self._tier_ids(group_id, rarity)
        if not tiers:
            candidates = [
                sid for sid in self.group_to_skill_ids.get(group_id, [])
                if self.skill_rarities.get(sid, 0) == rarity and sid not in owned_skill_ids
            ]
            return sorted(candidates, key=self._tier_sort_key)[0] if candidates else 0
        for index, sid in enumerate(tiers):
            if sid in owned_skill_ids:
                continue
            if index == 0 or tiers[index - 1] in owned_skill_ids:
                return sid
            return 0
        return 0

    def _unowned_white_tiers(self, group_id, owned_skill_ids):
        return [sid for sid in self._tier_ids(group_id, 1) if sid not in owned_skill_ids]

    def reset_scoped_failures(self):
        self.failed_this_turn = {}
        self.current_turn = None
        self.last_candidates = []
        self.last_selected = []
        self.last_attempt = []
        self.last_result = {}

    def _set_turn(self, turn):
        turn = int(turn or 0)
        if self.current_turn != turn:
            self.current_turn = turn
            self.failed_this_turn = {turn: set()}
        self.failed_this_turn.setdefault(turn, set())

    def _failed_for_turn(self, turn=None):
        turn = int(turn if turn is not None else self.current_turn or 0)
        return self.failed_this_turn.setdefault(turn, set())

    def buy(self, client, state, preset, force=False):
        """
        force=False  → always defer; no skills are bought mid-run.
        force=True   → end-of-career optimizer: score all available skill tips
                        by TT consistency model, run knapsack, buy best set.
        """
        data = state.get("data") or {}
        chara = data.get("chara_info") or data.get("single_mode_chara_light") or {}
        self.recover_after_error = False
        self.attempt_events = []

        if not force:
            self.last_candidates = []
            self.last_selected = []
            self.last_attempt = []
            self.last_result = {"skip": "deferred_to_end"}
            return state, 0

        if not chara:
            return state, 0

        points = int(chara.get("skill_point") or 0)
        turn = int(chara.get("turn") or 0)
        self._set_turn(turn)

        candidates = self._optimizer_build_candidates(chara, preset)
        self.last_candidates = [dict(c) for c in candidates]

        if not candidates:
            self.last_selected = []
            self.last_attempt = []
            self.last_result = {"skip": "no_optimizer_candidates", "points": points}
            return state, 0

        selected = self._knapsack_solve(candidates, points)
        self.last_selected = [dict(c) for c in selected]

        if not selected:
            self.last_attempt = []
            self.last_result = {"skip": "knapsack_empty", "points": points}
            return state, 0

        return self._buy_batch(client, state, selected, turn)

    def preview(self, state, preset, force=False):
        """Show what the optimizer would pick (used by the diagnostics panel)."""
        data = state.get("data") or {}
        chara = data.get("chara_info") or data.get("single_mode_chara_light") or {}
        if not chara:
            self.last_candidates = []
            self.last_selected = []
            return
        turn = int(chara.get("turn") or 0)
        self._set_turn(turn)
        points = int(chara.get("skill_point") or 0)
        candidates = self._optimizer_build_candidates(chara, preset)
        selected = self._knapsack_solve(candidates, points)
        self.last_candidates = [dict(c) for c in candidates]
        self.last_selected = [dict(c) for c in selected]

    # ── Optimizer core ──────────────────────────────────────────────────────

    def _optimizer_score(self, skill_id, running_style_value, distance_tag, mode="team_trials"):
        """
        Compute optimizer score for a single skill.

        mode="team_trials"  → SV × consistency²  (TT activation-based expected value)
                               Gold SV=12, White SV=5.  Penalises green/volatile skills.
        mode="score"        → grade_value-based (maximises the game's own score metric).
                               Style/distance tag bonuses applied on top.
        """
        rarity = self.skill_rarities.get(skill_id, 1)
        is_gold = rarity >= 2

        if mode == "score":
            # ── Score mode: maximise grade_value (game's own scoring metric) ──
            grade_value = int(self.skill_grade_values.get(skill_id) or 0)
            if grade_value <= 0:
                grade_value = 400 if is_gold else 150
            tags = self.skill_tags.get(skill_id) or []
            bonus = 0
            if distance_tag and distance_tag in tags:
                bonus += 80
            if running_style_value:
                style_tag = STYLE_TAG_MAP.get(running_style_value)
                if style_tag and style_tag in tags:
                    bonus += 80
            # Return as a comparable float (grade_value scale is ~85-633)
            return float(grade_value + bonus)

        # ── Team Trials mode: SV × consistency² ──────────────────────────────
        cgs = self.skill_condition_groups.get(skill_id) or []
        consistency, volatile = _score_tt_consistency(cgs, running_style_value)

        extra_pen    = 0.0
        expected_mul = 1.0
        if skill_id in self.green_skill_ids:
            extra_pen    += TT_GREEN_CONSISTENCY_PENALTY
            expected_mul *= (1.0 - TT_GREEN_EXPECTED_PENALTY)
        if volatile:
            extra_pen    += TT_VOLATILE_CONSIST_PENALTY
            expected_mul *= (1.0 - TT_VOLATILE_EXPECTED_PENALTY)

        consistency = _clamp(consistency - extra_pen, 0.05, 0.99)

        if is_gold and not volatile and consistency >= TT_GOLD_MIN_CONSISTENCY:
            consistency = _clamp(consistency + TT_GOLD_CONSISTENCY_BONUS, 0.05, 0.99)

        sv = TT_SV_GOLD if is_gold else TT_SV_WHITE
        expected = sv * (consistency ** 2) * expected_mul

        if is_gold and not volatile and (consistency - TT_GOLD_CONSISTENCY_BONUS) >= TT_GOLD_MIN_CONSISTENCY:
            expected += TT_GOLD_EXPECTED_BONUS

        return expected

    def _optimizer_build_candidates(self, chara, preset):
        """
        Build one scored candidate per skill group_id present in skill_tips_array.
        Scoring mode is read from preset["skill_optimizer_mode"]:
          "team_trials" → SV × consistency² (default)
          "score"       → grade_value-based
        Distance/style wrong-tag skills are hard-excluded in both modes.
        """
        owned = {int(item.get("skill_id") or 0) for item in chara.get("skill_array") or []}

        mode = str(preset.get("skill_optimizer_mode") or "team_trials")
        # Prefer the actual running style the chara used this career over the preset
        # default. They can differ when the bot auto-selected a style or the user
        # changed the preset mid-run, and buying wrong-style skills is wasteful.
        actual_style  = int(chara.get("race_running_style") or 0)
        preset_style  = int(preset.get("running_style") or 0)
        running_style_value = actual_style or preset_style
        if actual_style and actual_style != preset_style:
            print(f"[skills] using chara running style {actual_style} (preset has {preset_style})")
        style_tag    = STYLE_TAG_MAP.get(running_style_value)
        distance_tag = DISTANCE_TAG_MAP.get(int(preset.get("target_distance") or 0))

        best_per_group: dict = {}

        for tip in chara.get("skill_tips_array") or []:
            group_id   = int(tip.get("group_id") or 0)
            tip_rarity = int(tip.get("rarity") or 0)
            hint_level = int(tip.get("level") or 0)

            if not group_id:
                continue

            # Resolve which skill ID to buy for this tip.
            if tip_rarity:
                buyable_id = self._resolve_buyable_tier(group_id, tip_rarity, owned)
            else:
                buyable_id = self._resolve_buyable_tier(group_id, 2, owned)
                if buyable_id:
                    tip_rarity = 2
                else:
                    buyable_id = self._resolve_buyable_tier(group_id, 1, owned)
                    tip_rarity = 1 if buyable_id else 0

            if not buyable_id or buyable_id in owned:
                continue

            name = self.skill_names.get(buyable_id, "")
            if not name:
                continue

            # Hard-exclude skills tagged for a different distance or style.
            skill_tags = self.skill_tags.get(buyable_id) or []
            if distance_tag:
                if any(t in ALL_DISTANCE_TAGS and t != distance_tag for t in skill_tags):
                    continue
            if style_tag:
                if any(t in ALL_STYLE_TAGS and t != style_tag for t in skill_tags):
                    continue

            # Keep highest-rarity option per group.
            existing = best_per_group.get(group_id)
            if existing and existing["tip_rarity"] >= tip_rarity:
                continue

            # Bundled whites when buying a gold.
            bundled: list = []
            if tip_rarity == 2:
                bundled = self._unowned_white_tiers(group_id, owned)

            cost = self._estimate_cost({"skill_id": buyable_id, "hint_level": hint_level, "name": name})
            for w_id in bundled:
                cost += self._estimate_cost({
                    "skill_id": w_id,
                    "hint_level": 0,
                    "name": self.skill_names.get(w_id, ""),
                })

            # Score the main skill in the selected mode
            score = self._optimizer_score(buyable_id, running_style_value, distance_tag, mode)

            # Credit bundled whites at their own score
            for w_id in bundled:
                score += self._optimizer_score(w_id, running_style_value, distance_tag, mode)

            # Scale to integers for the knapsack (×10000 to preserve two decimal places)
            score_int = max(0, int(round(score * 10000)))

            best_per_group[group_id] = {
                "skill_id":            buyable_id,
                "group_id":            group_id,
                "tip_rarity":          tip_rarity,
                "hint_level":          hint_level,
                "name":                name,
                "cost":                cost,
                "score":               score_int,
                "bundled_skill_ids":   bundled,
                "resolution_reason":   "optimizer",
                "failed_scope":        None,
                "candidate_skill_ids": [buyable_id],
            }

        candidates = list(best_per_group.values())
        candidates.sort(key=lambda c: -c["score"])
        return candidates

    def _knapsack_solve(self, candidates, budget):
        """
        0/1 knapsack: maximise total TT expected value within SP budget.
        Returns the selected subset of candidates.
        """
        budget = max(0, int(budget))
        if not candidates or budget <= 0:
            return []

        costs  = [max(1, int(c["cost"]))  for c in candidates]
        scores = [max(0, int(c["score"])) for c in candidates]
        N = len(candidates)
        B = budget

        dp = [[0] * (B + 1) for _ in range(N + 1)]

        for i in range(1, N + 1):
            c = costs[i - 1]
            s = scores[i - 1]
            for b in range(B + 1):
                dp[i][b] = dp[i - 1][b]
                if b >= c and dp[i - 1][b - c] + s > dp[i][b]:
                    dp[i][b] = dp[i - 1][b - c] + s

        selected = []
        b = B
        for i in range(N, 0, -1):
            if dp[i][b] != dp[i - 1][b]:
                selected.append(candidates[i - 1])
                b -= costs[i - 1]

        selected.reverse()
        return selected

    def _candidates(self, chara, preset):
        owned = {int(item.get("skill_id") or 0) for item in chara.get("skill_array") or []}
        owned_groups = {self.skill_to_group_id.get(skill_id, skill_id // 10) for skill_id in owned}
        result = []
        for tip in chara.get("skill_tips_array") or []:
            resolved = self.resolve_skill_tip(tip, owned, owned_groups, preset)
            if not resolved or resolved.get("skip_reason"):
                continue
            skill_id = resolved["resolved_skill_id"]
            name = resolved["resolved_name"] or ""
            result.append({
                "skill_id": skill_id,
                "group_id": resolved["group_id"],
                "tip_rarity": resolved["tip_rarity"],
                "hint_level": resolved["hint_level"],
                "name": name,
                "priority": resolved["priority"],
                "cost": resolved["cost"],
                "bundled_skill_ids": resolved.get("bundled_skill_ids") or [],
                "resolution_reason": resolved["resolution_reason"],
                "failed_scope": resolved["failed_scope"],
                "candidate_skill_ids": resolved["candidate_skill_ids"],
            })
        result.sort(key=lambda item: (-item["hint_level"], item["cost"], item["skill_id"]))

        deduped = []
        seen = set()
        for item in result:
            if item["skill_id"] not in seen:
                seen.add(item["skill_id"])
                deduped.append(item)
        return deduped

    def resolve_skill_tip(self, tip, owned_skill_ids, owned_groups, preset):
        group_id = int(tip.get("group_id") or 0)
        tip_rarity = int(tip.get("rarity") or 0)
        hint_level = int(tip.get("level") or 0)
        failed = self._failed_for_turn()
        if tip_rarity:
            buyable_tier = self._resolve_buyable_tier(group_id, tip_rarity, owned_skill_ids)
            candidate_skill_ids = [buyable_tier] if buyable_tier else []
        else:
            candidate_skill_ids = [
                sid for sid in self.group_to_skill_ids.get(group_id, [])
                if sid not in owned_skill_ids
            ]

        row = {
            "group_id": group_id,
            "tip_rarity": tip_rarity,
            "hint_level": hint_level,
            "candidate_skill_ids": list(candidate_skill_ids),
            "resolved_skill_id": 0,
            "resolved_name": "",
            "cost": 0,
            "priority": 999,
            "resolution_reason": "",
            "master_exists": False,
            "skip_reason": None,
            "failed_scope": None,
        }
        if not candidate_skill_ids:
            row["skip_reason"] = "unknown_master"
            return row

        usable = [sid for sid in candidate_skill_ids if sid not in failed]
        if not usable:
            row["skip_reason"] = "failed_this_turn"
            row["failed_scope"] = "this_turn"
            return row

        normal = [sid for sid in usable if not (self.skill_names.get(sid, "").endswith(MARK_X) or self.skill_names.get(sid, "").endswith(MOJI_X))]
        if not normal:
            row["skip_reason"] = "no_normal_skills"
            return row

        normal.sort(key=self._tier_sort_key)
        resolved = normal[0]
        name = self.skill_names.get(resolved, "")

        best_priority = 999
        reason = "first_valid_variant"

        for sid in normal:
            s_name = self.skill_names.get(sid, "")
            if any(s_name.endswith(m) for m in [MARK_WHITE_CIRCLE, MARK_LARGE_CIRCLE, MOJI_WHITE_CIRCLE, MOJI_LARGE_CIRCLE]):
                best_priority = 500
                reason = "circle_variant"
                break

        if not name:
            row["skip_reason"] = "unknown_master"
            return row

        is_double = name.endswith(MARK_DOUBLE_CIRCLE) or name.endswith(MOJI_DOUBLE_CIRCLE)
        if preset.get("skip_double_circle_unless_high_hint", False) and is_double and hint_level < 4:
            row["skip_reason"] = "rule_rejected"
            return row

        row["resolved_skill_id"] = resolved
        row["resolved_name"] = name
        bundled_skill_ids = []
        cost = self._estimate_cost({"skill_id": resolved, "hint_level": hint_level, "name": name})
        if self.skill_rarities.get(resolved, 0) == 2:
            bundled_skill_ids = self._unowned_white_tiers(group_id, owned_skill_ids)
            for bundled_id in bundled_skill_ids:
                cost += self._estimate_cost({
                    "skill_id": bundled_id,
                    "hint_level": 0,
                    "name": self.skill_names.get(bundled_id, ""),
                })

        row["priority"] = best_priority
        row["cost"] = cost
        row["bundled_skill_ids"] = bundled_skill_ids
        row["resolution_reason"] = reason
        row["master_exists"] = resolved in self.skill_id_exists
        if resolved in failed:
            row["failed_scope"] = "this_turn"

        return row

    def _buy_batch(self, client, state, candidates, turn):
        if not candidates:
            return state, 0

        data = state.get("data") or {}
        chara = data.get("chara_info") or data.get("single_mode_chara_light") or {}
        current_turn = int(chara.get("turn") or 0)

        if current_turn != turn:
            self.last_result = {"skip": "stale_turn_detected", "request_current_turn": turn, "source_state_turn": current_turn}
            return state, 0

        valid_tips = set()
        for tip in chara.get("skill_tips_array") or []:
            group_id = int(tip.get("group_id") or 0)
            valid_tips.update(self.group_to_skill_ids.get(group_id, []))

        points = int(chara.get("skill_point") or 0)
        selected_total_cost = 0
        valid_candidates = []

        for item in candidates:
            skill_id = item["skill_id"]
            cost = int(item.get("cost") or 0)
            if skill_id <= 0 or item.get("skip_reason"):
                item["preflight_error"] = "invalid_skill"
                continue
            if skill_id not in valid_tips:
                item["preflight_error"] = "not_in_tips"
                continue
            if selected_total_cost + cost > points:
                item["preflight_error"] = "unaffordable"
                continue
            item["preflight_passed"] = True
            selected_total_cost += cost
            valid_candidates.append(item)

        if not valid_candidates:
            self.last_result = {"skip": "preflight_failed", "turn": turn, "points": points}
            return state, 0

        payload = []
        payload_ids = set()
        for item in valid_candidates:
            for skill_id in [item["skill_id"], *(item.get("bundled_skill_ids") or [])]:
                skill_id = int(skill_id or 0)
                if skill_id > 0 and skill_id not in payload_ids:
                    payload.append({"skill_id": skill_id, "level": 1})
                    payload_ids.add(skill_id)
        self.last_attempt = [dict(item) for item in valid_candidates]
        event = {
            "turn": turn,
            "selected": [dict(item) for item in candidates],
            "attempt": [dict(item) for item in valid_candidates],
            "payload": payload,
            "result": {},
        }
        self.attempt_events.append(event)

        try:
            result = client.gain_skills(payload, turn)
            self.last_result = {"result": "ok", "turn": turn, "count": len(valid_candidates), "payload": payload}
            event["result"] = self.last_result
            self._failed_for_turn(turn).clear()
            return self._merge_state(state, result), len(valid_candidates)
        except Exception as exc:
            print(f"Skill Purchase Error at turn {turn}: {exc}")
            if any(code in str(exc) for code in ("201", "205", "208")):
                self.recover_after_error = True
            self._failed_for_turn(turn).update(int(item["skill_id"]) for item in valid_candidates)
            self.last_result = {"result": "failed", "turn": turn, "error": str(exc), "payload": payload}
            event["result"] = self.last_result
            return state, 0


    def _merge_state(self, state, res):
        if res and isinstance(res, dict) and "data" in res:
            if not state: state = {}
            if "data" not in state: state["data"] = {}
            for k, v in res["data"].items():
                if isinstance(v, dict) and isinstance(state["data"].get(k), dict):
                    state["data"][k].update(v)
                else:
                    state["data"][k] = v
        return state

    def _select_skill_id(self, group_id, owned, rarity=0):
        owned_groups = {self.skill_to_group_id.get(sid, sid // 10) for sid in owned}
        resolved = self.resolve_skill_tip({"group_id": group_id, "rarity": rarity, "level": 0}, set(owned), owned_groups, {})
        return int((resolved or {}).get("resolved_skill_id") or 0)


    def _estimate_cost(self, candidate):
        name = candidate.get("name") or ""
        skill_id = candidate.get("skill_id") or 0
        level = candidate.get("hint_level") or 0
        is_circle = any(m in name for m in [MARK_WHITE_CIRCLE, MARK_LARGE_CIRCLE, MOJI_WHITE_CIRCLE, MOJI_LARGE_CIRCLE])
        if is_circle:
            base = 130
        elif skill_id >= 900000:
            base = 200
        else:
            base = self.skill_costs.get(skill_id)
            if not base:
                base = 200 if self.skill_rarities.get(skill_id, 0) >= 2 else 160
        return max(1, int(base * (100 - min(level, 5) * 10) / 100))
