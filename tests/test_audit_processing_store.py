from __future__ import annotations

import json
import os
import stat
from dataclasses import fields
from pathlib import Path

import pytest

from excel_to_skill.audit.processing_store import (
    LocalPreparedBundleStore,
    ProcessingStoreError,
    StoredPreparedBundle,
)


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "source-package"
    _write(package / "meta.json", b'{"source":"synthetic"}\n')
    _write(package / "data" / "cells.jsonl", b'{"sheet":"A","cell":"A1"}\n')
    _write(package / "layout" / "A.html", b"<table><td>A1</td></table>\n")
    return package


def _identity(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "workbook_id": "workbook-" + "a" * 48,
        "raw_snapshot_id": "b" * 64,
        "workbook_sha256": "c" * 64,
        "recipe": {"converter_version": "1.0", "full_names": False, "max_rows": 5000},
    }
    value.update(changes)
    return value


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)


def test_publish_replay_and_resolve_verify_the_exact_private_package(tmp_path: Path) -> None:
    package = _package(tmp_path)
    store = LocalPreparedBundleStore(tmp_path / "store")

    first = store.publish(package, _identity())
    second = store.publish(package, _identity())
    resolved = store.resolve(first)

    assert first == second
    assert len(first.snapshot_id) == 64
    assert len(first.package_manifest_sha256) == 64
    assert first.file_count == 3
    assert first.total_bytes == sum(
        path.stat().st_size for path in package.rglob("*") if path.is_file()
    )
    assert (resolved / "meta.json").read_bytes() == (package / "meta.json").read_bytes()
    assert (resolved / "data/cells.jsonl").read_bytes() == (
        package / "data/cells.jsonl"
    ).read_bytes()
    assert {field.name for field in fields(StoredPreparedBundle)} == {
        "snapshot_id",
        "package_manifest_sha256",
        "file_count",
        "total_bytes",
    }
    assert all(
        forbidden not in repr(first).lower()
        for forbidden in (str(tmp_path).lower(), "package_path", "asset_ref", "locator")
    )


def test_identity_changes_snapshot_without_changing_package_manifest(tmp_path: Path) -> None:
    package = _package(tmp_path)
    store = LocalPreparedBundleStore(tmp_path / "store")

    first = store.publish(package, _identity(profile="default"))
    second = store.publish(package, _identity(profile="full-names"))

    assert first.snapshot_id != second.snapshot_id
    assert first.package_manifest_sha256 == second.package_manifest_sha256
    assert store.resolve(first) != store.resolve(second)


def test_resolve_and_existing_target_replay_fail_closed_after_file_tamper(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path)
    store = LocalPreparedBundleStore(tmp_path / "store")
    stored = store.publish(package, _identity())
    resolved = store.resolve(stored)
    (resolved / "meta.json").write_bytes(b"tampered")
    os.chmod(resolved / "meta.json", 0o600)

    with pytest.raises(ProcessingStoreError) as resolve_error:
        store.resolve(stored)
    with pytest.raises(ProcessingStoreError) as replay_error:
        store.publish(package, _identity())

    assert resolve_error.value.code == "BUNDLE_INTEGRITY_MISMATCH"
    assert replay_error.value.code == "BUNDLE_INTEGRITY_MISMATCH"
    assert str(tmp_path) not in str(resolve_error.value)


def test_resolve_rejects_manifest_tamper_and_unexpected_stored_file(tmp_path: Path) -> None:
    package = _package(tmp_path)
    first_store = LocalPreparedBundleStore(tmp_path / "store-a")
    first = first_store.publish(package, _identity())
    first_package = first_store.resolve(first)
    manifest_path = first_package.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["total_bytes"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    os.chmod(manifest_path, 0o600)

    with pytest.raises(ProcessingStoreError) as manifest_error:
        first_store.resolve(first)
    assert manifest_error.value.code == "BUNDLE_INTEGRITY_MISMATCH"

    second_store = LocalPreparedBundleStore(tmp_path / "store-b")
    second = second_store.publish(package, _identity())
    second_package = second_store.resolve(second)
    _write(second_package / "unexpected.txt", b"not in manifest")
    os.chmod(second_package / "unexpected.txt", 0o600)

    with pytest.raises(ProcessingStoreError) as extra_error:
        second_store.resolve(second)
    assert extra_error.value.code == "BUNDLE_INTEGRITY_MISMATCH"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_publish_rejects_symlink_and_symlink_store_root(tmp_path: Path) -> None:
    package = _package(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (package / "data" / "escape").symlink_to(outside)
    store = LocalPreparedBundleStore(tmp_path / "store")

    with pytest.raises(ProcessingStoreError) as package_error:
        store.publish(package, _identity())
    assert package_error.value.code == "INVALID_PACKAGE"

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ProcessingStoreError) as root_error:
        LocalPreparedBundleStore(linked_root)
    assert root_error.value.code == "INVALID_ROOT"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_publish_rejects_special_files(tmp_path: Path) -> None:
    package = _package(tmp_path)
    os.mkfifo(package / "data" / "pipe")
    store = LocalPreparedBundleStore(tmp_path / "store")

    with pytest.raises(ProcessingStoreError) as error:
        store.publish(package, _identity())

    assert error.value.code == "INVALID_PACKAGE"


@pytest.mark.parametrize(
    ("max_files", "max_total_bytes", "files"),
    [
        (1, 100, {"a.txt": b"a", "b.txt": b"b"}),
        (2, 3, {"a.txt": b"four"}),
    ],
)
def test_publish_enforces_file_count_and_total_byte_bounds(
    tmp_path: Path,
    max_files: int,
    max_total_bytes: int,
    files: dict[str, bytes],
) -> None:
    package = tmp_path / "package"
    for name, content in files.items():
        _write(package / name, content)
    store = LocalPreparedBundleStore(
        tmp_path / "store",
        max_files=max_files,
        max_total_bytes=max_total_bytes,
    )

    with pytest.raises(ProcessingStoreError) as error:
        store.publish(package, _identity())

    assert error.value.code == "PACKAGE_LIMIT_EXCEEDED"


def test_invalid_identity_and_relative_root_fail_with_fixed_codes(tmp_path: Path) -> None:
    package = _package(tmp_path)
    store = LocalPreparedBundleStore(tmp_path / "store")

    with pytest.raises(ProcessingStoreError) as identity_error:
        store.publish(package, {"not_json": Path("private.xlsx")})
    with pytest.raises(ProcessingStoreError) as root_error:
        LocalPreparedBundleStore(Path("relative-store"))

    assert identity_error.value.code == "INVALID_IDENTITY"
    assert root_error.value.code == "INVALID_ROOT"


def test_publish_rejects_empty_content_and_store_tree_overlap(tmp_path: Path) -> None:
    empty_package = tmp_path / "empty-package"
    _write(empty_package / "empty.txt", b"")
    root = tmp_path / "store"
    store = LocalPreparedBundleStore(root)

    with pytest.raises(ProcessingStoreError) as empty_error:
        store.publish(empty_package, _identity())
    with pytest.raises(ProcessingStoreError) as overlap_error:
        store.publish(root, _identity())

    assert empty_error.value.code == "INVALID_PACKAGE"
    assert overlap_error.value.code == "INVALID_PACKAGE"


def test_store_object_and_all_copied_content_use_private_modes(tmp_path: Path) -> None:
    package = _package(tmp_path)
    root = tmp_path / "store"
    root.mkdir(mode=0o777)
    os.chmod(root, 0o777)
    store = LocalPreparedBundleStore(root)
    stored = store.publish(package, _identity())
    resolved = store.resolve(stored)

    assert _mode(root) == 0o700
    assert _mode(root / "objects") == 0o700
    assert _mode(root / ".publish.lock") == 0o600
    assert _mode(resolved.parent) == 0o700
    assert _mode(resolved) == 0o700
    assert _mode(resolved.parent / "manifest.json") == 0o600
    for path in resolved.rglob("*"):
        assert _mode(path) == (0o700 if path.is_dir() else 0o600)


def test_resolve_rejects_a_descriptor_for_an_unknown_object(tmp_path: Path) -> None:
    store = LocalPreparedBundleStore(tmp_path / "store")
    stored = StoredPreparedBundle(
        snapshot_id="a" * 64,
        package_manifest_sha256="b" * 64,
        file_count=1,
        total_bytes=1,
    )

    with pytest.raises(ProcessingStoreError) as error:
        store.resolve(stored)

    assert error.value.code == "STORE_UNAVAILABLE"
