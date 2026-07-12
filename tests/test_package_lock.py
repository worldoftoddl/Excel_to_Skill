from __future__ import annotations

import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from excel_to_skill import cache
import excel_to_skill.cli as cli_module


def test_package_lock_survives_package_directory_replacement(tmp_path: Path) -> None:
    pkg = tmp_path / "converted" / "workpaper_abc123"
    pkg.mkdir(parents=True)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first_writer() -> None:
        with cache.package_lock(pkg):
            first_entered.set()
            shutil.rmtree(pkg)
            pkg.mkdir()
            assert release_first.wait(timeout=5)

    def second_writer() -> None:
        assert first_entered.wait(timeout=5)
        with cache.package_lock(pkg):
            second_entered.set()

    first = threading.Thread(target=first_writer)
    second = threading.Thread(target=second_writer)
    first.start()
    second.start()
    assert first_entered.wait(timeout=5)
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()


def test_convert_relocks_when_source_change_derives_a_new_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "converted"
    source = tmp_path / "workpaper.xlsx"
    source.write_bytes(b"placeholder")
    package_a = root / "workpaper_aaaaaaaaaaaa"
    package_b = root / "workpaper_bbbbbbbbbbbb"

    def probe(path: Path, sha: str) -> cache.CacheProbe:
        return cache.CacheProbe(
            hit=False,
            reason="absent",
            sha256=sha * 64,
            package_dir=path.name,
            package_path=path,
            entry=None,
        )

    probes = [
        probe(package_a, "a"),
        probe(package_b, "b"),
        probe(package_b, "b"),
        probe(package_b, "b"),
    ]
    locked: list[Path] = []
    published: list[cache.CacheProbe] = []

    monkeypatch.setattr(cache, "probe", lambda *_args, **_kwargs: probes.pop(0))

    @contextmanager
    def fake_lock(path: Path):
        locked.append(path)
        yield

    def fake_convert(*_args, _probe=None, **_kwargs):
        published.append(_probe)
        return _probe.package_path

    monkeypatch.setattr(cache, "package_lock", fake_lock)
    monkeypatch.setattr(cli_module, "_convert_one_unlocked", fake_convert)

    result = cli_module._convert_one(source, root, force=False, cv="test")

    assert locked == [package_a, package_b]
    assert result == package_b
    assert len(published) == 1
    assert published[0].package_path == package_b


def test_convert_retries_when_source_changes_during_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "converted"
    source = tmp_path / "workpaper.xlsx"
    source.write_bytes(b"placeholder")
    package = root / "workpaper_aaaaaaaaaaaa"
    stable_probe = cache.CacheProbe(
        hit=False,
        reason="absent",
        sha256="a" * 64,
        package_dir=package.name,
        package_path=package,
        entry=None,
    )
    locked: list[Path] = []
    attempts = 0

    monkeypatch.setattr(cache, "probe", lambda *_args, **_kwargs: stable_probe)

    @contextmanager
    def fake_lock(path: Path):
        locked.append(path)
        yield

    def fake_convert(*_args, _probe=None, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise cli_module._SourceChangedDuringConversion("changed")
        return _probe.package_path

    monkeypatch.setattr(cache, "package_lock", fake_lock)
    monkeypatch.setattr(cli_module, "_convert_one_unlocked", fake_convert)

    result = cli_module._convert_one(source, root, force=False, cv="test")

    assert result == package
    assert locked == [package, package]
    assert attempts == 2
