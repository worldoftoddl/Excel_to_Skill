"""Transactional orchestration for workbook facts, standards context, and audit brief."""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .. import cache
from ..emit_skill_md import build_skill_md_from_package
from ..meta import _now_iso, set_audit_preparation
from .brief import BRIEF_PROMPT, BRIEF_VERSION, build_audit_brief
from .contract import PREPARE_VERSION, bundle_keys
from .context import build_standards_context
from .extract import EXTRACTOR_VERSION, _prompt_bundle, extract_audit_facts
from .llm import load_prompt, load_schema
from .model import json_sha256
from .standards import StandardsRetriever
from .validate import validate_audit_bundle, validate_audit_package


CONTEXT_VERSION = "0.2.0"
_ARTIFACTS = (
    "audit_facts.json",
    "standards_context.json",
    "audit_brief.json",
)


class AuditPrepareError(RuntimeError):
    """The audit bundle could not be prepared without damaging the prior ready bundle."""


@dataclass(frozen=True, slots=True)
class PrepareResult:
    package: Path
    facts_path: Path
    standards_path: Path
    brief_path: Path
    status: str
    cached: bool


@dataclass(frozen=True, slots=True)
class _CachedBundle:
    facts: dict
    context: dict
    brief: dict
    keys: tuple[str, str, str]
    recipes: dict


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise AuditPrepareError(f"audit artifact 읽기 실패({path.name}): {e}") from e
    if not isinstance(doc, dict):
        raise AuditPrepareError(f"audit artifact는 JSON 객체여야 합니다: {path.name}")
    return doc


def _write_json(path: Path, doc: dict) -> None:
    _write_text(
        path,
        json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        temp_path.unlink(missing_ok=True)


def _snapshot_files(paths: list[Path]) -> dict[Path, bytes | None]:
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


def _restore_files(snapshot: dict[Path, bytes | None]) -> None:
    """Best-effort rollback for the small fixed publish set."""
    for path, content in snapshot.items():
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        except OSError:
            # Preserve the original exception. verify will report any rollback damage.
            pass


def _descriptor_identity(descriptor: dict) -> dict:
    """Retriever recipe identity: every descriptor/config field except observation time."""
    if not isinstance(descriptor, dict):
        raise AuditPrepareError("retriever_descriptor는 JSON 객체여야 합니다.")
    try:
        identity = json.loads(json.dumps(descriptor, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as e:
        raise AuditPrepareError(f"retriever_descriptor가 JSON 값이 아닙니다: {e}") from e
    identity.pop("retrieved_at", None)
    return identity


def _facts_recipe(pkg: Path, meta: dict, *, model: str) -> str:
    _, _, prompt_sha = _prompt_bundle()
    return cache.audit_stage_recipe_key(
        "facts",
        prepare_version=PREPARE_VERSION,
        stage_version=EXTRACTOR_VERSION,
        inputs={
            "model": model,
            "prompt_sha256": prompt_sha,
            "schema_sha256": json_sha256(load_schema("audit_facts.schema.json")),
            "source": meta.get("source"),
            "converter_version": meta.get("converter_version"),
            "conversion_params": meta.get("conversion_params"),
            "sheets": meta.get("sheets"),
            "cells_sha256": cache.file_sha256(pkg / "data/cells.jsonl"),
        },
    )


def _standards_recipe(facts: dict, retriever_descriptor: dict) -> str:
    return cache.audit_stage_recipe_key(
        "standards",
        prepare_version=PREPARE_VERSION,
        stage_version=CONTEXT_VERSION,
        inputs={
            "audit_facts_sha256": json_sha256(facts),
            "retriever": _descriptor_identity(retriever_descriptor),
            "schema_sha256": json_sha256(load_schema("standards_context.schema.json")),
        },
    )


def _brief_recipe(facts: dict, context: dict, *, model: str) -> str:
    _, prompt_sha = load_prompt(BRIEF_PROMPT)
    return cache.audit_stage_recipe_key(
        "brief",
        prepare_version=PREPARE_VERSION,
        stage_version=BRIEF_VERSION,
        inputs={
            "model": model,
            "prompt_sha256": prompt_sha,
            "schema_sha256": json_sha256(load_schema("audit_brief.schema.json")),
            "audit_facts_sha256": json_sha256(facts),
            "standards_context_sha256": json_sha256(context),
        },
    )


def _context_has_errors(context: dict) -> bool:
    queries = context.get("queries")
    return isinstance(queries, list) and any(
        isinstance(query, dict) and query.get("status") == "error"
        for query in queries
    )


def _load_cached_bundle(pkg: Path) -> _CachedBundle | None:
    """Load a fully valid prior bundle; stage recipes decide how much may be reused."""
    paths = [pkg / "data" / name for name in _ARTIFACTS]
    if not all(path.is_file() for path in paths):
        return None
    try:
        validate_audit_package(pkg)
        facts, context, brief = (_read_json(path) for path in paths)
    except Exception:  # damaged/stale bundle is a cache miss and will be replaced transactionally
        return None

    keys = bundle_keys(facts, context, brief)
    audit_meta = _read_json(pkg / "meta.json").get("audit_preparation", {})
    if not isinstance(audit_meta, dict) or audit_meta.get("version") != PREPARE_VERSION or (
        audit_meta.get("facts_key"),
        audit_meta.get("standards_key"),
        audit_meta.get("brief_key"),
    ) != keys:
        return None
    if (
        audit_meta.get("status") != brief.get("readiness", {}).get("status")
        or audit_meta.get("review_status") != brief.get("review", {}).get("status")
    ):
        return None
    recipes = cache.get_audit(pkg.parent, pkg.name) or {}
    return _CachedBundle(facts, context, brief, keys, recipes)


def _prepare_package_unlocked(
    pkg: Path | str,
    *,
    client=None,
    client_factory=None,
    retriever: StandardsRetriever | None,
    retriever_descriptor: dict,
    model: str,
    force: bool = False,
    generated_at: str | None = None,
    eprint=None,
) -> PrepareResult:
    """Prepare all three audit artifacts and publish them only after complete validation."""
    pkg = Path(pkg)
    eprint = eprint or (lambda *args: None)
    if not (pkg / "meta.json").is_file():
        raise AuditPrepareError(f"패키지 meta.json이 없습니다: {pkg}")

    try:
        meta = _read_json(pkg / "meta.json")
        facts_recipe = _facts_recipe(pkg, meta, model=model)
        cached = None if force else _load_cached_bundle(pkg)
        recipe_state_ok = bool(
            cached and cached.recipes.get("prepare_version") == PREPARE_VERSION
        )
        reuse_facts = bool(
            recipe_state_ok
            and cached
            and cached.recipes.get("facts_recipe_key") == facts_recipe
        )
        standards_recipe: str | None = None
        brief_recipe: str | None = None
        reuse_context = False
        reuse_brief = False
        if reuse_facts and cached is not None:
            standards_recipe = _standards_recipe(cached.facts, retriever_descriptor)
            reuse_context = bool(
                cached.recipes.get("standards_recipe_key") == standards_recipe
                and not _context_has_errors(cached.context)
            )
            if reuse_context:
                brief_recipe = _brief_recipe(cached.facts, cached.context, model=model)
                reuse_brief = cached.recipes.get("brief_recipe_key") == brief_recipe

        if reuse_facts and reuse_context and reuse_brief and cached is not None:
            status = cached.brief.get("readiness", {}).get("status", "not_ready")
            # SKILL is a deterministic view over the current package. Repair deletion/tampering
            # without paying model/RAG cost when the authoritative audit bundle is a cache hit.
            skill_text = build_skill_md_from_package(pkg)
            skill_path = pkg / "SKILL.md"
            if not skill_path.is_file() or skill_path.read_text(encoding="utf-8") != skill_text:
                _atomic_write_text(skill_path, skill_text)
            eprint(f"[audit prepare cache hit] {pkg.name}")
            return PrepareResult(
                package=pkg,
                facts_path=pkg / "data" / _ARTIFACTS[0],
                standards_path=pkg / "data" / _ARTIFACTS[1],
                brief_path=pkg / "data" / _ARTIFACTS[2],
                status=status,
                cached=True,
            )

        if client is None:
            if client_factory is None:
                raise AuditPrepareError(
                    "cache miss인 audit prepare에는 client 또는 client_factory가 필요합니다."
                )
            client = client_factory()
        if not reuse_context and retriever is None:
            raise AuditPrepareError(
                "standards stage cache miss에는 StandardsRetriever가 필요합니다."
            )

        generated_at = generated_at or _now_iso()
        data_dir = pkg / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".audit_prepare_", dir=pkg) as td:
            staging = Path(td)
            if reuse_facts and cached is not None:
                facts = cached.facts
                _write_json(staging / _ARTIFACTS[0], facts)
                eprint(f"[audit prepare stage cache] facts reuse: {pkg.name}")
            else:
                facts = extract_audit_facts(
                    pkg,
                    client=client,
                    model=model,
                    generated_at=generated_at,
                    output=staging / _ARTIFACTS[0],
                )

            standards_recipe = _standards_recipe(facts, retriever_descriptor)
            if reuse_context and cached is not None:
                context = cached.context
                _write_json(staging / _ARTIFACTS[1], context)
                eprint(f"[audit prepare stage cache] standards reuse: {pkg.name}")
            else:
                context = build_standards_context(
                    facts,
                    retriever,  # type: ignore[arg-type]  # 위 cache-miss guard로 non-null
                    retriever_descriptor=retriever_descriptor,
                    audit_facts_sha256=json_sha256(facts),
                )

            brief_recipe = _brief_recipe(facts, context, model=model)
            brief = build_audit_brief(
                facts,
                context,
                client=client,
                model=model,
                generated_at=generated_at,
                eprint=eprint,
            )
            validate_audit_bundle(pkg, facts, context, brief)
            if not (staging / _ARTIFACTS[1]).is_file():
                _write_json(staging / _ARTIFACTS[1], context)
            _write_json(staging / _ARTIFACTS[2], brief)
            keys = bundle_keys(facts, context, brief)
            status = brief.get("readiness", {}).get("status", "not_ready")
            _write_text(
                staging / "SKILL.md",
                build_skill_md_from_package(pkg, audit_brief=brief),
            )

            # All expensive/semantic work and validation completed.  Publish the fixed artifact
            # paths and the agent-facing SKILL first. meta.json is the final atomic commit marker,
            # so a hard crash before it cannot advertise a bundle whose SKILL is still stale.
            # If any catchable local write fails, restore the prior complete bundle byte-for-byte.
            targets = [data_dir / name for name in _ARTIFACTS]
            protected = [*targets, pkg / "meta.json", pkg / "SKILL.md"]
            snapshot = _snapshot_files(protected)
            try:
                for name, target in zip(_ARTIFACTS, targets, strict=True):
                    (staging / name).replace(target)
                (staging / "SKILL.md").replace(pkg / "SKILL.md")
                _fsync_directory(data_dir)
                _fsync_directory(pkg)
                set_audit_preparation(
                    pkg,
                    status=status,
                    version=PREPARE_VERSION,
                    facts_key=keys[0],
                    standards_key=keys[1],
                    brief_key=keys[2],
                    prepared_at=generated_at,
                    review_status=brief.get("review", {}).get("status", "draft"),
                )
            except BaseException:
                _restore_files(snapshot)
                raise

        try:
            entry = cache.update_audit(
                pkg.parent,
                pkg.name,
                facts_key=keys[0],
                standards_key=keys[1],
                brief_key=keys[2],
                facts_recipe_key=facts_recipe,
                standards_recipe_key=standards_recipe,
                brief_recipe_key=brief_recipe,
                prepare_version=PREPARE_VERSION,
                status=status,
            )
            if entry is None:
                eprint(f"[audit prepare] _index.json에 {pkg.name} 항목 없음 — cache mirror 생략")
        except Exception as e:  # package artifacts are authoritative; index is only a mirror
            eprint(f"[audit prepare] cache mirror 갱신 실패(준비본은 유효): {e}")
        return PrepareResult(
            package=pkg,
            facts_path=data_dir / _ARTIFACTS[0],
            standards_path=data_dir / _ARTIFACTS[1],
            brief_path=data_dir / _ARTIFACTS[2],
            status=status,
            cached=False,
        )
    except Exception as e:
        if isinstance(e, AuditPrepareError):
            raise
        raise AuditPrepareError(f"audit prepare 실패: {e}") from e


@contextmanager
def _package_lock(pkg: Path):
    """동일 package의 동시 prepare가 publish/rollback을 교차시키지 못하게 한다."""
    try:
        import fcntl
    except ImportError as e:  # pragma: no cover - 현재 지원 런타임은 POSIX
        raise AuditPrepareError("이 플랫폼에서는 audit prepare file lock을 지원하지 않습니다.") from e
    lock_path = pkg / ".audit_prepare.lock"
    try:
        lock_file = lock_path.open("a+b")
    except OSError as e:
        raise AuditPrepareError(f"audit prepare lock 열기 실패: {e}") from e
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def prepare_package(
    pkg: Path | str,
    *,
    client=None,
    client_factory=None,
    retriever: StandardsRetriever | None,
    retriever_descriptor: dict,
    model: str,
    force: bool = False,
    generated_at: str | None = None,
    eprint=None,
) -> PrepareResult:
    """한 package를 잠근 뒤 세 audit stage를 준비·검증·게시한다."""
    path = Path(pkg)
    if not path.is_dir() or not (path / "meta.json").is_file():
        # 기존 오류 문구와 타입을 보존하고, 잘못된 경로에 lock 파일을 만들지 않는다.
        return _prepare_package_unlocked(
            path,
            client=client,
            client_factory=client_factory,
            retriever=retriever,
            retriever_descriptor=retriever_descriptor,
            model=model,
            force=force,
            generated_at=generated_at,
            eprint=eprint,
        )
    with _package_lock(path):
        return _prepare_package_unlocked(
            path,
            client=client,
            client_factory=client_factory,
            retriever=retriever,
            retriever_descriptor=retriever_descriptor,
            model=model,
            force=force,
            generated_at=generated_at,
            eprint=eprint,
        )
