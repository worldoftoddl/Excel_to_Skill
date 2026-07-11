"""Repository checkout과 설치 wheel에서 공통으로 쓰는 schema/prompt 위치."""
from __future__ import annotations

from pathlib import Path


_PACKAGE_DIR = Path(__file__).resolve().parent
_REPOSITORY_ROOT = _PACKAGE_DIR.parents[1]


def _resource_dir(name: str) -> Path:
    packaged = _PACKAGE_DIR / name
    if packaged.is_dir():
        return packaged
    # 개발 checkout에서는 root schemas/prompts를 직접 사용한다.
    return _REPOSITORY_ROOT / name


SCHEMA_DIR = _resource_dir("schemas")
PROMPT_DIR = _resource_dir("prompts")


__all__ = ["PROMPT_DIR", "SCHEMA_DIR"]
