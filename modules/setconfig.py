import os
import json

# Add typing and helpers
from typing import Any, Dict, Tuple
import re
from datetime import datetime

# Central settings schema with identifiers, types, defaults, and per-setting access control
# access values:
# 0: Editable
# 1: Not Editable (visible)
# 2: Hidden (non editable in any server)
# 3: Central Server only Editable (visible to all) (Bot Owner Can only Edit)
# 4: Central Server only Editable and Hidden to Non-Central Servers (Bot Owner Can only Edit)
SETTINGS_SCHEMA: Dict[str, Dict[str, Dict[str, Any]]] = {
    "General": {
        "DefaultAdmin": {
            "type": "role",
            "default": None,
            "access": 0,
            "description": "Default Admin Role ID in the Settings",
        },
        "DefaultRoleId": {
            "type": "role",
            "default": None,
            "access": 0,
            "description": "Default member role ID",
        },
        "GlobalHeartbeat": {
            "type": "int",
            "default": 1800,
            "min": 1800,
            "max": 86400,
            "access": 4,
            "description": "Central-only: how often the bot checks for updates/pings (seconds). Acts as a floor for per-guild schedules.",
        },
        "GlobalHeartbeatEnabled": {
            "type": "bool",
            "default": True,
            "access": 4,
            "description": "Central-only: enable or disable the global heartbeat loop.",
        },
        "LastHeartbeatTs": {
            "type": "str",
            "default": None,
            "access": 2,
        },
    },
    "Noticeboard": {
        "Enabled": {"type": "bool", "default": True, "access": 0},
        "ChannelId": {"type": "channel|Default", "default": "Default", "access": 0},
        "UpdateInterval": {
            "type": "int|null",
            "default": None,
            "min": 1800,
            "max": 86400,
            "access": 0,
            "description": "Set the update frequency, in seconds. Range (GlobalHeartbeat - 86400)",
        },
        "PingRoleId": {"type": "role|null", "default": None, "access": 0},
        "PingDailyTime": {
            "type": "time",
            "default": "15:00",
            "access": 3,
        },
        "SmartPingMode": {
            "type": "bool",
            "default": True,
            "access": 0,
            "description": "Skip daily ping if no work tomorrow/week",
        },
        "FollowMain": {
            "type": "bool",
            "default": True,
            "access": 0,
            "description": "If true, follow MAIN_GUILD noticeboard config",
        },
        "NoticeboardEditIDs": {"type": "list[int]", "default": [], "access": 2},
        "PingMessageEditID": {"type": "int|null", "default": None, "access": 2},
        "LastUpdateTs": {"type": "str", "default": None, "access": 2},
        "LastPingTs": {"type": "str", "default": None, "access": 2},
        "PingDayBlacklist": {
            "type": "list[str]|null",
            "default": ["Friday", "Saturday"],
            "access": 0,
            "choices": [
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            ],
        },
    },
    "Music": {
        "Enabled": {
            "type": "bool",
            "default": False,
            "access": 1,
        },  # Force Disable for now, not ready.
        "DJRole": {"type": "role|null", "default": None, "access": 0},
        "DJRoleRequired": {"type": "bool", "default": True, "access": 0},
        "Volume": {
            "type": "float",
            "default": 0.5,
            "min": 0.0,
            "max": 1.0,
            "access": 0,
        },
        "QueueLimit": {
            "type": "int",
            "default": 10,
            "min": 1,
            "max": 1000,
            "access": 3,
        },
        "MaxConcurrentInstances": {
            "type": "int",
            "default": 2,
            "min": 1,
            "max": 10,
            "access": 4,
            "description": "Central-only: maximum simultaneous music voice instances across all guilds.",
        },
        "QueueLimitEnabled": {
            "type": "bool",
            "default": True,
            "access": 1,
            "description": "If true, the queue limit will be enforced.",
        },
        "PlayerStick": {
            "type": "bool",
            "default": False,
            "access": 1,
            "description": "If true, the player will stick to the current channel.",
        },
        "TrackMaxDuration": {
            "type": "int",
            "default": 600,
            "min": 10,
            "max": 43200,
            "access": 3,
        },
        "RemoveNonSongsUsingSponsorBlock": {
            "type": "bool",
            "default": True,
            "access": 0,
        },
        "PlaylistAddLimit": {
            "type": "int",
            "default": 10,
            "min": 1,
            "max": 1000,
            "access": 3,
        },
    },
    "GoogleClassroom": {
        "Enabled": {"type": "bool", "default": False, "access": 1},
        "DefaultChannelId": {
            "type": "channel|Default",
            "default": "Default",
            "access": 0,
        },
    },
}


def _flatten_schema() -> Dict[str, Dict[str, Any]]:
    flat: Dict[str, Dict[str, Any]] = {}
    for section, fields in SETTINGS_SCHEMA.items():
        for key, meta in fields.items():
            flat[f"{section}.{key}"] = {"section": section, "key": key, **meta}
    return flat


_FLAT_SCHEMA = _flatten_schema()


def get_settings_schema() -> Dict[str, Dict[str, Any]]:
    return SETTINGS_SCHEMA


def get_setting_meta(path: str) -> Dict[str, Any]:
    return _FLAT_SCHEMA.get(path, {})


def _is_null(value: Any) -> bool:
    return value is None


def _coerce_time(s: str) -> str:
    if not isinstance(s, str):
        raise ValueError("time must be a string HH:MM")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s):
        raise ValueError("time must be in 24h HH:MM format")
    return s


def _coerce_date(s: str) -> str:
    if s is None:
        return None
    if not isinstance(s, str):
        raise ValueError("date must be string YYYY-MM-DD or null")
    # accept YYYY-MM-DD
    datetime.strptime(s, "%Y-%m-%d")
    return s


def _coerce_int(val: Any) -> int:
    if isinstance(val, bool):
        raise ValueError("int cannot be bool")
    return int(val)


def _coerce_float(val: Any) -> float:
    if isinstance(val, bool):
        raise ValueError("float cannot be bool")
    return float(val)


def _within(meta: Dict[str, Any], val: Any) -> Any:
    if val is None:
        return val
    if "min" in meta and val < meta["min"]:
        raise ValueError(f"value must be >= {meta['min']}")
    if "max" in meta and val > meta["max"]:
        raise ValueError(f"value must be <= {meta['max']}")
    if "choices" in meta and isinstance(meta["choices"], list):
        # allow list[str] choice members too
        if isinstance(val, list):
            invalid = [x for x in val if x not in meta["choices"]]
            if invalid:
                raise ValueError(f"invalid choices: {', '.join(map(str, invalid))}")
        elif val not in meta["choices"]:
            raise ValueError(
                f"value must be one of: {', '.join(map(str, meta['choices']))}"
            )
    return val


def _coerce_list_int(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [int(x) for x in v]
    raise ValueError("expected list[int]")


def _coerce_list_str(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    # Support comma-separated input "Mon,Tue"
    if isinstance(v, str):
        return [s.strip() for s in v.split(",") if s.strip()]
    raise ValueError("expected list[str]")


def _coerce_channel_or_default(v: Any) -> Any:
    if v == "Default":
        return "Default"
    if v in ("null", "None", None):
        return "Default"
    return int(v)


def _coerce_role_or_null(v: Any) -> Any:
    if v in (None, "null", "None"):
        return None
    return int(v)


def coerce_value_for_path(path: str, raw_value: Any) -> Any:
    meta = get_setting_meta(path)
    if not meta:
        # not in schema, store as-is
        return raw_value
    t = meta.get("type", "str")
    try:
        if t == "bool":
            if isinstance(raw_value, str):
                if raw_value.lower() in ("true", "1", "yes", "on"):
                    val = True
                elif raw_value.lower() in ("false", "0", "no", "off"):
                    val = False
                else:
                    raise ValueError("expected boolean (true/false)")
            else:
                val = bool(raw_value)
            return val
        if t == "int":
            return _within(meta, _coerce_int(raw_value))
        if t == "float":
            return _within(meta, _coerce_float(raw_value))
        if t == "int|null":
            if _is_null(raw_value) or (
                isinstance(raw_value, str) and raw_value.lower() in ("null", "none", "")
            ):
                return None
            return _within(meta, _coerce_int(raw_value))
        if t == "role":
            return int(raw_value)
        if t == "role|null":
            return _coerce_role_or_null(raw_value)
        if t == "channel|Default":
            return _coerce_channel_or_default(raw_value)
        if t == "time":
            return _coerce_time(raw_value)
        if t == "date|null":
            return _coerce_date(raw_value)
        if t == "list[int]":
            return _coerce_list_int(raw_value)
        if t == "list[str]":
            val = _coerce_list_str(raw_value)
            return _within(meta, val)
        if t == "list[str]|null":
            if raw_value is None or (
                isinstance(raw_value, str)
                and raw_value.strip().lower() in ("null", "none", "")
            ):
                return None
            val = _coerce_list_str(raw_value)
            return _within(meta, val)
        # default to string
        return str(raw_value)
    except Exception as e:
        raise ValueError(f"Invalid value for {path}: {e}")


def _ensure_schema_defaults(config_data: dict) -> Tuple[dict, bool]:
    """
    Ensure all schema-defined keys exist with proper types/defaults; returns (config, changed).
    """
    changed = False
    for path, meta in _FLAT_SCHEMA.items():
        # navigate and ensure presence
        cur = config_data
        keys = path.split(".")
        for k in keys[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
                changed = True
            cur = cur[k]
        leaf = keys[-1]
        if leaf not in cur:
            cur[leaf] = meta.get("default")
            changed = True
        else:
            # attempt to coerce to valid type/range
            try:
                coerced = coerce_value_for_path(path, cur[leaf])
                if coerced != cur[leaf]:
                    cur[leaf] = coerced
                    changed = True
                _within(meta, cur[leaf])
                if path == "Noticeboard.UpdateInterval":
                    try:
                        hb = int(
                            _get_by_path(
                                config_data,
                                "General.GlobalHeartbeat",
                                SETTINGS_SCHEMA["General"]["GlobalHeartbeat"][
                                    "default"
                                ],
                            )
                        )
                    except Exception:
                        hb = SETTINGS_SCHEMA["General"]["GlobalHeartbeat"]["default"]
                    if isinstance(cur[leaf], int) and cur[leaf] < hb:
                        cur[leaf] = hb
                        changed = True
            except Exception:
                cur[leaf] = meta.get("default")
                changed = True
    return config_data, changed


def _set_by_path(obj: dict, path: str, value):
    keys = path.split(".")
    cur = obj
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def _get_by_path(obj: dict, path: str, default=None):
    cur = obj
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def edit_json_file(guild_id, key, value, actor_user_id: int | None = None):
    config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")

    if not os.path.exists(config_file_path):
        raise FileNotFoundError(f"Config file for guild_id {guild_id} does not exist.")

    with open(config_file_path, "r") as config_file:
        config_data = json.load(config_file)

    try:
        coerced_value = coerce_value_for_path(key, value)
    except ValueError as e:
        raise

    try:
        current_hb = int(
            _get_by_path(
                config_data,
                "General.GlobalHeartbeat",
                SETTINGS_SCHEMA["General"]["GlobalHeartbeat"]["default"],
            )
        )
    except Exception:
        current_hb = SETTINGS_SCHEMA["General"]["GlobalHeartbeat"]["default"]

    meta = get_setting_meta(key)
    if key == "Noticeboard.UpdateInterval" and isinstance(coerced_value, int):
        if coerced_value < current_hb:
            coerced_value = current_hb

    # Owner-based central propagation: if access is 3 or 4 and the actor is OWNER_ID
    owner_env = os.getenv("OWNER_ID")
    is_owner = owner_env is not None and str(actor_user_id) == str(owner_env)
    if meta and meta.get("access") in (3, 4) and is_owner:
        for fname in os.listdir(config_dir):
            if not fname.endswith(".json"):
                continue
            fp = os.path.join(config_dir, fname)
            try:
                with open(fp, "r") as f:
                    cfg = json.load(f)
                if "." in key:
                    _set_by_path(cfg, key, coerced_value)
                else:
                    cfg[key] = coerced_value
                if key == "General.GlobalHeartbeat":
                    try:
                        new_hb = int(coerced_value)
                    except Exception:
                        new_hb = SETTINGS_SCHEMA["General"]["GlobalHeartbeat"][
                            "default"
                        ]
                    nb_val = _get_by_path(cfg, "Noticeboard.UpdateInterval", None)
                    if isinstance(nb_val, int) and nb_val < new_hb:
                        _set_by_path(cfg, "Noticeboard.UpdateInterval", new_hb)
                    # normalize: if null, keep null; otherwise ensure >= new floor in defaults pass
                # Also ensure schema (applies dynamic min normalization)
                cfg, _ = _ensure_schema_defaults(cfg)
                with open(fp, "w") as f:
                    json.dump(cfg, f, indent=4)
            except Exception:
                continue
        return

    if "." in key:
        _set_by_path(config_data, key, coerced_value)
    else:
        config_data[key] = coerced_value

    config_data, _ = _ensure_schema_defaults(config_data)
    with open(config_file_path, "w") as config_file:
        json.dump(config_data, config_file, indent=4)


def check_guild_config_available(guild_id):
    config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    return os.path.exists(config_file_path)


def cache_read_latest(weekSelect=None):
    cache_dir = os.path.join(os.path.dirname(__file__), "..", "cache")
    if not os.path.isdir(cache_dir):
        return None

    latest_file = None
    latest_time = None

    for file in os.listdir(cache_dir):
        date_part = None
        # Accept files like: cache_MM-HH_dd_mm_YYYY[ _week_<weekSelect>].json
        if weekSelect is None:
            if (
                file.startswith("cache_")
                and file.endswith(".json")
                and "_week_" not in file
            ):
                date_part = file.split("_")[1:6]  # [MM-HH, dd, mm, yyyy, maybe tail]
        else:
            if file.startswith("cache_") and file.endswith(f"_week_{weekSelect}.json"):
                date_part = file.split("_")[1:6]

        if not date_part:
            continue

        try:
            time_part = date_part[0].split("-")  # MM-HH
            minute_str = time_part[0]
            hour_str = time_part[1]
            day_str = date_part[1]
            month_str = date_part[2]
            year_str = date_part[3]
            file_datetime_str = (
                f"{day_str}_{month_str}_{year_str} {hour_str}:{minute_str}"
            )
            file_dt = datetime.strptime(file_datetime_str, "%d_%m_%Y %H:%M")
        except Exception:
            continue

        if latest_time is None or file_dt > latest_time:
            latest_time = file_dt
            latest_file = file

    if not latest_file:
        return None

    with open(os.path.join(cache_dir, latest_file), "r", encoding="utf-8") as f:
        return f.read()


def check_admin_role(guild_id, user_roles):
    config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")

    if not os.path.exists(config_file_path):
        return False

    with open(config_file_path, "r") as config_file:
        config_data = json.load(config_file)
        default_admin_role_id = _get_by_path(
            config_data, "General.DefaultAdmin", config_data.get("DefaultAdmin")
        )

        return any(role_id == default_admin_role_id for role_id in user_roles)


def json_get(guild_id):
    config_dir = os.path.join(os.path.dirname(__file__), "..", "config")
    config_file_path = os.path.join(config_dir, f"{guild_id}.json")
    with open(config_file_path, "r") as config_file:
        data = json.load(config_file)
    normalized, changed_schema = _ensure_schema_defaults(data)
    if normalized is not data or changed_schema:
        with open(config_file_path, "w") as config_file:
            json.dump(normalized, config_file, indent=4)
    return normalized
