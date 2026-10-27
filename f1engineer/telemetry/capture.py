"""Read/write .f1cap raw packet captures (see tools/record_udp.py for the format)."""
import struct
from pathlib import Path
from typing import BinaryIO, Iterator

MAGIC = b"F1CAP001"
_REC = struct.Struct("<dH")


def read_capture(path: str | Path) -> Iterator[tuple[float, bytes]]:
    """Yield (recv_time, raw_packet). Tolerates a truncated last record."""
    with open(path, "rb") as f:
        if f.read(8) != MAGIC:
            raise ValueError(f"{path} is not an f1cap file")
        while True:
            hdr = f.read(_REC.size)
            if len(hdr) < _REC.size:
                return
            t, n = _REC.unpack(hdr)
            data = f.read(n)
            if len(data) < n:
                return
            yield t, data


class CaptureWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f: BinaryIO = open(self.path, "wb")
        self._f.write(MAGIC)

    def write(self, t: float, data: bytes) -> None:
        self._f.write(_REC.pack(t, len(data)))
        self._f.write(data)

    def close(self) -> None:
        self._f.close()
