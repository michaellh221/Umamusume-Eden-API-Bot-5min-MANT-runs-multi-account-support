"""
EventManager — learns event-choice outcomes during play and scores choices.

DB schema (data/event_outcomes.json):
  {
    "<story_id>": {
      "<choice_num (0-based)>": {
        "diff": { "speed": 5.0, "vital": -3.0, "skill_point": 12.0, ... },
        "n": 7        # observations used to build the running average
      },
      ...
    },
    ...
  }

Keys in diff match chara_info fields: speed, stamina, power, guts, wiz,
vital, skill_point, playing_state, plus "gained_skill_hints" (list of
skill_id strings) for hints that appeared after the choice was made.

choose() returns a 0-based choice index (matches choice_number in check_event).
record() is called after each observed event to update the DB.
"""

import json
import threading
from pathlib import Path


# Stat keys tracked in diff (order matters for scoring)
_STAT_KEYS = ("speed", "stamina", "power", "guts", "wiz")
_ALL_DIFF_KEYS = _STAT_KEYS + ("vital", "skill_point", "playing_state")

# Hardcoded story-id overrides (index = 0-based choice number)
_FORCED_CHOICES: dict[str, int] = {
    "400004002": 1,  # always pick second option
}


class EventManager:
    """Learns event-choice outcomes over time and uses them to pick the best option."""

    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()
        self.outcomes: dict = {}

        # Set by choose(); read by runner to record the outcome after the call.
        self._last_chosen_story_id: str | None = None
        self._last_chosen_num: int | None = None

        self._load()

    # ── persistence ───────────────────────────────────────────────────────────

    def _db_path(self) -> Path:
        return self.base_dir / "data" / "event_outcomes.json"

    def _load(self):
        path = self._db_path()
        if not path.exists():
            return
        try:
            self.outcomes = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _save(self):
        path = self._db_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.outcomes, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # ── recording ─────────────────────────────────────────────────────────────

    def record(
        self,
        story_id: str | int,
        choice_num: int,
        chara_before: dict,
        chara_after: dict,
    ):
        """
        Diff chara_info before/after an event choice and persist the result.

        Called by runner._drain_events after each multi-choice event resolves.
        Updates a running average so repeated observations smooth out variance.
        """
        if not story_id or chara_before is None or chara_after is None:
            return

        # Build diff
        diff: dict = {}
        for key in _ALL_DIFF_KEYS:
            before_val = int(chara_before.get(key) or 0)
            after_val = int(chara_after.get(key) or 0)
            delta = after_val - before_val
            if delta != 0:
                diff[key] = delta

        # Skill hints that appeared after the choice
        before_ids = {
            str(h.get("skill_id", ""))
            for h in (chara_before.get("skill_hint_array") or [])
        }
        after_ids = {
            str(h.get("skill_id", ""))
            for h in (chara_after.get("skill_hint_array") or [])
        }
        new_hints = after_ids - before_ids
        if new_hints:
            diff["gained_skill_hints"] = sorted(new_hints)

        if not diff:
            return  # nothing changed — skip to avoid polluting DB with no-ops

        sid = str(story_id)
        cnum = str(choice_num)

        with self._lock:
            if sid not in self.outcomes:
                self.outcomes[sid] = {}

            existing = self.outcomes[sid].get(cnum)
            if existing is None:
                self.outcomes[sid][cnum] = {"diff": diff, "n": 1}
            else:
                # Running average for numeric keys; union for skill hints
                n = int(existing.get("n") or 1)
                old_diff = existing.get("diff") or {}
                all_keys = set(old_diff) | set(diff)
                new_diff: dict = {}
                for k in all_keys:
                    if k == "gained_skill_hints":
                        old_h = set(old_diff.get(k) or [])
                        new_h = set(diff.get(k) or [])
                        new_diff[k] = sorted(old_h | new_h)
                    else:
                        old_v = float(old_diff.get(k) or 0)
                        new_v = float(diff.get(k) or 0)
                        new_diff[k] = round((old_v * n + new_v) / (n + 1), 2)
                existing["diff"] = new_diff
                existing["n"] = n + 1

            self._save()

    # ── scoring ───────────────────────────────────────────────────────────────

    def _score_diff(self, diff: dict, stat_priority: list | None) -> float:
        """
        Score a recorded outcome diff.

        Higher = better.  Stat gains are weighted by preset priority order;
        SP and vital get smaller fixed weights; mood loss (playing_state < 0)
        is penalised; each new skill hint gives a bonus.
        """
        if not diff:
            return 0.0

        score = 0.0

        for i, key in enumerate(_STAT_KEYS):
            val = float(diff.get(key) or 0)
            if not val:
                continue
            mult = 1.0
            if stat_priority and key in stat_priority:
                rank = stat_priority.index(key)        # 0 = highest priority
                mult = max(0.5, 1.5 - rank * 0.2)     # 1.5 → 0.7 across 5 stats
            score += val * mult

        score += float(diff.get("skill_point") or 0) * 0.3
        score += float(diff.get("vital") or 0) * 0.5
        score -= max(0.0, -float(diff.get("playing_state") or 0)) * 2.0
        score += len(diff.get("gained_skill_hints") or []) * 5.0

        return score

    # ── choosing ─────────────────────────────────────────────────────────────

    def choose(self, event: dict, stat_priority: list | None = None) -> int:
        """
        Return the 0-based choice index that maximises expected outcome.

        Sets self._last_chosen_story_id / _last_chosen_num so runner can
        call record() after the API response arrives.

        Falls back to index 1 (second choice) when no data exists and the
        event has multiple options (historically better in Umamusume events),
        or index 0 for single-choice events.
        """
        story_id = str(event.get("story_id", ""))
        choices = (
            (event.get("event_contents_info") or {}).get("choice_array") or []
        )

        # Apply hardcoded overrides first
        if story_id in _FORCED_CHOICES:
            forced = _FORCED_CHOICES[story_id]
            self._last_chosen_story_id = story_id
            self._last_chosen_num = forced
            return forced

        if not choices:
            self._last_chosen_story_id = None
            self._last_chosen_num = None
            return 0

        # Look up recorded outcomes for this story
        outcome_data = self.outcomes.get(story_id)
        chosen_i: int | None = None

        if outcome_data:
            best_score: float | None = None
            for i in range(len(choices)):
                entry = outcome_data.get(str(i))
                if entry and "diff" in entry:
                    score = self._score_diff(entry["diff"], stat_priority)
                    if best_score is None or score > best_score:
                        best_score = score
                        chosen_i = i

        # No usable data — default: second choice for multi-choice, first otherwise
        if chosen_i is None:
            chosen_i = 1 if len(choices) > 1 else 0

        self._last_chosen_story_id = story_id
        self._last_chosen_num = chosen_i
        return chosen_i
