"""Command-line interface for the streaming NCM extractor.

SPDX-License-Identifier: GPL-2.0-or-later
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading
from typing import Iterable

try:
    from ncm_core import CancelledError, NCMError, extract_ncm
except ImportError:  # pragma: no cover
    from .ncm_core import CancelledError, NCMError, extract_ncm


VERSION = "1.0.0"


def _configure_stdio() -> None:
    """Prevent Windows legacy code pages from crashing on Unicode paths."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def _iter_inputs(inputs: Iterable[str], recursive: bool) -> list[tuple[Path, Path | None]]:
    found: list[tuple[Path, Path | None]] = []
    seen: set[str] = set()
    for raw in inputs:
        path = Path(raw).expanduser()
        if path.is_file():
            candidates: Iterable[tuple[Path, Path | None]] = ((path, None),)
        elif path.is_dir():
            root = path.resolve(strict=False)
            iterator = root.rglob("*") if recursive else root.glob("*")
            candidates = ((candidate, root) for candidate in iterator)
        else:
            raise FileNotFoundError(f"路径不存在：{path}")
        for candidate, root in candidates:
            if candidate.suffix.lower() != ".ncm" or not candidate.is_file():
                continue
            resolved = candidate.resolve(strict=False)
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                found.append((resolved, root))
    return found


def _output_dir(source: Path, source_root: Path | None, target: Path | None) -> Path | None:
    if target is None:
        return None
    if source_root is not None:
        try:
            return target / source.parent.relative_to(source_root)
        except ValueError:
            pass
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ncm-converter",
        description="批量从 NCM 原样提取实际的 FLAC/MP3 音频（不转码）。",
    )
    parser.add_argument("inputs", nargs="+", help="NCM 文件或包含 NCM 的文件夹")
    parser.add_argument("-o", "--output", type=Path, help="输出根目录；省略时保存到源文件旁")
    parser.add_argument(
        "--no-recursive", action="store_true", help="输入为文件夹时不搜索子文件夹"
    )
    parser.add_argument(
        "--on-exist",
        choices=("rename", "skip", "overwrite"),
        default="rename",
        help="同名文件策略（默认 rename）",
    )
    parser.add_argument(
        "--fetch-cover",
        action="store_true",
        help="缺封面时从受限的网易 HTTPS 图片域名补全",
    )
    parser.add_argument("--no-tags", action="store_true", help="不写入歌曲标签和封面")
    parser.add_argument("--json", action="store_true", help="以 JSON Lines 输出结果")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        files = _iter_inputs(args.inputs, recursive=not args.no_recursive)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except (OSError, FileNotFoundError) as exc:
        parser.error(str(exc))
    if not files:
        print("没有找到 NCM 文件。", file=sys.stderr)
        return 2

    target = args.output.resolve(strict=False) if args.output else None
    if target:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"无法创建输出目录：{exc}", file=sys.stderr)
            return 2

    failures = 0
    cancel_event = threading.Event()
    for index, (source, source_root) in enumerate(files, start=1):
        if not args.json:
            print(f"[{index}/{len(files)}] {source.name}")
        try:
            result = extract_ncm(
                source,
                output_dir=_output_dir(source, source_root, target),
                collision=args.on_exist,
                fetch_cover=args.fetch_cover,
                write_tags=not args.no_tags,
                cancel_event=cancel_event,
            )
            record = {
                "source": str(source),
                "output": str(result.output_path) if result.output_path else None,
                "format": result.format,
                "skipped": bool(result.skipped),
                "warnings": list(result.warnings or ()),
            }
            if args.json:
                print(json.dumps(record, ensure_ascii=True))
            else:
                state = "跳过" if result.skipped else "完成"
                print(f"  {state}: {record['output'] or '-'} ({str(result.format).upper()})")
                for warning in record["warnings"]:
                    print(f"  提示: {warning}")
        except KeyboardInterrupt:
            cancel_event.set()
            print("\n已取消。", file=sys.stderr)
            return 130
        except CancelledError:
            print("已取消。", file=sys.stderr)
            return 130
        except (NCMError, OSError, ValueError) as exc:
            failures += 1
            if args.json:
                print(
                    json.dumps(
                        {"source": str(source), "error": str(exc)}, ensure_ascii=True
                    )
                )
            else:
                print(f"  失败: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
