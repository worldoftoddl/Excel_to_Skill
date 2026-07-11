"""Small shared contract for publishing and consuming prepared audit bundles.

``prepare.py`` will re-export these names after its concurrent cache work is integrated.  Keeping
the consumer side independent avoids a prepare -> SKILL emitter -> consumer import cycle.
"""
from __future__ import annotations

import json

from .. import cache


PREPARE_VERSION = "0.1.0"


def _without_runtime_fields(doc: dict) -> dict:
    clone = json.loads(json.dumps(doc, ensure_ascii=False))
    generator = clone.get("generator")
    if isinstance(generator, dict):
        generator.pop("generated_at", None)
    retriever = clone.get("retriever")
    if isinstance(retriever, dict):
        retriever.pop("retrieved_at", None)
    for citation in clone.get("citations", []):
        if isinstance(citation, dict):
            citation.pop("retrieved_at", None)
    return clone


def bundle_keys(facts: dict, context: dict, brief: dict) -> tuple[str, str, str]:
    """Compute the three stage keys while excluding runtime timestamps."""
    facts_key = cache.artifact_key("audit_facts", _without_runtime_fields(facts))
    standards_key = cache.artifact_key(
        "standards_context",
        {"facts_key": facts_key, "artifact": _without_runtime_fields(context)},
    )
    brief_key = cache.artifact_key(
        "audit_brief",
        {
            "facts_key": facts_key,
            "standards_key": standards_key,
            "artifact": _without_runtime_fields(brief),
        },
    )
    return facts_key, standards_key, brief_key
