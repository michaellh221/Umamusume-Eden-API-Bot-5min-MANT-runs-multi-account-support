"""
main.py
=======
FastAPI backend for the Umamusume Sweepy bot web UI.

Entry point: run with  python main.py  (launches uvicorn on 127.0.0.1:1616).
Open http://127.0.0.1:1616 in your browser to access the UI.

Architecture overview
---------------------
- The server starts by injecting a Frida hook into the running game process to
  capture fresh auth credentials (auth_key, viewer_id, udid, etc.).  These are
  stored in  data/accounts/<account>.json  and used to build a UmaClient.
- After login the dashboard is loaded: umas, support cards, decks, parents, and
  friend supports are all fetched and cached in global state.
- The UI (public/) is a single-page app that polls the REST API for live status.
- Career runs are executed either via CareerRunner (background thread, standard
  mode) or manage_career_loop (manual LOOP: ON dev mode).

Global state
------------
  active_client          – the authenticated UmaClient instance
  active_account         – dict with account/career summary shown in the UI
  active_dashboard_data  – full snapshot returned to the UI on /api/status
  active_start_state     – TP / money / succession rank info refreshed pre-start

Fan stats
---------
  Stored in  data/fan_stats.json.  Appended after each completed career.
  Exposed via  GET /api/stats/fans.

Extending / forking notes
--------------------------
- To add a new API endpoint, add a FastAPI route function below the relevant
  section comment.
- To add a new account field, update get_account_status() and the UI.
- The frida-based auth refresh (refresh_auth_before_serving) requires the game
  to be running.  To skip it during development, stub it out and supply a
  pre-saved config in data/accounts/.
"""

import os
import json
import re
import subprocess
import sys

try:
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
except Exception:
    pass

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from pathlib import Path
import random
import time
import threading
import frida
from career_bot import master_data
from career_bot.presets import PresetStore
from career_bot.runner import CareerRunner
from uma_api.client import UmaClient
from career_bot.delay import GateKeeper, dna_sleep, dna_uniform

PROCESS_NAME = "UmamusumePrettyDerby.exe"
APP_ID = "3224770"

# ── Frida JS injection ─────────────────────────────────────────────────────
# Injected into the game process at startup to intercept TLS traffic and
# capture auth credentials (auth_key, viewer_id, udid) from live requests.
JS_CODE = r'''
'use strict';
(function() {
    var buffers = {};
    var attached = {};
    function hex2(n) { return ('0' + (n & 255).toString(16)).slice(-2); }
    function uuidFromHex(h) {
        return h.substring(0, 8) + '-' + h.substring(8, 12) + '-' + h.substring(12, 16) + '-' + h.substring(16, 20) + '-' + h.substring(20);
    }
    function b64(s) {
        var chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
        var out = [];
        var buffer = 0;
        var bits = 0;
        for (var i = 0; i < s.length; i++) {
            var c = s.charAt(i);
            if (c === '=') break;
            var idx = chars.indexOf(c);
            if (idx < 0) continue;
            buffer = (buffer << 6) | idx;
            bits += 6;
            if (bits >= 8) {
                bits -= 8;
                out.push((buffer >> bits) & 255);
            }
        }
        return out;
    }
    function parseWire(endpoint, viewerId, body, appVer, resVer) {
        var decoded = b64(body);
        if (decoded.length < 140) return;
        var headerLen = decoded[0] | (decoded[1] << 8) | (decoded[2] << 16) | (decoded[3] << 24);
        var blob1End = 4 + headerLen;
        if (headerLen < 120 || headerLen > 2048 || decoded.length < blob1End) return;
        
        var udidHex = '';
        for (var i = blob1End - 96; i < blob1End - 80; i++) udidHex += hex2(decoded[i]);
        var authHex = '';
        for (var j = blob1End - 48; j < blob1End; j++) authHex += hex2(decoded[j]);
        
        if (!viewerId || !authHex || authHex.length < 64 || udidHex.length !== 32) return;
        
        send({
            type: 'creds',
            endpoint: endpoint,
            viewer_id: parseInt(viewerId, 10),
            udid: uuidFromHex(udidHex),
            auth_key: authHex,
            auth_key_len: authHex.length / 2,
            app_ver: appVer,
            res_ver: resVer,
            body: body
        });
    }
    function parseHttp(text) {
        if (text.indexOf('/umamusume/') < 0) return;
        var em = text.match(/POST\s+\/umamusume\/([^\s]+)\s+HTTP/i);
        var vm = text.match(/(?:^|\r\n)(?:ViewerID|ViewerId):\s*(\d+)/i);
        var appVer = text.match(/(?:^|\r\n)APP-VER:\s*([^\r\n]+)/i);
        var resVer = text.match(/(?:^|\r\n)RES-VER:\s*([^\r\n]+)/i);
        var idx = text.indexOf('\r\n\r\n');
        if (!em || !vm || idx < 0) return;
        parseWire(em[1], vm[1], text.substring(idx + 4), appVer ? appVer[1].trim() : '', resVer ? resVer[1].trim() : '');
    }
    function parseChunk(key, chunk) {
        var buf = (buffers[key] || '') + chunk;
        if (buf.length > 2097152) buf = buf.substring(buf.length - 1048576);
        var start = buf.indexOf('POST ');
        if (start < 0) {
            buffers[key] = buf.slice(-4096);
            return;
        }
        if (start > 0) buf = buf.substring(start);
        var headerEnd = buf.indexOf('\r\n\r\n');
        if (headerEnd < 0) {
            buffers[key] = buf;
            return;
        }
        var headers = buf.substring(0, headerEnd);
        var lm = headers.match(/Content-Length:\s*(\d+)/i);
        var length = lm ? parseInt(lm[1], 10) : 0;
        var total = headerEnd + 4 + length;
        if (length > 0 && buf.length < total) {
            buffers[key] = buf;
            return;
        }
        parseHttp(length > 0 ? buf.substring(0, total) : buf);
        buffers[key] = buf.length > total ? buf.substring(total) : '';
    }
    function hookTls() {
        var ga = Process.findModuleByName('GameAssembly.dll');
        if (!ga) return false;
        var installFn = ga.findExportByName('il2cpp_unity_install_unitytls_interface');
        if (!installFn) return false;
        var rb = new Uint8Array(installFn.readByteArray(16));
        var realFn = installFn;
        if (rb[0] === 0xe9) {
            var off = rb[1] | (rb[2] << 8) | (rb[3] << 16) | (rb[4] << 24);
            if (off > 0x7fffffff) off -= 0x100000000;
            realFn = installFn.add(5 + off);
            rb = new Uint8Array(realFn.readByteArray(16));
        }
        var globalPtr = null;
        if (rb[0] === 0x48 && rb[1] === 0x89 && rb[2] === 0x0d) {
            var disp = rb[3] | (rb[4] << 8) | (rb[5] << 16) | (rb[6] << 24);
            if (disp > 0x7fffffff) disp -= 0x100000000;
            globalPtr = realFn.add(7 + disp);
        }
        if (!globalPtr) return false;
        var iface = globalPtr.readPointer();
        if (!iface || iface.isNull()) return false;
        var hookedTls = 0;
        [0xd0, 0xd8, 0xe0, 0xe8].forEach(function(off) {
            var addr = iface.add(off).readPointer();
            if (!addr || addr.isNull()) return;
            var key = 'tls_' + addr.toString();
            if (attached[key]) return;
            try {
                Interceptor.attach(addr, {
                    onEnter: function(args) {
                        var len = args[2].toInt32();
                        if (len <= 0 || len > 1048576 || args[1].isNull()) return;
                        try {
                            var bytes = args[1].readByteArray(len);
                            var u8 = new Uint8Array(bytes);
                            var s = '';
                            for (var i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
                            parseChunk(args[0].toString(), s);
                        } catch (e) {}
                    }
                });
                attached[key] = true;
                hookedTls++;
            } catch (e) {}
        });
        return hookedTls > 0;
    }
    var tlsDone = false;
    var timer = setInterval(function() {
        try {
            if (!tlsDone) tlsDone = hookTls();
            if (tlsDone) clearInterval(timer);
        } catch (e) {}
    }, 1000);
})();
'''


DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()

chara_map = {}
support_map = {}
active_client = None
active_account = None
active_dashboard_data = None
active_start_state = {}
active_parent_cards = {}
active_parent_rank_points = {}
pending_game_auth_config = {}
raw_load_index_response = None
active_selection = {
    "deck": None,
    "friend": None,
    "trainee": None,
    "veterans": [],
    "guestParent": None
}
turn_delay_min_sec = 2.5
turn_delay_max_sec = 5.0
turn_delay_restore_min_sec = 2.5
turn_delay_restore_max_sec = 5.0
turn_delay_disabled = False
preset_store = PresetStore(DIR)
career_runner = CareerRunner(DIR)

# ---------------------------------------------------------------------------
# Fan stats – persistent per-career tracking
# ---------------------------------------------------------------------------
import threading as _threading_mod
from datetime import date as _date_mod, datetime as _dt_mod

FAN_STATS_PATH = None   # resolved after base_dir is known
AUTH_CACHE_PATH = None  # resolved after base_dir is known

def _auth_cache_file():
    global AUTH_CACHE_PATH
    if AUTH_CACHE_PATH is None:
        AUTH_CACHE_PATH = base_dir / "uma_runtime" / "auth_cache.json"
    return AUTH_CACHE_PATH

AUTH_CACHE_MAX_AGE_HOURS = 20  # Steam session tickets expire; treat cache older than this as stale

def _save_auth_cache(cfg):
    """Persist Frida-captured auth config to disk so subsequent logins skip Steam API."""
    try:
        path = _auth_cache_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        saveable = {k: v for k, v in cfg.items() if k != 'steam_password_seed'}
        saveable['_cached_at'] = time.time()
        with open(path, 'w') as f:
            json.dump(saveable, f, indent=2)
    except Exception as e:
        print(f"[auth_cache] save error: {e}")

def _clear_auth_cache():
    """Delete the auth cache so next startup triggers a fresh capture."""
    try:
        path = _auth_cache_file()
        if path.exists():
            path.unlink()
            print("[auth_cache] cleared stale cache")
    except Exception as e:
        print(f"[auth_cache] clear error: {e}")

def _load_auth_cache():
    """Load cached auth config. Returns {} if missing, invalid, or older than AUTH_CACHE_MAX_AGE_HOURS."""
    try:
        path = _auth_cache_file()
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            cached_at = data.get('_cached_at', 0)
            age_hours = (time.time() - cached_at) / 3600
            if age_hours > AUTH_CACHE_MAX_AGE_HOURS:
                print(f"[auth_cache] expired ({age_hours:.1f}h old > {AUTH_CACHE_MAX_AGE_HOURS}h limit) — will recapture")
                return {}
            return data
    except Exception:
        pass
    return {}
_fan_stats_lock = _threading_mod.Lock()
_fan_stats = {"careers": []}
_session_fans_gained = 0

# ── Circle (club) info cache ────────────────────────────────────────────────
# Loaded once on first /api/stats/circle request; invalidated after each
# completed career so fans/ranking stay current without hammering the server.
_cached_circle_info: dict | None = None
_circle_refresh_needed = True          # True = fetch from game server next call

def _fan_stats_file():
    global FAN_STATS_PATH
    if FAN_STATS_PATH is None:
        FAN_STATS_PATH = base_dir / "fan_stats.json"
    return FAN_STATS_PATH

def _load_fan_stats():
    global _fan_stats
    path = _fan_stats_file()
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            try:
                _fan_stats = json.loads(text)
            except json.JSONDecodeError:
                # Corrupted file (e.g. Windows mount garbage appended) — try to recover
                # by finding the last valid closing brace of the top-level object.
                for i in range(len(text) - 1, -1, -1):
                    if text[i] == "}":
                        try:
                            _fan_stats = json.loads(text[:i + 1])
                            print(f"fan_stats: recovered from corrupted JSON (truncated at char {i+1})")
                            _save_fan_stats()   # re-write clean copy immediately
                            break
                        except json.JSONDecodeError:
                            continue
                else:
                    print("fan_stats load error: file unrecoverable, starting fresh")
    except Exception as e:
        print(f"fan_stats load error: {e}")
    # Back-compat: seed persistent totals from existing careers list if fields are missing
    careers = _fan_stats.get("careers", [])
    if "all_time_gained" not in _fan_stats:
        _fan_stats["all_time_gained"] = sum(c.get("fans_gained", 0) for c in careers)
    if "all_time_careers" not in _fan_stats:
        _fan_stats["all_time_careers"] = sum(1 for c in careers if c.get("finished", True))

def _save_fan_stats():
    path = _fan_stats_file()
    try:
        text = json.dumps(_fan_stats, ensure_ascii=False, indent=2)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)          # atomic on same filesystem
    except Exception as e:
        print(f"fan_stats save error: {e}")

def _career_grade(stats: dict) -> str:
    """Compute letter grade from final training stats (speed+stamina+power+guts+wit)."""
    total = sum(int(stats.get(k) or 0) for k in ("speed", "stamina", "power", "guts", "wit"))
    if   total >= 4000: return "S+"
    elif total >= 3500: return "S"
    elif total >= 3000: return "A"
    elif total >= 2500: return "B"
    elif total >= 2000: return "C"
    elif total >= 1500: return "D"
    elif total >= 1000: return "E"
    elif total >= 500:  return "F"
    else:               return "G"

def record_career_fans(card_id, fans_gained, final_fans, preset_name="", final_turn=0, running_style=0, final_stats=None):
    global _fan_stats, _session_fans_gained, _circle_refresh_needed
    _circle_refresh_needed = True  # refresh club fans on next /api/stats/circle call
    chara_name = chara_map.get(str(card_id), f"Uma ({card_id})")
    stats = final_stats or {}
    entry = {
        "timestamp": _dt_mod.now().isoformat(timespec="seconds"),
        "date": _date_mod.today().isoformat(),
        "card_id": str(card_id),
        "chara_name": chara_name,
        "fans_gained": int(fans_gained),
        "final_fans": int(final_fans),
        "preset": preset_name,
        "running_style": int(running_style or 0),
        "final_turn": int(final_turn),
        "finished": True,  # only recorded when runner reaches "done" — tracks completion state
        "final_stats": stats,
        "grade": _career_grade(stats),
    }
    with _fan_stats_lock:
        _fan_stats.setdefault("careers", []).append(entry)
        if len(_fan_stats["careers"]) > 500:
            _fan_stats["careers"] = _fan_stats["careers"][-500:]
        _fan_stats["all_time_gained"] = _fan_stats.get("all_time_gained", 0) + int(fans_gained)
        _fan_stats["all_time_careers"] = _fan_stats.get("all_time_careers", 0) + 1
        _session_fans_gained += int(fans_gained)
        _save_fan_stats()
# ---------------------------------------------------------------------------

base_dir = Path(__file__).parent.absolute()
bot_logs_dir = base_dir / "uma_runtime" / "bot_logs"
master_data_startup_status = master_data.status(base_dir)
if master_data_startup_status.get("exists"):
    master_data_startup_result = master_data.generate(base_dir)
    if master_data_startup_result.get("success"):
        print(f"[ok] Game data loaded")
    else:
        print(f"[warn] Game data load failed: {master_data_startup_result.get('detail')}")
elif master_data_startup_status.get("requires_user_action"):
    print(f"[warn] master.mdb not found at {master_data_startup_status.get('master_mdb_path')}")
chara_path = base_dir / 'data' / 'chara_list.json'
support_path = base_dir / 'data' / 'support_list.json'
images_dir = base_dir / 'data' / 'images'

if chara_path.exists():
    with open(chara_path, 'r', encoding='utf-8') as f:
        chara_map = json.load(f)
if support_path.exists():
    with open(support_path, 'r', encoding='utf-8') as f:
        support_map = json.load(f)

_load_fan_stats()

# ---------------------------------------------------------------------------
# Turn delay – persist to / restore from settings.json
# ---------------------------------------------------------------------------
SETTINGS_JSON_PATH = base_dir / "settings.json"

def _load_settings_json():
    try:
        with open(SETTINGS_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_settings_json(patch: dict):
    """Merge patch into settings.json on disk."""
    try:
        data = _load_settings_json()
        data.update(patch)
        with open(SETTINGS_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[settings] could not save settings.json: {e}")

def display_support_type(value):
    return {
        "Friends": "Pal",
        "Wisdom": "Wit"
    }.get(value, value)


def normalize_turn_delay(min_value, max_value, disabled=False):
    left = max(0.0, float(min_value or 0.0))
    right = max(0.0, float(max_value or 0.0))
    if left > right:
        right = left
    if disabled:
        left = 0.0
        right = 0.0
    return left, right, bool(disabled)

def set_turn_delay(min_value, max_value, disabled=False):
    import career_bot.delay as delay_module
    next_min, next_max, next_disabled = normalize_turn_delay(min_value, max_value, disabled)
    if not next_disabled:
        delay_module.TURN_DELAY_RESTORE_MIN = next_min
        delay_module.TURN_DELAY_RESTORE_MAX = next_max
    delay_module.TURN_DELAY_MIN = next_min
    delay_module.TURN_DELAY_MAX = next_max
    delay_module.GLOBAL_DELAYS_DISABLED = next_disabled  # only disables inter-turn wait
    # (delay settings applied silently)
    # Persist so fate tempo survives server restart
    _save_settings_json({"turn_delay": {
        "min": next_min,
        "max": next_max,
        "disabled": next_disabled,
        "restore_min": getattr(delay_module, "TURN_DELAY_RESTORE_MIN", next_min),
        "restore_max": getattr(delay_module, "TURN_DELAY_RESTORE_MAX", next_max),
    }})
    return get_turn_delay()

def get_turn_delay():
    import career_bot.delay as delay_module
    return {
        "success": True,
        "min": getattr(delay_module, "TURN_DELAY_MIN", 2.5),
        "max": getattr(delay_module, "TURN_DELAY_MAX", 5.0),
        "restore_min": getattr(delay_module, "TURN_DELAY_RESTORE_MIN", 2.5),
        "restore_max": getattr(delay_module, "TURN_DELAY_RESTORE_MAX", 5.0),
        "disabled": getattr(delay_module, "GLOBAL_DELAYS_DISABLED", False)
    }

# ---------------------------------------------------------------------------
# TP recovery mode – persisted in settings.json
#   potion_first  – use TP items first, fall back to Jewels if still short
#   potion_only   – TP items only; refuse to start if still short
#   jewels_only   – spend Jewels only (original behaviour)
# ---------------------------------------------------------------------------
TP_RECOVERY_MODES = ("potion_first", "potion_only", "jewels_only")

def load_tp_recovery_mode():
    mode = _load_settings_json().get("tp_recovery", "jewels_only")
    return mode if mode in TP_RECOVERY_MODES else "jewels_only"

def set_tp_recovery_mode(mode):
    if mode not in TP_RECOVERY_MODES:
        mode = "jewels_only"
    _save_settings_json({"tp_recovery": mode})
    return mode

def tp_recovery_label(mode):
    return {
        "potion_first": "TP items first, Jewels fallback",
        "potion_only":  "TP items only",
        "jewels_only":  "Jewels only",
    }.get(mode, "Jewels only")

# Restore turn delay from settings.json at startup (so fate tempo survives restarts)
def _restore_turn_delay():
    cfg = _load_settings_json().get("turn_delay", {})
    if cfg:
        set_turn_delay(cfg.get("min", 2.5), cfg.get("max", 5.0), cfg.get("disabled", False))

_restore_turn_delay()

# ── Helpers: state refresh ─────────────────────────────────────────────────
def update_start_state(data):
    global active_start_state
    if not data:
        return
    if data.get('tp_info'):
        tp_info = dict(data.get('tp_info'))
        active_start_state['tp_info'] = tp_info
    item_list = data.get('item_list') or data.get('user_item_array')
    if isinstance(item_list, list) and item_list:
        active_start_state['current_money'] = get_item_count(item_list, 59)
        # Succession rank point: try several locations the game API uses.
        # Check item_id=75 first (older clients), then top-level fields,
        # then user_info sub-dict.  Keep the best non-zero value found.
        srp = get_item_count(item_list, 75)
        if not srp:
            srp = (data.get('succession_rank_point')
                   or data.get('user_succession_rank_point')
                   or (data.get('user_info') or {}).get('succession_rank_point')
                   or 0)
        if srp:  # only overwrite if we found a real value
            active_start_state['succession_rank_point'] = int(srp)
        pass  # succession_rank_point cached silently


def normalize_friend_cards(data):
    source = 'refresh'
    friend_data = data.get('friend_support_card_data')
    if friend_data:
        source = 'initial'
        summaries = friend_data.get('summary_user_info_array', [])
        support_cards = friend_data.get('support_card_data_array', [])
    else:
        summaries = data.get('summary_user_info_array', [])
        support_cards = data.get('support_card_data_array', [])

    support_by_key = {}
    for sc in support_cards or []:
        key = (sc.get('viewer_id'), sc.get('support_card_id'))
        support_by_key[key] = sc

    friends = []
    exclude_viewer_ids = []
    seen = set()
    for info in summaries or []:
        viewer_id = info.get('viewer_id')
        support_card_id = info.get('support_card_id')
        if not viewer_id or not support_card_id:
            continue
        key = (viewer_id, support_card_id)
        if key in seen:
            continue
        seen.add(key)
        exclude_viewer_ids.append(viewer_id)
        card_data = support_by_key.get(key) or info.get('user_support_card') or {}
        support_info = support_map.get(str(support_card_id), {})
        
        friends.append({
            'viewer_id': viewer_id,
            'name': info.get('name', ''),
            'support_card_id': support_card_id,
            'support_name': support_info.get('name', f"Unknown ({support_card_id})"),
            'rarity': support_info.get('rarity', '?'),
            'type': display_support_type(support_info.get('type', 'Unknown')),
            'exp': card_data.get('exp', info.get('user_support_card', {}).get('exp')),
            'limit_break_count': card_data.get('limit_break_count', info.get('user_support_card', {}).get('limit_break_count')),
            'favorite_flag': card_data.get('favorite_flag', 0),
            'friend_state': info.get('friend_state', 0),
            'parent_data': info.get('user_trained_chara', {}),
            'trained_chara_id': info.get('user_trained_chara', {}).get('trained_chara_id'),
            'parent_viewer_id': info.get('viewer_id')
            
        })
    return friends, exclude_viewer_ids, source


def normalize_card_name(name):
    return re.sub(r'[^a-z0-9]+', '', re.sub(r'\([^)]*\)', '', str(name or '').lower()))


# ── Career start: validation helpers ──────────────────────────────────────
def validate_start_selection(req):
    support_ids = [int(card_id) for card_id in req.support_card_ids]
    friend_card_id = int(req.friend_card_id)
    if friend_card_id in support_ids:
        return "Friend support card is already in selected deck"

    friend_info = support_map.get(str(friend_card_id), {})
    friend_name = normalize_card_name(friend_info.get('name'))
    if not friend_name:
        return None

    for support_id in support_ids:
        support_name = normalize_card_name(support_map.get(str(support_id), {}).get('name'))
        if support_name and support_name == friend_name:
            return "Friend support card has same character as selected deck"

    trainee_name = normalize_card_name(chara_map.get(str(req.card_id), ''))
    if trainee_name and trainee_name == friend_name:
        return "Friend support card has same character as trainee"

    # Character-level parent conflict check (covers all variants of the same character).
    # Card IDs are 6-digit numbers where the first 4 digits identify the character:
    # e.g. 100101 (Mihono Bourbon) and 100102 (Mihono Bourbon special) both map to chara 1001.
    def card_chara_id(card_id):
        return int(str(int(card_id))[:4]) if card_id else 0

    trainee_chara_id = card_chara_id(req.card_id)
    parent1_cards = active_parent_cards.get(int(req.parent_id_1), [])
    parent2_cards = active_parent_cards.get(int(req.parent_id_2), [])
    for parent_card_id in filter(None, [
        parent1_cards[0] if parent1_cards else None,
        parent2_cards[0] if parent2_cards else None,
    ]):
        if card_chara_id(parent_card_id) == trainee_chara_id:
            return "Selected parent is the same character (or variant) as the trainee"

    return None


def deck_type_counts_from_ids(support_ids, friend_card_id=0):
    counts = [0] * 5
    for sid_int in list(support_ids or []) + ([friend_card_id] if friend_card_id else []):
        info = support_map.get(str(sid_int))
        if not info:
            continue
        ctype = info.get('type')
        if ctype == "Speed": counts[0] += 1
        elif ctype == "Stamina": counts[1] += 1
        elif ctype == "Power": counts[2] += 1
        elif ctype == "Guts": counts[3] += 1
        elif ctype == "Wisdom": counts[4] += 1
    return counts


def deck_type_counts_from_chara(chara_info):
    ids = []
    for card in (chara_info or {}).get('support_card_array') or []:
        sid = int(card.get('support_card_id') or 0)
        if sid:
            ids.append(sid)
    return deck_type_counts_from_ids(ids)


def apply_deck_type_counts(preset, req=None, chara_info=None):
    counts = None
    if req and (req.support_card_ids or req.friend_card_id):
        counts = deck_type_counts_from_ids(req.support_card_ids, req.friend_card_id)
    elif chara_info:
        counts = deck_type_counts_from_chara(chara_info)
    if counts is not None:
        preset["_deck_type_counts"] = counts
        scale_table = [0.0, 0.02, 0.05, 0.09, 0.14, 0.20]
        preset["_deck_multipliers"] = [1.0 + scale_table[min(5, c)] for c in counts]


def parent_rank_point(parent_id):
    parent = active_parent_rank_points.get(int(parent_id))
    if not parent:
        return 0
    rank = int(parent.get('rank') or 0)
    if rank == 13:
        return 62
    return int(parent.get('rank_point') or 0)


def selected_succession_rank_point(req):
    selected_total = parent_rank_point(req.parent_id_1) + parent_rank_point(req.parent_id_2)
    if selected_total:
        return selected_total
    return active_start_state.get('succession_rank_point', 0)

skill_data = {}
skill_data_path = base_dir / 'data' / 'skill_data.json'
if skill_data_path.exists():
    with open(skill_data_path, 'r', encoding='utf-8') as f:
        skill_data = json.load(f)

factor_map = {}
factor_map_path = base_dir / 'data' / 'factor_map.json'
if factor_map_path.exists():
    with open(factor_map_path, 'r', encoding='utf-8') as f:
        factor_map = json.load(f)

race_map = {}
race_map_path = base_dir / 'data' / 'race_map.json'
if race_map_path.exists():
    with open(race_map_path, 'r', encoding='utf-8') as f:
        race_map = json.load(f)

def skill_entry_name(entry):
    if isinstance(entry, dict):
        return entry.get("name") or ""
    return entry

def get_win_summary(win_saddle_ids):
    summary = {
        "g1": 0,
        "g2": 0,
        "g3": 0
    }

    for saddle_id in win_saddle_ids or []:
        race = race_map.get(str(saddle_id))
        grade = race.get("grade") if race else None
        if grade == "G1":
            summary["g1"] += 1
        elif grade == "G2":
            summary["g2"] += 1
        elif grade == "G3":
            summary["g3"] += 1

    summary["total"] = summary["g1"] + summary["g2"] + summary["g3"]
    return summary

def clean_factor_name(name, base_id=None, category=None):
    if not isinstance(name, str):
        return name

    if category == "skill" and "?" in name and base_id is not None:
        skill_name = skill_entry_name(skill_data.get(f"{base_id}2"))
        if skill_name:
            return skill_name
    return name.replace(" ?", " ○")

def get_factors(fid_array, owner_card_id=None):
    results = []
    category_order = {
        "stat": 0,
        "aptitude": 1,
        "unique": 2,
        "race": 3,
        "skill": 4,
        "scenario": 5,
        "other": 6
    }
    stat_map = {
        1: 'Speed', 2: 'Stamina', 3: 'Power', 4: 'Guts', 5: 'Wit',
        11: 'Turf', 12: 'Dirt',
        21: 'Short', 22: 'Mile', 23: 'Medium', 24: 'Long',
        31: 'Front Runner', 32: 'Pace Chaser', 33: 'Late Surger', 34: 'End Closer'
    }
    
    owner_cid_str = str(owner_card_id) if owner_card_id else ""
    if len(owner_cid_str) > 4: owner_cid_str = owner_cid_str[:4]

    for fid in fid_array:
        if not fid or fid <= 0: continue

        fid_str = str(fid)
        factor_info = factor_map.get(fid_str)
        if factor_info:
            base_id = fid // 100
            category = factor_info.get("category", "other")
            name = clean_factor_name(factor_info.get("name", f"Unknown({fid})"), base_id, category)
            stars = factor_info.get("stars", fid % 100)
            results.append({"name": name, "stars": stars, "id": fid, "category": category})
            continue

        base_id = fid // 100
        stars = fid % 100
        bid_str = str(base_id)
        name = f"Unknown({base_id})"
        category = "other"
        
        if base_id <= 34:
            category = "stat" if base_id <= 5 else "aptitude"
            name = stat_map.get(base_id, name)
        
        elif bid_str in skill_data:
            category = "skill"
            name = skill_entry_name(skill_data[bid_str])
            
        results.append({"name": name, "stars": stars, "id": base_id, "category": category})

    return [
        factor for _, factor in sorted(
            enumerate(results),
            key=lambda item: (category_order.get(item[1]["category"], 99), item[0])
        )
    ]


def get_chara_factor_ids(chara):
    factor_ids = chara.get('factor_id_array')
    if isinstance(factor_ids, list) and factor_ids:
        return factor_ids
    return [f.get('factor_id', 0) for f in chara.get('factor_info_array', [])]


def get_item_count(item_list, item_id):
    for item in item_list or []:
        if item.get('item_id') == item_id:
            return item.get('number', 0)
    return 0


# ── Dashboard / account status ─────────────────────────────────────────────
# get_account_status() builds the normalized account dict shown in the UI.
def get_account_status(data, career_data=None):
    tp_info = data.get('tp_info') or (active_client.tp_info if active_client else {})
    coin_info = data.get('coin_info') or (active_client.coin_info if active_client else {})
    item_list = data.get('item_list') or data.get('user_item_array')
    if item_list is None:
        gold = active_client.item_map.get(59, 0) if active_client else 0
    else:
        gold = get_item_count(item_list, 59)
    career = data.get('single_mode_chara_light') or None

    if career_data:
        career_payload = career_data.get('data') if career_data.get('data') else career_data
        if career_payload.get('chara_info'):
            career = career_payload.get('chara_info')

    status = {
        "tp": {
            "current": tp_info.get('current_tp', 0),
            "max": tp_info.get('max_tp', 0)
        },
        "carrots": {
            "free": coin_info.get('fcoin', 0) or 0,
            "paid": coin_info.get('coin', 0) or 0,
            "total": (coin_info.get('fcoin', 0) or 0) + (coin_info.get('coin', 0) or 0)
        },
        "gold": gold,
        "clocks": active_client.item_map.get(95, 0) if active_client else 0,
        "potions": active_client.tp_potion_count() if active_client else 0,
        "tp_recovery": {
            "mode": load_tp_recovery_mode(),
            "label": tp_recovery_label(load_tp_recovery_mode()),
        },
        "career": None
    }
    if career:
        card_id = str(career.get('card_id', ''))
        
        p1 = career.get('succession_trained_chara_id_1')
        p2 = career.get('succession_trained_chara_id_2')

        friend_viewer_id = None
        friend_card_id = None
        friend_support = None
        current_deck_cards = []
        current_deck_supports = []
        
        support_array = career.get('support_card_array') or []
        for sc in support_array:
            pos = sc.get('position')
            if pos == 6:
                friend_viewer_id = sc.get('owner_viewer_id')
                friend_card_id = sc.get('support_card_id')
                friend_info = support_map.get(str(friend_card_id))
                friend_support = {
                    "viewer_id": friend_viewer_id,
                    "support_card_id": friend_card_id,
                    "support_name": friend_info['name'] if friend_info else f"Unknown ({friend_card_id})",
                    "rarity": friend_info['rarity'] if friend_info else "?",
                    "type": display_support_type(friend_info['type']) if friend_info else "?",
                    "limit_break_count": sc.get('limit_break_count')
                }
            elif 1 <= pos <= 5:
                support_card_id = sc.get('support_card_id')
                current_deck_cards.append(support_card_id)
                support_info = support_map.get(str(support_card_id))
                current_deck_supports.append({
                    "id": str(support_card_id),
                    "name": support_info['name'] if support_info else f"Unknown ({support_card_id})",
                    "rarity": support_info['rarity'] if support_info else "?",
                    "type": display_support_type(support_info['type']) if support_info else "?"
                })

        matched_deck_id = None
        user_decks = data.get('support_card_deck_array') or []
        if current_deck_cards:
            current_deck_set = set(current_deck_cards)
            for deck in user_decks:
                deck_cards = deck.get('support_card_id_array') or []
                if set(deck_cards) == current_deck_set:
                    matched_deck_id = deck.get('deck_id')
                    break

        status["career"] = {
            "active": True,
            "card_id": card_id,
            "name": chara_map.get(card_id, f"Unknown ({card_id})"),
            "turn": career.get('turn', 0),
            "scenario_id": career.get('scenario_id', 0),
            "fans": career.get('fans', 0),
            "vital": career.get('vital', 0),
            "max_vital": career.get('max_vital', 0),
            "deck_id": matched_deck_id,
            "support_card_ids": current_deck_cards,
            "support_cards": current_deck_supports,
            "friend_viewer_id": friend_viewer_id,
            "friend_card_id": friend_card_id,
            "friend": friend_support,
            "parent_id_1": p1,
            "parent_id_2": p2,
        }

    return status




class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""
    code: str = ""
    steam_id: str = ""
    steam_session_ticket: str = ""

class DeleteCareerRequest(BaseModel):
    current_turn: int = 0

class StartCareerRequest(BaseModel):
    card_id: int
    support_card_ids: list[int]
    friend_viewer_id: int
    friend_card_id: int
    parent_id_1: int
    parent_id_2: int

    rental_viewer_id: int = 0
    rental_trained_chara_id: int = 0

    scenario_id: int = 4
    deck_id: int = 1
    use_tp: int = 30
    difficulty_id: int = 0
    difficulty: int = 0
    is_boost: int = 0
    boost_story_event_id: int = 0
    burn_clocks: bool = False

class RunCareerRequest(BaseModel):
    card_id: int = 0
    support_card_ids: list[int] = []
    friend_viewer_id: int = 0
    friend_card_id: int = 0
    parent_id_1: int = 0
    parent_id_2: int = 0
    rental_viewer_id: int = 0
    rental_trained_chara_id: int = 0
    scenario_id: int = 0
    deck_id: int = 1
    use_tp: int = 30
    difficulty_id: int = 0
    difficulty: int = 0
    is_boost: int = 0
    boost_story_event_id: int = 0
    preset_name: str = ""
    max_steps: int = 2500
    burn_clocks: bool = False
    dev_mode: bool = False

class SaveRacesRequest(BaseModel):
    preset_name: str
    races: list[int]

class SavePresetRequest(BaseModel):
    preset: dict

class DeletePresetByNameRequest(BaseModel):
    name: str

class CareerActionRequest(BaseModel):
    command_type: int
    command_id: int
    current_turn: int
    current_vital: int
    command_group_id: int = 0
    select_id: int = 0

class FriendListRequest(BaseModel):
    exclude_viewer_ids: list[int] = []

class ApiDelayRequest(BaseModel):
    min: float = 1.6
    max: float = 4.0
    disabled: bool = False

class MasterDataPathRequest(BaseModel):
    master_mdb_path: str

@app.get("/api/settings/turn-delay")
async def get_turn_delay_settings():
    return get_turn_delay()

@app.post("/api/settings/turn-delay")
async def set_turn_delay_settings(req: ApiDelayRequest):
    return set_turn_delay(req.min, req.max, req.disabled)

@app.get("/api/master-data/status")
async def master_data_status():
    return master_data.status(base_dir)

@app.post("/api/master-data/path")
async def set_master_data_path(req: MasterDataPathRequest):
    status = master_data.set_master_mdb_path(base_dir, req.master_mdb_path)
    if status.get("exists"):
        result = master_data.generate(base_dir)
        if result.get("success"):
            status["generated"] = result.get("generated", [])
        else:
            status["generation_error"] = result.get("detail") or "master_data generation failed"
    return status

@app.post("/api/master-data/generate")
async def generate_master_data():
    result = master_data.generate(base_dir)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("detail") or "master_data generation failed")
    return result

@app.post("/api/presets/save_races")
async def save_races(req: SaveRacesRequest):
    preset = preset_store.load(req.preset_name)
    if not preset:
        return {"success": False, "detail": f"{req.preset_name} preset missing"}
    preset["extra_race_list"] = req.races
    preset_store.save(preset)
    return {"success": True}

@app.get("/api/presets")
async def get_presets():
    return {"success": True, "presets": preset_store.read_all()}

@app.post("/api/presets")
async def save_preset(req: SavePresetRequest):
    return {"success": True, "preset": preset_store.save(req.preset)}

@app.post("/api/presets/delete")
async def delete_preset(req: DeletePresetByNameRequest):
    return {"success": preset_store.delete(req.name)}

@app.get("/api/skills")
async def get_skills():
    current_skill_data = {}
    path = base_dir / 'data' / 'skill_data.json'
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            current_skill_data = json.load(f)
    return {"success": True, "skills": current_skill_data}

# ── Career start / resume ──────────────────────────────────────────────────
def start_career_from_request(req):
    global active_account, active_dashboard_data
    if not active_client:
        return {"success": False, "detail": "Not logged in"}

    if active_account and active_account.get("career") and active_account["career"].get("active"):
         return {"success": False, "detail": "Cannot start a new career while another is active"}

    if not req.friend_viewer_id or not req.friend_card_id:
        return {"success": False, "detail": "Friend support card is required"}
    
    selection_error = validate_start_selection(req)
    if selection_error:
        return {"success": False, "detail": selection_error}

    try:
        res = active_client.read_info()
        data = res.get('data', {})
        active_client.refresh_cached_account_state(data)
        update_start_state(data)
        active_account = get_account_status(data)
        if active_dashboard_data:
            active_dashboard_data["account"] = active_account
    except Exception as _ri_err:
        if "394" in str(_ri_err):
            return {"success": False, "detail": "Session expired (394) — please log in again", "session_expired": True}
        # Non-fatal: proceed with cached state

    # Re-check career state with fresh server data (catches stale local state)
    if active_account and active_account.get("career") and active_account["career"].get("active"):
        return {"success": False, "detail": "Server has an active career – use RESUME or delete it first"}

    if not active_start_state.get('tp_info'):
        return {"success": False, "detail": "Missing live TP state; login again before starting career"}
    if 'current_money' not in active_start_state:
        return {"success": False, "detail": "Missing live item state; login again before starting career"}

    tp_info = active_start_state['tp_info']
    current_tp = int(tp_info.get('current_tp') or 0)
    current_money = active_start_state['current_money']
    succession_rank_point = selected_succession_rank_point(req)

    # ── TP recovery (BEFORE pre_single_mode) ──────────────────────────────────
    # item/use_recovery_item works pre-career only BEFORE pre_single_mode is called.
    # Calling pre_single_mode first puts the session into a state where the
    # endpoint returns 102.  Match v6.3's ordering: potions → pre_single_mode → start.
    JEWEL_FLOOR = 1000
    _tp_mode = load_tp_recovery_mode()
    if req.use_tp and current_tp < req.use_tp:
        if _tp_mode in ("potion_first", "potion_only"):
            attempt = 0
            while current_tp < req.use_tp and attempt < 10:
                print(f"[tp] TP short ({current_tp}/{req.use_tp}), mode={_tp_mode} — trying potion (attempt {attempt+1})")
                try:
                    active_client.use_recovery_item(item_num=1)
                    tp_info = active_client.tp_info
                    active_start_state['tp_info'] = tp_info
                    new_tp = int((tp_info or {}).get('current_tp') or 0)
                    print(f"[tp] potion attempt {attempt+1}: {current_tp} -> {new_tp} TP")
                    if new_tp <= current_tp:
                        # TP didn't increase — no more potions available
                        print(f"[tp] TP unchanged after potion — no potions left")
                        break
                    current_tp = new_tp
                except Exception as e:
                    print(f"[tp] potion use failed: {e}")
                    if "394" in str(e):
                        return {"success": False, "detail": "Session expired (394) during TP recovery — please log in again", "session_expired": True}
                    break
                attempt += 1
        if _tp_mode == "potion_only" and current_tp < req.use_tp:
            return {"success": False, "detail": f"TP short ({current_tp}/{req.use_tp}) — items-only mode, not spending Jewels"}

    if req.use_tp and current_tp < req.use_tp:
        if _tp_mode == "potion_only":
            return {"success": False, "detail": f"TP short — items-only mode, not spending Jewels"}
        total_jewels = (active_client.coin_info.get("fcoin", 0)
                        + active_client.coin_info.get("coin", 0))
        if total_jewels < JEWEL_FLOOR:
            print(f"[tp] Jewels too low ({total_jewels} < {JEWEL_FLOOR}) — stopping loop to protect balance")
            return {"success": False, "detail": f"TP short ({current_tp}/{req.use_tp}) and Jewels below safety floor ({total_jewels}/{JEWEL_FLOOR})"}
        print(f"[tp] TP still short after items, spending Jewels ({total_jewels} available)")
        for attempt in range(3):
            try:
                needed = ((req.use_tp - current_tp) + 29) // 30
                active_client.recovery_tp(needed)
                tp_info = active_client.tp_info
                active_start_state['tp_info'] = tp_info
                current_tp = int(tp_info.get('current_tp') or 0)
                print(f"[tp] Jewel attempt {attempt+1}: TP now {current_tp}")
                if current_tp >= req.use_tp:
                    break
            except Exception as e:
                print(f"[tp] Jewel attempt {attempt+1} failed: {e}")
                if "213" in str(e):
                    try:
                        res = active_client.call("load/index", {"adid": ""})
                        active_client.refresh_cached_account_state(res.get("data", {}))
                        tp_info = active_client.tp_info
                        active_start_state['tp_info'] = tp_info
                        current_tp = int(tp_info.get('current_tp') or 0)
                    except Exception:
                        pass
                dna_sleep(1.0, 1.0)

    if req.use_tp and current_tp < req.use_tp:
        return {"success": False, "detail": f"Not enough TP: {current_tp}/{req.use_tp}"}
    # ── end TP recovery ────────────────────────────────────────────────────────

    try:
        pre_res = active_client.pre_single_mode([req.friend_viewer_id] if req.friend_viewer_id else [])
        # pre_single_mode returns friends' succession characters in
        # succession_trained_chara_data.  Merge these into active_parent_cards
        # so friend-owned parents survive across loop iterations without being
        # cleared by the validation step below.
        try:
            pre_data = pre_res.get("data") or {}
            stcd = pre_data.get("succession_trained_chara_data") or {}
            for chara in stcd.get("succession_trained_chara_array") or []:
                tid = chara.get("trained_chara_id")
                cid_raw = str(chara.get("card_id", ""))
                if tid and cid_raw.isdigit():
                    lineage = [int(cid_raw)] + [
                        int(sc.get("card_id"))
                        for sc in (chara.get("succession_chara_array") or [])
                        if sc.get("card_id")
                    ]
                    active_parent_cards[int(tid)] = lineage
                    active_parent_rank_points[int(tid)] = {
                        "rank": chara.get("rank", 0),
                        "rank_score": chara.get("rank_score", 0),
                    }
        except Exception as _pre_e:
            print(f"[career] pre_single_mode parent merge failed: {_pre_e}")
        dna_sleep(0.5, 1.5)
    except Exception as e:
        print("PRE_SINGLE_MODE ERROR:", e)

    # Validate veteran parent IDs against known active_parent_cards.
    # If any are missing after the pre_single_mode refresh above, try load/index
    # (covers own characters).  Only zero out an ID if still not found after
    # both refreshes — and never mutate req permanently; use local overrides
    # instead so subsequent loop iterations keep the original IDs.
    veteran_ids = [pid for pid in (int(req.parent_id_1 or 0), int(req.parent_id_2 or 0)) if pid]
    if veteran_ids and any(pid not in active_parent_cards for pid in veteran_ids):
        try:
            li_res = active_client.call("load/index", {"adid": ""})
            li_data = (li_res.get("data") or {})
            for chara in li_data.get("trained_chara") or []:
                tid = chara.get("trained_chara_id")
                cid_raw = str(chara.get("card_id", ""))
                if tid and cid_raw.isdigit():
                    lineage = [int(cid_raw)] + [int(sc.get("card_id")) for sc in chara.get("succession_chara_array") or [] if sc.get("card_id")]
                    active_parent_cards[int(tid)] = lineage
                    active_parent_rank_points[int(tid)] = {"rank": chara.get("rank", 0), "rank_score": chara.get("rank_score", 0)}
            print("[career] refreshed parent list from load/index")
        except Exception as _e:
            print(f"[career] parent refresh failed: {_e}")
    # Use local variables rather than mutating req — this preserves the original
    # IDs for subsequent loop iterations even when a parent can't be found.
    _parent_id_1 = int(req.parent_id_1 or 0)
    _parent_id_2 = int(req.parent_id_2 or 0)
    for _attr, _pid_ref in (("parent_id_1", _parent_id_1), ("parent_id_2", _parent_id_2)):
        if _pid_ref and _pid_ref not in active_parent_cards:
            print(f"[career] parent {_pid_ref} not found after refresh — skipping for this run")
            if _attr == "parent_id_1":
                _parent_id_1 = 0
            else:
                _parent_id_2 = 0

    # Resolve human-readable names for debug logging
    def _resolve_name(card_id_str):
        return chara_map.get(str(card_id_str), f"Unknown ({card_id_str})")
    trainee_name = _resolve_name(req.card_id)
    parent1_cards = active_parent_cards.get(int(req.parent_id_1), [])
    parent2_cards = active_parent_cards.get(int(req.parent_id_2), [])
    parent1_name = _resolve_name(parent1_cards[0]) if parent1_cards else f"Unknown (id={req.parent_id_1})"
    parent2_name = _resolve_name(parent2_cards[0]) if parent2_cards else f"Unknown (id={req.parent_id_2})"
    print(f"[career] start: {trainee_name} | p1={parent1_name} p2={parent2_name} rental={req.rental_trained_chara_id} deck={req.deck_id}")

    # ── Rebuild active_selection so portraits survive server restarts ──────────
    try:
        sel_cards = []
        for sid in (req.support_card_ids or []):
            s = str(sid)
            info = support_map.get(s, {})
            sel_cards.append({'id': s, 'name': info.get('name', f'Unknown ({s})'),
                              'rarity': info.get('rarity', '?'),
                              'type': display_support_type(info.get('type', 0))})
        active_selection['deck'] = {'id': req.deck_id, 'name': f'Deck {req.deck_id}', 'cards': sel_cards}
        if req.friend_card_id:
            s = str(req.friend_card_id)
            info = support_map.get(s, {})
            active_selection['friend'] = {'support_card_id': s, 'support_name': info.get('name', f'Unknown ({s})')}
        vets = []
        for pid in [req.parent_id_1, req.parent_id_2]:
            try:
                if pid:
                    lin = active_parent_cards.get(int(pid), [])
                    if lin:
                        cid = str(lin[0])
                        vets.append({'card_id': cid, 'name': chara_map.get(cid, f'Unknown ({cid})')})
            except (TypeError, ValueError):
                pass
        active_selection['veterans'] = vets
        try:
            if req.rental_trained_chara_id:
                lin = active_parent_cards.get(int(req.rental_trained_chara_id), [])
                if lin:
                    cid = str(lin[0])
                    active_selection['guestParent'] = {'card_id': cid, 'name': chara_map.get(cid, f'Unknown ({cid})')}
        except (TypeError, ValueError):
            pass
    except Exception as _sel_e:
        print(f"[career] selection rebuild skipped: {_sel_e}")

    result = active_client.start_career(
        card_id=req.card_id,
        support_card_ids=req.support_card_ids,
        friend_viewer_id=req.friend_viewer_id,
        friend_card_id=req.friend_card_id,
        parent_id_1=_parent_id_1,
        parent_id_2=_parent_id_2,
        rental_viewer_id=req.rental_viewer_id,
        rental_trained_chara_id=req.rental_trained_chara_id,
        scenario_id=req.scenario_id,
        deck_id=req.deck_id,
        use_tp=req.use_tp,
        tp_info=tp_info,
        current_money=current_money,
        succession_rank_point=succession_rank_point,
        difficulty_id=req.difficulty_id,
        difficulty=req.difficulty,
        is_boost=req.is_boost,
        boost_story_event_id=req.boost_story_event_id,
    )

    return {"success": True, "result": result}

def apply_career_result(result):
    global active_account, active_dashboard_data, active_parent_cards, active_parent_rank_points
    result_data = result.get('data', {})
    update_start_state(result_data)
    account = get_account_status(result_data, result)
    chara_info = result_data.get('chara_info') or {}

    # Refresh parent cache from start response succession data so newly created
    # veterans are immediately available for the next loop iteration without
    # requiring a manual /api/parents reload.
    try:
        stcd = result_data.get('succession_trained_chara_data') or {}
        for chara in stcd.get('succession_trained_chara_array') or []:
            tid = chara.get('trained_chara_id')
            cid_raw = str(chara.get('card_id', ''))
            if tid and cid_raw.isdigit():
                lineage = [int(cid_raw)] + [
                    int(sc.get('card_id')) for sc in (chara.get('succession_chara_array') or [])
                    if sc.get('card_id')
                ]
                active_parent_cards[int(tid)] = lineage
                active_parent_rank_points[int(tid)] = {
                    'rank': chara.get('rank', 0),
                    'rank_score': chara.get('rank_score', 0)
                }
    except Exception as _e:
        print(f"[apply_career_result] parent cache refresh skipped: {_e}")
    if chara_info:
        account["career"] = account.get("career") or {}
        card_id = str(chara_info.get('card_id', account["career"].get("card_id", '')))
        account["career"].update({
            "active": True,
            "card_id": card_id,
            "name": chara_map.get(card_id, f"Unknown ({card_id})"),
            "turn": chara_info.get('turn', 0),
            "scenario_id": chara_info.get('scenario_id', 0),
            "fans": chara_info.get('fans', 0),
            "vital": chara_info.get('vital', 0),
            "max_vital": chara_info.get('max_vital', 0),
            # Aptitude fields — used by solver when no live chara_info is passed
            "proper_ground_turf":     chara_info.get('proper_ground_turf', 8),
            "proper_ground_dirt":     chara_info.get('proper_ground_dirt', 1),
            "proper_distance_short":  chara_info.get('proper_distance_short', 8),
            "proper_distance_mile":   chara_info.get('proper_distance_mile', 8),
            "proper_distance_middle": chara_info.get('proper_distance_middle', 8),
            "proper_distance_long":   chara_info.get('proper_distance_long', 8),
        })
    active_account = account
    if active_dashboard_data:
        active_dashboard_data["account"] = account
    return account, chara_info

@app.post("/api/login")
async def login(req: LoginRequest):
    from uma_api.client import UmaClient, get_ticket
    from career_bot.delay import GateKeeper
    global active_client, active_account, active_dashboard_data, active_start_state, active_parent_cards, active_parent_rank_points, pending_game_auth_config, raw_load_index_response, active_selection
    try:
        chara = None
        cfg = dict(pending_game_auth_config)
        pending_game_auth_config = {}
        # If no fresh Frida capture, try the saved auth cache to skip Steam API
        if not has_fresh_auth_config(cfg):
            cached = _load_auth_cache()
            if has_fresh_auth_config(cached):
                cfg = cached
                print("[auth_cache] using cached auth — skipping Steam API call")

        active_client = None
        active_account = None
        active_dashboard_data = None
        active_start_state = {}
        active_parent_cards = {}
        active_parent_rank_points = {}
        raw_load_index_response = None
        active_selection = {
            "deck": None,
            "friend": None,
            "trainee": None,
            "veterans": [],
            "guestParent": None
        }

        # Use Steam credentials from: (1) form submit, (2) cached config, (3) get_ticket()
        # Only call get_ticket() — which triggers Steam Guard 2FA — if we have no other source.
        if req.steam_id and req.steam_session_ticket:
            cfg['steam_id'] = str(req.steam_id)
            cfg['steam_session_ticket'] = str(req.steam_session_ticket)
        elif not (cfg.get('steam_id') and cfg.get('steam_session_ticket')):
            # No cached steam creds — must call Steam API (may trigger 2FA)
            if not (req.username and req.password):
                raise Exception('Steam credentials required')
            sid, tkt = get_ticket(req.username, req.password, req.code)
            cfg['steam_id'] = sid
            cfg['steam_session_ticket'] = tkt
        else:
            print("[auth_cache] reusing cached Steam credentials — skipping Steam API call")
        cfg['steam_password_seed'] = req.password
        if not has_fresh_auth_config(cfg):
            import threading
            threading.Thread(target=refresh_auth_before_serving, daemon=True).start()
            return {"success": False, "detail": "Game launching for auth capture — log in to the target account, then click LOGIN again.", "needs_auth_capture": True}

        c = UmaClient(cfg, trace_enabled=False)
        gated_client = GateKeeper(c)
        res = gated_client.login()
        if not res:
            raise HTTPException(status_code=401, detail="Game login failed")
        active_client = gated_client
        _save_auth_cache(cfg)  # persist for next restart

        d = res.get('data', {})
        career_data = None
        if d.get('single_mode_chara_light') or d.get('single_mode_chara'):
            try:
                career_res = active_client.load_career()
                career_data = career_res.get('data')
            except Exception:
                pass
        
        account = get_account_status(d, career_data)
        active_account = account
        active_start_state = {}
        active_parent_cards = {}
        active_parent_rank_points = {}
        update_start_state(d)
        
        umas = []
        card_list = d.get('card_list', [])
        for card in card_list:
            cid = str(card.get('card_id', card.get('id', '')))
            umas.append({
                'id': cid, 
                'name': chara_map.get(cid, f"Unknown ({cid})")
            })
            
        decks = []
        deck_array = d.get('support_card_deck_array', [])
        for deck in deck_array:
            cards = []
            for cid in deck.get('support_card_id_array', []):
                sid = str(cid)
                info = support_map.get(sid)
                if info:
                    cards.append({
                        'id': sid,
                        'name': info['name'],
                        'rarity': info['rarity'],
                        'type': display_support_type(info['type'])
                    })
                else:
                    cards.append({'id': sid, 'name': f'Unknown ({sid})', 'rarity': '?', 'type': '?'})
            
            decks.append({
                'id': deck.get('deck_id'),
                'name': deck.get('name', f'Deck {deck.get("deck_id")}'),
                'cards': cards
            })

        parents = []
        trained_chara_list = d.get('trained_chara', [])
        for chara in trained_chara_list:


            raw_id = str(chara.get('card_id', ''))

            if '{' in raw_id or '-' in raw_id or not raw_id.isdigit():
                found = False
                for key, val in chara.items():
                    val_str = str(val)
                    if val_str.isdigit() and len(val_str) >= 4:
                        raw_id = val_str
                        found = True
                        break
                if not found:
                    continue
            
            cid = raw_id

            tree = {
                "self": {"card_id": cid, "name": chara_map.get(cid, f"Unknown ({cid})"), "factors": [], "wins": get_win_summary(chara.get('win_saddle_id_array', []))},
                "p1": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "p2": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "gp1": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "gp2": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "gp3": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "gp4": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])}
            }
            
            tree["self"]["factors"] = get_factors(get_chara_factor_ids(chara), cid)

            for sc in chara.get('succession_chara_array', []):
                pos = sc.get('position_id')
                sc_cid = sc.get('card_id', 0)
                key = ""
                if pos == 10: key = "p1"
                elif pos == 20: key = "p2"
                elif pos == 11: key = "gp1"
                elif pos == 12: key = "gp2"
                elif pos == 21: key = "gp3"
                elif pos == 22: key = "gp4"
                
                if key:
                    tree[key]["card_id"] = sc_cid
                    tree[key]["name"] = chara_map.get(str(sc_cid), f"Unknown ({sc_cid})")
                    tree[key]["factors"] = get_factors(sc.get('factor_id_array', []), sc_cid)
                    tree[key]["wins"] = get_win_summary(sc.get('win_saddle_id_array', []))


            parents.append({
                'instance_id': chara.get('trained_chara_id'),
                'card_id': cid,
                'name': chara_map.get(cid, f"Unknown ({cid})"),
                'rank': chara.get('rank', 0),
                'tree': tree
            })
            lineage_cards = [int(cid)]
            for sc in chara.get('succession_chara_array', []) or []:
                sc_cid = sc.get('card_id', 0)
                if sc_cid:
                    lineage_cards.append(int(sc_cid))
            active_parent_cards[int(chara.get('trained_chara_id'))] = lineage_cards
            active_parent_rank_points[int(chara.get('trained_chara_id'))] = {
                'rank': chara.get('rank', 0),
                'rank_score': chara.get('rank_score', 0)
            }

        friend_support_data = d.get('friend_support_card_data', {})

        for info in friend_support_data.get('summary_user_info_array', []):
            utc = info.get('user_trained_chara')
            if not utc:
                continue

            cid = str(utc.get('card_id', 0))

            parents.append({
                'instance_id': utc.get('trained_chara_id'),
                'card_id': cid,
                'name': f"[GUEST] {info.get('name', 'Unknown')}",
                'rank': utc.get('rank', 0),
                'is_guest': True,
                'guest_viewer_id': info.get('viewer_id', 0),
                'tree': {
                    "self": {
                        "card_id": cid,
                            "name": info.get('name', 'Unknown'),
                            "factors": [],
                            "wins": get_win_summary([])
                    },
                    "p1": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                    "p2": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                    "gp1": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                    "gp2": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                    "gp3": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                    "gp4": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])}
            }
        })

        
        active_dashboard_data = {
            "success": True,
            "account": account,
            "umas": umas,
            "decks": decks,
            "parents": parents
        }
        return active_dashboard_data
    except Exception as e:
        msg = str(e)
        if "STEAM_GUARD_REQUIRED" in msg:
             pending_game_auth_config = cfg
             return {"success": False, "needs_2fa": True}
        if "394" in msg:
            # Cached credentials are rejected by the server — wipe the cache
            # and immediately kick off a fresh Frida capture in the background.
            _clear_auth_cache()
            import threading
            threading.Thread(target=refresh_auth_before_serving, daemon=True).start()
            return {"success": False, "detail": "Session expired — game launching for fresh auth capture. Log in to the target account, then click LOGIN again.", "session_expired": True, "needs_auth_capture": True}
        return {"success": False, "detail": str(e)}

@app.get("/api/session")
async def session_status():
    global active_client, active_dashboard_data, active_account, active_selection
    if not active_client or not active_dashboard_data:
        return {"success": False}
    
    data = dict(active_dashboard_data)
    if active_account:
        data["account"] = active_account
    data["selection"] = active_selection
    data["success"] = True
    return data

class UISelectionRequest(BaseModel):
    selection: dict

@app.post("/api/selection")
async def update_selection(req: UISelectionRequest):
    global active_selection
    active_selection = req.selection
    return {"success": True}

@app.post("/api/logout")
async def logout():
    global active_client, active_account, active_dashboard_data, active_start_state, active_parent_cards, active_parent_rank_points, raw_load_index_response, pending_game_auth_config, active_selection, _cached_circle_info, _circle_refresh_needed
    _cached_circle_info = None
    _circle_refresh_needed = True
    active_client = None
    active_account = None
    active_dashboard_data = None
    active_start_state = {}
    active_parent_cards = {}
    active_parent_rank_points = {}
    raw_load_index_response = None
    pending_game_auth_config = {}
    active_selection = {
        "deck": None,
        "friend": None,
        "trainee": None,
        "veterans": [],
        "guestParent": None
    }
    return {"success": True}

def _build_parent_entry(chara, owner_name):
    """Build a parent dict from a trained_chara object."""
    raw_id = str(chara.get('card_id', ''))
    if not raw_id.isdigit():
        return None
    cid = raw_id
    tree = {
        "self": {"card_id": cid, "name": chara_map.get(cid, f"Unknown ({cid})"), "factors": get_factors(get_chara_factor_ids(chara), cid), "wins": get_win_summary(chara.get('win_saddle_id_array', []))},
        "p1":  {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
        "p2":  {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
        "gp1": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
        "gp2": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
        "gp3": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
        "gp4": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
    }
    pos_map = {10: "p1", 20: "p2", 11: "gp1", 12: "gp2", 21: "gp3", 22: "gp4"}
    for sc in chara.get('succession_chara_array') or []:
        key = pos_map.get(sc.get('position_id'))
        sc_cid = sc.get('card_id', 0)
        if key:
            tree[key] = {"card_id": sc_cid, "name": chara_map.get(str(sc_cid), f"Unknown ({sc_cid})"), "factors": get_factors(sc.get('factor_id_array', []), sc_cid), "wins": get_win_summary(sc.get('win_saddle_id_array', []))}
    instance_id = chara.get('trained_chara_id')
    lineage_cards = [int(cid)] + [int(sc.get('card_id')) for sc in chara.get('succession_chara_array') or [] if sc.get('card_id')]
    active_parent_cards[int(instance_id)] = lineage_cards
    active_parent_rank_points[int(instance_id)] = {'rank': chara.get('rank', 0), 'rank_score': chara.get('rank_score', 0)}
    return {'instance_id': instance_id, 'card_id': cid, 'name': chara_map.get(cid, f"Unknown ({cid})"), 'owner_name': owner_name, 'rank': chara.get('rank', 0), 'tree': tree}

@app.get("/api/follow/parents")
async def get_follow_parents():
    if not active_client:
        raise HTTPException(status_code=401, detail="Not logged in")
    try:
        # pre_single_mode/index already returns followed players' succession umas
        # in succession_trained_chara_data — no separate follow endpoint needed
        result = active_client.pre_single_mode([])
        data = result.get('data', {})

        stcd = data.get('succession_trained_chara_data', {})
        chara_array = stcd.get('succession_trained_chara_array', [])
        user_array = stcd.get('summary_user_info_array', [])

        user_map = {u['viewer_id']: u.get('name', f'Player {u["viewer_id"]}') for u in user_array}

        all_parents = []
        for chara in chara_array:
            trained_chara_id = chara.get('trained_chara_id')
            if not trained_chara_id:
                continue

            card_id = str(chara.get('card_id', ''))
            rank_num = int(chara.get('rank') or 0)
            rank_score = int(chara.get('rank_score') or 0)
            owner_viewer_id = chara.get('viewer_id', 0)
            owner_name = user_map.get(owner_viewer_id, f'Player {owner_viewer_id}')

            tree = {
                "self": {"card_id": card_id, "name": chara_map.get(card_id, f"Unknown ({card_id})"), "factors": get_factors(get_chara_factor_ids(chara), card_id), "wins": get_win_summary(chara.get('win_saddle_id_array', []))},
                "p1":  {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "p2":  {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "gp1": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "gp2": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "gp3": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])},
                "gp4": {"card_id": 0, "name": "", "factors": [], "wins": get_win_summary([])}
            }
            for sc in chara.get('succession_chara_array', []):
                pos = sc.get('position_id')
                sc_cid = sc.get('card_id', 0)
                key = {10: "p1", 20: "p2", 11: "gp1", 12: "gp2", 21: "gp3", 22: "gp4"}.get(pos)
                if key:
                    tree[key] = {
                        "card_id": sc_cid,
                        "name": chara_map.get(str(sc_cid), f"Unknown ({sc_cid})"),
                        "factors": get_factors(sc.get('factor_id_array', []), sc_cid),
                        "wins": get_win_summary(sc.get('win_saddle_id_array', []))
                    }

            lineage_cards = [int(card_id)] if card_id.isdigit() else []
            for sc in chara.get('succession_chara_array', []) or []:
                sc_cid = sc.get('card_id', 0)
                if sc_cid:
                    lineage_cards.append(int(sc_cid))
            active_parent_cards[int(trained_chara_id)] = lineage_cards
            active_parent_rank_points[int(trained_chara_id)] = {'rank': rank_num, 'rank_score': rank_score}

            all_parents.append({
                'instance_id': trained_chara_id,
                'card_id': card_id,
                'name': f"[{owner_name}] {chara_map.get(card_id, f'Unknown ({card_id})')}",
                'rank': rank_num,
                'tree': tree,
                'owner_viewer_id': owner_viewer_id,
                'owner_name': owner_name,
                'from_follow': True,
            })

        return {'success': True, 'parents': all_parents}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── REST API: career lifecycle ──────────────────────────────────────────────
@app.post("/api/career/start")
async def start_career(req: StartCareerRequest):
    try:
        started = start_career_from_request(req)
        if not started.get("success"):
            return started
        account, chara_info = apply_career_result(started["result"])
        return {"success": True, "account": account, "chara_info": chara_info}
    except Exception as e:
        return {"success": False, "detail": str(e)}

backend_loop_thread = None
backend_loop_stop = False

# ── LOOP mode: managed career loop (dev/continuous mode) ───────────────────
# Runs in a background thread when LOOP: ON is enabled.
# Automatically retries / starts new careers until stopped.
def manage_career_loop(req, preset, initial_result):
    global backend_loop_stop, active_account, active_client
    max_steps = max(1, min(int(req.max_steps or 2500), 3000))
    consecutive_fails = 0
    current_result = initial_result
    
    while not backend_loop_stop:
        career_runner.start(active_client, preset, current_result, max_steps, burn_clocks=req.burn_clocks, dev_mode=req.dev_mode)
        
        while career_runner.snapshot().get("running"):
            if backend_loop_stop:
                career_runner.stop()
                return
            dna_sleep(1.0, 1.0)
            
        status = career_runner.snapshot()
        if status.get("finished"):
            try:
                _fans_end = int(status.get("final_fans") or 0)
                _card_id  = status.get("final_card_id") or (
                    active_account["career"].get("card_id") if active_account and active_account.get("career") else "")
                _fans_start = int((active_account or {}).get("career", {}).get("fans") or 0) if _fans_end else 0
                _fans_gained = max(0, _fans_end - _fans_start)
                if _fans_end > 0:
                    record_career_fans(
                        card_id=_card_id,
                        fans_gained=_fans_gained,
                        final_fans=_fans_end,
                        preset_name=preset.get("name", ""),
                        final_turn=int(status.get("turn") or 0),
                        running_style=int(preset.get("running_style") or 0),
                        final_stats=status.get("final_stats") or {},
                    )
            except Exception as _e:
                print(f"fan_stats record error: {_e}")
        # Always clear career.active once the runner stops, whether the career
        # finished cleanly, errored out, or was stopped by the user.
        if active_account and "career" in active_account and active_account["career"]:
            active_account["career"]["active"] = False

        if status.get("last_error"):
            consecutive_fails += 1
            if consecutive_fails >= 3:
                break
        else:
            consecutive_fails = 0

        if not req.dev_mode:
            break
            
        # Wait between careers — gives the server time to clear the previous
        # career slot before we attempt to open a new one (avoids 501 rejections).
        for _ in range(30):
            if backend_loop_stop:
                return
            dna_sleep(1.0, 1.0)

        started_ok = False
        while not started_ok and not backend_loop_stop:
            try:
                started = start_career_from_request(req)
                if not started.get("success"):
                    if started.get("session_expired"):
                        print(f"[loop] session expired (394) — stopping loop, re-login required")
                        backend_loop_stop = True
                        return
                    consecutive_fails += 1
                    if consecutive_fails >= 5:
                        break
                    for _ in range(15):
                        if backend_loop_stop:
                            return
                        dna_sleep(1.0, 1.0)
                    continue
                current_result = started["result"]
                account, chara_info = apply_career_result(current_result)
                active_account = account
                started_ok = True
                consecutive_fails = 0
            except Exception as e:
                err_str = str(e)
                # 501 = server hard-refuses new career (slot locked, daily limit,
                # rental expired, etc.).  No point retrying immediately — stop the
                # loop and let the user investigate.
                if "501" in err_str:
                    print(f"[loop] 501 on career start — stopping loop: {e}")
                    backend_loop_stop = True
                    return
                consecutive_fails += 1
                if consecutive_fails >= 5:
                    break
                for _ in range(15):
                    if backend_loop_stop:
                        return
                    dna_sleep(1.0, 1.0)

        if not started_ok:
            break

@app.post("/api/career/run")
async def run_career(req: RunCareerRequest):
    global active_account, backend_loop_thread, backend_loop_stop
    runner_active = career_runner.snapshot().get("running")
    loop_alive = backend_loop_thread and backend_loop_thread.is_alive()
    if runner_active:
        return {"success": False, "detail": "Career runner loop already active"}
    if loop_alive:
        # Loop thread is alive but the runner is not processing a career turn —
        # it's stuck in a retry-wait. Signal it to stop and let the user take over.
        print("[career/run] stopping idle loop thread so manual start can proceed")
        backend_loop_stop = True
        backend_loop_thread.join(timeout=5)
        backend_loop_stop = False
    preset_name = req.preset_name or "xguri parent"
    preset = preset_store.load(preset_name)
    if not preset:
        return {"success": False, "detail": f"{preset_name} preset missing"}
    
    try:
        # Always do a fresh server check — the in-memory career.active flag may
        # have been cleared by the runner stopping, even if the career is still
        # live on the server (e.g. user stopped bot mid-run to change preset).
        load_data = {}
        try:
            index_result = active_client.call('load/index')
            load_data = index_result.get('data', {})
            update_start_state(load_data)
            account = get_account_status(load_data)
            active_account = account
        except Exception as _e:
            print(f"[career/run] load/index failed: {_e}")
            account = active_account or {}
        career = account.get("career") or {}

        # load/index doesn't always include single_mode_chara_light even when a career
        # is mid-run (e.g. bot stopped between turns). Try load_career directly as a
        # second check before falling through to start_career_from_request.
        if not career.get("active"):
            try:
                probe = active_client.load_career()
                if (probe.get('data') or {}).get('chara_info'):
                    print("[career/run] load/index missed active career — detected via load_career probe")
                    account = get_account_status(load_data, probe)
                    active_account = account
                    career = account.get("career") or {}
            except Exception as _probe_e:
                print(f"[career/run] load_career probe: {_probe_e}")

        _need_fresh_start = not career.get("active")
        if career.get("active"):
            try:
                career_result = active_client.load_career()
                career_data = career_result.get('data', {})
                account = get_account_status(load_data, career_result)
                active_account = account
                career_status = account.get("career")
                req.card_id = int(career_status.get("card_id"))
                req.support_card_ids = career_status.get("support_card_ids")
                req.friend_viewer_id = int(career_status.get("friend_viewer_id"))
                req.friend_card_id = int(career_status.get("friend_card_id"))
                req.parent_id_1 = int(career_status.get("parent_id_1"))
                req.parent_id_2 = int(career_status.get("parent_id_2"))
                req.deck_id = int(career_status.get("deck_id"))
                req.scenario_id = int(career_status.get("scenario_id") or preset.get("scenario_id", 4))
                # Rebuild active_selection from the resumed career so portraits show in monitor tab
                try:
                    sel_cards = []
                    for sid in (req.support_card_ids or []):
                        s = str(sid)
                        info = support_map.get(s, {})
                        sel_cards.append({'id': s, 'name': info.get('name', f'Unknown ({s})'),
                                          'rarity': info.get('rarity', '?'),
                                          'type': display_support_type(info.get('type', 0))})
                    active_selection['deck'] = {'id': req.deck_id, 'name': f'Deck {req.deck_id}', 'cards': sel_cards}
                    fid = career_status.get("friend_card_id")
                    if fid:
                        s = str(fid)
                        info = support_map.get(s, {})
                        active_selection['friend'] = {'support_card_id': s, 'support_name': info.get('name', f'Unknown ({s})')}
                    vets = []
                    for pid in [career_status.get("parent_id_1"), career_status.get("parent_id_2")]:
                        if pid:
                            lin = active_parent_cards.get(int(pid), [])
                            if lin:
                                cid = str(lin[0])
                                vets.append({'card_id': cid, 'name': chara_map.get(cid, f'Unknown ({cid})')})
                    active_selection['veterans'] = vets
                except Exception as _sel_e:
                    print(f"[career/run] selection rebuild skipped: {_sel_e}")
                chara_info = career_data.get('chara_info') or {}
                if active_dashboard_data:
                    active_dashboard_data["account"] = account
                result = career_result
            except Exception as _load_e:
                if "394" in str(_load_e):
                    print(f"[career/run] load_career 394 — career already gone on server, starting fresh")
                    _need_fresh_start = True
                else:
                    raise
        if _need_fresh_start:
            if not req.scenario_id:
                req.scenario_id = int(preset.get("scenario_id", 4))
            started = start_career_from_request(req)
            if not started.get("success"):
                return started
            result = started["result"]
            account, chara_info = apply_career_result(result)

        apply_deck_type_counts(preset, req=req, chara_info=chara_info)
        
        # Always run through manage_career_loop so fan stats are recorded on
        # completion regardless of mode.  manage_career_loop already breaks after
        # the first career when dev_mode is False (non-LOOP mode).
        backend_loop_stop = False
        backend_loop_thread = threading.Thread(target=manage_career_loop, args=(req, preset, result), daemon=True)
        backend_loop_thread.start()
        dna_sleep(0.5, 0.5)
            
        return {"success": True, "account": account, "chara_info": chara_info, "runner": career_runner.snapshot()}
    except Exception as e:
        return {"success": False, "detail": str(e)}

@app.get("/api/career/runner")
async def career_runner_status():
    # Include fresh account + selection so diagnostics card always shows current run.
    snap = career_runner.snapshot()
    account = active_account
    # If the runner is actively processing turns, guarantee career.active=True so
    # the monitor tab never shows "No active career" during a live run.
    if snap.get("running") and account:
        career = account.get("career")
        if career and not career.get("active"):
            career["active"] = True

    # If selection portraits are missing but a career is loaded, rebuild from
    # career data so portraits appear without needing a server restart + resume.
    if not active_selection.get("deck") and account:
        career = account.get("career") or {}
        sup_ids = career.get("support_card_ids") or []
        if sup_ids:
            try:
                sel_cards = []
                for sid in sup_ids:
                    s = str(sid)
                    info = support_map.get(s, {})
                    sel_cards.append({'id': s, 'name': info.get('name', f'Unknown ({s})'),
                                      'rarity': info.get('rarity', '?'),
                                      'type': display_support_type(info.get('type', 0))})
                active_selection['deck'] = {'id': career.get('deck_id', 0), 'name': 'Active Deck', 'cards': sel_cards}
                fid = career.get('friend_card_id')
                if fid:
                    s = str(fid)
                    info = support_map.get(s, {})
                    active_selection['friend'] = {'support_card_id': s, 'support_name': info.get('name', f'Unknown ({s})')}
                vets = []
                for pid in [career.get('parent_id_1'), career.get('parent_id_2')]:
                    if pid:
                        lin = active_parent_cards.get(int(pid), [])
                        if lin:
                            cid = str(lin[0])
                            vets.append({'card_id': cid, 'name': chara_map.get(cid, f'Unknown ({cid})')})
                active_selection['veterans'] = vets
            except Exception:
                pass

    return {
        "success": True,
        "runner": snap,
        "account": account,
        "selection": active_selection,
    }

@app.post("/api/career/runner/stop")
async def stop_career_runner():
    global backend_loop_stop
    backend_loop_stop = True
    career_runner.stop()
    return {"success": True, "runner": career_runner.snapshot()}

class BurnClocksRequest(BaseModel):
    burn_clocks: bool

@app.post("/api/career/runner/burn_clocks")
async def set_burn_clocks(req: BurnClocksRequest):
    career_runner.set_burn_clocks(req.burn_clocks)
    return {"success": True, "runner": career_runner.snapshot()}

@app.post("/api/career/friends")
async def get_friend_list(req: FriendListRequest):
    global active_client, active_dashboard_data, active_account
    if not active_client:
        return {"success": False, "detail": "Not logged in"}

    if active_account and active_account.get("career") and active_account["career"].get("active"):
        return {
            "success": True,
            "friends": [],
            "exclude_viewer_ids": [],
            "source": "Active Career (Skip)"
        }

    if not req.exclude_viewer_ids and active_dashboard_data is not None and "friends" in active_dashboard_data:
        return {
            "success": True,
            "friends": active_dashboard_data["friends"],
            "exclude_viewer_ids": active_dashboard_data.get("friendExcludeIds", []),
            "source": "cache"
        }

    try:
        result = active_client.pre_single_mode(req.exclude_viewer_ids)
        data = result.get('data', {})

        import json
        with open("debug_pre_single_mode.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        update_start_state(data)
        friends, exclude_viewer_ids, source = normalize_friend_cards(data)

        if active_dashboard_data is not None:
            active_dashboard_data["friends"] = friends
            active_dashboard_data["friendExcludeIds"] = exclude_viewer_ids
            active_dashboard_data["friendsLoaded"] = True

        return {
            "success": True,
            "friends": friends,
            "exclude_viewer_ids": exclude_viewer_ids,
            "source": source
        }
    
    except Exception as e:
        return {"success": False, "detail": str(e)}

@app.post("/api/career/action")
async def career_action(req: CareerActionRequest):
    global active_client, active_account
    if not active_client:
        return {"success": False, "detail": "Not logged in"}
    
    try:
        result = active_client.exec_command(
            command_type=req.command_type,
            command_id=req.command_id,
            current_turn=req.current_turn,
            current_vital=req.current_vital,
            command_group_id=req.command_group_id,
            select_id=req.select_id
        )
        
        data = result.get('data', {})
        return {
            "success": True,
            "chara_info": data.get('chara_info', {}),
            "command_result": data.get('command_result', {})
        }
    except Exception as e:
        return {"success": False, "detail": str(e)}

@app.post("/api/career/delete")
async def delete_career(req: DeleteCareerRequest):
    global active_client, active_account, active_dashboard_data, backend_loop_thread
    if not active_client:
        return {"success": False, "detail": "Not logged in"}
    if career_runner.snapshot().get("running") or (backend_loop_thread and backend_loop_thread.is_alive()):
        return {"success": False, "detail": "Cannot delete career while runner is active"}

    try:
        account = active_account or {}
        career = account.get("career") or {}
        if not career.get("active"):
            load_result = active_client.call('load/index')
            load_data = load_result.get('data', {})
            update_start_state(load_data)
            account = get_account_status(load_data)
            active_account = account
            career = account.get("career") or {}
        current_turn = req.current_turn or career.get("turn", 0) or 1
        if not career.get("active") and not req.current_turn:
            return {"success": False, "detail": "No active career"}
        active_client.finish_career(current_turn=current_turn, is_force_delete=True)
        account["career"] = None
        active_account = account
        if active_dashboard_data:
            active_dashboard_data["account"] = account
        return {"success": True, "account": account}
    except Exception as e:
        return {"success": False, "detail": str(e)}

@app.get("/api/settings/tp-recovery")
async def get_tp_recovery_settings():
    mode = load_tp_recovery_mode()
    potions = None
    if active_client is not None:
        try:
            potions = active_client.tp_potion_count()
        except Exception:
            potions = None
    return {
        "success": True,
        "mode": mode,
        "label": tp_recovery_label(mode),
        "modes": list(TP_RECOVERY_MODES),
        "potions": potions,
    }

class TpRecoveryRequest(BaseModel):
    mode: str = "jewels_only"

@app.post("/api/settings/tp-recovery")
async def set_tp_recovery_settings(req: TpRecoveryRequest):
    mode = set_tp_recovery_mode(req.mode)
    return {"success": True, "mode": mode, "label": tp_recovery_label(mode)}

@app.post("/api/career/give-up")
async def give_up_career():
    """
    Permanently abandon the active career (equivalent to in-game Give Up).

    If the bot runner is currently active, it is stopped first; then
    single_mode_free/finish is called with is_force_delete=True to cancel
    the career on the server.  The active career state is cleared afterwards.
    """
    global active_client, active_account, active_dashboard_data, backend_loop_thread
    if not active_client:
        return {"success": False, "detail": "Not logged in"}

    # 1. Stop the runner if it is running and wait for it to exit
    snap = career_runner.snapshot()
    if snap.get("running"):
        career_runner.stop()
        if career_runner.thread and career_runner.thread.is_alive():
            career_runner.thread.join(timeout=12)

    try:
        account = active_account or {}
        career = account.get("career") or {}
        current_turn = snap.get("turn") or career.get("turn") or 1

        # 2. Refresh career state from server if needed
        if not career.get("active"):
            load_result = active_client.call('load/index')
            load_data = load_result.get('data', {})
            update_start_state(load_data)
            account = get_account_status(load_data)
            active_account = account
            career = account.get("career") or {}
            current_turn = career.get("turn") or current_turn

        if not career.get("active"):
            return {"success": False, "detail": "No active career to give up"}

        # 3. Cancel the career on the server (is_force_delete=True = give up)
        active_client.finish_career(current_turn=int(current_turn), is_force_delete=True)

        account["career"] = None
        active_account = account
        if active_dashboard_data:
            active_dashboard_data["account"] = account

        print(f"[give-up] Career abandoned at turn {current_turn}")
        return {"success": True, "account": account}
    except Exception as e:
        return {"success": False, "detail": str(e)}


@app.get("/api/debug/start_state")
async def get_start_state():
    return active_start_state

@app.get("/api/status/items")
async def get_item_status():
    """Returns all items the user currently owns (from cached item_map after login/load)."""
    if not active_client:
        return {"error": "not logged in"}
    from career_bot.items import ITEM_NAMES, TP_RESTORE_ITEMS
    items = []
    for item_id, count in sorted(active_client.item_map.items()):
        if count > 0:
            items.append({
                "item_id": item_id,
                "name": ITEM_NAMES.get(item_id, f"Unknown ({item_id})"),
                "count": count,
                "is_tp_item": item_id in TP_RESTORE_ITEMS,
                "tp_per_use": TP_RESTORE_ITEMS.get(item_id),
            })
    return {"items": items, "total_unique": len(items)}

@app.get("/api/debug/events")
async def debug_events():
    """Fetch load/index and return any keys that look event/boost/difficulty related."""
    if not active_client:
        return {"error": "not logged in"}
    try:
        res = active_client.call('load/index', {})
        data = res.get('data', {})
    except Exception as e:
        return {"error": str(e)}
    keywords = ('event', 'boost', 'story', 'difficulty', 'challenge', 'playthrough', 'reward')
    found = {}
    for k, v in data.items():
        if any(kw in k.lower() for kw in keywords):
            found[k] = v
    return {"matched_keys": found, "all_keys": list(data.keys())}

@app.get("/api/showtime-info")
async def showtime_info():
    """Return the active Showtime event ID and difficulty_id from load/index.

    story_event_id      → use as boost_story_event_id in the start request
    difficulty_id       → use as selected_difficulty_info.difficulty_id
    max_difficulty      → highest unlocked difficulty level (open_difficulty_index)
    """
    if not active_client:
        return {"error": "not logged in"}
    try:
        res = active_client.call('load/index', {})
        data = res.get('data', {})
    except Exception as e:
        return {"error": str(e)}

    story_event_id = data.get('story_event_id', 0)

    # single_mode_difficulty_info_array holds the Showtime scenario entry
    diff_array = data.get('single_mode_difficulty_info_array', [])
    difficulty_id = 0
    max_difficulty = 5
    if diff_array:
        entry = diff_array[0]
        difficulty_id = entry.get('difficulty_id', 0)
        max_difficulty = entry.get('open_difficulty_index', 5)

    return {
        "story_event_id": story_event_id,
        "difficulty_id": difficulty_id,
        "max_difficulty": max_difficulty,
    }

# ── REST API: circle (club) stats ──────────────────────────────────────────
@app.get("/api/stats/circle")
async def get_circle_stats(refresh: bool = False):
    """Return club info: name from cache, fans/ranking from circle/detail with circle_id.

    Results are cached in _cached_circle_info and only re-fetched from the game
    server when:
      - The cache is empty (first call after startup / login)
      - _circle_refresh_needed is True (set after each completed career)
      - The caller passes ?refresh=true
    """
    global _cached_circle_info, _circle_refresh_needed

    if not active_client:
        return {"success": False, "detail": "Not logged in"}

    # Return cached result if it's still valid
    if _cached_circle_info is not None and not _circle_refresh_needed and not refresh:
        return {"success": True, "circle": _cached_circle_info}

    ld = active_client.cached_load_data or {}
    cd = (ld.get("circle_data") or ld.get("user_circle_info")
          or ld.get("circle_info_data") or {})
    ci = cd.get("circle_info") or cd
    name = ci.get("name")
    circle_id = ci.get("circle_id")

    member_num = None
    comment = None
    rank = None
    score = None

    def _parse_circle_response(data):
        nonlocal name, member_num, comment, rank, score
        if not isinstance(data, dict):
            return
        ci_live = data.get("circle_info") or {}
        name = ci_live.get("name") or name
        member_num = ci_live.get("member_num")
        comment = ci_live.get("comment")
        ranking = data.get("circle_ranking_this_month") or {}
        rank = ranking.get("rank")
        score = ranking.get("point")

    if circle_id:
        # Try circle/detail first, fall back to circle/room_enter
        for endpoint, payload in [
            ("circle/detail",     {"circle_id": circle_id}),
            ("circle/room_enter", {"circle_id": circle_id, "no_join_user": False}),
        ]:
            try:
                res = active_client.call(endpoint, payload)
                data = res.get("data") or res
                _parse_circle_response(data)
                print(f"[circle] fetched via {endpoint}: name={name!r} rank={rank} score={score} members={member_num}")
                break
            except Exception as e:
                print(f"[circle/{endpoint}] failed: {e}")

    result = {}
    if name:
        result["name"] = name
    if member_num is not None:
        result["member_num"] = int(member_num)
    if rank is not None:
        result["rank"] = int(rank)
    if score is not None:
        result["score"] = int(score)
    if comment:
        result["comment"] = comment

    if not result:
        return {"success": False, "detail": "No circle data available"}

    # Store in cache; clear the refresh flag
    _cached_circle_info = result
    _circle_refresh_needed = False
    return {"success": True, "circle": result}

# ── REST API: fan stats ─────────────────────────────────────────────────────
@app.get("/api/stats/fans")
async def get_fan_stats():
    """Fan farming stats: session totals, daily totals, per-career history, circle info."""
    _load_fan_stats()   # reload from disk so edits/backfills are always reflected
    today = _date_mod.today().isoformat()
    with _fan_stats_lock:
        careers = list(_fan_stats.get("careers", []))

    today_gained = sum(c["fans_gained"] for c in careers if c.get("date") == today)
    total_gained = sum(c["fans_gained"] for c in careers)

    # circle (club) info from cached load/index response
    circle_info = None
    if active_client and active_client.cached_load_data:
        ld = active_client.cached_load_data
        # Try known key names first, then fall back to any key containing "circle" or "guild"
        _cd = ld.get("circle_data")
        circle_info = _cd.get("circle_info") if isinstance(_cd, dict) else None

    # current fans — prefer live runner snapshot (updates every turn) over stale account cache
    current_fans = None
    live_gain = 0
    if active_account and active_account.get("career") and active_account["career"].get("active"):
        snap = career_runner.snapshot()
        current_fans = snap.get("current_fans") or active_account["career"].get("fans")
        # fans gained so far in the active career = current - fans at career start
        fans_at_start = int(active_account["career"].get("fans") or 0)
        live_gain = max(0, int(current_fans or 0) - fans_at_start)

    # Persistent all-time totals (never capped by the 500-entry careers list)
    with _fan_stats_lock:
        all_time_base = _fan_stats.get("all_time_gained", total_gained)
        all_time_careers = _fan_stats.get("all_time_careers", 0)

    return {
        "session_gained": _session_fans_gained,
        "today_gained": today_gained,
        "total_gained": all_time_base + live_gain,
        "careers_count": all_time_careers,
        "recent_careers": careers[-30:][::-1],   # newest first, last 30
        "current_fans": current_fans,
        "circle_info": circle_info,
    }

@app.delete("/api/stats/fans")
async def clear_fan_stats():
    global _fan_stats, _session_fans_gained
    with _fan_stats_lock:
        _fan_stats = {"careers": [], "all_time_gained": 0, "all_time_careers": 0}
        _session_fans_gained = 0
        _save_fan_stats()
    return {"success": True}


@app.post("/api/stats/backfill")
async def backfill_fan_stats():
    """Rebuild fan_stats.json by scanning all career log files for fans data."""
    global _fan_stats, _session_fans_gained
    import glob as _glob

    log_dir = base_dir / "uma_runtime" / "bot_logs"
    logs    = sorted(_glob.glob(str(log_dir / "career_log_*.json")))

    careers      = []
    total_gained = 0
    total_count  = 0

    for path in logs:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue

        status = d.get("status", "")
        if status not in ("finished", "stopped", "error", "give_up"):
            continue

        # Scan only first 3 and last 5 turns for fans (finish endpoint has empty chara_info)
        turns = d.get("turns") or []
        def _scan(subset):
            out = []
            for t in subset:
                for c in t.get("api_calls") or []:
                    if c.get("direction") not in ("RES", "RESP"):
                        continue
                    ci = ((c.get("data") or {}).get("data") or {}).get("chara_info") or {}
                    fans = ci.get("fans")
                    if fans:
                        out.append((int(fans), str(ci.get("card_id") or "")))
            return out
        head = _scan(turns[:3])
        tail = _scan(turns[-5:])
        if head or tail:
            initial_fans = head[0][0] if head else tail[0][0]
            final_fans, card_id = tail[-1] if tail else head[-1]
        else:
            initial_fans = final_fans = 0
            card_id = ""

        fans_gained = max(0, final_fans - initial_fans)
        finished    = (status == "finished")
        started_at  = d.get("started_at", "")

        entry = {
            "card_id":     card_id,
            "preset":      d.get("preset_name", ""),
            "date":        started_at[:10] if started_at else "",
            "started_at":  started_at,
            "fans_gained": fans_gained,
            "final_fans":  final_fans,
            "final_turn":  d.get("final_turn", 0),
            "finished":    finished,
        }
        careers.append(entry)
        if finished:
            total_count  += 1
            total_gained += fans_gained

    careers = careers[-500:]
    with _fan_stats_lock:
        _fan_stats = {
            "careers":          careers,
            "all_time_gained":  total_gained,
            "all_time_careers": total_count,
        }
        _save_fan_stats()

    return {
        "success":      True,
        "entries":      len(careers),
        "finished":     total_count,
        "total_gained": total_gained,
    }



@app.get("/api/career/log-detail")
async def career_log_detail(started_at: str = ""):
    """
    Return a summary of a specific career log file: final stats, fans, skills selected.
    Matches by started_at field inside the log (since filenames use ended_at timestamp).
    """
    log_dir = base_dir / "uma_runtime" / "bot_logs"
    target = started_at.strip()

    # Try latest_career_log.json shortcut first (no started_at needed, just omit it)
    matched = None
    if not target:
        latest = log_dir / "latest_career_log.json"
        if latest.exists():
            matched = latest
    else:
        import glob as _glob
        for path in sorted(_glob.glob(str(log_dir / "career_log_*.json"))):
            try:
                with open(path, encoding="utf-8") as f:
                    first_chunk = f.read(512)  # started_at is always near the top
                if repr(target) in first_chunk or target in first_chunk:
                    matched = Path(path)
                    break
            except Exception:
                continue

    if matched is None:
        return {"error": "log not found", "started_at": target}

    try:
        with open(matched, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return {"error": str(e)}

    turns = d.get("turns") or []
    last_turn = turns[-1] if turns else {}

    # ── final stats ──────────────────────────────────────────────────────────
    final_stats = last_turn.get("stats") or {}

    # ── final fans (last race_out / any RESP with chara_info.fans) ───────────
    final_fans = 0
    initial_fans = 0
    _found_any = False
    for t in turns:
        for c in t.get("api_calls") or []:
            if c.get("direction") not in ("RES", "RESP"):
                continue
            ci = ((c.get("data") or {}).get("data") or {}).get("chara_info") or {}
            fans = ci.get("fans")
            if fans:
                if not _found_any:
                    initial_fans = int(fans)
                    _found_any = True
                final_fans = int(fans)

    # ── skills selected (last turn with non-empty bot_skill_selected) ─────────
    skills_selected = []
    for t in reversed(turns):
        sel = t.get("bot_skill_selected") or []
        if sel:
            skills_selected = sel
            break

    # ── owned skills at end ───────────────────────────────────────────────────
    owned_skills = last_turn.get("owned_skills") or []

    return {
        "preset_name":     d.get("preset_name", ""),
        "status":          d.get("status", ""),
        "started_at":      d.get("started_at", ""),
        "ended_at":        d.get("ended_at", ""),
        "final_turn":      d.get("final_turn", 0),
        "final_stats":     final_stats,
        "final_fans":      final_fans,
        "fans_gained":     max(0, final_fans - initial_fans),
        "skills_selected": skills_selected,
        "owned_skills":    owned_skills,
        "log_file":        matched.name,
    }

_sniffer_session = None
_sniffer_log = []
_sniffer_filter = []
_sniffer_lock = threading.Lock()

SNIFFER_JS = r'''
'use strict';
(function() {
    // ── TLS hook: captures outgoing HTTP request endpoint names ──────────────
    var buffers = {};
    var attached = {};
    function parseHttp(text) {
        if (text.indexOf('/umamusume/') < 0) return;
        var em = text.match(/POST\s+\/umamusume\/([^\s]+)\s+HTTP/i);
        if (!em) return;
        send({type: 'endpoint', endpoint: em[1], body: ''});
    }
    function parseChunk(key, chunk) {
        var buf = (buffers[key] || '') + chunk;
        if (buf.length > 2097152) buf = buf.substring(buf.length - 1048576);
        var start = buf.indexOf('POST ');
        if (start < 0) { buffers[key] = buf.slice(-4096); return; }
        if (start > 0) buf = buf.substring(start);
        var headerEnd = buf.indexOf('\r\n\r\n');
        if (headerEnd < 0) { buffers[key] = buf; return; }
        var headers = buf.substring(0, headerEnd);
        var lm = headers.match(/Content-Length:\s*(\d+)/i);
        var length = lm ? parseInt(lm[1], 10) : 0;
        var total = headerEnd + 4 + length;
        if (length > 0 && buf.length < total) { buffers[key] = buf; return; }
        parseHttp(length > 0 ? buf.substring(0, total) : buf);
        buffers[key] = buf.length > total ? buf.substring(total) : '';
    }
    function hookTls() {
        var ga = Process.findModuleByName('GameAssembly.dll');
        if (!ga) return false;
        var installFn = ga.findExportByName('il2cpp_unity_install_unitytls_interface');
        if (!installFn) return false;
        var rb = new Uint8Array(installFn.readByteArray(16));
        var realFn = installFn;
        if (rb[0] === 0xe9) {
            var off = rb[1] | (rb[2] << 8) | (rb[3] << 16) | (rb[4] << 24);
            if (off > 0x7fffffff) off -= 0x100000000;
            realFn = installFn.add(5 + off);
            rb = new Uint8Array(realFn.readByteArray(16));
        }
        var globalPtr = null;
        if (rb[0] === 0x48 && rb[1] === 0x89 && rb[2] === 0x0d) {
            var disp = rb[3] | (rb[4] << 8) | (rb[5] << 16) | (rb[6] << 24);
            if (disp > 0x7fffffff) disp -= 0x100000000;
            globalPtr = realFn.add(7 + disp);
        }
        if (!globalPtr) return false;
        var iface = globalPtr.readPointer();
        if (!iface || iface.isNull()) return false;
        var hookedTls = 0;
        [0xd0, 0xd8, 0xe0, 0xe8].forEach(function(off) {
            var addr = iface.add(off).readPointer();
            if (!addr || addr.isNull()) return;
            var key = 'tls_' + addr.toString();
            if (attached[key]) return;
            try {
                Interceptor.attach(addr, {
                    onEnter: function(args) {
                        var len = args[2].toInt32();
                        if (len <= 0 || len > 1048576 || args[1].isNull()) return;
                        try {
                            var bytes = args[1].readByteArray(len);
                            var u8 = new Uint8Array(bytes);
                            var s = '';
                            for (var i = 0; i < u8.length; i++) s += String.fromCharCode(u8[i]);
                            parseChunk(args[0].toString(), s);
                        } catch(e) {}
                    }
                });
                attached[key] = true;
                hookedTls++;
            } catch(e) {}
        });
        return hookedTls > 0;
    }
    var tlsDone = false;
    var timer = setInterval(function() {
        try { if (!tlsDone) tlsDone = hookTls(); if (tlsDone) clearInterval(timer); } catch(e) {}
    }, 1000);

    // ── HttpHelper hook: captures decoded response bodies (post-decrypt) ──────
    // Same technique as SweepTosher/dumper — hooks DecompressResponse to get
    // clean msgpack before any game-side processing.
    function hookHttpHelper() {
        var ga = Process.findModuleByName('GameAssembly.dll');
        if (!ga) return;
        var il2cpp_domain_get = new NativeFunction(ga.findExportByName('il2cpp_domain_get'), 'pointer', []);
        var il2cpp_domain_get_assemblies = new NativeFunction(ga.findExportByName('il2cpp_domain_get_assemblies'), 'pointer', ['pointer', 'pointer']);
        var il2cpp_assembly_get_image = new NativeFunction(ga.findExportByName('il2cpp_assembly_get_image'), 'pointer', ['pointer']);
        var il2cpp_class_from_name = new NativeFunction(ga.findExportByName('il2cpp_class_from_name'), 'pointer', ['pointer', 'pointer', 'pointer']);
        var il2cpp_class_get_method_from_name = new NativeFunction(ga.findExportByName('il2cpp_class_get_method_from_name'), 'pointer', ['pointer', 'pointer', 'int']);
        var il2cpp_array_length_fn = new NativeFunction(ga.findExportByName('il2cpp_array_length'), 'uint', ['pointer']);
        var arrayAddrExport = ga.findExportByName('il2cpp_array_addr_with_size');
        var il2cpp_array_addr = arrayAddrExport ? new NativeFunction(arrayAddrExport, 'pointer', ['pointer', 'int', 'uint']) : null;

        var domain = il2cpp_domain_get();
        var sizeOut = Memory.alloc(4);
        var assemblies = il2cpp_domain_get_assemblies(domain, sizeOut);
        var assemblyCount = sizeOut.readU32();
        var nsPtr = Memory.allocUtf8String('Gallop');
        var cnPtr = Memory.allocUtf8String('HttpHelper');
        var foundClass = null;
        for (var i = 0; i < assemblyCount && !foundClass; i++) {
            var assembly = assemblies.add(i * Process.pointerSize).readPointer();
            var image = il2cpp_assembly_get_image(assembly);
            var klass = il2cpp_class_from_name(image, nsPtr, cnPtr);
            if (!klass.isNull()) foundClass = klass;
        }
        if (!foundClass) return;

        function readManagedArray(arr) {
            var len = il2cpp_array_length_fn(arr);
            if (len <= 0 || len > 50 * 1024 * 1024) return null;
            var dataPtr = il2cpp_array_addr ? il2cpp_array_addr(arr, 1, 0) : arr.add(0x20);
            return dataPtr.readByteArray(len);
        }

        // Hook DecompressResponse — fires after the server response is decrypted
        var decompName = Memory.allocUtf8String('DecompressResponse');
        var decompMethod = il2cpp_class_get_method_from_name(foundClass, decompName, 1);
        if (!decompMethod.isNull()) {
            Interceptor.attach(decompMethod.readPointer(), {
                onLeave: function(retval) {
                    if (!retval.isNull()) {
                        try {
                            var data = readManagedArray(retval);
                            if (data) send({type: 'response_body'}, data);
                        } catch(e) {}
                    }
                }
            });
        }

        // Hook CompressRequest — fires before the request is encrypted (outgoing)
        var compName = Memory.allocUtf8String('CompressRequest');
        var compMethod = il2cpp_class_get_method_from_name(foundClass, compName, 1);
        if (!compMethod.isNull()) {
            Interceptor.attach(compMethod.readPointer(), {
                onEnter: function(args) {
                    try {
                        var data = readManagedArray(args[0]);
                        if (!data) data = readManagedArray(args[1]);
                        if (data) send({type: 'request_body'}, data);
                    } catch(e) {}
                }
            });
        }
    }

    setTimeout(function() { try { hookHttpHelper(); } catch(e) {} }, 2000);
})();
'''

class SniffStartRequest(BaseModel):
    endpoints: list = []  # if non-empty, only capture these endpoint names (substring match)

@app.post("/api/debug/sniff_start")
async def sniff_start(req: SniffStartRequest = SniffStartRequest()):
    global _sniffer_session, _sniffer_log, _sniffer_filter
    if _sniffer_session:
        return {"error": "already sniffing"}
    try:
        import frida as _frida
        _sniffer_log = []
        _sniffer_filter = [e.lower() for e in req.endpoints]  # empty = capture all
        session = _frida.attach(PROCESS_NAME)
        script = session.create_script(SNIFFER_JS)
        # CompressRequest fires BEFORE the TLS write, so request_body arrives
        # before the matching endpoint message. Buffer it and pair on endpoint arrival.
        _last_endpoint = [None]
        _pending_request_body = [None]
        _pending_response_body = [None]

        def _ep_matches(ep):
            if not _sniffer_filter:
                return True
            ep_lower = ep.lower()
            return any(f in ep_lower for f in _sniffer_filter)

        def on_msg(message, data):
            if message.get('type') != 'send':
                return
            payload = message.get('payload') or {}
            msg_type = payload.get('type')

            if msg_type == 'request_body' and data:
                # Buffer — endpoint name arrives next (TLS write fires after compress)
                try:
                    import msgpack as _msgpack
                    _pending_request_body[0] = _msgpack.unpackb(bytes(data), raw=False, strict_map_key=False)
                except Exception as e:
                    print(f"[SNIFF:request_body] decode error: {e}")

            elif msg_type == 'response_body' and data:
                try:
                    import msgpack as _msgpack
                    decoded = _msgpack.unpackb(bytes(data), raw=False, strict_map_key=False)
                    _pending_response_body[0] = decoded.get('data', decoded) if isinstance(decoded, dict) else decoded
                except Exception as e:
                    print(f"[SNIFF:response_body] decode error: {e}")

            elif msg_type == 'endpoint':
                ep = payload.get('endpoint', '')
                _last_endpoint[0] = ep

                req_body = _pending_request_body[0]
                _pending_request_body[0] = None
                resp_body = _pending_response_body[0]
                _pending_response_body[0] = None

                if _ep_matches(ep):
                    print(f"[SNIFF] {ep}")
                    if req_body is not None:
                        with _sniffer_lock:
                            _sniffer_log.append({"endpoint": ep, "type": "request_body", "payload": req_body})
                    if resp_body is not None:
                        with _sniffer_lock:
                            _sniffer_log.append({"endpoint": ep, "type": "response_body", "payload": resp_body})

        script.on('message', on_msg)
        script.load()
        _sniffer_session = (session, script)
        filter_msg = f" (filtering: {req.endpoints})" if req.endpoints else " (capturing all endpoints)"
        return {"success": True, "message": f"Sniffing started{filter_msg} — run a career, then call /api/debug/sniff_stop"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/debug/sniff_stop")
async def sniff_stop():
    global _sniffer_session, _sniffer_log
    if not _sniffer_session:
        return {"error": "not sniffing"}
    try:
        session, script = _sniffer_session
        script.unload()
        session.detach()
    except Exception:
        pass
    _sniffer_session = None
    with _sniffer_lock:
        log = list(_sniffer_log)
    out_path = base_dir / "debug_sniff_log.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"captured_endpoints": log}, f, indent=2, ensure_ascii=False, default=str)
    print(f"[sniff] log saved to {out_path}")
    return {"success": True, "saved_to": str(out_path), "entry_count": len(log)}

@app.get("/api/debug/sniff_log")
async def sniff_log():
    with _sniffer_lock:
        return {"sniffing": _sniffer_session is not None, "captured_endpoints": list(_sniffer_log)}

# ── Bot-side capture (no game required) ──────────────────────────────────────
# Hooks the bot's own HTTP client to capture specific endpoint
# request/response payloads without needing the game open.

_bot_capture_log = []
_bot_capture_filter = []   # endpoint substrings to capture; empty = all
_bot_capture_active = False
_bot_capture_lock = threading.Lock()

class BotSniffStartRequest(BaseModel):
    endpoints: list = ["single_mode_free/start", "single_mode_free/finish", "pre_single_mode"]

@app.post("/api/debug/bot_sniff_start")
async def bot_sniff_start(req: BotSniffStartRequest = BotSniffStartRequest()):
    global _bot_capture_log, _bot_capture_filter, _bot_capture_active
    if not active_client:
        return {"error": "not logged in"}
    _bot_capture_log = []
    _bot_capture_filter = [e.lower() for e in req.endpoints]
    _bot_capture_active = True

    def _on_api_log(direction, ep, data, req_id=None):
        if not _bot_capture_active:
            return
        ep_lower = ep.lower()
        if _bot_capture_filter and not any(f in ep_lower for f in _bot_capture_filter):
            return
        # Strip common fields to keep the file readable
        if direction == "REQ" and isinstance(data, dict):
            payload = data.get("payload", data)
            clean = {k: v for k, v in payload.items()
                     if k not in ("viewer_id","device","device_id","device_name",
                                  "graphics_device_name","ip_address","platform_os_version",
                                  "carrier","keychain","locale","button_info",
                                  "dmm_viewer_id","dmm_onetime_token",
                                  "steam_id","steam_session_ticket")}
        elif direction == "RES":
            raw = data.get("data", data) if isinstance(data, dict) else data
            clean = raw
        else:
            clean = data
        with _bot_capture_lock:
            _bot_capture_log.append({"direction": direction, "endpoint": ep, "payload": clean})
        print(f"[BOT_SNIFF:{direction}] {ep}")

    active_client.on_api_log = _on_api_log
    filter_msg = req.endpoints if req.endpoints else ["all"]
    return {"success": True, "message": f"Bot sniffing started, capturing: {filter_msg}"}

@app.post("/api/debug/bot_sniff_stop")
async def bot_sniff_stop():
    global _bot_capture_active
    _bot_capture_active = False
    if active_client:
        active_client.on_api_log = None
    with _bot_capture_lock:
        log = list(_bot_capture_log)
    out_path = base_dir / "debug_bot_sniff_log.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"captured": log}, f, indent=2, ensure_ascii=False, default=str)
    print(f"[bot_sniff] saved {len(log)} entries to {out_path}")
    return {"success": True, "saved_to": str(out_path), "entry_count": len(log)}

class RawCallRequest(BaseModel):
    endpoint: str
    payload: dict = {}

@app.post("/api/debug/call")
async def debug_raw_call(req: RawCallRequest):
    """Call any game API endpoint with a custom payload. Useful for discovering correct deck/item endpoints."""
    if not active_client:
        return {"error": "not logged in"}
    try:
        result = active_client.call(req.endpoint, req.payload)
        return {"success": True, "endpoint": req.endpoint, "result": result}
    except Exception as e:
        return {"success": False, "endpoint": req.endpoint, "error": str(e)}

@app.get("/api/debug/raw_load")
async def get_raw_load():
    if not active_client:
        return {"error": "not logged in"}
    try:
        res = active_client.call('load/index', {'adid': ''})
        d = res.get('data', {})
        # Return all top-level keys summary + full content of deck-related keys
        summary = {}
        deck_data = {}
        deck_keywords = {'deck', 'party', 'support_card'}
        for k, v in d.items():
            if any(kw in k.lower() for kw in deck_keywords):
                deck_data[k] = v
            elif isinstance(v, list):
                summary[k] = f"list[{len(v)}]"
                if v and isinstance(v[0], dict):
                    summary[k] += " keys=" + str(list(v[0].keys())[:8])
            elif isinstance(v, dict):
                summary[k] = f"dict keys={list(v.keys())[:8]}"
            else:
                summary[k] = repr(v)[:80]
        return {"keys": summary, "deck_related": deck_data}
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/images/{image_name}")
async def get_image(image_name: str):
    name_no_ext = image_name.split('?')[0].replace('.png', '')
    
    exact_path = images_dir / f"{name_no_ext}.png"
    if exact_path.exists():
        return FileResponse(exact_path, media_type="image/png", headers={"Cache-Control": "no-cache"})
    
    for fallback_id in ['100101', '10010', '10000', '10001']:
        fb_path = images_dir / f"{fallback_id}.png"
        if fb_path.exists():
            return FileResponse(fb_path, media_type="image/png", headers={"Cache-Control": "no-cache"})
    
    raise HTTPException(status_code=404, detail="Image not found")


# ── AI endpoints ──────────────────────────────────────────────────────────────

@app.get("/api/ai/status")
async def ai_status():
    try:
        from career_bot.ai_dataset import dataset_status
        return dataset_status(bot_logs_dir)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/ai/train-now")
async def ai_train_now():
    try:
        from career_bot.ai_trainer import train_once
        from career_bot.ai_dataset import ai_root_from_output_dir
        root = ai_root_from_output_dir(bot_logs_dir)
        run_info = train_once(root)
        return {"success": True, **run_info}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/ai/advisor/latest")
async def ai_advisor_latest():
    try:
        from career_bot.ai_advisor import post_run_advice
        return post_run_advice(bot_logs_dir)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ai/advisor/programs")
async def ai_advisor_programs():
    try:
        from career_bot.ai_advisor import all_race_program_hints
        return {"programs": all_race_program_hints(bot_logs_dir)}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ai/advisor/program/{program_id}")
async def ai_advisor_program(program_id: str):
    try:
        from career_bot.ai_advisor import race_program_hint
        return race_program_hint(program_id, bot_logs_dir)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ai/auto-training/status")
async def ai_trainer_status():
    try:
        from career_bot.ai_trainer import trainer_status
        return trainer_status(bot_logs_dir)
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ai/auto-training/config")
async def ai_auto_config_get():
    try:
        from career_bot.ai_trainer import load_auto_config
        from career_bot.ai_dataset import ai_root_from_output_dir
        root = ai_root_from_output_dir(bot_logs_dir)
        return load_auto_config(root)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/ai/auto-training/config")
async def ai_auto_config_set(request: Request):
    try:
        from career_bot.ai_trainer import save_auto_config
        from career_bot.ai_dataset import ai_root_from_output_dir
        body = await request.json()
        root = ai_root_from_output_dir(bot_logs_dir)
        save_auto_config(root, body)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/ai/dashboard")
async def ai_dashboard():
    try:
        from career_bot.ai_trainer import latest_dashboard
        return latest_dashboard(bot_logs_dir)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/ai/rebuild")
async def ai_rebuild_from_logs():
    try:
        from career_bot.ai_dataset import rebuild_from_career_logs
        result = rebuild_from_career_logs(base_dir)
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── race solver API ────────────────────────────────────────────────────────────

@app.get("/api/solver/status")
async def solver_status_endpoint():
    try:
        from career_bot.race_solver import solver_status
        return solver_status(base_dir)
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/solver/plan/{preset_name}")
async def solver_get_plan(preset_name: str):
    try:
        from career_bot.race_solver import plan_summary
        return plan_summary(base_dir, preset_name)
    except Exception as e:
        return {"available": False, "error": str(e)}


@app.post("/api/solver/run")
async def solver_run(request: Request):
    try:
        body = await request.json()
        preset_name = str(body.get("preset_name") or "")
        if not preset_name:
            return {"success": False, "error": "preset_name required"}
        from career_bot.presets import PresetStore
        from career_bot.race_solver import solve
        store = PresetStore(base_dir)
        preset = store.load(preset_name)
        if not preset:
            return {"success": False, "error": f"Preset '{preset_name}' not found"}
        # Use chara_info from body if provided (live solve during career).
        # Fall back to aptitudes stored in the active account's career dict so
        # the solver knows whether this character can run dirt races.
        chara_info = body.get("chara_info") or {}
        current_turn = int(body.get("current_turn") or 1)
        # Only use stored aptitudes when solving mid-career (current_turn > 1).
        # Pre-career solves (turn=1) should not inherit a previous character's
        # low aptitudes — they belong to a different character.
        if not chara_info and current_turn > 1 and active_account:
            career = (active_account or {}).get("career") or {}
            apt_keys = ["proper_ground_turf", "proper_ground_dirt",
                        "proper_distance_short", "proper_distance_mile",
                        "proper_distance_middle", "proper_distance_long"]
            stored_apt = {k: career[k] for k in apt_keys if k in career}
            if stored_apt:
                chara_info = stored_apt
        plan = solve(base_dir, preset, chara_info=chara_info or None, current_turn=current_turn)
        return plan
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/solver/candidates/{preset_name}")
async def solver_candidates(preset_name: str):
    """Return scored race candidates for a preset (for UI preview)."""
    try:
        from career_bot.presets import PresetStore
        from career_bot.race_solver import _race_candidates
        store = PresetStore(base_dir)
        preset = store.load(preset_name)
        if not preset:
            return {"success": False, "error": f"Preset '{preset_name}' not found"}
        chara = {
            "proper_ground_turf": 8, "proper_ground_dirt": 8,
            "proper_distance_short": 8, "proper_distance_mile": 8,
            "proper_distance_middle": 8, "proper_distance_long": 8,
        }
        candidates = _race_candidates(base_dir, preset, chara)
        candidates.sort(key=lambda r: (int(r["turn"]), -float(r.get("score") or 0)))
        return {"success": True, "candidates": candidates}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── static files ───────────────────────────────────────────────────────────────

@app.get("/styles.css")
async def styles_css():
    path = base_dir / "public" / "styles.css"
    if path.exists():
        return FileResponse(path, media_type="text/css", headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="styles.css not found")

@app.get("/app.js")
async def app_js():
    path = base_dir / "public" / "app.js"
    if path.exists():
        return FileResponse(path, media_type="application/javascript", headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="app.js not found")


@app.get("/sweep.png")
async def sweep_png():
    path = base_dir / "public" / "sweep.png"
    if path.exists():
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="sweep.png not found")

@app.get("/broom.png")
async def broom_png():
    path = base_dir / "public" / "broom.png"
    if path.exists():
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="broom.png not found")

@app.get("/assets/data/{file_name}")
async def get_asset_data(file_name: str):
    path = base_dir / 'public' / 'assets' / 'data' / file_name
    if path.exists():
        return FileResponse(path, headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="File not found")

@app.get("/races/{file_name}")
async def get_race_image(file_name: str):
    path = base_dir / "public" / "races" / file_name
    if path.exists():
        return FileResponse(path, headers={"Cache-Control": "max-age=31536000"})
    raise HTTPException(status_code=404, detail="Race image not found")

@app.get("/", response_class=HTMLResponse)
async def root():
    index_path = base_dir / "public" / "index.html"
    if index_path.exists():
        return FileResponse(index_path, media_type="text/html", headers={"Cache-Control": "no-cache"})
    return "index.html not found"

def set_console_topmost():
    if os.name != 'nt':
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        ctypes.windll.user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
    except Exception:
        pass

def kill_process_by_name(name):
    if os.name != 'nt':
        return
    try:
        subprocess.run(['taskkill', '/IM', name, '/F'], capture_output=True, text=True, timeout=10)
    except Exception:
        pass

def kill_listeners_on_port(port):
    if os.name != 'nt':
        return
    try:
        proc = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True,
            text=True,
            timeout=5
        )
    except Exception:
        return

    current_pid = os.getpid()
    pids = set()
    marker = f':{port}'
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local_addr = parts[1]
        state = parts[3].upper() if len(parts) >= 5 else ''
        pid_text = parts[-1]
        if marker not in local_addr or state != 'LISTENING':
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid and pid != current_pid:
            pids.add(pid)

    if not pids:
        return
    print(f"Port {port} already in use; killing listener PID(s): {', '.join(map(str, sorted(pids)))}", flush=True)
    for pid in sorted(pids):
        try:
            subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True, text=True, timeout=5)
        except Exception:
            pass
    dna_sleep(0.5, 0.5)

def has_fresh_auth_config(cfg):
    app_ver = str(cfg.get('app_ver') or '').strip()
    res_ver = str(cfg.get('res_ver') or '').strip()
    if not app_ver or not res_ver:
        return False
    if int(cfg.get('auth_key_len') or 0) != 48:
        return False
    viewer_id = cfg.get('viewer_id')
    udid = str(cfg.get('udid') or '').strip()
    auth_key = str(cfg.get('auth_key') or '').strip().lower()
    if not viewer_id or not udid or not auth_key:
        return False
    if not re.fullmatch(r'[0-9a-f]+', auth_key):
        return False
    if len(auth_key) < 32 or len(auth_key) % 2:
        return False
    if len(udid) != 36 or udid.count('-') != 4:
        return False
    return True

def launch_game():
    if os.name != 'nt':
        print('Auth refresh needs Windows Steam launch.')
        return False
    try:
        os.startfile(f'steam://rungameid/{APP_ID}')
        return True
    except Exception as e:
        print(f'Failed to launch Umamusume through Steam: {e}')
        return False

# ── Auth refresh: Frida-based credential capture ────────────────────────────
# Called once at startup. Injects JS_CODE into the game process via Frida,
# waits for a live API call to capture fresh credentials, then saves them.
def refresh_auth_before_serving(timeout_sec=None):
    global pending_game_auth_config
    timeout_sec = timeout_sec or int(os.environ.get('SWEEPY_AUTH_CAPTURE_TIMEOUT_SEC', '180'))
    started_at = time.time()
    deadline = started_at + timeout_sec

    print('[NEED TO CAPTURE AUTH]', flush=True)
    if not launch_game():
        return False
    
    print(f'Waiting up to {timeout_sec}s for user to enter game menu', flush=True)

    session = None
    captured_data = {}
    done = {'ok': False}

    def on_message(message, data):
        if message.get('type') == 'error':
            print(f"Frida Error: {message.get('description')}", flush=True)
            return
        payload = message.get('payload') or {}
        if payload.get('type') == 'creds':
            if payload.get('app_ver') and payload.get('res_ver'):
                try:
                    from uma_api.client import unpack
                    wire = unpack(payload.get('body') or '', payload.get('udid') or '')
                    for key in (
                        'viewer_id',
                        'device_id',
                        'device_name',
                        'graphics_device_name',
                        'ip_address',
                        'platform_os_version',
                        'locale',
                        'steam_id',
                        'steam_session_ticket',
                    ):
                        if wire.get(key) is not None:
                            payload[key] = wire.get(key)
                except Exception:
                    pass
                captured_data.update(payload)
                done['ok'] = True

    while time.time() < deadline:
        try:
            session = frida.attach(PROCESS_NAME)
            break
        except Exception:
            dna_sleep(1.0, 1.0)
    
    if not session:
        print(f'Error: {PROCESS_NAME} not found within timeout.', flush=True)
        return False

    try:
        script = session.create_script(JS_CODE)
        script.on('message', on_message)
        script.load()

        while time.time() < deadline:
            if done['ok']:
                if has_fresh_auth_config(captured_data):
                    pending_game_auth_config = dict(captured_data)
                    dna_sleep(2.0, 4.0)
                    kill_process_by_name(PROCESS_NAME)
                    return True
            dna_sleep(0.5, 0.5)
    except Exception as e:
        print(f'Frida injection failed: {e}', flush=True)
    finally:
        if session:
            try:
                session.detach()
            except Exception:
                pass

    print('Auth refresh failed: no fresh credentials captured before timeout.', flush=True)
    return False


if __name__ == "__main__":
    import uvicorn

    try:
        subprocess.run(["git", "pull"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    set_console_topmost()
    kill_listeners_on_port(8000)
    print("[ok] Starting server on http://localhost:8000", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
