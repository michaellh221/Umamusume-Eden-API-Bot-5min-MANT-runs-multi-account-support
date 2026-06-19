"""
career_bot/race_solver.py
=========================
Race schedule solver for Sweepy改二.

Given a preset's aptitudes + constraints, plans which turns to race across
the 72-turn career using either:
  • MILP (scipy.optimize.milp)  — exact global optimum, preferred
  • Beam search                 — dependency-free heuristic fallback

Entry points
------------
solve(base_dir, preset, chara_info=None)  →  plan dict
    Build a full race schedule. Returns a plan with `decisions` (turn→action)
    and `extra_race_list` (list of program_ids in turn order).

solver_status(base_dir)  →  dict
    Whether the last solve succeeded, which backend was used, etc.

load_plan(base_dir, preset_name)  →  plan dict | None
plan_for_preset(plan, current_turn)  →  list[int]
    Filter the plan to races on or after current_turn, as program_ids.

The plan is saved per-preset under  uma_runtime/solver/<preset_name>.json
so it survives between bot restarts and can be previewed in the UI.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Constants ──────────────────────────────────────────────────────────────

SUMMER_TURNS = {37, 38, 39, 40, 61, 62, 63, 64}

# Grade score table — inferred from race name keywords present in race_map.json.
# Keys are substrings matched case-insensitively against race names.
GRADE_KEYWORDS: List[Tuple[str, str]] = [
    # G1 flagship races (highest fan reward)
    ("Japan Cup", "G1"), ("Tenno Sho", "G1"), ("Arima Kinen", "G1"),
    ("Takarazuka", "G1"), ("Yasuda Kinen", "G1"), ("Victoria Mile", "G1"),
    ("Osaka Hai", "G1"), ("Oka Sho", "G1"), ("Japanese Derby", "G1"),
    ("Tokyo Yushun", "G1"), ("Kikka Sho", "G1"), ("Autumn Tenno", "G1"),
    ("Spring Tenno", "G1"), ("Satsuki Sho", "G1"), ("Sprinters", "G1"),
    ("Mile Championship", "G1"), ("Champions Cup", "G1"), ("February Stakes", "G1"),
    ("Hanshin Juvenile", "G1"), ("Asahi Hai", "G1"), ("NHK Mile", "G1"),
    ("Oaks", "G1"), ("Dirt", "G1"),
    # G2 races
    ("Yomiuri Shimbun Hai", "G2"), ("Kyoto Kinen", "G2"), ("Nikko Sho", "G2"),
    ("CBC Sho", "G2"), ("Sapporo", "G2"), ("Hakodate", "G2"),
    ("Rose Stakes", "G2"), ("Radio Nikkei", "G2"), ("Fuji Stakes", "G2"),
    ("Keio Hai", "G2"), ("Kivamura", "G2"),
    # Stakes / OP are generic
    ("Stakes", "OP"), ("Sho", "OP"), ("Kinen", "G2"),
]

GRADE_BASE_SCORE: Dict[str, float] = {
    "G1":     10.0,
    "G2":     6.0,
    "G3":     4.0,
    "OP":     2.0,
    "PRE-OP": 1.0,
}

# Aptitude value thresholds (game uses 1–8, where 8=S, 7=A, 6=B …)
APT_MIN_DEFAULT = 6  # B rank — minimum to consider a race


# ── Path helpers ───────────────────────────────────────────────────────────

def _solver_dir(base_dir: Any) -> Path:
    path = Path(base_dir)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate / "uma_runtime" / "solver"
    return path.parent / "uma_runtime" / "solver"


def _plan_path(base_dir: Any, preset_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(preset_name or "default"))
    return _solver_dir(base_dir) / f"{safe}.json"


def _status_path(base_dir: Any) -> Path:
    return _solver_dir(base_dir) / "solver_status.json"


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
        Path(tmp).replace(path)
    finally:
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass


# ── Race data helpers ──────────────────────────────────────────────────────

def _load_race_map(base_dir: Any) -> dict:
    path = Path(base_dir) / "data" / "race_map.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _infer_grade(name: str) -> str:
    """Guess grade from race name keywords."""
    low = name.lower()
    # Explicit grade markers in name
    if " g1" in low or "(g1)" in low:
        return "G1"
    if " g2" in low or "(g2)" in low:
        return "G2"
    if " g3" in low or "(g3)" in low:
        return "G3"
    for keyword, grade in GRADE_KEYWORDS:
        if keyword.lower() in low:
            return grade
    return "OP"


def _race_candidates(base_dir: Any, preset: dict, chara: dict) -> List[dict]:
    """
    Build a list of candidate race slots from race_map.json.

    Returns list of dicts with keys:
        turn, program_id, name, grade, ground, distance, score (base only)
    """
    race_map = _load_race_map(base_dir)
    meta = race_map.get("meta") or {}
    program = race_map.get("program") or {}

    apt_floor = int((preset.get("solver_apt_floor") or APT_MIN_DEFAULT))
    include_op = bool(preset.get("solver_include_op") or False)
    allow_summer = bool(preset.get("solver_allow_summer") or False)
    target_distance = int(preset.get("target_distance") or 0)

    # Character aptitudes (1–8 scale, 8=S, 7=A …)
    def apt(key: str) -> int:
        try:
            return int(chara.get(key) or 1)
        except (TypeError, ValueError):
            return 1

    ground_apt = {1: apt("proper_ground_turf"), 2: apt("proper_ground_dirt")}
    dist_apt = {
        "short":  apt("proper_distance_short"),
        "mile":   apt("proper_distance_mile"),
        "middle": apt("proper_distance_middle"),
        "long":   apt("proper_distance_long"),
    }

    def dist_key(meters: int) -> str:
        if meters <= 1400:
            return "short"
        if meters <= 1800:
            return "mile"
        if meters <= 2400:
            return "middle"
        return "long"

    candidates = []
    seen = set()

    for meta_id_str, meta_entry in meta.items():
        pid = int(meta_entry.get("program_id") or 0)
        turn = int(meta_entry.get("turn") or 0)
        if not pid or not turn or turn < 1 or turn > 72:
            continue
        if (turn, pid) in seen:
            continue
        seen.add((turn, pid))

        prog = program.get(str(pid)) or program.get(pid) or {}
        name = meta_entry.get("name") or prog.get("name") or f"Race {pid}"
        ground = int(prog.get("ground") or 1)
        distance = int(prog.get("distance") or 1600)
        dk = dist_key(distance)

        # Aptitude gate
        if ground_apt.get(ground, 1) < apt_floor:
            continue
        if dist_apt.get(dk, 1) < apt_floor:
            continue

        grade = _infer_grade(name)
        if grade == "OP" and not include_op:
            continue

        # Base score
        base = GRADE_BASE_SCORE.get(grade, 2.0)

        # Distance preference bonus
        dist_bonus = 0.0
        if target_distance:
            preferred_dk = dist_key(target_distance)
            if dk == preferred_dk:
                dist_bonus = 3.0
            elif abs(distance - target_distance) <= 400:
                dist_bonus = 1.0

        # Aptitude bonus (better apt = more confident win = more value)
        apt_g = ground_apt.get(ground, 1)
        apt_d = dist_apt.get(dk, 1)
        apt_bonus = (apt_g + apt_d - apt_floor * 2) * 0.5

        score = base + dist_bonus + apt_bonus

        if not allow_summer and turn in SUMMER_TURNS:
            score -= 5.0  # heavy penalty (usually net negative)

        candidates.append({
            "turn": turn,
            "program_id": pid,
            "name": name,
            "grade": grade,
            "ground": ground,
            "distance": distance,
            "distance_key": dk,
            "score": score,
        })

    return candidates


# ── Solver backends ────────────────────────────────────────────────────────

def _scipy_milp_available() -> bool:
    try:
        from scipy.optimize import milp, LinearConstraint, Bounds  # noqa: F401
        import scipy.sparse  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def _solve_milp(
    candidates: List[dict],
    manual_locks: dict,
    max_streak: int,
    start_turn: int,
    allow_summer: bool,
    timeout: float = 30.0,
) -> List[dict]:
    """Exact MILP backend via scipy.optimize.milp."""
    import numpy as np
    from scipy.optimize import milp, LinearConstraint, Bounds
    from scipy.sparse import lil_matrix

    n = len(candidates)
    c = np.array([-float(r.get("score") or 0) for r in candidates], dtype=float)
    integrality = np.ones(n, dtype=int)
    bounds = Bounds(np.zeros(n), np.ones(n))

    rows_by_turn: Dict[int, List[int]] = defaultdict(list)
    for idx, row in enumerate(candidates):
        rows_by_turn[int(row["turn"])].append(idx)

    constraints = []
    lbs: List[float] = []
    ubs: List[float] = []

    # At most one race per turn
    for turn, idxs in rows_by_turn.items():
        if len(idxs) > 1:
            constraints.append(idxs)
            lbs.append(0)
            ubs.append(1)

    # Manual locks: turn → program_id or "train"
    for turn_str, lock in manual_locks.items():
        turn = int(turn_str)
        if turn < start_turn or turn > 72:
            continue
        idxs = rows_by_turn.get(turn, [])
        if lock == "train":
            # Force zero races this turn
            if idxs:
                constraints.append(idxs)
                lbs.append(0)
                ubs.append(0)
        elif str(lock).isdigit():
            pid = int(lock)
            matched = [i for i in idxs if int(candidates[i]["program_id"]) == pid]
            if matched:
                constraints.append(matched)
                lbs.append(1)
                ubs.append(1)

    # Max consecutive race streak
    window = max_streak + 1
    for turn in range(start_turn, 73 - window + 2):
        idxs = []
        for t in range(turn, turn + window):
            idxs.extend(rows_by_turn.get(t, []))
        if len(idxs) > max_streak:
            constraints.append(idxs)
            lbs.append(0)
            ubs.append(max_streak)

    mat = lil_matrix((len(constraints), n), dtype=float)
    for r_idx, idxs in enumerate(constraints):
        for c_idx in idxs:
            mat[r_idx, c_idx] = 1.0

    linear = LinearConstraint(mat.tocsr(), np.array(lbs, dtype=float), np.array(ubs, dtype=float))
    options = {"time_limit": max(1.0, float(timeout)), "disp": False}
    result = milp(c=c, integrality=integrality, bounds=bounds, constraints=linear, options=options)

    if not result.success or result.x is None:
        raise RuntimeError(f"MILP infeasible: {getattr(result, 'message', 'unknown')}")

    picked = []
    for idx, value in enumerate(result.x):
        if value >= 0.5:
            picked.append(dict(candidates[idx]))
    picked.sort(key=lambda r: int(r["turn"]))
    return picked


def _solve_beam(
    candidates: List[dict],
    manual_locks: dict,
    max_streak: int,
    start_turn: int,
    allow_summer: bool,
    beam_width: int = 32,
) -> List[dict]:
    """Greedy beam-search fallback (no external dependencies)."""
    # Group candidates by turn
    by_turn: Dict[int, List[dict]] = defaultdict(list)
    for row in candidates:
        by_turn[int(row["turn"])].append(row)

    # Build forced locks
    forced_race: Dict[int, int] = {}
    forced_train: set = set()
    for turn_str, lock in manual_locks.items():
        turn = int(turn_str)
        if lock == "train":
            forced_train.add(turn)
        elif str(lock).isdigit():
            forced_race[turn] = int(lock)

    # State: (score_sum, streak, scheduled_races)
    # Represent as beam of (neg_score, streak, list_of_rows)
    beam = [(0.0, 0, [])]

    all_turns = sorted(set(by_turn.keys()) | set(forced_race.keys()) | set(forced_train))
    all_turns = [t for t in all_turns if t >= start_turn]

    for turn in all_turns:
        new_beam = []
        for total_score, streak, history in beam:
            # Forced train
            if turn in forced_train:
                new_beam.append((total_score, 0, history))
                continue
            # Forced race
            if turn in forced_race:
                pid = forced_race[turn]
                matched = [r for r in by_turn.get(turn, []) if int(r["program_id"]) == pid]
                if matched:
                    r = matched[0]
                    new_streak = streak + 1
                    if new_streak <= max_streak:
                        new_beam.append((total_score + r["score"], new_streak, history + [r]))
                        continue
                new_beam.append((total_score, 0, history))
                continue

            # Train option
            new_beam.append((total_score, 0, history))

            # Race options
            if streak < max_streak:
                options = by_turn.get(turn, [])
                best = sorted(options, key=lambda r: r["score"], reverse=True)[:3]
                for r in best:
                    new_beam.append((total_score + r["score"], streak + 1, history + [r]))

        # Keep top beam_width states
        new_beam.sort(key=lambda x: -x[0])
        beam = new_beam[:beam_width]

    if not beam:
        return []
    _, _, picked = beam[0]
    picked.sort(key=lambda r: int(r["turn"]))
    return picked


# ── Public API ─────────────────────────────────────────────────────────────

def solve(
    base_dir: Any,
    preset: dict,
    chara_info: Optional[dict] = None,
    current_turn: int = 1,
    timeout: float = 30.0,
) -> dict:
    """
    Build and save a race schedule for the given preset.

    Parameters
    ----------
    base_dir     : project root directory
    preset       : hydrated preset dict
    chara_info   : live chara_info from game state (for aptitudes); if None,
                   assumes max aptitude (all races eligible)
    current_turn : plan from this turn forward (default 1 = full career)
    timeout      : MILP solver timeout in seconds

    Returns
    -------
    dict with keys:
        success, backend, solver_mode, race_count, decisions, extra_race_list,
        schedule (list of race dicts), generated_at, notes
    """
    chara = chara_info or {}
    # If no chara info, assume best aptitude so all races pass the gate
    if not chara:
        chara = {
            "proper_ground_turf": 8, "proper_ground_dirt": 8,
            "proper_distance_short": 8, "proper_distance_mile": 8,
            "proper_distance_middle": 8, "proper_distance_long": 8,
        }

    max_streak = int(preset.get("solver_max_races_in_row") or 2)
    allow_summer = bool(preset.get("solver_allow_summer") or False)
    manual_locks = dict(preset.get("solver_manual_locks") or {})
    start_turn = max(1, int(current_turn or 1))

    candidates = _race_candidates(base_dir, preset, chara)
    # Filter to future turns
    candidates = [r for r in candidates if int(r["turn"]) >= start_turn]

    if not candidates:
        plan = _empty_plan(start_turn, "no eligible races")
        _save_plan(base_dir, preset.get("name", "default"), plan)
        return plan

    backend = "milp"
    notes = []
    try:
        if not _scipy_milp_available():
            raise RuntimeError("scipy MILP not available")
        picked = _solve_milp(candidates, manual_locks, max_streak, start_turn, allow_summer, timeout)
        notes.append("Exact MILP backend (scipy) used.")
    except Exception as exc:
        backend = "beam"
        notes.append(f"MILP unavailable ({exc}), using beam fallback.")
        try:
            picked = _solve_beam(candidates, manual_locks, max_streak, start_turn, allow_summer)
            notes.append("Beam heuristic backend used.")
        except Exception as exc2:
            plan = _empty_plan(start_turn, f"solver error: {exc2}")
            _save_plan(base_dir, preset.get("name", "default"), plan)
            return plan

    decisions: Dict[int, dict] = {t: {"type": "train"} for t in range(start_turn, 73)}
    for row in picked:
        t = int(row["turn"])
        decisions[t] = {
            "type": "race",
            "program_id": int(row["program_id"]),
            "name": row.get("name", ""),
            "grade": row.get("grade", ""),
            "score": round(float(row.get("score") or 0), 3),
        }

    extra_race_list = [int(r["program_id"]) for r in picked]

    plan = {
        "success": True,
        "backend": backend,
        "solver_mode": "auto",
        "generated_at": int(time.time()),
        "start_turn": start_turn,
        "race_count": len(picked),
        "extra_race_list": extra_race_list,
        "schedule": [
            {
                "turn": int(r["turn"]),
                "program_id": int(r["program_id"]),
                "name": r.get("name", ""),
                "grade": r.get("grade", ""),
                "distance": int(r.get("distance") or 0),
                "score": round(float(r.get("score") or 0), 3),
            }
            for r in picked
        ],
        "decisions": {str(k): v for k, v in decisions.items()},
        "notes": notes,
    }
    _save_plan(base_dir, preset.get("name", "default"), plan)
    _write_status(base_dir, plan)
    return plan


def _empty_plan(start_turn: int, reason: str) -> dict:
    return {
        "success": False,
        "backend": "none",
        "solver_mode": "auto",
        "generated_at": int(time.time()),
        "start_turn": start_turn,
        "race_count": 0,
        "extra_race_list": [],
        "schedule": [],
        "decisions": {},
        "notes": [reason],
    }


def _save_plan(base_dir: Any, preset_name: str, plan: dict) -> None:
    try:
        _atomic_write(_plan_path(base_dir, preset_name), plan)
    except Exception:
        pass


def _write_status(base_dir: Any, plan: dict) -> None:
    try:
        status = {
            "last_solve_at": int(time.time()),
            "success": plan.get("success", False),
            "backend": plan.get("backend", "none"),
            "race_count": plan.get("race_count", 0),
            "start_turn": plan.get("start_turn", 1),
            "notes": plan.get("notes", []),
        }
        _atomic_write(_status_path(base_dir), status)
    except Exception:
        pass


def load_plan(base_dir: Any, preset_name: str) -> Optional[dict]:
    """Load a previously-saved solver plan for a preset."""
    path = _plan_path(base_dir, preset_name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def plan_for_turn(plan: Optional[dict], current_turn: int) -> List[int]:
    """Return program_ids for races on or after current_turn, from saved plan."""
    if not plan or not plan.get("success"):
        return []
    decisions = plan.get("decisions") or {}
    result = []
    for turn_str, action in sorted(decisions.items(), key=lambda x: int(x[0])):
        turn = int(turn_str)
        if turn < current_turn:
            continue
        if action.get("type") == "race":
            pid = action.get("program_id")
            if pid:
                result.append(int(pid))
    return result


def solver_status(base_dir: Any) -> dict:
    """Return the last solver run status."""
    path = _status_path(base_dir)
    milp_ok = _scipy_milp_available()
    base = {
        "milp_available": milp_ok,
        "backend_label": "MILP (scipy)" if milp_ok else "Beam (fallback)",
    }
    if not path.exists():
        return {**base, "last_solve_at": None, "success": None, "race_count": 0, "notes": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {**base, **data}
    except Exception:
        return {**base, "last_solve_at": None, "success": None, "race_count": 0, "notes": []}


def plan_summary(plan: Optional[dict]) -> dict:
    """Compact summary of a plan for the UI."""
    if not plan:
        return {"available": False}
    return {
        "available": plan.get("success", False),
        "backend": plan.get("backend", "none"),
        "race_count": plan.get("race_count", 0),
        "start_turn": plan.get("start_turn", 1),
        "generated_at": plan.get("generated_at"),
        "notes": plan.get("notes", []),
        "schedule": plan.get("schedule", []),
    }
