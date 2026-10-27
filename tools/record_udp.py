"""Record raw F1 UDP telemetry to a capture file for replay/testing.

File format (.f1cap): 8-byte magic b"F1CAP001", then repeated records of
  <f64 recv_time_s><u16 length><length bytes raw packet>
Stdlib only, so it runs before any dependencies are installed.

Usage: python tools/record_udp.py [--port 20777] [--out data/captures/NAME.f1cap]
Stop with Ctrl+C.
"""
import argparse
import socket
import struct
import time
from collections import Counter
from pathlib import Path

MAGIC = b"F1CAP001"
PACKET_NAMES = {
    0: "Motion", 1: "Session", 2: "LapData", 3: "Event", 4: "Participants",
    5: "CarSetups", 6: "CarTelemetry", 7: "CarStatus", 8: "FinalClassification",
    9: "LobbyInfo", 10: "CarDamage", 11: "SessionHistory", 12: "TyreSets",
    13: "MotionEx", 14: "TimeTrial", 15: "LapPositions",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = Path(args.out or f"data/captures/capture_{time.strftime('%Y%m%d_%H%M%S')}.f1cap")
    out.parent.mkdir(parents=True, exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind((args.host, args.port))
    sock.settimeout(1.0)

    counts: Counter = Counter()
    total = 0
    fmt_seen = None
    last_print = time.time()
    print(f"Listening on {args.host}:{args.port} -> {out}  (Ctrl+C to stop)")
    with out.open("wb") as f:
        f.write(MAGIC)
        try:
            while True:
                try:
                    data, _ = sock.recvfrom(4096)
                except socket.timeout:
                    data = None
                now = time.time()
                if data:
                    f.write(struct.pack("<dH", now, len(data)))
                    f.write(data)
                    total += 1
                    if len(data) >= 6:
                        fmt_seen = struct.unpack_from("<H", data, 0)[0]
                        counts[data[5]] += 1
                if now - last_print >= 2.0:
                    summary = ", ".join(f"{PACKET_NAMES.get(k, k)}={v}" for k, v in sorted(counts.items()))
                    print(f"[{time.strftime('%H:%M:%S')}] packets={total} format={fmt_seen} | {summary or 'waiting for game...'}")
                    f.flush()
                    last_print = now
        except KeyboardInterrupt:
            pass
    print(f"Saved {total} packets ({out.stat().st_size / 1e6:.1f} MB) to {out}")


if __name__ == "__main__":
    main()
