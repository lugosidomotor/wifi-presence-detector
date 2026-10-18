"""Window features of one link: RSSI spread and level, CSI amplitude spread and profile, mechanical periodicity."""

from __future__ import annotations

import numpy as np

MIN_SAMPLES = 5


def uniform(times: np.ndarray, values: np.ndarray):
    """Resample irregular packets onto an even grid so the spectrum is meaningful; returns (series, rate)."""
    duration = float(times[-1] - times[0])
    if duration <= 0:
        return values, 0.0
    grid = np.linspace(times[0], times[-1], len(times))
    return np.interp(grid, times, values), (len(times) - 1) / duration


def mechanical(times: np.ndarray, values: np.ndarray, min_hz: float = 1.5) -> float:
    """0..1: how much of the fluctuation is a single fast tone (fan, pump, washing machine) instead of a body.

    People move broadband and mostly below ~1.5 Hz; rotating machinery puts its energy into one sharp line.
    """
    if len(values) < 16:
        return 0.0
    series, rate = uniform(times, values)
    if rate <= 2 * min_hz:
        return 0.0
    index = np.arange(len(series))
    series = series - np.polyval(np.polyfit(index, series, 1), index)
    power = np.abs(np.fft.rfft(series * np.hanning(len(series)))) ** 2
    freqs = np.fft.rfftfreq(len(series), 1 / rate)
    everything, fast = power[freqs > 0.1], power[freqs >= min_hz]
    if len(fast) < 4 or everything.sum() <= 1e-12:
        return 0.0
    fast_share = fast.sum() / everything.sum()
    peak = fast.max() / fast.sum()
    flat = 3 / len(fast)  # a Hann-windowed tone covers about three bins
    if flat >= 0.4:
        return 0.0
    return float(fast_share * np.clip((peak - flat) / (0.4 - flat), 0, 1))


def extract(times: np.ndarray, rssi: np.ndarray, amps: np.ndarray | None) -> dict | None:
    """Features of one window, or None when the link delivered too few samples."""
    if len(rssi) < MIN_SAMPLES and (amps is None or len(amps) < MIN_SAMPLES):
        return None
    feats = {"samples": int(max(len(rssi), 0 if amps is None else len(amps)))}
    mech_times, mech_values = None, None
    if len(rssi) >= MIN_SAMPLES:
        feats["rssi_mean"] = float(np.mean(rssi))
        feats["rssi_std"] = float(np.std(rssi))
        mech_times, mech_values = times, rssi
    if amps is not None and len(amps) >= MIN_SAMPLES:
        level = amps.mean(axis=1, keepdims=True)
        level[level <= 1e-6] = 1.0
        # Per-packet normalisation removes the receiver's automatic gain control steps.
        normalized = amps / level
        feats["csi_std"] = float(normalized.std(axis=0).mean())
        feats["csi_profile"] = normalized.mean(axis=0)
        centered = normalized - feats["csi_profile"]
        try:
            _, _, vt = np.linalg.svd(centered, full_matrices=False)
            principal = centered @ vt[0]
        except np.linalg.LinAlgError:
            principal = centered.mean(axis=1)
        # CSI packets carry their own timestamps only through arrival order: spread them over the window.
        csi_times = np.linspace(times[0], times[-1], len(principal)) if len(times) >= 2 else np.arange(len(principal)) / 50
        mech_times, mech_values = csi_times, principal
    feats["mech"] = mechanical(mech_times, mech_values) if mech_values is not None else 0.0
    return feats


def profile_shift(profile: np.ndarray | None, reference: np.ndarray | None) -> float | None:
    """1 - correlation of the mean CSI amplitude profile against the empty-room profile."""
    if profile is None or reference is None or len(profile) != len(reference):
        return None
    mask = reference > 0.05 * max(float(reference.mean()), 1e-9)
    if mask.sum() < 4:
        return None
    a, b = profile[mask] - profile[mask].mean(), reference[mask] - reference[mask].mean()
    denominator = float(np.sqrt((a * a).sum() * (b * b).sum()))
    if denominator <= 1e-12:
        return None
    return float(1 - (a * b).sum() / denominator)
