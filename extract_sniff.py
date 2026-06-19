"""
Run this from the umamusume-sweepy folder:
  python extract_sniff.py

Reads debug_sniff_log.json and saves a smaller file with only the
endpoints we care about for Showtime + finish analysis.
"""
import json, sys, pathlib

INPUT  = pathlib.Path(__file__).parent / "debug_sniff_log.json"
OUTPUT = pathlib.Path(__file__).parent / "sniff_extract.json"

KEEP = [
    "single_mode_free/start",
    "single_mode_free/finish",
    "pre_single_mode/index",
    "story_event",
    "single_mode_free/check_event",
]

print(f"Reading {INPUT} …", flush=True)
with open(INPUT, "r", encoding="utf-8", errors="replace") as f:
    data = json.load(f)

entries = data.get("captured_endpoints", [])
print(f"Total entries: {len(entries)}", flush=True)

kept = []
for e in entries:
    ep = e.get("endpoint", "")
    if any(k in ep for k in KEEP):
        # Trim huge arrays in response bodies to keep file manageable
        payload = e.get("payload", {})
        if e.get("type") == "response_body":
            trimmed = {}
            for k, v in payload.items():
                if isinstance(v, list) and len(v) > 20:
                    trimmed[k] = v[:5]
                    trimmed[f"__{k}_len"] = len(v)
                elif isinstance(v, dict):
                    trimmed[k] = v
                else:
                    trimmed[k] = v
            e = dict(e, payload=trimmed)
        kept.append(e)

print(f"Kept {len(kept)} entries matching filter", flush=True)

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump({"captured_endpoints": kept}, f, indent=2, ensure_ascii=False, default=str)

print(f"Saved to {OUTPUT}")
