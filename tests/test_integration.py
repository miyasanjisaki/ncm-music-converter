"""Integration tests for tagging, cancellation, and the command-line wrapper."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mutagen.flac import FLAC
from mutagen.id3 import ID3

from src.ncm_cli import main as cli_main
from src import ncm_core
from src.ncm_core import CancelledError, extract_ncm

try:
    from .ncm_fixture import (
        DEFAULT_METADATA,
        build_ncm,
        minimal_flac,
        minimal_id3_mp3,
        minimal_png,
    )
except ImportError:  # pragma: no cover
    from ncm_fixture import (  # type: ignore[no-redef]
        DEFAULT_METADATA,
        build_ncm,
        minimal_flac,
        minimal_id3_mp3,
        minimal_png,
    )


def _flac_audio_tail(data: bytes) -> bytes:
    if not data.startswith(b"fLaC"):
        raise AssertionError("not FLAC")
    offset = 4
    while True:
        header = data[offset : offset + 4]
        if len(header) != 4:
            raise AssertionError("truncated FLAC metadata")
        last = bool(header[0] & 0x80)
        length = int.from_bytes(header[1:4], "big")
        offset += 4 + length
        if last:
            return data[offset:]


def _mp3_audio_after_id3(data: bytes) -> bytes:
    if not data.startswith(b"ID3"):
        return data
    if len(data) < 10:
        raise AssertionError("truncated ID3")
    size = 0
    for value in data[6:10]:
        size = (size << 7) | value
    footer = 10 if data[5] & 0x10 else 0
    return data[10 + size + footer :]


class NcmCoreIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="ncm-integration-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_flac_and_mp3_tags_preserve_audio_essence(self) -> None:
        cases = (
            ("tagged-flac.ncm", minimal_flac(tail_size=4097), "flac"),
            ("tagged-mp3.ncm", minimal_id3_mp3(frame_count=3, tail_size=257), "mp3"),
        )
        for filename, payload, audio_format in cases:
            with self.subTest(audio_format=audio_format):
                fixture = build_ncm(payload, metadata=DEFAULT_METADATA, cover=minimal_png())
                source = fixture.write(self.root / filename)
                source_hash = hashlib.sha256(source.read_bytes()).digest()
                result = extract_ncm(source, output_dir=self.root / audio_format)
                output = Path(result.output_path)

                self.assertEqual(result.format, audio_format)
                self.assertFalse(result.skipped)
                self.assertEqual(source_hash, hashlib.sha256(source.read_bytes()).digest())
                if audio_format == "flac":
                    tags = FLAC(output)
                    self.assertEqual(tags.get("title"), [DEFAULT_METADATA["musicName"]])
                    self.assertEqual(len(tags.pictures), 1)
                    self.assertEqual(_flac_audio_tail(output.read_bytes()), _flac_audio_tail(payload))
                else:
                    tags = ID3(output)
                    self.assertEqual(str(tags["TIT2"]), DEFAULT_METADATA["musicName"])
                    self.assertEqual(len(tags.getall("APIC")), 1)
                    self.assertEqual(_mp3_audio_after_id3(output.read_bytes()), _mp3_audio_after_id3(payload))

    def test_cancel_removes_part_and_does_not_commit_output(self) -> None:
        payload = minimal_flac(tail_size=2 * 1024 * 1024 + 19)
        source = build_ncm(payload, metadata=None, metadata_mode="none").write(
            self.root / "cancel.ncm"
        )
        output_dir = self.root / "cancel-output"
        cancel = threading.Event()

        def progress(done: int, _total: int) -> None:
            if done:
                cancel.set()

        with self.assertRaises(CancelledError):
            extract_ncm(
                source,
                output_dir=output_dir,
                write_tags=False,
                cancel_event=cancel,
                progress_cb=progress,
            )
        self.assertFalse((output_dir / "cancel.flac").exists())
        self.assertEqual(list(output_dir.glob("*.part")), [])
        self.assertEqual(list(output_dir.glob(".*.part")), [])

    def test_untrusted_remote_cover_url_degrades_to_warning(self) -> None:
        metadata = dict(DEFAULT_METADATA)
        metadata["albumPic"] = "https://example.com/not-allowed.jpg"
        source = build_ncm(
            minimal_flac(), metadata=metadata, cover=b""
        ).write(self.root / "unsafe-cover.ncm")
        result = extract_ncm(source, self.root / "unsafe-cover", fetch_cover=True)
        self.assertTrue(Path(result.output_path).is_file())
        self.assertTrue(any("封面补全失败" in warning for warning in result.warnings))

    def test_malformed_remote_cover_url_also_degrades_to_warning(self) -> None:
        metadata = dict(DEFAULT_METADATA)
        metadata["albumPic"] = "https://[music.126.net/broken"
        source = build_ncm(minimal_flac(), metadata=metadata, cover=b"").write(
            self.root / "malformed-cover.ncm"
        )

        result = extract_ncm(source, self.root / "malformed-cover", fetch_cover=True)

        self.assertTrue(Path(result.output_path).is_file())
        self.assertTrue(any("封面补全失败" in warning for warning in result.warnings))

    def test_no_tags_never_downloads_remote_cover(self) -> None:
        metadata = dict(DEFAULT_METADATA)
        metadata["albumPic"] = "https://music.126.net/unused.png"
        source = build_ncm(minimal_flac(), metadata=metadata, cover=b"").write(
            self.root / "no-tags-cover.ncm"
        )

        with mock.patch.object(ncm_core, "_download_cover") as download:
            result = extract_ncm(
                source,
                self.root / "no-tags-cover",
                fetch_cover=True,
                write_tags=False,
            )

        download.assert_not_called()
        self.assertTrue(Path(result.output_path).is_file())

    def test_cover_tag_failure_keeps_text_tags(self) -> None:
        source = build_ncm(
            minimal_flac(tail_size=97),
            metadata=DEFAULT_METADATA,
            cover=minimal_png(),
        ).write(self.root / "cover-tag-failure.ncm")
        original = ncm_core._write_tags

        def fail_cover(path, probe, cover, mime):
            if cover:
                raise ValueError("synthetic cover failure")
            return original(path, probe, cover, mime)

        with mock.patch.object(ncm_core, "_write_tags", side_effect=fail_cover):
            result = extract_ncm(source, self.root / "cover-tag-failure")

        audio = FLAC(Path(result.output_path))
        self.assertEqual(audio.get("title"), [DEFAULT_METADATA["musicName"]])
        self.assertEqual(audio.pictures, [])
        self.assertTrue(any("已保留文本标签" in warning for warning in result.warnings))

    def test_collision_race_never_overwrites_an_external_file(self) -> None:
        payload = minimal_flac(tail_size=123)
        for collision in ("rename", "skip"):
            with self.subTest(collision=collision):
                source = build_ncm(
                    payload,
                    metadata=None,
                    metadata_mode="none",
                ).write(self.root / f"race-{collision}.ncm")
                output_dir = self.root / f"race-output-{collision}"
                desired = output_dir / f"race-{collision}.flac"
                sentinel = b"external file must survive"

                def create_racing_target(done: int, _total: int) -> None:
                    if done and not desired.exists():
                        desired.write_bytes(sentinel)

                result = extract_ncm(
                    source,
                    output_dir,
                    collision=collision,
                    write_tags=False,
                    progress_cb=create_racing_target,
                )

                self.assertEqual(desired.read_bytes(), sentinel)
                if collision == "rename":
                    self.assertFalse(result.skipped)
                    self.assertNotEqual(Path(result.output_path), desired)
                    self.assertEqual(Path(result.output_path).read_bytes(), payload)
                else:
                    self.assertTrue(result.skipped)
                    self.assertEqual(Path(result.output_path), desired)
                    self.assertEqual(result.bytes_written, 0)

    def test_cli_converts_directory_and_preserves_relative_tree(self) -> None:
        input_root = self.root / "input"
        source = build_ncm(
            minimal_flac(tail_size=81), metadata=None, metadata_mode="none"
        ).write(input_root / "子目录" / "曲目.ncm")
        output_root = self.root / "output"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli_main(
                [str(input_root), "-o", str(output_root), "--no-tags", "--json"]
            )
        self.assertEqual(code, 0, stderr.getvalue())
        output = output_root / "子目录" / "曲目.flac"
        self.assertEqual(output.read_bytes(), minimal_flac(tail_size=81))
        record = json.loads(stdout.getvalue())
        self.assertEqual(record["source"], str(source))
        self.assertEqual(record["output"], str(output))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
