"""Local workstation preferences; loading them never sends a device command."""

from __future__ import annotations

from pathlib import Path
import json
import math
import os
import ipaddress


SETTINGS_PATH = Path(__file__).resolve().parents[1] / "settings.json"
DEFAULTS_PATH = Path(__file__).with_name("settings.example.json")


def load_settings() -> dict:
    settings = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(settings, dict):
        raise ValueError(f"Expected a JSON object in {DEFAULTS_PATH}")
    if SETTINGS_PATH.is_file():
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Expected a JSON object in {SETTINGS_PATH}")
        # Read old camera identities once; the vendor DLL setting is obsolete.
        for old,new in [('neptune_serial','aravis_serial'),('neptune_ip','aravis_ip')]:
            previous = raw.pop(old, None)
            if previous is not None and new not in raw:
                raw[new] = previous
        raw.pop('neptune_sdk_dll', None)
        settings.update(raw)
    if not isinstance(settings["sdk_root"], str):
        raise ValueError("sdk_root must be a folder path string")
    if settings["camera_mode"] not in ("simulator", "native"):
        raise ValueError("camera_mode must be simulator or native")
    if type(settings["ccd_setpoint_c"]) is not int:
        raise ValueError("ccd_setpoint_c must be an integer")
    if type(settings["cooler_on_new_connection"]) is not bool:
        raise ValueError("cooler_on_new_connection must be true or false")
    for key in ("exposure_s", "cycle_s"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number")
    if settings["exposure_s"] <= 0 or settings["cycle_s"] < 0:
        raise ValueError("Exposure must be positive and cycle must be nonnegative")
    if type(settings["accumulations"]) is not int or settings["accumulations"] < 1:
        raise ValueError("accumulations must be a positive integer")
    interval = settings["temperature_interval_s"]
    if type(interval) is not int or not 10 <= interval <= 60:
        raise ValueError("temperature_interval_s must be 10–60 seconds")
    thresholds = settings["temperature_log_thresholds_c"]
    if not isinstance(thresholds, list) or any(type(value) is not int for value in thresholds):
        raise ValueError("temperature_log_thresholds_c must be a list of integers")
    size = settings["window_size"]
    if (not isinstance(size, list) or len(size) != 2 or
            any(type(value) is not int or value < 640 for value in size)):
        raise ValueError("window_size must contain two integers of at least 640")
    for key in ("aravis_dll_dir", "aravis_serial", "aravis_ip", "aravis_host_ip"):
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise ValueError(f"{key} must be a nonempty string")
    for key in ('aravis_ip','aravis_host_ip'):
        ipaddress.IPv4Address(settings[key])
    dll_dir = Path(settings['aravis_dll_dir']).expanduser()
    settings['aravis_dll_dir'] = str((SETTINGS_PATH.parent / dll_dir).resolve())
    for key,low,high in [('aravis_frame_rate',1,16),('aravis_packet_delay_us',0,40),('aravis_warmup_s',0,30)]:
        value=settings[key]
        if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or not low<=value<=high:
            raise ValueError(f'{key} must be {low}..{high}')
    for key in ('aravis_freeze_white_balance','aravis_manage_network'):
        if type(settings[key]) is not bool: raise ValueError(key+' must be true or false')
    if not isinstance(settings["kinesis_sdk_root"], str) or not settings["kinesis_sdk_root"].strip():
        raise ValueError("kinesis_sdk_root must be a folder path")
    serials = settings["kinesis_serials"]
    if (not isinstance(serials, list) or not serials
            or any(not isinstance(s, str) or len(s) != 8 or not s.isascii()
                   or not s.isdigit() or not s.startswith("55") for s in serials)
            or len(set(serials)) != len(serials)):
        raise ValueError("kinesis_serials must list unique eight-digit K10CR2 serials")
    return settings


def save_settings(settings: dict) -> None:
    validated = dict(settings)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_name(f"settings.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(validated, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)
