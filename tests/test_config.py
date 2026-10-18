import pytest

from wifisense.config import ConfigError, load_config, public_layout


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


def test_defaults_are_valid(tmp_path):
    cfg = load_config(None, environ={"WIFI_DATA_DIR": str(tmp_path)})
    assert cfg["source"]["mode"] == "auto"
    assert cfg["links"][0]["source"] == "rssi"
    assert cfg["notifications"]["enabled"] is False
    assert cfg["web"]["port"] == 8010


def test_environment_wins_and_enables_ntfy(tmp_path):
    cfg = load_config(None, environ={
        "WIFI_DATA_DIR": str(tmp_path), "NTFY_URL": "https://ntfy.sh/", "NTFY_TOPIC": "/home/",
        "WIFI_MOTION_THRESHOLD": "6", "WIFI_POKE": "off", "LOG_LEVEL": "debug",
    })
    assert cfg["notifications"]["enabled"] is True
    assert cfg["notifications"]["url"] == "https://ntfy.sh"
    assert cfg["notifications"]["topic"] == "home"
    assert cfg["detection"]["motion_threshold"] == 6
    assert cfg["rssi"]["poke"] is False
    assert cfg["logging"]["level"] == "DEBUG"


def test_demo_replaces_layout_and_forces_sim(tmp_path):
    cfg = load_config(None, environ={"WIFI_DATA_DIR": str(tmp_path), "WIFI_SOURCE": "auto"}, demo=True)
    assert cfg["source"]["mode"] == "sim"
    assert len(cfg["links"]) == 3
    layout = public_layout(cfg)
    assert {link["kind"] for link in layout["links"]} == {"rssi", "csi"}
    assert all(link["source"] == "sim" for link in layout["links"])


@pytest.mark.parametrize("yaml_text, message", [
    ("notifications: {topic: secret}\n", "environment-only"),
    ("links: [{id: a, source: rssi, tx: router, rx: nowhere}]\n", "rx must name a node"),
    ("links: [{id: a, source: esp32, tx: router, rx: pi}]\n", "serial or udp_port"),
    ("links: [{id: a, source: wifi, tx: router, rx: pi}]\n", "source must be"),
    ("nodes: [{id: Router, position: [0, 0, 0]}]\n", "lowercase id"),
    ("nodes: [{id: router, position: [0, 0]}]\n", "position"),
    ("detection: {window_seconds: 0.5}\n", "two ticks"),
    ("alarm: {trigger: cat}\n", "human or any"),
])
def test_invalid_yaml(tmp_path, yaml_text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, yaml_text), environ={"WIFI_DATA_DIR": str(tmp_path)})


def test_bad_environment_value(tmp_path):
    with pytest.raises(ConfigError, match="WIFI_HOLD_SECONDS"):
        load_config(None, environ={"WIFI_DATA_DIR": str(tmp_path), "WIFI_HOLD_SECONDS": "soon"})


def test_esp32_link_from_yaml(tmp_path):
    cfg = load_config(write(tmp_path, """
nodes:
  - {id: router, kind: ap, position: [0.3, 1.8, 0.3]}
  - {id: esp, position: [4, 1, 3]}
links:
  - {id: esp-router, source: esp32, tx: router, rx: esp, udp_port: 5566, ghz: 5}
"""), environ={"WIFI_DATA_DIR": str(tmp_path)})
    link = public_layout(cfg)["links"][0]
    assert link["kind"] == "csi" and link["ghz"] == 5 and link["length"] == 4.65
    assert "udp_port" not in link  # transport details stay out of the public API
