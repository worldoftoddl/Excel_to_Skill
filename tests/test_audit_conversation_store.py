"""Private refs-only storage for persistent audit conversations."""
from __future__ import annotations

import hashlib
import json
import math
import stat
from pathlib import Path

import pytest

from excel_to_skill.audit.conversation_store import (
    ConversationArtifactStore,
    ConversationArtifactStoreError,
)


def _object_path(root: Path, ref: dict[str, str]) -> Path:
    return root.joinpath(*ref["storage_key"].split("/"))


def test_write_returns_refs_only_and_loads_canonical_payload(tmp_path: Path) -> None:
    root = tmp_path / "private"
    store = ConversationArtifactStore(root)
    thread_id = "client-sensitive-thread"
    question = "매출채권 완전성에 미비점이 있나요?"

    ref = store.write(
        thread_id,
        kind="question",
        schema_version="audit_conversation.question.v1",
        payload={"question": question, "options": {"limit": 8}},
    )

    assert set(ref) == {
        "kind",
        "schema_version",
        "storage_key",
        "content_sha256",
    }
    assert thread_id not in ref["storage_key"]
    assert question not in json.dumps(ref, ensure_ascii=False)
    assert ref["storage_key"].startswith(
        f"threads/{hashlib.sha256(thread_id.encode()).hexdigest()}/objects/"
    )
    assert store.load(
        thread_id,
        ref,
        expected_kind="question",
        expected_schema_version="audit_conversation.question.v1",
    ) == {"options": {"limit": 8}, "question": question}

    object_path = _object_path(root, ref)
    document = json.loads(object_path.read_text(encoding="utf-8"))
    assert document == {
        "kind": "question",
        "payload": {"options": {"limit": 8}, "question": question},
        "schema_version": "audit_conversation.question.v1",
    }
    assert object_path.read_bytes().endswith(b"\n")
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(object_path.stat().st_mode) == 0o600


def test_content_address_is_order_independent_idempotent_and_thread_scoped(
    tmp_path: Path,
) -> None:
    store = ConversationArtifactStore(tmp_path / "private")
    first = store.write(
        "thread-a",
        kind="answer",
        schema_version="answer.v1",
        payload={"b": 2, "a": [1, True]},
    )
    again = store.write(
        "thread-a",
        kind="answer",
        schema_version="answer.v1",
        payload={"a": [1, True], "b": 2},
    )
    other_thread = store.write(
        "thread-b",
        kind="answer",
        schema_version="answer.v1",
        payload={"a": [1, True], "b": 2},
    )

    assert first == again
    assert first["content_sha256"] == other_thread["content_sha256"]
    assert first["storage_key"] != other_thread["storage_key"]
    assert len(list((tmp_path / "private").rglob("*.json"))) == 2


@pytest.mark.parametrize(
    "thread_id",
    ["", " leading", "trailing ", "bad\nline", "x" * 257],
)
def test_thread_id_must_be_bounded_clean_text(
    tmp_path: Path,
    thread_id: str,
) -> None:
    store = ConversationArtifactStore(tmp_path / "private")

    with pytest.raises(ConversationArtifactStoreError, match="thread_id"):
        store.write(
            thread_id,
            kind="question",
            schema_version="question.v1",
            payload={},
        )


@pytest.mark.parametrize(
    "payload",
    [object(), {1: "not-a-string-key"}, {"value": math.nan}],
)
def test_write_rejects_non_json_payloads(tmp_path: Path, payload: object) -> None:
    store = ConversationArtifactStore(tmp_path / "private")

    with pytest.raises(ConversationArtifactStoreError, match="JSON"):
        store.write(
            "thread-a",
            kind="question",
            schema_version="question.v1",
            payload=payload,
        )


def test_load_rejects_unknown_fields_traversal_and_cross_thread_refs(
    tmp_path: Path,
) -> None:
    store = ConversationArtifactStore(tmp_path / "private")
    ref = store.write(
        "thread-a",
        kind="question",
        schema_version="question.v1",
        payload={"question": "무엇이 누락됐나요?"},
    )

    with pytest.raises(ConversationArtifactStoreError, match="필드"):
        store.load("thread-a", {**ref, "payload": "leak"})
    with pytest.raises(ConversationArtifactStoreError, match="storage_key"):
        store.load("thread-a", {**ref, "storage_key": "../../secret.json"})
    with pytest.raises(ConversationArtifactStoreError, match="thread"):
        store.load("thread-b", ref)


def test_load_rejects_ref_envelope_and_digest_tampering(tmp_path: Path) -> None:
    root = tmp_path / "private"
    store = ConversationArtifactStore(root)
    ref = store.write(
        "thread-a",
        kind="answer",
        schema_version="answer.v1",
        payload={"answer": "현재 결론"},
    )

    with pytest.raises(ConversationArtifactStoreError, match="kind"):
        store.load("thread-a", {**ref, "kind": "question"})
    with pytest.raises(ConversationArtifactStoreError, match="schema_version"):
        store.load("thread-a", {**ref, "schema_version": "answer.v2"})
    with pytest.raises(ConversationArtifactStoreError, match="storage key"):
        store.load("thread-a", {**ref, "content_sha256": "0" * 64})
    with pytest.raises(ConversationArtifactStoreError, match="기대한 값"):
        store.load("thread-a", ref, expected_kind="question")

    object_path = _object_path(root, ref)
    document = json.loads(object_path.read_text(encoding="utf-8"))
    document["payload"]["answer"] = "변조된 결론"
    object_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConversationArtifactStoreError, match="digest"):
        store.load("thread-a", ref)


def test_load_rejects_noncanonical_json_and_object_symlink(tmp_path: Path) -> None:
    root = tmp_path / "private"
    store = ConversationArtifactStore(root)
    ref = store.write(
        "thread-a",
        kind="answer",
        schema_version="answer.v1",
        payload={"answer": "결론"},
    )
    object_path = _object_path(root, ref)
    document = json.loads(object_path.read_text(encoding="utf-8"))
    object_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConversationArtifactStoreError, match="canonical"):
        store.load("thread-a", ref)

    object_path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    object_path.symlink_to(outside)
    with pytest.raises(ConversationArtifactStoreError, match="symbolic link"):
        store.load("thread-a", ref)


def test_write_rejects_symlinked_store_component(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "threads").symlink_to(outside, target_is_directory=True)
    store = ConversationArtifactStore(root)

    with pytest.raises(ConversationArtifactStoreError, match="symbolic link"):
        store.write(
            "thread-a",
            kind="question",
            schema_version="question.v1",
            payload={},
        )


def test_constructor_rejects_symlinked_store_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "private"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ConversationArtifactStoreError, match="symbolic link"):
        ConversationArtifactStore(root)
