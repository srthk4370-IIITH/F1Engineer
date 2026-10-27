"""Tiny local knowledge retrieval (keyword overlap scoring over markdown sections; corpus is small)."""
import re
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).parent.parent / "data" / "knowledge.md"
_WORD = re.compile(r"[a-z0-9]+")


@lru_cache(maxsize=1)
def _sections() -> list[tuple[str, str, set[str]]]:
    out = []
    for block in _PATH.read_text(encoding="utf-8").split("## ")[1:]:
        title, _, body = block.partition("\n")
        out.append((title.strip(), body.strip(), set(_WORD.findall((title + " " + body).lower()))))
    return out


def search(query: str, k: int = 2) -> list[dict]:
    q = set(_WORD.findall(query.lower()))
    scored = sorted(_sections(), key=lambda s: -(len(q & s[2]) + 3 * len(q & set(_WORD.findall(s[0].lower())))))
    return [{"title": t, "text": b} for t, b, w in scored[:k] if q & w]
