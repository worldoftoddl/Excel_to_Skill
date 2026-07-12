"""Atomic human approval/rejection for a committed audit bundle."""
from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path

from .. import cache
from ..emit_skill_md import build_skill_md_from_package
from ..meta import _now_iso, set_audit_preparation
from .consume import AuditConsumeError, load_validated_audit_bundle
from .contract import PREPARE_VERSION, bundle_keys
from .model import json_sha256
from .prepare import (
    _brief_recipe,
    _facts_recipe,
    _fsync_directory,
    _restore_files,
    _snapshot_files,
    _standards_recipe,
    _write_json,
    _write_text,
)
from .validate import validate_audit_bundle


class AuditReviewError(RuntimeError):
    """A committed audit bundle could not be reviewed safely."""


def review_audit_package(
    pkg: Path | str,
    *,
    status: str,
    note: str | None = None,
    reviewed_at: str | None = None,
    eprint=None,
) -> dict:
    """Approve or reject facts+brief together and republish their dependent hashes."""
    path = Path(pkg)
    if status not in {"approved", "rejected"}:
        raise AuditReviewError("audit review status는 approved/rejected 중 하나여야 합니다.")
    if status == "rejected" and not (isinstance(note, str) and note.strip()):
        raise AuditReviewError("audit-review --reject에는 --note가 필요합니다.")
    if status == "approved":
        note = None
    else:
        note = note.strip()
        if len(note) > 2000:
            raise AuditReviewError("audit-review --note는 2,000자 이하여야 합니다.")
    reviewed_at = reviewed_at or _now_iso()
    eprint = eprint or (lambda *args: None)

    try:
        with cache.package_lock(path):
            loaded = load_validated_audit_bundle(path)
            assert loaded is not None
            if status == "approved":
                from ..verify import verify_package

                verification = verify_package(path)
                if not verification.ok:
                    failures = [
                        f"{check.name}: {check.detail}"
                        for check in verification.checks
                        if not check.skipped and not check.ok
                    ]
                    raise AuditReviewError(
                        f"verify 실패로 audit 승인 거부 — {failures}"
                    )
            _, old_facts, old_context, old_brief = loaded
            meta_path = path / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            old_keys = bundle_keys(old_facts, old_context, old_brief)
            old_facts_model = old_facts.get("generator", {}).get("model")
            old_brief_model = old_brief.get("generator", {}).get("model")
            old_facts_recipe = _facts_recipe(path, meta, model=old_facts_model)
            old_standards_recipe = _standards_recipe(
                old_facts, old_context["retriever"]
            )
            old_brief_recipe = _brief_recipe(
                old_facts, old_context, model=old_brief_model
            )
            cache_state = cache.get_audit(path.parent, path.name) or {}
            recipe_witness = (
                cache_state.get("prepare_version") == PREPARE_VERSION
                and tuple(cache_state.get(name) for name in (
                    "facts_key", "standards_key", "brief_key"
                )) == old_keys
                and tuple(cache_state.get(name) for name in (
                    "facts_recipe_key", "standards_recipe_key", "brief_recipe_key"
                )) == (
                    old_facts_recipe, old_standards_recipe, old_brief_recipe
                )
            )
            facts = copy.deepcopy(old_facts)
            context = copy.deepcopy(old_context)
            brief = copy.deepcopy(old_brief)
            review = {"status": status, "reviewed_at": reviewed_at, "note": note}
            facts["review"] = copy.deepcopy(review)
            context["input"]["audit_facts_sha256"] = json_sha256(facts)
            brief["review"] = copy.deepcopy(review)
            brief["inputs"].update({
                "audit_facts_sha256": json_sha256(facts),
                "standards_context_sha256": json_sha256(context),
            })
            validate_audit_bundle(path, facts, context, brief)
            keys = bundle_keys(facts, context, brief)
            audit_meta = meta.get("audit_preparation", {})
            prepared_at = audit_meta.get("prepared_at")
            skill_text = build_skill_md_from_package(path, audit_brief=brief)

            targets = [
                path / "data/audit_facts.json",
                path / "data/standards_context.json",
                path / "data/audit_brief.json",
            ]
            protected = [*targets, path / "SKILL.md", meta_path]
            snapshot = _snapshot_files(protected)
            with tempfile.TemporaryDirectory(prefix=".audit_review_", dir=path) as td:
                staging = Path(td)
                for target, document in zip(
                    targets, (facts, context, brief), strict=True
                ):
                    _write_json(staging / target.name, document)
                _write_text(staging / "SKILL.md", skill_text)
                try:
                    for target in targets:
                        (staging / target.name).replace(target)
                    (staging / "SKILL.md").replace(path / "SKILL.md")
                    _fsync_directory(path / "data")
                    _fsync_directory(path)
                    set_audit_preparation(
                        path,
                        status=brief["readiness"]["status"],
                        version=PREPARE_VERSION,
                        facts_key=keys[0],
                        standards_key=keys[1],
                        brief_key=keys[2],
                        prepared_at=prepared_at,
                        review_status=status,
                    )
                except BaseException:
                    _restore_files(snapshot)
                    raise

            update = {
                "facts_key": keys[0],
                "standards_key": keys[1],
                "brief_key": keys[2],
                "prepare_version": PREPARE_VERSION,
                "status": brief["readiness"]["status"],
            }
            if recipe_witness:
                update.update({
                    "facts_recipe_key": old_facts_recipe,
                    "standards_recipe_key": _standards_recipe(
                        facts, context["retriever"]
                    ),
                    "brief_recipe_key": _brief_recipe(
                        facts, context, model=brief.get("generator", {}).get("model")
                    ),
                })
            else:
                update.update({
                    "facts_recipe_key": None,
                    "standards_recipe_key": None,
                    "brief_recipe_key": None,
                })
            try:
                cache.update_audit(path.parent, path.name, **update)
            except Exception as e:  # package commit is authoritative; cache is a mirror
                eprint(f"[audit-review] cache mirror 갱신 실패: {e}")
            return {
                "status": status,
                "facts": str(targets[0]),
                "brief": str(targets[2]),
                "skill": str(path / "SKILL.md"),
            }
    except (AuditReviewError, AuditConsumeError):
        raise
    except Exception as e:
        raise AuditReviewError(f"audit-review 실패: {e}") from e


def approve_audit_package(pkg: Path | str, **kwargs) -> dict:
    return review_audit_package(pkg, status="approved", **kwargs)


def reject_audit_package(pkg: Path | str, *, note: str, **kwargs) -> dict:
    return review_audit_package(pkg, status="rejected", note=note, **kwargs)


__all__ = [
    "AuditReviewError",
    "approve_audit_package",
    "reject_audit_package",
    "review_audit_package",
]
