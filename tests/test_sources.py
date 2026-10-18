import numpy as np

from wifisense.buffer import LinkBuffer
from wifisense.sources.esp32 import parse_csi_line
from wifisense.sources.rssi import checksum, default_gateway, parse_iw_link, parse_proc_wireless

PROC = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   58.  -52.  -256        0      0      0      0     27        0
  eth0: 0000   70.  204.  0          0      0      0      0      0        0
"""


def test_proc_wireless_dbm_and_unsigned_byte():
    assert parse_proc_wireless(PROC, "wlan0") == (58.0, -52.0, None)
    assert parse_proc_wireless(PROC, "eth0")[1] == -52.0  # 204 as an unsigned byte
    assert parse_proc_wireless(PROC, "wlan1") is None


def test_iw_link():
    text = """Connected to a4:2b:b0:11:22:33 (on wlan0)
\tSSID: Otthon
\tfreq: 5180.0
\tsignal: -48 dBm
\ttx bitrate: 433.3 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 1
"""
    assert parse_iw_link(text) == {"bssid": "a4:2b:b0:11:22:33", "ssid": "Otthon", "freq_mhz": 5180,
                                   "signal": -48.0, "tx_bitrate": "433.3 Mbit/s"}
    assert parse_iw_link("Not connected.") == {}


def test_default_gateway(tmp_path):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t202\t00000000\n"
        "wlan0\t00000000\t0102A8C0\t0003\t0\t0\t303\t00000000\n"
        "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t303\t00FFFFFF\n"
    )
    assert default_gateway("wlan0", route) == "192.168.2.1"
    assert default_gateway("wlan1", route) is None


def test_icmp_checksum_verifies_to_zero():
    packet = bytes([8, 0, 0, 0, 0, 0, 0, 1]) + b"wifisense"
    value = checksum(packet)
    filled = packet[:2] + value.to_bytes(2, "big") + packet[4:]
    assert checksum(filled) == 0


def test_esp32_csi_line():
    line = ('CSI_DATA,12,aa:bb:cc:dd:ee:ff,-47,11,1,7,0,1,1,1,0,0,0,-92,0,6,0,123456,0,64,0,8,0,'
            '"[3,4,0,5,-6,8,1,0]"')
    rssi, amps = parse_csi_line(line)
    assert rssi == -47
    np.testing.assert_allclose(amps, [5, 5, 10, 1])
    assert parse_csi_line(line, mac_filter="11:22:33:44:55:66") is None
    assert parse_csi_line(line, mac_filter="AA:BB:CC:DD:EE:FF") is not None
    assert parse_csi_line("I (123) wifi: connected") is None
    assert parse_csi_line('CSI_DATA,1,aa,-40,"[1,2,3]"') is None  # odd count


def test_buffer_window_and_staleness():
    now = [100.0]
    buffer = LinkBuffer(clock=lambda: now[0])
    for i in range(20):
        buffer.append(-50.0 - i % 2, [1, 2, 3, 4], t=90 + i * 0.5)
    buffer.append(-40.0, [1, 2, 3], t=99.9)  # a different subcarrier count is a different frame type
    times, rssi, amps = buffer.window(3.0, 100.0)
    assert len(rssi) == 7 and rssi[-1] == -40
    assert amps.shape[1] == 4 and len(amps) == 6  # the lone 3-subcarrier frame is left out
    assert buffer.info(5, 100.0)["state"] == "ok"
    assert buffer.info(5, 110.0)["state"] == "stale"


def test_rssi_source_reads_proc_file(tmp_path, demo_cfg):
    import time

    from wifisense.sources.rssi import RssiSource

    proc = tmp_path / "wireless"
    proc.write_text(PROC)
    demo_cfg["rssi"].update(proc_path=str(proc), poke=False, sample_hz=50)
    buffer = LinkBuffer()
    source = RssiSource({"id": "pi-router"}, demo_cfg, buffer)
    source.iw = None  # no iw subprocess in tests
    source.start()
    try:
        deadline = time.monotonic() + 3
        while buffer.total < 5 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        source.close()
    _, rssi, _ = buffer.window(10)
    assert len(rssi) >= 5 and set(rssi) == {-52.0}
    assert buffer.info(5)["state"] == "ok" and buffer.meta["interface"] == "wlan0"
