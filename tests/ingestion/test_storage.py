import hashlib
import io
import uuid
from pathlib import Path

import pytest

from backend.ingestion.storage import (
    ArtifactStorage,
    ArtifactTooLargeError,
    detect_mime,
    safe_filename,
)


def test_safe_filename_removes_paths_and_control_characters() -> None:
    assert safe_filename("../../payload.zip") == "payload.zip"
    assert safe_filename(r"C:\temp\payload?.zip") == "payload_.zip"
    assert safe_filename(". ") == "artifact.bin"


def test_detect_mime_prefers_file_signature() -> None:
    assert detect_mime("payload.txt", b"PK\x03\x04data", "text/plain") == "application/zip"
    assert detect_mime("config.json", b'{"ok": true}') == "application/json"
    assert detect_mime("notes.unknown", b"plain text\n") == "text/plain"


def test_store_fileobj_calculates_digest_and_isolates_challenge(tmp_path: Path) -> None:
    challenge_id = uuid.uuid4()
    content = b"PK\x03\x04fixture"
    storage = ArtifactStorage(tmp_path)

    stored = storage.store_fileobj(
        challenge_id,
        "../fixture.zip",
        io.BytesIO(content),
        declared_content_type="application/octet-stream",
    )

    assert stored.path.read_bytes() == content
    assert stored.path.parent == tmp_path.resolve() / str(challenge_id)
    assert stored.sha256 == hashlib.sha256(content).hexdigest()
    assert stored.size_bytes == len(content)
    assert stored.content_type == "application/zip"
    assert storage.resolve(stored.storage_key) == stored.path


def test_store_fileobj_removes_partial_file_when_limit_is_exceeded(tmp_path: Path) -> None:
    challenge_id = uuid.uuid4()
    storage = ArtifactStorage(tmp_path, max_size_bytes=4)

    with pytest.raises(ArtifactTooLargeError):
        storage.store_fileobj(challenge_id, "large.bin", io.BytesIO(b"12345"))

    assert list((tmp_path / str(challenge_id)).iterdir()) == []


def test_resolve_rejects_storage_key_escape(tmp_path: Path) -> None:
    storage = ArtifactStorage(tmp_path)

    with pytest.raises(ValueError, match="escaped"):
        storage.resolve(str(Path("..") / "outside.bin"))
