"""Coarse floor map of where the disturbance probably is, from which links are (and are not) disturbed.

Each link is sensitive inside its first Fresnel zone. A cell near disturbed links and far from calm links
scores high. With a single link the map is a band along that link: the position along it is unknown.
"""

from __future__ import annotations

import math

import numpy as np

from .config import node_map

LIGHT = 0.2998  # speed of light in m/ns, so wavelength = LIGHT / GHz


def fresnel_radius(length: float, ghz: float) -> float:
    """Largest radius of the first Fresnel zone, halfway along the link."""
    return 0.5 * math.sqrt(LIGHT / ghz * length)


class Localizer:
    def __init__(self, cfg: dict, cell: float = 0.25):
        room = cfg["room"]
        nodes = node_map(cfg)
        self.cols = max(2, round(room["width"] / cell))
        self.rows = max(2, round(room["depth"] / cell))
        xs = (np.arange(self.cols) + 0.5) * room["width"] / self.cols
        zs = (np.arange(self.rows) + 0.5) * room["depth"] / self.rows
        self.gx, self.gz = np.meshgrid(xs, zs)
        self.ids = [link["id"] for link in cfg["links"]]
        self.sensitivity = {}
        for link in cfg["links"]:
            a, b = nodes[link["tx"]]["position"], nodes[link["rx"]]["position"]
            length = max(math.dist(a, b), 0.1)
            # People are tall: the floor projection of the link is what a standing body can cross.
            distance = self._segment_distance(a[0], a[2], b[0], b[2])
            width = fresnel_radius(length, link["ghz"]) + 0.35
            self.sensitivity[link["id"]] = np.exp(-((distance / width) ** 2))

    def _segment_distance(self, ax, az, bx, bz):
        dx, dz = bx - ax, bz - az
        span = dx * dx + dz * dz
        if span < 1e-9:
            return np.hypot(self.gx - ax, self.gz - az)
        t = np.clip(((self.gx - ax) * dx + (self.gz - az) * dz) / span, 0, 1)
        return np.hypot(self.gx - (ax + t * dx), self.gz - (az + t * dz))

    def update(self, scores: dict):
        """(heat grid rows×cols in 0..1 or None, estimated [x, 0, z] or None) for per-link disturbance scores."""
        observed = {i: min(max(max(scores.get(i, {}).get("motion", 0), scores.get(i, {}).get("presence", 0)) / 1.5, 0), 1)
                    for i in self.ids}
        if max(observed.values(), default=0) < 0.35:
            return None, None
        log_like = np.zeros_like(self.gx)
        for link_id, p in observed.items():
            e = self.sensitivity[link_id]
            log_like += np.log(0.05 + e * p + (1 - e) * (1 - p))
        heat = np.exp(log_like - log_like.max())
        position = None
        if len(self.ids) >= 2:
            mask = heat >= 0.5
            weights = heat * mask
            total = float(weights.sum())
            if total > 0:
                position = [round(float((self.gx * weights).sum() / total), 2), 0.0,
                            round(float((self.gz * weights).sum() / total), 2)]
        return heat, position
