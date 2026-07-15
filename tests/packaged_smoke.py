"""Create and verify the synthetic fixture used by the packaged build smoke test."""

from __future__ import annotations

from pathlib import Path
import sys

from mutagen.flac import FLAC

from ncm_fixture import build_ncm, minimal_flac, minimal_png


EXPECTED_TITLE = "合成打包自测音轨"


def create(path: Path) -> None:
    metadata = {
        "musicName": EXPECTED_TITLE,
        "artist": [["自测艺术家", 0]],
        "album": "自测专辑",
        "format": "mp3",  # Deliberately wrong: detection must use file bytes.
        "publishTime": 1_704_067_200_000,
    }
    fixture = build_ncm(
        minimal_flac(tail_size=8192),
        metadata=metadata,
        cover=minimal_png(),
        cover_padding=37,
    )
    fixture.write(path)


def verify(path: Path) -> None:
    if not path.is_file() or path.read_bytes()[:4] != b"fLaC":
        raise SystemExit("packaged output is missing or is not FLAC")
    audio = FLAC(path)
    if audio.get("title") != [EXPECTED_TITLE]:
        raise SystemExit("packaged output title tag is missing")
    if audio.get("date") != ["2024-01-01"]:
        raise SystemExit("packaged output timestamp normalization failed")
    if not audio.pictures or audio.pictures[0].mime != "image/png":
        raise SystemExit("packaged output cover is missing")


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"create", "verify"}:
        print("usage: packaged_smoke.py create|verify PATH", file=sys.stderr)
        return 64
    path = Path(argv[2])
    if argv[1] == "create":
        create(path)
    else:
        verify(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
