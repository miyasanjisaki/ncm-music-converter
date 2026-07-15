"""Safe, streaming NCM extraction core.

The NCM container stores an already encoded audio payload.  This module
decrypts that payload in chunks and preserves the original FLAC/MP3 codec;
it does not transcode audio.

SPDX-License-Identifier: GPL-2.0-or-later
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)
import uuid

from Crypto.Cipher import AES
from Crypto.Util.strxor import strxor


NCM_MAGIC = b"CTENFDAM"
CORE_KEY = bytes.fromhex("687a4852416d736f356b496e62617857")
META_KEY = bytes.fromhex("2331346c6a6b5f215c5d2630553c2728")
KEY_PREFIX = b"neteasecloudmusic"
META_PREFIX = b"163 key(Don't modify):"
META_PLAIN_PREFIX = b"music:"

# Public on purpose: tests can force a non-256-aligned chunk and verify that
# the key-stream position is based on the global audio offset.
STREAM_CHUNK_SIZE = 1024 * 1024
SNIFF_SIZE = 256 * 1024

MAX_KEY_SIZE = 1024 * 1024
MAX_METADATA_SIZE = 16 * 1024 * 1024
MAX_COVER_FRAME_SIZE = 64 * 1024 * 1024
MAX_COVER_DATA_SIZE = 20 * 1024 * 1024
MAX_REMOTE_COVER_SIZE = 10 * 1024 * 1024
REMOTE_COVER_TIMEOUT = 10.0

ProgressCallback = Callable[[int, int], None]


class NCMError(Exception):
    """The input is not a supported, structurally valid NCM container."""


class CancelledError(NCMError):
    """The caller requested cancellation before atomic output commit."""


@dataclass(frozen=True)
class NCMMetadata:
    title: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    album_artist: str = ""
    track: str = ""
    date: str = ""
    format_hint: str = ""
    album_pic_url: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def artist(self) -> str:
        return " / ".join(self.artists)


@dataclass(frozen=True)
class ProbeResult:
    source_path: Path
    file_size: int
    audio_offset: int
    audio_size: int
    format: str
    extension: str
    metadata: NCMMetadata | None
    warnings: tuple[str, ...] = ()
    cover_data: bytes = field(default=b"", repr=False)
    cover_mime: str | None = None
    key_stream: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class ExtractionResult:
    source_path: Path
    output_path: Path | None
    format: str
    bytes_written: int
    warnings: tuple[str, ...] = ()
    skipped: bool = False


def _read_exact(stream, size: int, label: str) -> bytes:
    if size < 0:
        raise NCMError(f"{label}长度无效。")
    data = stream.read(size)
    if len(data) != size:
        raise NCMError(f"文件在“{label}”处不完整或已损坏。")
    return data


def _read_u32(stream, label: str) -> int:
    return struct.unpack("<I", _read_exact(stream, 4, label))[0]


def _pkcs7_unpad(data: bytes, label: str) -> bytes:
    if not data or len(data) % AES.block_size:
        raise NCMError(f"{label}的 AES 数据长度无效。")
    padding = data[-1]
    if padding < 1 or padding > AES.block_size:
        raise NCMError(f"{label}的加密填充无效。")
    if data[-padding:] != bytes((padding,)) * padding:
        raise NCMError(f"{label}的加密填充不一致。")
    return data[:-padding]


def _aes_decrypt(ciphertext: bytes, key: bytes, label: str) -> bytes:
    if not ciphertext or len(ciphertext) % AES.block_size:
        raise NCMError(f"{label}不是完整的 AES 数据块。")
    plaintext = AES.new(key, AES.MODE_ECB).decrypt(ciphertext)
    return _pkcs7_unpad(plaintext, label)


def _build_key_stream(audio_key: bytes) -> bytes:
    if not audio_key:
        raise NCMError("音频流密钥为空。")
    box = list(range(256))
    last = 0
    key_offset = 0
    for index in range(256):
        swap = box[index]
        cursor = (swap + last + audio_key[key_offset]) & 0xFF
        key_offset = (key_offset + 1) % len(audio_key)
        box[index], box[cursor] = box[cursor], swap
        last = cursor

    stream = bytearray(256)
    for index in range(256):
        cursor = (index + 1) & 0xFF
        key_index = (box[cursor] + box[(box[cursor] + cursor) & 0xFF]) & 0xFF
        stream[index] = box[key_index]
    return bytes(stream)


def _crypt_audio_chunk(data: bytes, key_stream: bytes, offset: int) -> bytes:
    if not data:
        return b""
    start = offset & 0xFF
    cycle = key_stream[start:] + key_stream[:start]
    mask = (cycle * ((len(data) + 255) // 256))[: len(data)]
    return strxor(data, mask)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _first_nonempty(raw: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            if value.strip():
                return value
        elif value is not None:
            try:
                if len(value) == 0:  # type: ignore[arg-type]
                    continue
            except (TypeError, ValueError):
                pass
            return value
    return ""


def _parse_artists(value: Any) -> tuple[str, ...]:
    names: list[str] = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, (list, tuple)) and entry:
                name = _clean_text(entry[0])
            elif isinstance(entry, dict):
                name = _clean_text(entry.get("name"))
            else:
                name = _clean_text(entry)
            if name and name not in names:
                names.append(name)
    elif isinstance(value, str):
        name = value.strip()
        if name:
            names.append(name)
    return tuple(names)


def _metadata_date(raw: Mapping[str, Any]) -> str:
    """Return a tag-friendly date, including NCM millisecond timestamps."""

    explicit = _first_nonempty(raw, "date", "year")
    if _clean_text(explicit):
        return _clean_text(explicit)

    published = raw.get("publishTime", "")
    text = _clean_text(published)
    if not text:
        return ""
    try:
        timestamp = float(text)
        if abs(timestamp) >= 100_000_000_000:
            timestamp /= 1000.0
        if 0 < timestamp <= 4_102_444_800:  # Through 2100-01-01 UTC.
            return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        pass
    return text


def _metadata_from_mapping(raw: Mapping[str, Any]) -> NCMMetadata:
    track_value = _first_nonempty(raw, "trackNo", "trackNumber", "track")
    return NCMMetadata(
        title=_clean_text(_first_nonempty(raw, "musicName", "name", "title")),
        artists=_parse_artists(_first_nonempty(raw, "artist", "artists")),
        album=_clean_text(raw.get("album")),
        album_artist=_clean_text(_first_nonempty(raw, "albumArtist", "albumartist")),
        track=_clean_text(track_value),
        date=_metadata_date(raw),
        format_hint=_clean_text(raw.get("format")).lower(),
        album_pic_url=_clean_text(_first_nonempty(raw, "albumPic", "albumPicUrl")),
        raw=dict(raw),
    )


def _decode_metadata(data: bytes) -> NCMMetadata:
    decoded = bytes(value ^ 0x63 for value in data)
    if not decoded.startswith(META_PREFIX):
        raise NCMError("元数据标记不匹配。")
    encoded = decoded[len(META_PREFIX) :].strip()
    if not encoded:
        raise NCMError("元数据内容为空。")
    try:
        ciphertext = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise NCMError("元数据 Base64 无效。") from exc
    plaintext = _aes_decrypt(ciphertext, META_KEY, "元数据")
    if not plaintext.startswith(META_PLAIN_PREFIX):
        raise NCMError("元数据明文标记不匹配。")
    try:
        raw = json.loads(plaintext[len(META_PLAIN_PREFIX) :].decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NCMError("元数据 JSON 无效。") from exc
    if not isinstance(raw, dict):
        raise NCMError("元数据 JSON 不是对象。")
    return _metadata_from_mapping(raw)


def _mpeg_header_info(data: bytes, offset: int = 0) -> tuple[int, tuple[int, int, int]] | None:
    if offset < 0 or offset + 4 > len(data):
        return None
    first, second, third, _fourth = data[offset : offset + 4]
    if first != 0xFF or (second & 0xE0) != 0xE0:
        return None
    version_id = (second >> 3) & 0x03
    layer_id = (second >> 1) & 0x03
    bitrate_index = (third >> 4) & 0x0F
    sample_index = (third >> 2) & 0x03
    padding = (third >> 1) & 0x01
    if version_id == 1 or layer_id == 0:
        return None
    if bitrate_index in (0, 15) or sample_index == 3:
        return None

    mpeg1 = version_id == 3
    if mpeg1:
        tables = {
            3: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
            2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
            1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
        }
    else:
        tables = {
            3: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
            2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
            1: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        }
    bitrate = tables[layer_id][bitrate_index] * 1000
    sample_rate = (44100, 48000, 32000)[sample_index]
    if version_id == 2:
        sample_rate //= 2
    elif version_id == 0:
        sample_rate //= 4

    if layer_id == 3:  # Layer I
        frame_length = ((12 * bitrate) // sample_rate + padding) * 4
    elif layer_id == 1 and not mpeg1:  # MPEG-2/2.5 Layer III
        frame_length = (72 * bitrate) // sample_rate + padding
    else:
        frame_length = (144 * bitrate) // sample_rate + padding
    if frame_length < 4:
        return None
    return frame_length, (version_id, layer_id, sample_rate)


def _looks_like_mpeg(data: bytes) -> bool:
    first = _mpeg_header_info(data, 0)
    if first is None:
        return False
    frame_length, signature = first
    if frame_length + 4 > len(data):
        return len(data) >= frame_length
    second = _mpeg_header_info(data, frame_length)
    return second is not None and second[1] == signature


def _detect_audio_format(data: bytes) -> tuple[str, str]:
    if data.startswith(b"fLaC"):
        return "flac", ".flac"
    if data.startswith(b"ID3"):
        if len(data) < 10 or any(byte & 0x80 for byte in data[6:10]):
            raise NCMError("解密后出现损坏的 ID3 音频头。")
        tag_size = (
            (data[6] << 21)
            | (data[7] << 14)
            | (data[8] << 7)
            | data[9]
        )
        audio_start = 10 + tag_size
        if data[5] & 0x10:  # ID3v2.4 footer-present flag.
            audio_start += 10
        if audio_start + 4 <= len(data) and not _looks_like_mpeg(data[audio_start:]):
            raise NCMError("解密后的 ID3 标签后没有有效的 MPEG 音频帧。")
        return "mp3", ".mp3"
    if _looks_like_mpeg(data):
        return "mp3", ".mp3"
    preview = data[:8].hex(" ").upper()
    raise NCMError(f"解密后的音频格式无法识别（文件头：{preview}）。")


def _cover_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    return None


def probe_ncm(path: str | os.PathLike[str]) -> ProbeResult:
    """Parse and validate an NCM file without creating output."""

    source = Path(path).expanduser().resolve(strict=False)
    try:
        file_size = source.stat().st_size
    except OSError as exc:
        raise NCMError(f"无法读取源文件：{exc}") from exc
    if not source.is_file():
        raise NCMError("源路径不是文件。")

    warnings: list[str] = []
    metadata: NCMMetadata | None = None
    cover_data = b""
    cover_mime: str | None = None

    try:
        with source.open("rb") as stream:
            if _read_exact(stream, len(NCM_MAGIC), "NCM 文件头") != NCM_MAGIC:
                raise NCMError("不是有效的 NCM 文件（文件头不匹配）。")
            _read_exact(stream, 2, "容器版本")

            key_length = _read_u32(stream, "流密钥长度")
            if key_length <= 0 or key_length > MAX_KEY_SIZE:
                raise NCMError(f"流密钥长度不合理：{key_length} 字节。")
            if key_length % AES.block_size:
                raise NCMError("流密钥不是完整的 AES 数据块。")
            if stream.tell() + key_length > file_size:
                raise NCMError("流密钥长度超出文件边界。")
            encrypted_key = bytes(value ^ 0x64 for value in _read_exact(stream, key_length, "流密钥"))
            key_plaintext = _aes_decrypt(encrypted_key, CORE_KEY, "流密钥")
            if not key_plaintext.startswith(KEY_PREFIX):
                raise NCMError("流密钥明文标记不匹配，可能是不支持的 NCM 变体。")
            audio_key = key_plaintext[len(KEY_PREFIX) :]
            key_stream = _build_key_stream(audio_key)

            metadata_length = _read_u32(stream, "元数据长度")
            if metadata_length > MAX_METADATA_SIZE:
                raise NCMError(f"元数据长度不合理：{metadata_length} 字节。")
            if stream.tell() + metadata_length > file_size:
                raise NCMError("元数据长度超出文件边界。")
            if metadata_length:
                metadata_blob = _read_exact(stream, metadata_length, "元数据")
                try:
                    metadata = _decode_metadata(metadata_blob)
                except NCMError as exc:
                    warnings.append(f"元数据读取失败：{exc} 将保留无标签音频。")

            _read_exact(stream, 5, "CRC/保留区")
            cover_frame_length = _read_u32(stream, "封面预留区长度")
            cover_data_length = _read_u32(stream, "封面数据长度")
            if cover_frame_length > MAX_COVER_FRAME_SIZE:
                raise NCMError(f"封面预留区长度不合理：{cover_frame_length} 字节。")
            if cover_data_length > cover_frame_length:
                raise NCMError("封面数据长度大于封面预留区长度。")
            if stream.tell() + cover_frame_length > file_size:
                raise NCMError("封面预留区长度超出文件边界。")
            if cover_data_length:
                if cover_data_length <= MAX_COVER_DATA_SIZE:
                    cover_data = _read_exact(stream, cover_data_length, "封面数据")
                    cover_mime = _cover_mime(cover_data)
                    if cover_mime is None:
                        warnings.append("内嵌封面不是受支持的 JPEG/PNG，已忽略封面。")
                        cover_data = b""
                else:
                    stream.seek(cover_data_length, os.SEEK_CUR)
                    warnings.append("内嵌封面过大，已忽略封面。")
            padding = cover_frame_length - cover_data_length
            if padding:
                stream.seek(padding, os.SEEK_CUR)

            audio_offset = stream.tell()
            audio_size = file_size - audio_offset
            if audio_size < 4:
                raise NCMError("NCM 中没有完整的音频数据。")
            encrypted_head = _read_exact(stream, min(audio_size, SNIFF_SIZE), "音频头")
            decrypted_head = _crypt_audio_chunk(encrypted_head, key_stream, 0)
            audio_format, extension = _detect_audio_format(decrypted_head)
    except NCMError:
        raise
    except OSError as exc:
        raise NCMError(f"读取 NCM 失败：{exc}") from exc

    return ProbeResult(
        source_path=source,
        file_size=file_size,
        audio_offset=audio_offset,
        audio_size=audio_size,
        format=audio_format,
        extension=extension,
        metadata=metadata,
        warnings=tuple(warnings),
        cover_data=cover_data,
        cover_mime=cover_mime,
        key_stream=key_stream,
    )


def _resolve_output_path(target: Path, collision: str) -> tuple[Path, bool]:
    if collision not in {"rename", "skip", "overwrite"}:
        raise ValueError("collision 必须是 rename、skip 或 overwrite。")
    if not target.exists() or collision == "overwrite":
        return target, False
    if collision == "skip":
        return target, True
    for number in range(1, 100_000):
        candidate = target.with_name(f"{target.stem} ({number}){target.suffix}")
        if not candidate.exists():
            return candidate, False
    raise NCMError("同名文件过多，无法生成新的输出文件名。")


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledError("转换已取消。")


def _extract_raw(
    probe: ProbeResult,
    destination: Path,
    cancel_event: threading.Event | None,
    progress_cb: ProgressCallback | None,
) -> int:
    written = 0
    try:
        with probe.source_path.open("rb") as source, destination.open("xb") as output:
            source.seek(probe.audio_offset)
            while written < probe.audio_size:
                _check_cancel(cancel_event)
                wanted = min(max(1, STREAM_CHUNK_SIZE), probe.audio_size - written)
                encrypted = source.read(wanted)
                if not encrypted:
                    raise NCMError("读取音频数据时提前到达文件结尾。")
                decrypted = _crypt_audio_chunk(encrypted, probe.key_stream, written)
                output.write(decrypted)
                written += len(encrypted)
                if progress_cb is not None:
                    progress_cb(written, probe.audio_size)
            output.flush()
            os.fsync(output.fileno())
        if written != probe.audio_size or destination.stat().st_size != probe.audio_size:
            raise NCMError("输出字节数与 NCM 音频区长度不一致。")
        _validate_audio_file(destination, probe.format)
    except BaseException:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return written


def _validate_audio_file(path: Path, audio_format: str) -> None:
    try:
        with path.open("rb") as stream:
            head = stream.read(SNIFF_SIZE)
    except OSError as exc:
        raise NCMError(f"无法校验临时输出：{exc}") from exc
    detected, _extension = _detect_audio_format(head)
    if detected != audio_format:
        raise NCMError("临时输出的实际格式与探测结果不一致。")


def _write_mp3_tags(path: Path, metadata: NCMMetadata | None, cover: bytes, mime: str | None) -> None:
    from mutagen.id3 import (
        APIC,
        ID3,
        ID3NoHeaderError,
        TALB,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TRCK,
    )

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    if metadata is not None:
        replacements = (
            ("TIT2", metadata.title, TIT2),
            ("TPE1", metadata.artist, TPE1),
            ("TALB", metadata.album, TALB),
            ("TPE2", metadata.album_artist, TPE2),
            ("TRCK", metadata.track, TRCK),
            ("TDRC", metadata.date, TDRC),
        )
        for frame_id, value, frame_type in replacements:
            if value:
                tags.delall(frame_id)
                tags.add(frame_type(encoding=1, text=[value]))
    if cover and mime:
        tags.delall("APIC")
        tags.add(APIC(encoding=1, mime=mime, type=3, desc="Cover", data=cover))
    tags.save(path, v2_version=3, v23_sep="/")


def _write_flac_tags(path: Path, metadata: NCMMetadata | None, cover: bytes, mime: str | None) -> None:
    from mutagen.flac import FLAC, Picture

    audio = FLAC(path)
    if metadata is not None:
        values = {
            "title": metadata.title,
            "artist": metadata.artist,
            "album": metadata.album,
            "albumartist": metadata.album_artist,
            "tracknumber": metadata.track,
            "date": metadata.date,
        }
        for key, value in values.items():
            if value:
                audio[key] = [value]
    if cover and mime:
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = "Cover"
        picture.data = cover
        audio.clear_pictures()
        audio.add_picture(picture)
    audio.save()


def _write_tags(path: Path, probe: ProbeResult, cover: bytes, mime: str | None) -> None:
    if probe.metadata is None and not cover:
        return
    if probe.format == "mp3":
        _write_mp3_tags(path, probe.metadata, cover, mime)
    elif probe.format == "flac":
        _write_flac_tags(path, probe.metadata, cover, mime)


def _normalise_cover_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())
    except (UnicodeError, ValueError) as exc:
        raise NCMError("封面地址格式无效。") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() == "http" and (host == "music.126.net" or host.endswith(".music.126.net")):
        parsed = parsed._replace(scheme="https")
    if parsed.scheme.lower() != "https":
        raise NCMError("封面地址不是 HTTPS。")
    if not (host == "music.126.net" or host.endswith(".music.126.net")):
        raise NCMError("封面地址不属于网易图片域名。")
    if parsed.username or parsed.password:
        raise NCMError("封面地址包含不允许的认证信息。")
    try:
        port = parsed.port
    except ValueError as exc:
        raise NCMError("封面地址端口无效。") from exc
    if port not in (None, 443):
        raise NCMError("封面地址使用了不允许的端口。")
    netloc = host if port is None else f"{host}:{port}"
    try:
        return urlunsplit(("https", netloc, parsed.path or "/", parsed.query, ""))
    except (UnicodeError, ValueError) as exc:
        raise NCMError("封面地址格式无效。") from exc


class _SafeCoverRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        safe_url = _normalise_cover_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def _set_response_timeout(response, timeout: float) -> None:  # noqa: ANN001
    """Best-effort socket timeout tightening for a bounded streaming read."""

    candidates = (
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
    )
    for candidate in candidates:
        if candidate is not None and hasattr(candidate, "settimeout"):
            try:
                candidate.settimeout(max(0.1, timeout))
            except OSError:
                pass
            return


def _download_cover(
    url: str,
    cancel_event: threading.Event | None = None,
) -> tuple[bytes, str]:
    deadline = time.monotonic() + REMOTE_COVER_TIMEOUT
    try:
        safe_url = _normalise_cover_url(url)
        request = Request(safe_url, headers={"User-Agent": "NCM-Music-Converter/1.0"})
        opener = build_opener(_SafeCoverRedirectHandler())
        with opener.open(request, timeout=REMOTE_COVER_TIMEOUT) as response:
            length_header = response.headers.get("Content-Length")
            if length_header:
                try:
                    declared = int(length_header)
                except ValueError:
                    declared = 0
                if declared > MAX_REMOTE_COVER_SIZE:
                    raise NCMError("远程封面超过 10 MB 上限。")
            chunks: list[bytes] = []
            received = 0
            read = getattr(response, "read1", response.read)
            while received <= MAX_REMOTE_COVER_SIZE:
                _check_cancel(cancel_event)
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise NCMError("下载封面超过 10 秒总时限。")
                _set_response_timeout(response, min(2.0, remaining_time))
                chunk = read(min(64 * 1024, MAX_REMOTE_COVER_SIZE + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            data = b"".join(chunks)
    except NCMError:
        raise
    except (HTTPError, URLError, TimeoutError, socket.timeout, OSError, UnicodeError, ValueError) as exc:
        raise NCMError(f"下载封面失败：{exc}") from exc
    if len(data) > MAX_REMOTE_COVER_SIZE:
        raise NCMError("远程封面超过 10 MB 上限。")
    mime = _cover_mime(data)
    if mime is None:
        raise NCMError("远程封面不是受支持的 JPEG/PNG。")
    return data, mime


def _fsync_file(path: Path) -> None:
    try:
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    except OSError as exc:
        raise NCMError(f"无法同步临时输出：{exc}") from exc


def _commit_without_overwrite(part: Path, target: Path) -> None:
    """Atomically publish *part* only if *target* does not already exist."""

    if os.name == "nt":
        os.rename(part, target)  # Windows rename does not replace an existing path.
        return
    os.link(part, target)
    part.unlink()


def _commit_part(
    part: Path,
    desired: Path,
    target: Path,
    collision: str,
) -> tuple[Path, bool]:
    if collision == "overwrite":
        os.replace(part, target)
        return target, False
    while True:
        try:
            _commit_without_overwrite(part, target)
            return target, False
        except FileExistsError:
            if collision == "skip":
                part.unlink(missing_ok=True)
                return target, True
            target, _skipped = _resolve_output_path(desired, "rename")


def extract_ncm(
    path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str] | None = None,
    collision: str = "rename",
    fetch_cover: bool = False,
    write_tags: bool = True,
    cancel_event: threading.Event | None = None,
    progress_cb: ProgressCallback | None = None,
) -> ExtractionResult:
    """Extract one NCM to its original codec and atomically commit the output."""

    _check_cancel(cancel_event)
    probe = probe_ncm(path)
    warnings = list(probe.warnings)
    target_dir = (
        Path(output_dir).expanduser().resolve(strict=False)
        if output_dir is not None
        else probe.source_path.parent
    )
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NCMError(f"无法创建输出目录：{exc}") from exc
    if not target_dir.is_dir():
        raise NCMError("输出路径不是文件夹。")

    desired = target_dir / f"{probe.source_path.stem}{probe.extension}"
    target, skipped = _resolve_output_path(desired, collision)
    if skipped:
        return ExtractionResult(
            source_path=probe.source_path,
            output_path=target,
            format=probe.format,
            bytes_written=0,
            warnings=tuple(warnings),
            skipped=True,
        )

    part = target_dir / f".ncm-{uuid.uuid4().hex}.part"
    cover = probe.cover_data
    cover_mime = probe.cover_mime
    try:
        written = _extract_raw(probe, part, cancel_event, progress_cb)
        _check_cancel(cancel_event)

        if write_tags and fetch_cover and not cover and probe.metadata and probe.metadata.album_pic_url:
            try:
                cover, cover_mime = _download_cover(
                    probe.metadata.album_pic_url,
                    cancel_event=cancel_event,
                )
            except CancelledError:
                raise
            except NCMError as exc:
                warnings.append(f"封面补全失败：{exc}")

        if write_tags:
            try:
                _write_tags(part, probe, cover, cover_mime)
                _validate_audio_file(part, probe.format)
                _fsync_file(part)
            except Exception as full_error:
                try:
                    part.unlink(missing_ok=True)
                except OSError:
                    pass
                written = _extract_raw(probe, part, cancel_event, None)
                if cover and probe.metadata is not None:
                    try:
                        _write_tags(part, probe, b"", None)
                        _validate_audio_file(part, probe.format)
                        _fsync_file(part)
                        warnings.append(
                            f"封面写入失败：{full_error} 已保留文本标签。"
                        )
                    except Exception as text_error:
                        warnings.append(
                            f"标签/封面写入失败：{full_error}；"
                            f"文本标签重试也失败：{text_error} 已保留无标签音频。"
                        )
                        try:
                            part.unlink(missing_ok=True)
                        except OSError:
                            pass
                        written = _extract_raw(probe, part, cancel_event, None)
                else:
                    warnings.append(
                        f"标签/封面写入失败：{full_error} 已保留无标签音频。"
                    )

        _check_cancel(cancel_event)
        try:
            target, skipped_at_commit = _commit_part(
                part,
                desired,
                target,
                collision,
            )
        except OSError as exc:
            raise NCMError(f"无法提交输出文件：{exc}") from exc
    except BaseException:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return ExtractionResult(
        source_path=probe.source_path,
        output_path=target,
        format=probe.format,
        bytes_written=0 if skipped_at_commit else written,
        warnings=tuple(warnings),
        skipped=skipped_at_commit,
    )


__all__ = [
    "CancelledError",
    "ExtractionResult",
    "NCMError",
    "NCMMetadata",
    "ProbeResult",
    "STREAM_CHUNK_SIZE",
    "extract_ncm",
    "probe_ncm",
]
