from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MEDIA_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic"
PLAYBACK_MEDIA = {
    "movie:30001": "playback-movie.mkv",
    "movie:30002": "playback-movie.mkv",
    "series:50001": "playback-episode.mkv",
    "series:50002": "playback-episode.mkv",
}


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int
    total: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"


def parse_single_range(value: str | None, total: int) -> ByteRange | None:
    if not value:
        return None
    if total <= 0 or not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid or unsupported byte range")
    spec = value[6:].strip()
    if "-" not in spec:
        raise ValueError("invalid byte range")
    start_text, end_text = spec.split("-", 1)
    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        start = max(0, total - suffix)
        return ByteRange(start, total - 1, total)
    start = int(start_text)
    if start < 0 or start >= total:
        raise ValueError("unsatisfiable byte range")
    end = total - 1 if not end_text else min(int(end_text), total - 1)
    if end < start:
        raise ValueError("unsatisfiable byte range")
    return ByteRange(start, end, total)


def deterministic_media(marker: str, size: int = 2_097_152) -> bytes:
    header = b"\x1aE\xdf\xa3M3UNDLE-LAB-MATROSKA\x00"
    seed = (marker + "|").encode("utf-8")
    payload = header + seed
    repeats = (size + len(payload) - 1) // len(payload)
    return (payload * repeats)[:size]


def playback_media(marker: str) -> bytes:
    filename = PLAYBACK_MEDIA.get(marker)
    if filename is None:
        raise KeyError(f"No playback media fixture for {marker!r}")
    return (MEDIA_DIR / filename).read_bytes()
