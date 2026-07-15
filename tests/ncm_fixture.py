"""Build small, deterministic NCM files without using any real music.

The fixture implements the documented/commonly implemented NCM container
layout.  Every byte in the audio and cover payloads is generated here; no
copyrighted recording or artwork is checked into the test suite.

PyCryptodome is intentionally used instead of duplicating AES.  This keeps the
fixture independent from the implementation under test: a broken AES helper in
``src.ncm_core`` cannot accidentally produce fixtures that agree with itself.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from Crypto.Cipher import AES


NCM_MAGIC = b"CTENFDAM"
CORE_KEY = bytes.fromhex("687a4852416d736f356b496e62617857")
META_KEY = bytes.fromhex("2331346c6a6b5f215c5d2630553c2728")
KEY_PLAINTEXT_PREFIX = b"neteasecloudmusic"
META_CIPHERTEXT_PREFIX = b"163 key(Don't modify):"

DEFAULT_AUDIO_KEY = bytes(
    (
        0x31,
        0x7A,
        0xC4,
        0x09,
        0xE8,
        0x55,
        0xA2,
        0x6D,
        0x10,
        0xBC,
        0x43,
        0x92,
        0xEF,
        0x78,
        0x26,
        0xD1,
        0x5B,
        0x8E,
        0x04,
        0xF7,
        0x69,
        0xAD,
        0x38,
        0xC0,
        0x17,
        0x73,
        0xDA,
        0x4F,
        0x81,
        0x2C,
        0xB6,
        0x5E,
    )
)

DEFAULT_METADATA: Mapping[str, Any] = {
    "musicName": "合成测试音轨",
    "artist": [["测试艺术家", 0]],
    "album": "无版权测试专辑",
    "albumPic": "",
    "format": "flac",
    "bitrate": 1_411_200,
    "duration": 1_000,
}


def deterministic_bytes(length: int, *, seed: int = 19) -> bytes:
    """Return reproducible non-random bytes with a full 256-byte cycle."""

    if length < 0:
        raise ValueError("length must be non-negative")
    return bytes(((index * 73 + seed) & 0xFF) for index in range(length))


def minimal_flac(*, tail_size: int = 0) -> bytes:
    """Return a minimal recognizable FLAC stream plus deterministic tail data.

    The STREAMINFO block is structurally valid.  No encoded song frames are
    included; the optional tail exists solely to exercise streaming extraction.
    """

    if tail_size < 0:
        raise ValueError("tail_size must be non-negative")

    min_block_size = max_block_size = 4096
    sample_rate = 44_100
    channels_minus_one = 1
    bits_per_sample_minus_one = 15
    total_samples = 0
    packed_audio_info = (
        (sample_rate << 44)
        | (channels_minus_one << 41)
        | (bits_per_sample_minus_one << 36)
        | total_samples
    )
    streaminfo = (
        struct.pack(">HH", min_block_size, max_block_size)
        + b"\x00" * 6  # minimum and maximum frame sizes, 24 bits each
        + packed_audio_info.to_bytes(8, "big")
        + b"\x00" * 16  # MD5 of decoded audio; zero means unavailable
    )
    assert len(streaminfo) == 34
    last_streaminfo_block = b"\x80\x00\x00\x22" + streaminfo
    return b"fLaC" + last_streaminfo_block + deterministic_bytes(tail_size)


def minimal_mpeg(*, frame_count: int = 2, tail_size: int = 0) -> bytes:
    """Return synthetic MPEG-1 Layer III frames without an ID3 tag."""

    if frame_count < 1:
        raise ValueError("frame_count must be positive")
    if tail_size < 0:
        raise ValueError("tail_size must be non-negative")

    # MPEG-1 Layer III, 128 kbps, 44.1 kHz, stereo.  Such a frame is 417 bytes.
    header = b"\xFF\xFB\x90\x64"
    frame_length = (144 * 128_000) // 44_100
    frames = []
    for index in range(frame_count):
        frames.append(
            header
            + deterministic_bytes(
                frame_length - len(header), seed=(37 + index * 11) & 0xFF
            )
        )
    return b"".join(frames) + deterministic_bytes(tail_size, seed=211)


def minimal_id3_mp3(*, frame_count: int = 2, tail_size: int = 0) -> bytes:
    """Return an empty ID3v2.4 tag followed by synthetic MPEG frames."""

    # The four sync-safe size bytes encode zero bytes of ID3 frame data.
    id3_header = b"ID3\x04\x00\x00\x00\x00\x00\x00"
    return id3_header + minimal_mpeg(frame_count=frame_count, tail_size=tail_size)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def minimal_png() -> bytes:
    """Generate a valid 1x1 black RGB PNG for synthetic cover tests."""

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    # Filter byte followed by one black RGB pixel.
    idat = zlib.compress(b"\x00\x00\x00\x00")
    return signature + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def _pkcs7_pad(data: bytes) -> bytes:
    pad_length = AES.block_size - (len(data) % AES.block_size)
    return data + bytes((pad_length,)) * pad_length


def _aes_ecb_encrypt(key: bytes, plaintext: bytes) -> bytes:
    return AES.new(key, AES.MODE_ECB).encrypt(_pkcs7_pad(plaintext))


def _xor(data: bytes, mask: int) -> bytes:
    return bytes(value ^ mask for value in data)


def _build_key_box(audio_key: bytes) -> list[int]:
    if not audio_key:
        raise ValueError("audio_key must not be empty")

    key_box = list(range(256))
    last_byte = 0
    key_offset = 0
    for index in range(256):
        swap = key_box[index]
        cursor = (swap + last_byte + audio_key[key_offset]) & 0xFF
        key_offset = (key_offset + 1) % len(audio_key)
        key_box[index], key_box[cursor] = key_box[cursor], swap
        last_byte = cursor
    return key_box


def crypt_audio(payload: bytes, audio_key: bytes = DEFAULT_AUDIO_KEY) -> bytes:
    """Apply the symmetric NCM audio XOR stream at absolute audio offsets."""

    key_box = _build_key_box(audio_key)
    encrypted = bytearray(len(payload))
    for offset, value in enumerate(payload):
        cursor = (offset + 1) & 0xFF
        key_index = (
            key_box[cursor]
            + key_box[(key_box[cursor] + cursor) & 0xFF]
        ) & 0xFF
        encrypted[offset] = value ^ key_box[key_index]
    return bytes(encrypted)


def _metadata_section(
    metadata: Mapping[str, Any] | None,
    mode: str,
) -> bytes:
    normalized_mode = {
        "corrupt": "bad_base64",
        "missing": "none",
        "absent": "none",
    }.get(mode, mode)

    if normalized_mode == "none" or metadata is None:
        return b""
    if normalized_mode == "bad_base64":
        encoded = b"this-is-not-valid-base64%%%"
    elif normalized_mode == "bad_json":
        encoded = base64.b64encode(
            _aes_ecb_encrypt(META_KEY, b"music:{definitely-not-json")
        )
    elif normalized_mode == "valid":
        json_bytes = json.dumps(
            dict(metadata), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        encoded = base64.b64encode(
            _aes_ecb_encrypt(META_KEY, b"music:" + json_bytes)
        )
    else:
        raise ValueError(f"unsupported metadata_mode: {mode!r}")

    return _xor(META_CIPHERTEXT_PREFIX + encoded, 0x63)


@dataclass(frozen=True)
class NcmFixture:
    """A generated NCM blob and offsets useful for corruption tests."""

    blob: bytes
    payload: bytes
    audio_offset: int
    key_length_offset: int
    key_data_length: int
    metadata_length_offset: int
    metadata_data_length: int
    cover_frame_length_offset: int
    cover_data_length_offset: int

    def write(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.blob)
        return destination

    def with_u32(self, offset: int, value: int) -> bytes:
        """Return a copy with one little-endian length field replaced."""

        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("value must fit in uint32")
        mutated = bytearray(self.blob)
        mutated[offset : offset + 4] = struct.pack("<I", value)
        return bytes(mutated)


def build_ncm(
    payload: bytes,
    *,
    metadata: Mapping[str, Any] | None = DEFAULT_METADATA,
    metadata_mode: str = "valid",
    cover: bytes = b"",
    cover_padding: int = 0,
    audio_key: bytes = DEFAULT_AUDIO_KEY,
) -> NcmFixture:
    """Build a complete deterministic NCM fixture.

    ``cover_padding`` creates a cover frame whose reserved frame length is
    larger than its actual image-data length.  Readers must skip both the cover
    bytes and this padding before decrypting the audio payload.
    """

    if not payload:
        raise ValueError("payload must not be empty")
    if cover_padding < 0:
        raise ValueError("cover_padding must be non-negative")

    encrypted_key = _xor(
        _aes_ecb_encrypt(CORE_KEY, KEY_PLAINTEXT_PREFIX + audio_key), 0x64
    )
    encrypted_metadata = _metadata_section(metadata, metadata_mode)

    blob = bytearray()
    blob.extend(NCM_MAGIC)
    blob.extend(b"\x02\x00")  # two-byte container version/reserved field

    key_length_offset = len(blob)
    blob.extend(struct.pack("<I", len(encrypted_key)))
    blob.extend(encrypted_key)

    metadata_length_offset = len(blob)
    blob.extend(struct.pack("<I", len(encrypted_metadata)))
    blob.extend(encrypted_metadata)

    blob.extend(b"\x00" * 5)  # CRC/reserved gap used by existing NCM readers

    cover_frame_length_offset = len(blob)
    cover_frame_length = len(cover) + cover_padding
    blob.extend(struct.pack("<I", cover_frame_length))
    cover_data_length_offset = len(blob)
    blob.extend(struct.pack("<I", len(cover)))
    blob.extend(cover)
    blob.extend(b"\xA5" * cover_padding)

    audio_offset = len(blob)
    blob.extend(crypt_audio(payload, audio_key))

    return NcmFixture(
        blob=bytes(blob),
        payload=bytes(payload),
        audio_offset=audio_offset,
        key_length_offset=key_length_offset,
        key_data_length=len(encrypted_key),
        metadata_length_offset=metadata_length_offset,
        metadata_data_length=len(encrypted_metadata),
        cover_frame_length_offset=cover_frame_length_offset,
        cover_data_length_offset=cover_data_length_offset,
    )


__all__ = [
    "DEFAULT_METADATA",
    "NCM_MAGIC",
    "NcmFixture",
    "build_ncm",
    "crypt_audio",
    "deterministic_bytes",
    "minimal_flac",
    "minimal_id3_mp3",
    "minimal_mpeg",
    "minimal_png",
]
