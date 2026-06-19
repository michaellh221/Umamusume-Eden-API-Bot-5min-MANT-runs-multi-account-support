"""
backfill_fan_stats.py
Rebuilds fan_stats.json from all existing career log files.
Run once from the project root:
    python backfill_fan_stats.py
"""
import json
import glob
import sys
from pathlib import Path

BASE_DIR  = Path(__file__).parent.absolute()
LOG_DIR   = BASE_DIR / "uma_runtime" / "bot_logs"
OUT_FILE  = BASE_DIR / "fan_stats.json"


def fans_from_log(d):
    """
    Extract (initial_fans, final_fans, card_id) from a career log.

    Scans only the first 3 and last 5 turns for speed — the finish endpoint
    returns empty chara_info, so the last fans value is usually in race_out
    of the final race turn. Scanning all turns is ~10x slower and unnecessary.
    """
    turns = d.get("turns") or []
    if not turns:
        return 0, 0, ""

    def scan_turns(subset):
        result = []
        for t in subset:
            for c in t.get("api_calls") or []:
                if c.get("direction") not in ("RES", "RESP"):
                    continue
                ci = ((c.get("data") or {}).get("data") or {}).get("chara_info") or {}
                fans = ci.get("fans")
                if fans:
                    result.append((int(fans), str(ci.get("card_id") or "")))
        return result

    head = scan_turns(turns[:3])
    tail = scan_turns(turns[-5:])

    if not head and not tail:
        return 0, 0, ""

    initial_fans = head[0][0] if head else (tail[0][0] if tail else 0)
    final_fans, card_id = tail[-1] if tail else (head[-1] if head else (0, ""))
    return initial_fans, final_fans, card_id


def main():
    logs = sorted(glob.glob(str(LOG_DIR / "career_log_*.json")))
    print(f"Found {len(logs)} career logs in {LOG_DIR}")

    careers      = []
    total_gained = 0
    total_count  = 0  # all finished careers, regardless of fan data
    skipped      = 0

    # Build preset name → running_style map from saved presets
    import sys as _sys
    _sys.path.insert(0, str(BASE_DIR))
    try:
        from career_bot.presets import PresetStore
        _store = PresetStore(BASE_DIR)
        preset_style_map = {p.get("name", ""): int(p.get("running_style") or 0) for p in _store.read_all()}
    except Exception as _e:
        preset_style_map = {}
        print(f"  (preset lookup failed: {_e})")

    for i, path in enumerate(logs):
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(logs)}...", flush=True)
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            print(f"  SKIP {Path(path).name}: {e}")
            skipped += 1
            continue

        status = d.get("status", "")
        if status not in ("finished", "stopped", "error", "give_up"):
            skipped += 1
            continue

        initial_fans, final_fans, card_id = fans_from_log(d)
        fans_gained = max(0, final_fans - initial_fans)
        finished    = (status == "finished")
        started_at  = d.get("started_at", "")

        entry = {
            "card_id":     card_id,
            "preset":      d.get("preset_name", ""),
            "running_style": int((preset_style_map.get(d.get("preset_name", "")) or 0)),
            "date":        started_at[:10] if started_at else "",
            "started_at":  started_at,
            "fans_gained": fans_gained,
            "final_fans":  final_fans,
            "final_turn":  d.get("final_turn", 0),
            "finished":    finished,
        }
        careers.append(entry)

        if finished:
            total_count  += 1          # count every finished career
            total_gained += fans_gained

    # Keep at most the last 500 careers (same cap as main.py)
    careers = careers[-500:]

    result = {
        "careers":          careers,
        "all_time_gained":  total_gained,
        "all_time_careers": total_count,
    }

    OUT_FILE.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nWrote {OUT_FILE}")
    print(f"  Entries   : {len(careers)}")
    print(f"  Finished  : {total_count}")
    print(f"  Total fans: {total_gained:,}")
    if skipped:
        print(f"  Skipped   : {skipped} (in-progress / corrupt)")
    if careers:
        last = careers[-1]
        print(f"  Last entry: {last['date']} | {last['preset']} | +{last['fans_gained']:,} fans \u2192 {last['final_fans']:,}")


if __name__ == "__main__":
    main()
