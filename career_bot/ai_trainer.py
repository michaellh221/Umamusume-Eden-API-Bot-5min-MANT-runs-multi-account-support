"""
career_bot/ai_trainer.py
========================
Background training: reads JSONL datasets, builds lookup tables and scoring
adjustments, writes model files to  uma_runtime/ai/.

Entry points
------------
after_career_export(output_dir, manifest, build_version)
    Called by ai_dataset.export_report_ai_datasets() after each export.
    Triggers a background train_once() when auto-training is enabled and
    enough new records have accumulated.

train_once(ai_root)
    Blocking: reads JSONL, builds all tables, writes all model files.

start_background_trainer(output_dir)
    Starts a daemon thread that calls train_once() after each career.

trainer_status(output_dir)  →  dict
    Returns auto-training state and latest training run info.

load_auto_config(ai_root) / save_auto_config(ai_root, config)
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from career_bot.ai_dataset import (
    _read_jsonl, _atomic_write_json, now_iso,
    rebuild_advisor_stats, safe_float, safe_int,
    ai_root_from_output_dir, runtime_output_root,
    DATASET_FILES,
)
from career_bot.ai_modeling import (
    BetaPosterior, HierarchicalLevel,
    hierarchical_posterior, score_program, global_base_rate,
)

# ── constants ──────────────────────────────────────────────────────────────────

DEFAULT_AUTO_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "min_new_careers_per_run": 1,
    "min_total_records": 5,
}

# ── path helpers ───────────────────────────────────────────────────────────────

def _ai_root(output_dir: Any) -> Path:
    return ai_root_from_output_dir(output_dir)


def _state_path(ai_root: Path) -> Path:
    return ai_root / "auto_training_state.json"


def _config_path(ai_root: Path) -> Path:
    return ai_root / "auto_training_config.json"


# ── config I/O ─────────────────────────────────────────────────────────────────

def load_auto_config(ai_root: Path) -> Dict[str, Any]:
    path = _config_path(ai_root)
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_AUTO_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_AUTO_CONFIG)


def save_auto_config(ai_root: Path, config: Dict[str, Any]) -> None:
    merged = dict(DEFAULT_AUTO_CONFIG)
    merged.update(config)
    _atomic_write_json(_config_path(ai_root), merged)


def _load_state(ai_root: Path) -> Dict[str, Any]:
    path = _state_path(ai_root)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"careers_since_last_train": 0, "last_trained_at": None, "total_trains": 0}


def _save_state(ai_root: Path, state: Dict[str, Any]) -> None:
    _atomic_write_json(_state_path(ai_root), state)


# ── table builders ─────────────────────────────────────────────────────────────

def _build_race_outcome_table(turn_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate race outcomes by program_id."""
    programs: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "starts": 0, "wins": 0, "total_reward": 0.0,
        "total_rank": 0.0, "rank_count": 0,
    })
    for row in turn_rows:
        action = row.get("action") or {}
        if str(action.get("type")) != "race":
            continue
        pid = action.get("program_id")
        if not pid:
            continue
        key = str(pid)
        outcome = row.get("outcome") or {}
        race_result = outcome.get("race_result") or {}
        rank = safe_int(race_result.get("rank"), 99) if race_result else 99
        reward = safe_float(outcome.get("reward"))
        p = programs[key]
        p["starts"]       += 1
        p["wins"]         += 1 if rank == 1 else 0
        p["total_reward"] += reward
        if race_result and rank < 99:
            p["total_rank"]  += rank
            p["rank_count"]  += 1
    table: Dict[str, Any] = {}
    for pid, p in programs.items():
        s = p["starts"]
        rc = p["rank_count"]
        table[pid] = {
            "starts":     s,
            "wins":       p["wins"],
            "win_rate":   round(p["wins"] / s, 4) if s else 0.0,
            "avg_reward": round(p["total_reward"] / s, 4) if s else 0.0,
            "avg_rank":   round(p["total_rank"] / rc, 2) if rc else None,
        }
    return table


def _build_event_outcome_table(event_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate event outcomes by (event_name, choice)."""
    buckets: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "total_reward": 0.0, "deltas": defaultdict(float)})
    for row in event_rows:
        key = f"{row.get('event_name', '')}::{row.get('choice', '')}"
        b = buckets[key]
        b["count"]        += 1
        b["total_reward"] += safe_float(row.get("reward"))
        for stat, val in (row.get("stat_deltas") or {}).items():
            b["deltas"][stat] += safe_float(val)
    table: Dict[str, Any] = {}
    for key, b in buckets.items():
        c = b["count"]
        table[key] = {
            "count":          c,
            "avg_reward":     round(b["total_reward"] / c, 4) if c else 0.0,
            "avg_stat_deltas": {k: round(v / c, 2) for k, v in b["deltas"].items()},
        }
    return table


def _build_item_effectiveness_table(turn_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate item usage rewards by item_id / item type."""
    buckets: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "total_reward": 0.0})
    for row in turn_rows:
        meta = row.get("turn_metadata") or {}
        items_used = meta.get("item_usage_attempts") or []
        reward = safe_float((row.get("outcome") or {}).get("reward"))
        for item in items_used:
            if isinstance(item, dict):
                key = str(item.get("item_id") or item.get("item_type") or "unknown")
                buckets[key]["count"]        += 1
                buckets[key]["total_reward"] += reward
    table: Dict[str, Any] = {}
    for key, b in buckets.items():
        c = b["count"]
        table[key] = {
            "count":      c,
            "avg_reward": round(b["total_reward"] / c, 4) if c else 0.0,
        }
    return table


# ── Bayesian policy adjustments ────────────────────────────────────────────────

def _build_policy_adjustments(race_outcome_table: Dict[str, Any]) -> Dict[str, Any]:
    """Score every race program with the hierarchical Bayesian model."""
    base_rate = global_base_rate(race_outcome_table)
    adjustments: Dict[str, Any] = {}
    for pid, bucket in race_outcome_table.items():
        starts = safe_int(bucket.get("starts"))
        if starts < 1:
            continue
        level = HierarchicalLevel(name="program", key=pid, stats=bucket)
        posterior, sources = hierarchical_posterior(
            [level],
            prior_mean=base_rate,
            prior_strength=4.0,
        )
        avg_reward = safe_float(bucket.get("avg_reward"))
        adjustments[pid] = {
            "program_id":   pid,
            "starts":       starts,
            "sources":      sources,
            **score_program(posterior, avg_reward, risk_quantile=0.25),
        }
    return adjustments


def _build_suggested_config(
    race_table: Dict[str, Any],
    policy_adj: Dict[str, Any],
) -> Dict[str, Any]:
    """Suggest simple config tweaks based on observed data."""
    suggestions: List[str] = []

    # Find top-3 performing programs
    sorted_progs = sorted(
        [(pid, d.get("adjustment", 0.0)) for pid, d in policy_adj.items()],
        key=lambda x: x[1], reverse=True
    )
    top = sorted_progs[:3]
    if top:
        suggestions.append(
            f"Top programs by Bayesian score: {', '.join(pid for pid, _ in top)}"
        )

    # Programs with high win rate but few starts (high potential)
    high_potential = [
        pid for pid, b in race_table.items()
        if safe_int(b.get("starts")) >= 3
        and safe_float(b.get("win_rate")) >= 0.5
    ]
    if high_potential:
        suggestions.append(
            f"Programs with ≥50%% win rate (run more to confirm): {', '.join(high_potential[:5])}"
        )

    return {"built_at": now_iso(), "suggestions": suggestions}


# ── dashboard ──────────────────────────────────────────────────────────────────

def _build_dashboard(
    race_table: Dict[str, Any],
    event_table: Dict[str, Any],
    item_table: Dict[str, Any],
    policy_adj: Dict[str, Any],
    summary_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    total_careers = len(summary_rows)
    completed = sum(1 for r in summary_rows if r.get("career_completed"))
    total_races = sum(safe_int(b.get("starts")) for b in race_table.values())
    total_wins  = sum(safe_int(b.get("wins")) for b in race_table.values())

    avg_fans = 0.0
    if summary_rows:
        fans_vals = [safe_int(r.get("final_fans")) for r in summary_rows if r.get("final_fans")]
        avg_fans = sum(fans_vals) / len(fans_vals) if fans_vals else 0.0

    top_program = max(policy_adj.items(), key=lambda x: x[1].get("adjustment", 0), default=(None, {}))

    return {
        "built_at":       now_iso(),
        "total_careers":  total_careers,
        "completed_careers": completed,
        "completion_rate": round(completed / total_careers, 4) if total_careers else 0.0,
        "total_races":    total_races,
        "overall_win_rate": round(total_wins / total_races, 4) if total_races else 0.0,
        "avg_final_fans": round(avg_fans),
        "race_programs":  len(race_table),
        "event_choices":  len(event_table),
        "top_program": {
            "id":         top_program[0],
            "adjustment": top_program[1].get("adjustment"),
        } if top_program[0] else None,
    }


# ── train_once ─────────────────────────────────────────────────────────────────

def train_once(ai_root: Path) -> Dict[str, Any]:
    """Build all model files from the current JSONL datasets."""
    started_at = time.time()

    turn_rows    = _read_jsonl(ai_root / DATASET_FILES["turn_decisions"])
    event_rows   = _read_jsonl(ai_root / DATASET_FILES["event_outcomes"])
    summary_rows = _read_jsonl(ai_root / DATASET_FILES["career_summaries"])

    race_table   = _build_race_outcome_table(turn_rows)
    event_table  = _build_event_outcome_table(event_rows)
    item_table   = _build_item_effectiveness_table(turn_rows)
    policy_adj   = _build_policy_adjustments(race_table)
    suggested    = _build_suggested_config(race_table, policy_adj)
    dashboard    = _build_dashboard(race_table, event_table, item_table, policy_adj, summary_rows)
    advisor      = rebuild_advisor_stats(ai_root)

    ai_root.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(ai_root / "race_outcome_table.json",     race_table)
    _atomic_write_json(ai_root / "event_outcome_table.json",    event_table)
    _atomic_write_json(ai_root / "item_effectiveness_table.json", item_table)
    _atomic_write_json(ai_root / "policy_adjustments.json",     policy_adj)
    _atomic_write_json(ai_root / "suggested_config_tuning.json", suggested)
    _atomic_write_json(ai_root / "ai_dashboard.json",           dashboard)

    elapsed = round(time.time() - started_at, 2)
    run_info = {
        "trained_at":    now_iso(),
        "elapsed_sec":   elapsed,
        "records": {
            "turn_decisions":   len(turn_rows),
            "event_outcomes":   len(event_rows),
            "career_summaries": len(summary_rows),
        },
        "built": {
            "race_programs":    len(race_table),
            "event_choices":    len(event_table),
            "item_types":       len(item_table),
            "policy_programs":  len(policy_adj),
        },
    }
    _atomic_write_json(ai_root / "latest_training_run.json", run_info)
    return run_info


# ── background thread ──────────────────────────────────────────────────────────

_trainer_lock = threading.Lock()
_trainer_active = False


def after_career_export(
    output_dir: Any,
    manifest: Mapping[str, Any],
    build_version: str = "",
) -> None:
    """Hook called by ai_dataset after each export. Triggers background training."""
    global _trainer_active

    try:
        root = _ai_root(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        cfg = load_auto_config(root)
        if not cfg.get("enabled", True):
            return

        state = _load_state(root)
        state["careers_since_last_train"] = state.get("careers_since_last_train", 0) + 1
        _save_state(root, state)

        min_new = safe_int(cfg.get("min_new_careers_per_run"), 1)
        if state["careers_since_last_train"] < min_new:
            return

        with _trainer_lock:
            if _trainer_active:
                return
            _trainer_active = True

        def _run():
            global _trainer_active
            try:
                run_info = train_once(root)
                state2 = _load_state(root)
                state2["careers_since_last_train"] = 0
                state2["last_trained_at"] = now_iso()
                state2["total_trains"] = state2.get("total_trains", 0) + 1
                state2["last_run_info"] = run_info
                _save_state(root, state2)
            except Exception:
                pass
            finally:
                global _trainer_active
                _trainer_active = False

        t = threading.Thread(target=_run, daemon=True, name="ai-trainer")
        t.start()

    except Exception:
        pass


def start_background_trainer(output_dir: Any) -> None:
    """Stub kept for explicit initialisation calls; actual training fires via after_career_export."""
    pass


def trainer_status(output_dir: Any) -> Dict[str, Any]:
    root = _ai_root(output_dir)
    state  = _load_state(root)
    config = load_auto_config(root)

    run_info_path = root / "latest_training_run.json"
    run_info = {}
    if run_info_path.exists():
        try:
            run_info = json.loads(run_info_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "auto_training_enabled":       config.get("enabled", True),
        "active":                      _trainer_active,
        "careers_since_last_train":    state.get("careers_since_last_train", 0),
        "last_trained_at":             state.get("last_trained_at"),
        "total_trains":                state.get("total_trains", 0),
        "min_new_careers_per_run":     config.get("min_new_careers_per_run", 1),
        "latest_training_run":         run_info,
    }


def latest_dashboard(output_dir: Any) -> Dict[str, Any]:
    root = _ai_root(output_dir)
    path = root / "ai_dashboard.json"
    if not path.exists():
        return {"available": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["available"] = True
        return data
    except Exception:
        return {"available": False}
