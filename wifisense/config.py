"""Defaults < YAML < environment; integrations are activated by environment only."""

from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import re

import yaml

DEFAULTS = {
    # Room box in metres: x = width, y = height, z = depth; the origin is a floor corner.
    "room": {"width": 5.0, "depth": 4.0, "height": 2.6},
    "nodes": [
        {"id": "router", "name": "Router", "kind": "ap", "position": [0.4, 1.8, 0.4]},
        {"id": "pi", "name": "Raspberry Pi", "kind": "sensor", "position": [4.5, 1.0, 3.5]},
    ],
    "links": [
        {"id": "pi-router", "name": "Pi ↔ router", "source": "rssi", "tx": "router", "rx": "pi"},
    ],
    # auto: every link uses its own source; sim: every link is simulated (development, demo).
    "source": {"mode": "auto"},
    "rssi": {"interface": "wlan0", "sample_hz": 10, "poke": True, "poke_hz": 10, "proc_path": "/proc/net/wireless"},
    "esp32": {"baud": 921600},
    "sim": {"auto": True, "seed": None},
    "detection": {
        "tick_seconds": 0.5,
        "window_seconds": 3.0,
        "warmup_seconds": 20,
        # Robust z-scores against the empty-room baseline; a score of 1.0 in the UI is this threshold.
        "motion_threshold": 4.0,
        "presence_threshold": 5.0,
        "trigger_seconds": 1.0,
        "hold_seconds": 45,
        "human_threshold": 0.6,
        "confirm_seconds": 2.0,
        # A level shift without movement keeps an episode open at most this long, then becomes the baseline.
        "static_max_seconds": 600,
        # Baseline centres follow slow drift while the room is empty (time constant).
        "adapt_seconds": 600,
        "stale_seconds": 5,
    },
    "calibration": {"seconds": 60},
    "alarm": {"trigger": "human", "arm_delay_seconds": 60, "cooldown_seconds": 120},
    "notifications": {"enabled": False, "url": "", "topic": "", "token": "", "webhook_url": ""},
    "storage": {"retention_days": 30},
    "web": {"enabled": True, "host": "0.0.0.0", "port": 8010},
    "logging": {"level": "INFO"},
    "data_dir": "./data",
}

# Built-in layout for --demo: one RSSI link and two ESP32 CSI links across a living room.
DEMO_LAYOUT = {
    "room": {"width": 6.0, "depth": 4.5, "height": 2.6},
    "nodes": [
        {"id": "router", "name": "Router", "kind": "ap", "position": [0.35, 1.9, 0.35]},
        {"id": "pi", "name": "Raspberry Pi", "kind": "sensor", "position": [5.6, 1.1, 4.1]},
        {"id": "esp-window", "name": "ESP32 · ablak", "kind": "sensor", "position": [5.7, 0.9, 0.5]},
        {"id": "esp-door", "name": "ESP32 · ajtó", "kind": "sensor", "position": [0.4, 1.0, 4.1]},
    ],
    "links": [
        {"id": "pi-router", "name": "Pi ↔ router", "source": "rssi", "tx": "router", "rx": "pi"},
        {"id": "window-router", "name": "Ablak ↔ router", "source": "esp32", "tx": "router", "rx": "esp-window"},
        {"id": "door-router", "name": "Ajtó ↔ router", "source": "esp32", "tx": "router", "rx": "esp-door"},
    ],
}

SOURCES = ("rssi", "esp32", "sim")
NODE_KINDS = ("ap", "sensor")
ALARM_TRIGGERS = ("human", "any")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    pass


def boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("true", "1", "yes", "on"):
        return True
    if normalized in ("false", "0", "no", "off"):
        return False
    raise ConfigError("expected true or false")


# Every supported variable is documented in .env.example. NTFY_* and LOG_LEVEL are shared with the camera stack.
ENV_MAP = {
    "WIFI_SOURCE": ("source", "mode", str),
    "WIFI_INTERFACE": ("rssi", "interface", str),
    "WIFI_SAMPLE_HZ": ("rssi", "sample_hz", float),
    "WIFI_POKE": ("rssi", "poke", boolean),
    "WIFI_POKE_HZ": ("rssi", "poke_hz", float),
    "WIFI_ESP32_BAUD": ("esp32", "baud", int),
    "WIFI_SIM_AUTO": ("sim", "auto", boolean),
    "WIFI_WINDOW_SECONDS": ("detection", "window_seconds", float),
    "WIFI_WARMUP_SECONDS": ("detection", "warmup_seconds", float),
    "WIFI_MOTION_THRESHOLD": ("detection", "motion_threshold", float),
    "WIFI_PRESENCE_THRESHOLD": ("detection", "presence_threshold", float),
    "WIFI_TRIGGER_SECONDS": ("detection", "trigger_seconds", float),
    "WIFI_HOLD_SECONDS": ("detection", "hold_seconds", float),
    "WIFI_HUMAN_THRESHOLD": ("detection", "human_threshold", float),
    "WIFI_CONFIRM_SECONDS": ("detection", "confirm_seconds", float),
    "WIFI_STATIC_MAX_SECONDS": ("detection", "static_max_seconds", float),
    "WIFI_ADAPT_SECONDS": ("detection", "adapt_seconds", float),
    "WIFI_CALIBRATION_SECONDS": ("calibration", "seconds", float),
    "WIFI_ALARM_TRIGGER": ("alarm", "trigger", str),
    "WIFI_ARM_DELAY_SECONDS": ("alarm", "arm_delay_seconds", float),
    "WIFI_ALARM_COOLDOWN_SECONDS": ("alarm", "cooldown_seconds", float),
    "NTFY_URL": ("notifications", "url", str),
    "NTFY_TOPIC": ("notifications", "topic", str),
    "NTFY_TOKEN": ("notifications", "token", str),
    "WIFI_WEBHOOK_URL": ("notifications", "webhook_url", str),
    "WIFI_RETENTION_DAYS": ("storage", "retention_days", float),
    "WIFI_WEB_ENABLED": ("web", "enabled", boolean),
    "WIFI_WEB_HOST": ("web", "host", str),
    "WIFI_WEB_PORT": ("web", "port", int),
    "LOG_LEVEL": ("logging", "level", str),
}
# Credentials and outbound integrations must not live in the committed YAML.
ENV_ONLY = {("notifications", key) for key in ("url", "topic", "token", "webhook_url")}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _position(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ConfigError(f"{label}: position must be [x, y, z] in metres")
    try:
        result = [float(v) for v in value]
    except (TypeError, ValueError):
        raise ConfigError(f"{label}: position must be numeric") from None
    if not all(math.isfinite(v) for v in result):
        raise ConfigError(f"{label}: position must be finite")
    return result


def _positive(cfg, section, key, allow_zero=False):
    value = cfg[section][key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{section}.{key} must be a number")
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{section}.{key} must be {'non-negative' if allow_zero else 'positive'}")


def validate(cfg: dict) -> dict:
    for key in ("width", "depth", "height"):
        _positive(cfg, "room", key)
    nodes = cfg["nodes"]
    if not isinstance(nodes, list) or not nodes:
        raise ConfigError("nodes: at least one node is required")
    ids = set()
    for node in nodes:
        if not isinstance(node, dict) or not ID_PATTERN.match(str(node.get("id", ""))):
            raise ConfigError("nodes: every node needs a lowercase id (a-z, 0-9, - and _)")
        if node["id"] in ids:
            raise ConfigError(f"nodes: duplicate id {node['id']}")
        ids.add(node["id"])
        node["position"] = _position(node.get("position"), f"node {node['id']}")
        node.setdefault("name", node["id"])
        node.setdefault("kind", "sensor")
        if node["kind"] not in NODE_KINDS:
            raise ConfigError(f"node {node['id']}: kind must be one of {', '.join(NODE_KINDS)}")
    links = cfg["links"]
    if not isinstance(links, list) or not links:
        raise ConfigError("links: at least one link is required")
    link_ids = set()
    for link in links:
        if not isinstance(link, dict) or not ID_PATTERN.match(str(link.get("id", ""))):
            raise ConfigError("links: every link needs a lowercase id (a-z, 0-9, - and _)")
        if link["id"] in link_ids:
            raise ConfigError(f"links: duplicate id {link['id']}")
        link_ids.add(link["id"])
        for end in ("tx", "rx"):
            if link.get(end) not in ids:
                raise ConfigError(f"link {link['id']}: {end} must name a node")
        if link["tx"] == link["rx"]:
            raise ConfigError(f"link {link['id']}: tx and rx must differ")
        if link.get("source") not in SOURCES:
            raise ConfigError(f"link {link['id']}: source must be one of {', '.join(SOURCES)}")
        if link["source"] == "esp32" and cfg["source"]["mode"] != "sim":
            if bool(link.get("serial")) == bool(link.get("udp_port")):
                raise ConfigError(f"link {link['id']}: esp32 needs exactly one of serial or udp_port")
        link.setdefault("name", link["id"])
        # 2.4 GHz unless stated: only used for the Fresnel-zone size in the 3D view and the simulator.
        link.setdefault("ghz", 2.4)
    if cfg["source"]["mode"] not in ("auto", "sim"):
        raise ConfigError("source.mode must be auto or sim")
    for key in ("sample_hz", "poke_hz"):
        _positive(cfg, "rssi", key)
    if not 1 <= cfg["rssi"]["sample_hz"] <= 100:
        raise ConfigError("rssi.sample_hz must be between 1 and 100")
    for key in ("tick_seconds", "window_seconds", "motion_threshold", "presence_threshold", "adapt_seconds",
                "stale_seconds", "human_threshold"):
        _positive(cfg, "detection", key)
    for key in ("warmup_seconds", "trigger_seconds", "hold_seconds", "confirm_seconds", "static_max_seconds"):
        _positive(cfg, "detection", key, allow_zero=True)
    detection = cfg["detection"]
    if detection["window_seconds"] < 2 * detection["tick_seconds"]:
        raise ConfigError("detection.window_seconds must be at least two ticks")
    if detection["human_threshold"] > 1:
        raise ConfigError("detection.human_threshold must be between 0 and 1")
    _positive(cfg, "calibration", "seconds")
    if cfg["calibration"]["seconds"] < 10:
        raise ConfigError("calibration.seconds must be at least 10")
    for key in ("arm_delay_seconds", "cooldown_seconds"):
        _positive(cfg, "alarm", key, allow_zero=True)
    if cfg["alarm"]["trigger"] not in ALARM_TRIGGERS:
        raise ConfigError("alarm.trigger must be human or any")
    _positive(cfg, "storage", "retention_days")
    if not 1 <= int(cfg["web"]["port"]) <= 65535:
        raise ConfigError("web.port must be between 1 and 65535")
    cfg["logging"]["level"] = str(cfg["logging"]["level"]).upper()
    if cfg["logging"]["level"] not in LOG_LEVELS:
        raise ConfigError(f"logging.level must be one of {', '.join(LOG_LEVELS)}")
    notifications = cfg["notifications"]
    for key in ("url", "webhook_url"):
        url = notifications[key].strip().rstrip("/")
        if url and not re.match(r"^https?://[^/\s]+", url):
            raise ConfigError(f"{key}: expected an http(s) URL")
        notifications[key] = url
    notifications["topic"] = notifications["topic"].strip().strip("/")
    notifications["enabled"] = bool(notifications["url"] and notifications["topic"])
    return cfg


def load_config(path: str | None = "config.yaml", environ=None, demo: bool = False) -> dict:
    environ = os.environ if environ is None else environ
    cfg = copy.deepcopy(DEFAULTS)
    if path and Path(path).is_file():
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: expected a mapping")
        for section, key in ENV_ONLY:
            if key in (loaded.get(section) or {}):
                raise ConfigError(f"{section}.{key} is environment-only; set it in .env")
        cfg = merge(cfg, loaded)
    if demo:
        cfg = merge(cfg, DEMO_LAYOUT)
        # The layout lists are replaced, not merged, so the demo never mixes with a real installation.
        cfg["nodes"], cfg["links"] = copy.deepcopy(DEMO_LAYOUT["nodes"]), copy.deepcopy(DEMO_LAYOUT["links"])
        cfg["source"]["mode"] = "sim"
    for name, (section, key, cast) in ENV_MAP.items():
        raw = environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            cfg[section][key] = cast(raw)
        except (ValueError, ConfigError) as exc:
            raise ConfigError(f"{name}: {exc}") from None
    if demo:
        cfg["source"]["mode"] = "sim"
    data_dir = environ.get("WIFI_DATA_DIR") or cfg["data_dir"]
    cfg["data_dir"] = str(Path(data_dir).expanduser())
    return validate(cfg)


def node_map(cfg: dict) -> dict:
    return {node["id"]: node for node in cfg["nodes"]}


def public_layout(cfg: dict) -> dict:
    """Room, nodes and links for the 3D view; no transport details (serial ports, sockets)."""
    nodes = node_map(cfg)
    links = []
    for link in cfg["links"]:
        tx, rx = nodes[link["tx"]]["position"], nodes[link["rx"]]["position"]
        links.append({
            "id": link["id"],
            "name": link["name"],
            "source": "sim" if cfg["source"]["mode"] == "sim" else link["source"],
            "kind": "csi" if link["source"] == "esp32" else "rssi",
            "tx": link["tx"],
            "rx": link["rx"],
            "ghz": link["ghz"],
            "length": round(math.dist(tx, rx), 2),
        })
    return {"room": cfg["room"], "nodes": cfg["nodes"], "links": links}
