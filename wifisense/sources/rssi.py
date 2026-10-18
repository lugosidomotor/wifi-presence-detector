"""RSSI of the host's own WiFi connection (Linux, any driver): no extra hardware.

The Raspberry Pi measures the signal of every frame it receives from its access point. A person moving
through or near the line between the two makes that level fluctuate. /proc/net/wireless is read
unprivileged; inside Docker it needs network_mode: host to see the host's wlan0.

Optional "poke": tiny ICMP echo (or UDP) packets to the gateway keep fresh frames arriving between beacons.
"""

from __future__ import annotations

import logging
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import time

from .base import Source

log = logging.getLogger(__name__)


def parse_proc_wireless(text: str, interface: str):
    """(link quality, level dBm, noise dBm or None) of one interface, or None when it is not listed."""
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep or name.strip() != interface:
            continue
        fields = rest.split()
        if len(fields) < 3:
            return None
        try:
            quality = float(fields[1].rstrip("."))
            level = float(fields[2].rstrip("."))
            noise = float(fields[3].rstrip(".")) if len(fields) > 3 else None
        except ValueError:
            return None
        # Old wireless extensions report dBm as an unsigned byte (e.g. 204 means -52 dBm).
        if level > 0:
            level -= 256
        if noise is not None and (noise > 0 or noise <= -256):
            noise = None if noise <= -256 else noise - 256
        return quality, level, noise
    return None


def parse_iw_link(text: str) -> dict:
    """SSID, BSSID, frequency and bitrate from `iw dev <if> link`; empty when not connected."""
    info = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Connected to "):
            info["bssid"] = line.split()[2]
        elif line.startswith("SSID:"):
            info["ssid"] = line[5:].strip()
        elif line.startswith("freq:"):
            try:
                info["freq_mhz"] = round(float(line.split()[1]))
            except (IndexError, ValueError):
                pass
        elif line.startswith("signal:"):
            try:
                info["signal"] = float(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("tx bitrate:"):
            info["tx_bitrate"] = line.split(":", 1)[1].strip().split(" MBit")[0] + " Mbit/s"
    return info


def default_gateway(interface: str, route_path: str = "/proc/net/route"):
    """IPv4 default gateway of the interface from the kernel routing table, or None."""
    try:
        lines = Path(route_path).read_text().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[0] == interface and fields[1] == "00000000":
            return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    return None


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


class Poker:
    """Unprivileged ICMP echo (Linux ping sockets) or, failing that, UDP to the discard port."""

    def __init__(self, gateway: str):
        self.gateway = gateway
        self.seq = 0
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
            self.kind = "icmp"
        except OSError:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.kind = "udp"
        self.sock.setblocking(False)

    def send(self):
        self.seq = (self.seq + 1) & 0xFFFF
        try:
            if self.kind == "icmp":
                header = struct.pack("!BBHHH", 8, 0, 0, 0, self.seq)
                payload = b"wifisense"
                packet = struct.pack("!BBHHH", 8, 0, checksum(header + payload), 0, self.seq) + payload
                self.sock.sendto(packet, (self.gateway, 0))
            else:
                self.sock.sendto(b"wifisense", (self.gateway, 9))
            while True:  # drain replies; only their reception matters
                self.sock.recv(512)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            pass

    def close(self):
        self.sock.close()


class RssiSource(Source):
    name = "rssi"
    META_SECONDS = 15

    def __init__(self, link: dict, cfg: dict, buffer):
        super().__init__(link["id"])
        rssi = cfg["rssi"]
        self.buffer = buffer
        self.interface = link.get("interface") or rssi["interface"]
        self.proc = Path(rssi["proc_path"])
        self.period = 1 / rssi["sample_hz"]
        self.poke_period = 1 / rssi["poke_hz"] if rssi["poke"] else None
        self.iw = shutil.which("iw")

    def on_error(self, exc):
        self.buffer.set_state("error", f"{self.interface}: {exc}")

    def _meta(self):
        if not self.iw:
            return {}
        try:
            result = subprocess.run([self.iw, "dev", self.interface, "link"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return {}
        return parse_iw_link(result.stdout)

    def _power_save(self):
        """True when the WiFi chip sleeps between beacons: fewer fresh RSSI readings."""
        if not self.iw:
            return None
        try:
            result = subprocess.run([self.iw, "dev", self.interface, "get", "power_save"],
                                    capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        answer = result.stdout.strip().lower()
        return answer.endswith("on") if answer.startswith("power save") else None

    def _read(self, use_proc: bool):
        """Current level in dBm, or None when the interface is not connected."""
        if use_proc:
            reading = parse_proc_wireless(self.proc.read_text(), self.interface)
            return None if reading is None or reading[1] <= -127 else reading[1]
        return self._meta().get("signal")

    def run(self):
        # Kernels without wireless extensions have no /proc/net/wireless: fall back to `iw ... link`.
        use_proc = self.proc.is_file()
        if not use_proc and not self.iw:
            raise RuntimeError("nincs /proc/net/wireless és iw sem: Linux WiFi-interfész kell (Dockerben network_mode: host)")
        if not use_proc:
            log.info("rssi %s: %s missing, reading %s through iw", self.label, self.proc, self.interface)
        power_save = self._power_save()
        if power_save:
            log.warning("rssi %s: power save is on; run 'sudo iw dev %s set power_save off' for steadier readings",
                        self.label, self.interface)
        self.buffer.set_state("starting", f"{self.interface} olvasása", interface=self.interface, kind="rssi",
                              power_save=power_save)
        poker = None
        gateway = default_gateway(self.interface) if self.poke_period else None
        if gateway:
            try:
                poker = Poker(gateway)
                log.info("rssi %s: poking gateway %s via %s", self.label, gateway, poker.kind)
            except OSError as exc:
                log.info("rssi %s: no poke socket (%s)", self.label, exc)
        next_sample = next_poke = next_meta = time.monotonic()
        missing_since = None
        try:
            while not self.stopping.is_set():
                now = time.monotonic()
                if poker and now >= next_poke:
                    poker.send()
                    next_poke = now + self.poke_period
                if now >= next_meta:
                    meta = self._meta()
                    meta.pop("signal", None)
                    self.buffer.set_state(self.buffer.state, self.buffer.message, **meta)
                    next_meta = now + self.META_SECONDS
                if now >= next_sample:
                    level = self._read(use_proc)
                    if level is None:
                        missing_since = missing_since or now
                        if now - missing_since > 3:
                            self.buffer.set_state("error", f"{self.interface}: nincs WiFi-kapcsolat")
                    else:
                        missing_since = None
                        self.buffer.append(level)
                    next_sample += self.period
                    if next_sample < now:  # fell behind (suspend, load): do not burst
                        next_sample = now + self.period
                wake = min(next_sample, next_poke if poker else next_sample)
                self.stopping.wait(max(0.0, wake - time.monotonic()))
        finally:
            if poker:
                poker.close()
