"""Signal sources. Each one feeds a LinkBuffer from its own thread and never raises into the runtime."""

from __future__ import annotations

from .base import Source
from .esp32 import Esp32Source
from .rssi import RssiSource
from .sim import SimWorld

__all__ = ["Source", "Esp32Source", "RssiSource", "SimWorld", "build_sources"]


def build_sources(cfg: dict, buffers: dict, clock=None):
    """One source per link; in sim mode a single simulated room drives every link."""
    if cfg["source"]["mode"] == "sim" or all(link["source"] == "sim" for link in cfg["links"]):
        return [SimWorld(cfg, buffers, **({"clock": clock} if clock else {}))]
    sources = []
    for link in cfg["links"]:
        buffer = buffers[link["id"]]
        if link["source"] == "rssi":
            sources.append(RssiSource(link, cfg, buffer))
        elif link["source"] == "esp32":
            sources.append(Esp32Source(link, cfg, buffer))
        else:
            raise ValueError(f"link {link['id']}: sim links need source.mode=sim")
    return sources
