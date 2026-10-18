"""ESP32 channel state information (CSI) over USB serial or UDP, in the Espressif esp-csi text format.

A CSI line holds, per received frame, the complex channel response of every OFDM subcarrier. Bodies change
the multipath pattern, so CSI sees much smaller movements than RSSI, and still people through the profile.

Line format (esp-csi csi_recv / csi_recv_router examples):
    CSI_DATA,<seq>,<mac>,<rssi>,<rate>,...,<len>,<first_word>,"[i0,r0,i1,r1,...]"
Amplitude per subcarrier = hypot of each (imaginary, real) int8 pair.
"""

from __future__ import annotations

import logging
import socket

import numpy as np

from .base import Source

log = logging.getLogger(__name__)
MAC_FIELD, RSSI_FIELD = 2, 3


def parse_csi_line(line: str, mac_filter: str | None = None):
    """(rssi, amplitudes) or None for anything that is not a usable CSI_DATA line."""
    if not line.startswith("CSI_DATA"):
        return None
    head, bracket, tail = line.partition("[")
    if not bracket:
        return None
    fields = [f.strip() for f in head.split(",")]
    if len(fields) <= RSSI_FIELD:
        return None
    if mac_filter and fields[MAC_FIELD].lower() != mac_filter.lower():
        return None
    try:
        rssi = float(fields[RSSI_FIELD])
        values = np.array([int(v) for v in tail.split("]")[0].replace(" ", ",").split(",") if v.strip()], dtype=np.float32)
    except ValueError:
        return None
    if len(values) < 8 or len(values) % 2:
        return None
    pairs = values.reshape(-1, 2)
    return rssi, np.hypot(pairs[:, 0], pairs[:, 1])


class Esp32Source(Source):
    name = "esp32"

    def __init__(self, link: dict, cfg: dict, buffer):
        super().__init__(link["id"])
        self.buffer = buffer
        self.serial_port = link.get("serial")
        self.udp_port = link.get("udp_port")
        self.baud = int(link.get("baud") or cfg["esp32"]["baud"])
        self.mac = link.get("mac")
        self.handle = None

    def on_error(self, exc):
        self.buffer.set_state("error", str(exc))

    def _interrupt(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _feed(self, line: str):
        parsed = parse_csi_line(line.strip(), self.mac)
        if parsed is not None:
            self.buffer.append(parsed[0], parsed[1])

    def run(self):
        if self.udp_port:
            self._run_udp()
        else:
            self._run_serial()

    def _run_udp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", int(self.udp_port)))
        sock.settimeout(1.0)
        self.handle = sock
        self.buffer.set_state("starting", f"UDP :{self.udp_port} várakozás", kind="csi", transport=f"udp:{self.udp_port}")
        while not self.stopping.is_set():
            try:
                data = sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                if self.stopping.is_set():
                    return
                raise
            for line in data.decode("ascii", "ignore").splitlines():
                self._feed(line)

    def _run_serial(self):
        try:
            import serial  # pyserial: optional, only needed for USB-connected ESP32 boards
        except ImportError:
            raise RuntimeError("pyserial nincs telepítve (pip install pyserial)") from None
        port = serial.Serial(self.serial_port, self.baud, timeout=1)
        self.handle = port
        self.buffer.set_state("starting", f"{self.serial_port} várakozás", kind="csi", transport="serial")
        pending = b""
        while not self.stopping.is_set():
            try:
                chunk = port.read(4096)
            except (OSError, TypeError):
                if self.stopping.is_set():
                    return
                raise
            if not chunk:
                continue
            pending += chunk
            *lines, pending = pending.split(b"\n")
            if len(pending) > 65536:  # garbage without newlines: resynchronise
                pending = b""
            for line in lines:
                self._feed(line.decode("ascii", "ignore"))
