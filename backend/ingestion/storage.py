from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass(frozen=True)
class StoredArtifact:
    original_name: str
    storage_key: str
    path: Path
    size_bytes: int
    sha256: str
    content_type: str


class ArtifactTooLargeError(ValueError):
    pass


class ArtifactStorage:
    def __init__(self, root: Path, *, max_size_bytes: int = 512 * 1024 * 1024) -> None:
        self.root = root.resolve()
        self.max_size_bytes = max_size_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def store_fileobj(
        self,
        challenge_id: uuid.UUID,
        original_name: str,
        source: BinaryIO,
        *,
        declared_content_type: str | None = None,
    ) -> StoredArtifact:
        safe_name = safe_filename(original_name)
        directory = self._challenge_directory(challenge_id)
        temporary = directory / f".{uuid.uuid4().hex}.upload"
        digest = hashlib.sha256()
        sample = bytearray()
        size = 0
        try:
            with temporary.open("xb") as destination:
                while chunk := source.read(1024 * 1024):
                    size = self._account_chunk(size, chunk)
                    digest.update(chunk)
                    if len(sample) < 8192:
                        sample.extend(chunk[: 8192 - len(sample)])
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            return self._finalize(
                challenge_id,
                safe_name,
                temporary,
                size,
                digest.hexdigest(),
                bytes(sample),
                declared_content_type,
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    async def store_async(
        self,
        challenge_id: uuid.UUID,
        original_name: str,
        chunks: AsyncIterator[bytes],
        *,
        declared_content_type: str | None = None,
    ) -> StoredArtifact:
        safe_name = safe_filename(original_name)
        directory = self._challenge_directory(challenge_id)
        temporary = directory / f".{uuid.uuid4().hex}.upload"
        digest = hashlib.sha256()
        sample = bytearray()
        size = 0
        try:
            with temporary.open("xb") as destination:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size = self._account_chunk(size, chunk)
                    digest.update(chunk)
                    if len(sample) < 8192:
                        sample.extend(chunk[: 8192 - len(sample)])
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            return self._finalize(
                challenge_id,
                safe_name,
                temporary,
                size,
                digest.hexdigest(),
                bytes(sample),
                declared_content_type,
            )
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def remove(self, storage_key: str) -> None:
        target = (self.root / storage_key).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("artifact storage key escaped the storage root")
        target.unlink(missing_ok=True)

    def resolve(self, storage_key: str) -> Path:
        target = (self.root / storage_key).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("artifact storage key escaped the storage root")
        return target

    def _challenge_directory(self, challenge_id: uuid.UUID) -> Path:
        directory = self.root / str(challenge_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _account_chunk(self, current_size: int, chunk: bytes) -> int:
        size = current_size + len(chunk)
        if size > self.max_size_bytes:
            raise ArtifactTooLargeError(
                f"artifact exceeds {self.max_size_bytes} byte limit"
            )
        return size

    def _finalize(
        self,
        challenge_id: uuid.UUID,
        safe_name: str,
        temporary: Path,
        size: int,
        sha256: str,
        sample: bytes,
        declared_content_type: str | None,
    ) -> StoredArtifact:
        final_name = f"{uuid.uuid4().hex}-{safe_name}"
        final_path = temporary.with_name(final_name)
        temporary.replace(final_path)
        storage_key = f"{challenge_id}/{final_name}"
        return StoredArtifact(
            original_name=safe_name,
            storage_key=storage_key,
            path=final_path,
            size_bytes=size,
            sha256=sha256,
            content_type=detect_mime(safe_name, sample, declared_content_type),
        )


def safe_filename(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()
    base = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", base)
    base = re.sub(r"\s+", " ", base).strip(" .")
    return base[:255] or "artifact.bin"


def detect_mime(
    name: str,
    sample: bytes,
    declared_content_type: str | None = None,
) -> str:
    signatures = (
        (b"PK\x03\x04", "application/zip"),
        (b"\x7fELF", "application/x-elf"),
        (b"MZ", "application/vnd.microsoft.portable-executable"),
        (b"%PDF-", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"\x1f\x8b", "application/gzip"),
        (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    )
    for signature, mime in signatures:
        if sample.startswith(signature):
            return mime

    if sample:
        try:
            decoded = sample.decode("utf-8")
            if name.lower().endswith(".json"):
                json.loads(decoded)
                return "application/json"
            printable = sum(character.isprintable() or character in "\r\n\t" for character in decoded)
            if printable / len(decoded) >= 0.95:
                return "text/plain"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    guessed, _encoding = mimetypes.guess_type(name)
    if guessed:
        return guessed
    if declared_content_type and declared_content_type != "application/octet-stream":
        return declared_content_type.split(";", maxsplit=1)[0].strip().lower()
    return "application/octet-stream"
