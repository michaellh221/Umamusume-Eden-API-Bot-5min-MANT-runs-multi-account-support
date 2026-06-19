import json
from pathlib import Path


def _solver_mode(preset):
    return str((preset or {}).get("race_solver_mode") or "manual").strip().lower()


class RacePlanner:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.meta = {}
        self.program = {}
        self.instance = {}
        self.rejected = set()
        # Solver plan loaded at career start when mode=="auto"
        self._solver_plan = None
        self._load()

    def _load(self):
        path = self.base_dir / "data" / "race_map.json"
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        self.meta = {int(k): v for k, v in (data.get("meta") or {}).items()}
        self.program = {int(k): v for k, v in (data.get("program") or {}).items()}
        self.instance = {int(k): [int(item) for item in v] for k, v in (data.get("instance") or {}).items()}

    def get_rival_race_map(self, state):
        rivals = (
            state.get("data", {})
            .get("free_data_set", {})
            .get("rival_race_info_array", [])
        )
        return {
            int(r["program_id"]): int(r["chara_id"])
            for r in rivals
            if "program_id" in r and "chara_id" in r
        }

    def load_solver_plan(self, preset):
        """Load (or re-generate) the solver plan at career start."""
        if _solver_mode(preset) != "auto":
            self._solver_plan = None
            return
        try:
            from career_bot.race_solver import load_plan
            self._solver_plan = load_plan(self.base_dir, preset.get("name", "default"))
        except Exception:
            self._solver_plan = None

    def wanted_programs(self, preset, turn=None):
        result = []
        seen = set()
        current_turn = int(turn or 0)

        # Auto-solver mode: read from solver plan decisions
        if _solver_mode(preset) == "auto" and self._solver_plan:
            decisions = self._solver_plan.get("decisions") or {}
            key = str(current_turn)
            action = decisions.get(key) or {}
            if action.get("type") == "race":
                pid = action.get("program_id")
                if pid:
                    return [int(pid)]
            return []

        for value in preset.get("extra_race_list") or []:
            try:
                race_id = int(value)
            except (TypeError, ValueError):
                continue
            
            pids = []
            if race_id in self.meta:
                info = self.meta[race_id]
                occurrence_turn = int(info.get("turn") or 0)
                if current_turn and occurrence_turn and occurrence_turn != current_turn:
                    continue
                pid = info.get("program_id")
                if pid:
                    pids.append(pid)
            elif race_id in self.program:
                pids.append(race_id)
            else:
                for program_id in self.instance.get(race_id, []):
                    pids.append(program_id)
            
            for pid in pids:
                if pid not in seen:
                    seen.add(pid)
                    result.append(pid)
        return result

    def available_programs(self, state):
        data = state.get("data") or {}
        rca = data.get("race_condition_array") or []
        available = set()
        for item in rca:
            pid = int(item.get("program_id") or 0)
            if pid:
                available.add(pid)
        return available

    def forced_program(self, state):
        data = state.get("data") or {}
        home = data.get("home_info") or {}
        commands = home.get("command_info_array") or []
        race_enabled = any(cmd.get("command_type") == 4 and cmd.get("command_id") == 401 and cmd.get("is_enable", 0) for cmd in commands)
        other_enabled = any(cmd.get("command_type") != 4 and cmd.get("is_enable", 0) for cmd in commands)
        if not race_enabled or other_enabled:
            return 0
        turn = int((data.get("chara_info") or {}).get("turn") or 0)
        for item in data.get("race_condition_array") or []:
            pid = int(item.get("program_id") or 0)
            if pid and (turn, pid) not in self.rejected:
                return pid
        race = data.get("race_start_info") or {}
        pid = int(race.get("program_id") or 0)
        if pid and (turn, pid) not in self.rejected:
            return pid
        return 0

    def is_debut_race(self, program_id):
        """Junior Make Debut races are identified by name 'Junior Make Debut'."""
        info = self.program.get(int(program_id or 0)) or {}
        return info.get("name") == "Junior Make Debut"

    def check_aptitude(self, chara, program_id):
        info = self.program.get(int(program_id or 0)) or {}
        ground = int(info.get("ground") or 1)
        distance = int(info.get("distance") or 1200)
        
        if ground == 2:
            g_apt = int(chara.get("proper_ground_dirt") or 1)
        else:
            g_apt = int(chara.get("proper_ground_turf") or 1)
            
        if distance <= 1400:
            d_apt = int(chara.get("proper_distance_short") or 1)
        elif distance <= 1800:
            d_apt = int(chara.get("proper_distance_mile") or 1)
        elif distance <= 2400:
            d_apt = int(chara.get("proper_distance_middle") or 1)
        else:
            d_apt = int(chara.get("proper_distance_long") or 1)
            
        return g_apt >= 6 and d_apt >= 6

    def choose(self, state, preset):
        data = state.get("data") or {}
        turn = int((data.get("chara_info") or {}).get("turn") or 0)
        
        home = data.get("home_info") or {}
        commands = home.get("command_info_array") or []
        race_enabled = any(cmd.get("command_type") == 4 and cmd.get("command_id") == 401 and cmd.get("is_enable", 0) for cmd in commands)
        if not race_enabled:
            return 0

        available = self.available_programs(state)
        if not available:
            return 0
    
        wanted = self.wanted_programs(preset, turn)
        valid_wanted = [pid for pid in wanted if pid in available and (turn, pid) not in self.rejected]
        
        if not valid_wanted:
            chara = data.get("chara_info") or {}
            # Run the debut race if available and aptitude is OK (>= B on both
            # ground and distance). This unlocks the rest of the race schedule.
            # Any other non-wanted race is skipped — bot will train instead.
            # The truly forced debut (training disabled) is handled by forced_program().
            for pid in available:
                if (turn, pid) not in self.rejected and self.is_debut_race(pid) and self.check_aptitude(chara, pid):
                    return pid
            return 0
            
        is_mant = int((data.get("chara_info") or {}).get("scenario_id") or 0) == 4
        if not is_mant:
            return valid_wanted[0]
            
        main_race_id = valid_wanted[0]
        rival_map = self.get_rival_race_map(state)
        
        if main_race_id in rival_map:
            return main_race_id
            
        for overwrite_pid in valid_wanted[1:]:
            if overwrite_pid in rival_map:
                return overwrite_pid
                
        return main_race_id

    def reject(self, turn, program_id):
        self.rejected.add((int(turn or 0), int(program_id or 0)))

    def label(self, program_id):
        info = self.program.get(int(program_id or 0)) or {}
        name = info.get("name") or ""
        race_instance_id = info.get("race_instance_id") or ""
        return f"{program_id} {race_instance_id} {name}".strip()
