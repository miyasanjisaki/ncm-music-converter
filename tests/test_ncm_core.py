"""Black-box tests for ``src.ncm_core`` using only synthetic media bytes."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import ncm_core
from src.ncm_core import NCMError, extract_ncm, probe_ncm

try:  # Supports both ``unittest`` discovery forms and direct module execution.
    from .ncm_fixture import (
        DEFAULT_METADATA,
        NCM_MAGIC,
        build_ncm,
        minimal_flac,
        minimal_id3_mp3,
        minimal_mpeg,
        minimal_png,
    )
except ImportError:  # pragma: no cover - exercised only by alternate discovery
    from ncm_fixture import (  # type: ignore[no-redef]
        DEFAULT_METADATA,
        NCM_MAGIC,
        build_ncm,
        minimal_flac,
        minimal_id3_mp3,
        minimal_mpeg,
        minimal_png,
    )


def _extract_without_tags(source: Path, output_dir: Path, **kwargs):
    """Disable optional tag rewriting when the public API supports the flag."""

    if "write_tags" in inspect.signature(extract_ncm).parameters:
        kwargs["write_tags"] = False
    return extract_ncm(source, output_dir=output_dir, **kwargs)


def _extension_value(value: object) -> str:
    return str(value).lower().lstrip(".")


class NcmCoreSyntheticTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="ncm-core-synthetic-"
        )
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _write(self, name: str, fixture) -> Path:
        return fixture.write(self.root / name)

    def test_probe_uses_decrypted_payload_magic_for_all_supported_forms(self) -> None:
        cases = (
            ("minimal-flac.ncm", minimal_flac(), "flac", "mp3"),
            ("id3-mp3.ncm", minimal_id3_mp3(), "mp3", "flac"),
            ("mpeg-without-id3.ncm", minimal_mpeg(), "mp3", "flac"),
        )

        for filename, payload, expected_format, misleading_hint in cases:
            with self.subTest(filename=filename):
                metadata = dict(DEFAULT_METADATA)
                metadata["format"] = misleading_hint
                fixture = build_ncm(payload, metadata=metadata)
                source = self._write(filename, fixture)

                result = probe_ncm(source)

                self.assertEqual(result.format, expected_format)
                self.assertEqual(_extension_value(result.extension), expected_format)
                self.assertEqual(Path(result.source_path), source)
                self.assertEqual(result.file_size, len(fixture.blob))
                self.assertEqual(result.audio_offset, fixture.audio_offset)
                self.assertEqual(result.audio_size, len(payload))
                self.assertIsNotNone(result.metadata)

    def test_cover_frame_padding_is_not_mistaken_for_audio(self) -> None:
        payload = minimal_flac(tail_size=777)
        fixture = build_ncm(
            payload,
            metadata=None,
            metadata_mode="none",
            cover=minimal_png(),
            cover_padding=513,
        )
        source = self._write("cover-with-reserved-padding.ncm", fixture)

        probe = probe_ncm(source)
        self.assertEqual(probe.audio_offset, fixture.audio_offset)
        self.assertEqual(probe.audio_size, len(payload))

        result = _extract_without_tags(source, self.root / "cover-output")
        self.assertFalse(result.skipped)
        self.assertEqual(result.format, "flac")
        self.assertEqual(result.bytes_written, len(payload))
        self.assertEqual(Path(result.output_path).read_bytes(), payload)

    def test_streaming_decryption_preserves_bytes_across_256_boundaries(self) -> None:
        # A 257-byte I/O chunk is deliberate: resetting the NCM key-stream index
        # for every read corrupts each chunk after the first one.
        payload = minimal_flac(tail_size=256 * 19 + 173)
        fixture = build_ncm(payload, metadata=None, metadata_mode="none")
        source = self._write("stream-boundary.ncm", fixture)
        output_dir = self.root / "stream-output"

        chunk_constant = next(
            (
                name
                for name in (
                    "STREAM_CHUNK_SIZE",
                    "AUDIO_CHUNK_SIZE",
                    "CHUNK_SIZE",
                    "_STREAM_CHUNK_SIZE",
                    "_AUDIO_CHUNK_SIZE",
                    "_CHUNK_SIZE",
                )
                if hasattr(ncm_core, name)
            ),
            None,
        )
        patcher = (
            mock.patch.object(ncm_core, chunk_constant, 257)
            if chunk_constant is not None
            else mock.patch.object(ncm_core, "__ncm_test_noop__", 257, create=True)
        )
        with patcher:
            result = _extract_without_tags(source, output_dir)

        self.assertFalse(result.skipped)
        self.assertEqual(result.format, "flac")
        self.assertEqual(result.bytes_written, len(payload))
        self.assertEqual(Path(result.output_path).read_bytes(), payload)

    def test_unicode_paths_and_collision_rename_then_skip(self) -> None:
        payload = minimal_mpeg(frame_count=3, tail_size=301)
        fixture = build_ncm(payload, metadata=None, metadata_mode="none")
        source = fixture.write(self.root / "输入 音乐" / "合成夜曲 🎵.ncm")
        output_dir = self.root / "导出到 MP3 播放器"

        first = _extract_without_tags(source, output_dir, collision="rename")
        renamed = _extract_without_tags(source, output_dir, collision="rename")
        skipped = _extract_without_tags(source, output_dir, collision="skip")

        first_path = Path(first.output_path)
        renamed_path = Path(renamed.output_path)
        skipped_path = Path(skipped.output_path)
        self.assertTrue(first_path.is_file())
        self.assertTrue(renamed_path.is_file())
        self.assertNotEqual(first_path, renamed_path)
        self.assertEqual(first_path.read_bytes(), payload)
        self.assertEqual(renamed_path.read_bytes(), payload)
        self.assertFalse(first.skipped)
        self.assertFalse(renamed.skipped)
        self.assertTrue(skipped.skipped)
        self.assertEqual(skipped_path, first_path)
        self.assertEqual(skipped.format, "mp3")
        self.assertTrue(hasattr(skipped, "warnings"))

    def test_absent_and_corrupt_metadata_degrade_without_losing_audio(self) -> None:
        payload = minimal_flac(tail_size=333)
        cases = (
            ("metadata-absent", "none", False),
            ("metadata-bad-base64", "bad_base64", True),
            ("metadata-bad-json", "bad_json", True),
        )

        for stem, metadata_mode, expects_warning in cases:
            with self.subTest(metadata_mode=metadata_mode):
                fixture = build_ncm(
                    payload,
                    metadata=None if metadata_mode == "none" else DEFAULT_METADATA,
                    metadata_mode=metadata_mode,
                )
                source = self._write(f"{stem}.ncm", fixture)

                probe = probe_ncm(source)
                self.assertEqual(probe.format, "flac")
                self.assertIsNone(probe.metadata)
                if expects_warning:
                    self.assertTrue(probe.warnings)

                result = _extract_without_tags(source, self.root / f"out-{stem}")
                self.assertFalse(result.skipped)
                self.assertEqual(result.format, "flac")
                self.assertEqual(Path(result.output_path).read_bytes(), payload)
                if expects_warning:
                    self.assertTrue(result.warnings)

    def test_publish_timestamp_is_normalized_for_audio_tags(self) -> None:
        metadata = dict(DEFAULT_METADATA)
        metadata.pop("date", None)
        metadata.pop("year", None)
        metadata["publishTime"] = 1_704_067_200_000
        source = self._write(
            "timestamp.ncm",
            build_ncm(minimal_flac(), metadata=metadata),
        )

        probe = probe_ncm(source)

        self.assertIsNotNone(probe.metadata)
        self.assertEqual(probe.metadata.date, "2024-01-01")

    def test_empty_primary_metadata_fields_use_compatible_fallbacks(self) -> None:
        metadata = dict(DEFAULT_METADATA)
        metadata.update(
            {
                "musicName": "",
                "name": "fallback title",
                "artist": [],
                "artists": [{"name": "fallback artist"}],
                "albumPic": None,
                "albumPicUrl": "https://music.126.net/fallback.png",
                "date": "",
                "year": 2025,
            }
        )
        source = self._write(
            "metadata-fallbacks.ncm",
            build_ncm(minimal_flac(), metadata=metadata),
        )

        probe = probe_ncm(source)

        self.assertIsNotNone(probe.metadata)
        self.assertEqual(probe.metadata.title, "fallback title")
        self.assertEqual(probe.metadata.artists, ("fallback artist",))
        self.assertEqual(probe.metadata.album_pic_url, "https://music.126.net/fallback.png")
        self.assertEqual(probe.metadata.date, "2025")

    def test_keyboard_interrupt_cleans_temporary_output(self) -> None:
        source = self._write(
            "interrupt.ncm",
            build_ncm(
                minimal_flac(tail_size=4096),
                metadata=None,
                metadata_mode="none",
            ),
        )
        output_dir = self.root / "interrupted-output"

        def interrupt(_done: int, _total: int) -> None:
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            extract_ncm(
                source,
                output_dir=output_dir,
                write_tags=False,
                progress_cb=interrupt,
            )

        self.assertFalse((output_dir / "interrupt.flac").exists())
        self.assertEqual(list(output_dir.glob("*.part")), [])
        self.assertEqual(list(output_dir.glob(".*.part")), [])

    def test_id3_without_mpeg_audio_is_rejected(self) -> None:
        fake_id3_audio = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"not-mpeg" * 100
        source = self._write(
            "fake-id3.ncm",
            build_ncm(fake_id3_audio, metadata=None, metadata_mode="none"),
        )

        with self.assertRaises(NCMError):
            probe_ncm(source)

    def test_truncated_structural_sections_raise_ncm_error(self) -> None:
        fixture = build_ncm(
            minimal_flac(),
            cover=minimal_png(),
            cover_padding=17,
        )
        cuts = {
            "magic": len(NCM_MAGIC) - 1,
            "key-length": fixture.key_length_offset + 2,
            "key-data": fixture.key_length_offset + 4 + fixture.key_data_length - 1,
            "metadata-length": fixture.metadata_length_offset + 2,
            "metadata-data": (
                fixture.metadata_length_offset
                + 4
                + fixture.metadata_data_length
                - 1
            ),
            "cover-frame-length": fixture.cover_frame_length_offset + 2,
            "cover-data-length": fixture.cover_data_length_offset + 2,
            "audio-header": fixture.audio_offset + 3,
        }

        for label, cut_at in cuts.items():
            with self.subTest(section=label):
                source = self.root / f"truncated-{label}.ncm"
                source.write_bytes(fixture.blob[:cut_at])
                with self.assertRaises(NCMError):
                    probe_ncm(source)

    def test_oversized_or_inconsistent_lengths_are_rejected(self) -> None:
        fixture = build_ncm(
            minimal_flac(),
            metadata=DEFAULT_METADATA,
            cover=minimal_png(),
            cover_padding=7,
        )
        # Large enough to exceed any sane NCM section, while still avoiding an
        # excessive allocation if a regressed reader calls read(size) before
        # checking the bytes remaining in this deliberately tiny file.
        oversized_length = 32 * 1024 * 1024 + 3
        malformed_blobs = {
            "key": fixture.with_u32(
                fixture.key_length_offset, oversized_length
            ),
            "metadata": fixture.with_u32(
                fixture.metadata_length_offset, oversized_length
            ),
            "cover-frame": fixture.with_u32(
                fixture.cover_frame_length_offset, oversized_length
            ),
            "cover-data-over-frame": fixture.with_u32(
                fixture.cover_data_length_offset,
                len(minimal_png()) + 8,
            ),
        }

        for label, malformed in malformed_blobs.items():
            with self.subTest(length=label):
                source = self.root / f"oversized-{label}.ncm"
                source.write_bytes(malformed)
                with self.assertRaises(NCMError):
                    probe_ncm(source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
