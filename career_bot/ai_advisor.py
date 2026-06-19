"""
career_bot/ai_advisor.py
========================
Reads the pre-built model files from  uma_runtime/ai/  and produces
human-readable hints for the UI.

Functions
---------
race_program_hint(program_id, output_dir)  →  dict
    Bayesian score and suggestion for a single race program.

hierarchical_race_program_hint(program_id, scenario_id, preset_name, output_dir)  →  dict
    Same but pulls in context-specific buckets (program × scenario × preset).

post_run_advice(output_dir)  →  dict
    High-level tips derived from the latest dashboard and training run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from career_bot.ai_dataset import (
    safe_int, safe_float, now_iso, ai_root_from_output_dir
)
from career_bot.ai_modeling import (
    BetaPosterior, HierarchicalLevel,
    hierarchical_posterior, score_program, global_base_rate,
)

# ── I/O helpers ────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ai_root(output_dir: Any) -> Path:
    return ai_root_from_output_dir(output_dir)


# ── public API ─────────────────────────────────────────────────────────────────

def race_program_hint(
    program_id: Any,
    output_dir: Any,
) -> Dict[str, Any]:
    """Return a Bayesian score hint for a single race program."""
    root = _ai_root(output_dir)
    pid  = str(program_id)

    policy = _load_json(root / "policy_adjustments.json") or {}
    race_table = _load_json(root / "race_outcome_table.json") or {}

    if pid in policy:
        entry = policy[pid]
        bucket = race_table.get(pid, {})
        return {
            "program_id":   pid,
            "adjustment":   entry.get("adjustment"),
            "lcb":          entry.get("lcb"),
            "ucb":          entry.get("ucb"),
            "mean":         entry.get("mean"),
            "starts":       entry.get("starts"),
            "win_rate":     bucket.get("win_rate"),
            "avg_reward":   bucket.get("avg_reward"),
            "available":    True,
            "sources":      entry.get("sources", []),
        }

    if pid in race_table:
        bucket = race_table[pid]
        starts = safe_int(bucket.get("starts"))
        if starts > 0:
            base_rate = global_base_rate(race_table)
            level = HierarchicalLevel(name="program", key=pid, stats=bucket)
            posterior, sources = hierarchical_posterior(
                [level], prior_mean=base_rate, prior_strength=4.0
            )
            avg_reward = safe_float(bucket.get("avg_reward"))
            scored = score_program(posterior, avg_reward)
            return {
                "program_id": pid,
                **scored,
                "starts":     starts,
                "win_rate":   bucket.get("win_rate"),
                "avg_reward": avg_reward,
                "available":  True,
                "sources":    sources,
            }

    return {
        "program_id": pid,
        "available":  False,
        "detail":     "No data yet for this race program.",
    }


def hierarchical_race_program_hint(
    program_id: Any,
    scenario_id: Optional[int],
    preset_name: Optional[str],
    output_dir: Any,
) -> Dict[str, Any]:
    """Bayesian hint using all available context levels."""
    root = _ai_root(output_dir)
    pid  = str(program_id)

    advisor = _load_json(root / "advisor_stats.json") or {}
    race_table = _load_json(root / "race_outcome_table.json") or {}

    ctx = advisor.get("race_programs_context", {})
    by_program = ctx.get("by_program", {})

    global_bucket = race_table.get(pid)
    prog_bucket   = by_program.get(pid)

    levels = []
    if global_bucket:
        levels.append(HierarchicalLevel(name="global_program",  key=pid, stats=global_bucket))
    if prog_bucket:
        levels.append(HierarchicalLevel(name="context_program", key=pid, stats=prog_bucket))

    if not levels:
        return {
            "program_id": pid,
            "available":  False,
            "detail":     "No data yet for this race program.",
        }

    base_rate = global_base_rate(race_table) if race_table else 0.5
    posterior, sources = hierarchical_posterior(levels, prior_mean=base_rate)
    avg_reward = safe_float((global_bucket or {}).get("avg_reward"))
    scored = score_program(posterior, avg_reward)

    return {
        "program_id":  pid,
        "scenario_id": scenario_id,
        "preset_name": preset_name,
        "available":   True,
        "sources":     sources,
        **scored,
        "starts":      safe_int((global_bucket or {}).get("starts")),
        "win_rate":    (global_bucket or {}).get("win_rate"),
    }


def all_race_program_hints(output_dir: Any) -> List[Dict[str, Any]]:
    """Return hints for every known race program, sorted by adjustment desc."""
    root = _ai_root(output_dir)
    policy = _load_json(root / "policy_adjustments.json") or {}
    race_table = _load_json(root / "race_outcome_table.json") or {}

    results = []
    for pid, entry in policy.items():
        bucket = race_table.get(pid, {})
        results.append({
            "program_id": pid,
            "adjustment": entry.get("adjustment"),
            "lcb":        entry.get("lcb"),
            "ucb":        entry.get("ucb"),
            "mean":       entry.get("mean"),
            "starts":     entry.get("starts"),
            "win_rate":   bucket.get("win_rate"),
            "avg_reward": bucket.get("avg_reward"),
        })

    results.sort(key=lambda x: (x.get("adjustment") or 0.0), reverse=True)
    return results


def post_run_advice(output_dir: Any) -> Dict[str, Any]:
    """High-level tips from the latest dashboard."""
    root = _ai_root(output_dir)

    dashboard = _load_json(root / "ai_dashboard.json")
    suggested  = _load_json(root / "suggested_config_tuning.json")
    run_info   = _load_json(root / "latest_training_run.json")

    if not dashboard:
        return {
            "available": False,
            "detail":    "No training data yet — run a few careers first.",
        }

    tips = []

    wr = dashboard.get("overall_win_rate", 0.0)
    if wr < 0.25 and safe_int(dashboard.get("total_races")) >= 20:
        tips.append("Win rate is below 25% — consider adding high-scoring races to extra_race_list.")

    completion = dashboard.get("completion_rate", 0.0)
    if completion < 0.8 and safe_int(dashboard.get("total_careers")) >= 5:
        tips.append("More than 20% of careers aren't completing — check give-up triggers and TP recovery thresholds.")

    top = dashboard.get("top_program")
    if top and top.get("id"):
        tips.append(f"Best-performing race program so far: {top['id']}.")

    for s in (suggested or {}).get("suggestions", []):
        tips.append(s)

    return {
        "available":        True,
        "queried_at":       now_iso(),
        "total_careers":    dashboard.get("total_careers"),
        "completion_rate":  dashboard.get("completion_rate"),
        "overall_win_rate": dashboard.get("overall_win_rate"),
        "avg_final_fans":   dashboard.get("avg_final_fans"),
        "top_program":      top,
        "tips":             tips,
        "latest_training_run": run_info,
    }
