"""§6 추출 캐시 — 결정론 계층 산출물의 재사용 판정(파일시스템 기준).

산출물 본체는 DB가 아니라 파일시스템 폴더에 둔다:

    converted/                               ← 출력 루트(--out DIR로 변경 가능)
    ├── _index.json                          ← 목록표(대장)
    └── {원본stem_slug}_{sha256 앞 12자}/     ← 원본 1개 = 폴더 1개

추출 캐시 키(§6)는 ``sha256(file) + converter_version``. hit 판정은 오너 결정에 따라
**"같은 sha256 + 같은 converter_version + 패키지 폴더 실재" 세 조건 모두 충족**일 때만
성립한다. 셋 중 하나라도 어긋나면 miss(재생성). 폴더명에는 sha 앞 12자만 박히므로,
전체 sha256은 색인 항목과 대조해 12자 접두 충돌(사실상 없음)을 방어한다.

- converter_version이 다르면 이 단계에선 그냥 miss로 보고 재생성한다. §6의
  semantics/review 자동 승계는 해석 계층(M3)이 생긴 뒤에 붙인다 — 지금은 유보.
- _index.json 항목의 ``generated_at``(최종생성시각)은 **가변값**이다. 색인은 결정론
  산출물 계약(§4) 밖의 운영 파일이므로 재현성 비교(V3) 대상이 아니다.

직렬화 관례는 다른 방출기(meta·emit_refs·emit_diag)와 동일: 레코드 필드는 고정 순서
(sort_keys 미사용), indent=2·ensure_ascii=False·allow_nan=False, 끝에 개행 1개. 단
``entries`` 묶음은 폴더명 키로 정렬해 삽입 순서와 무관하게 파일이 안정되도록 한다
(레코드 필드 순서를 뒤섞는 게 아니라 컬렉션만 정렬).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .meta import _converter_version, _now_iso, _source_sha256

_INDEX_NAME = "_index.json"
_INDEX_VERSION = 1
_SHA_PREFIX = 12  # 폴더명에 박는 sha256 접두 길이
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')  # 경로 위험 문자


def slugify(name: str) -> str:
    """§4.0 slug 규칙: 공백류→``_``, 경로 위험 문자 제거, 한글 유지."""
    s = _UNSAFE.sub("", re.sub(r"\s+", "_", name.strip()))
    s = s.strip("._")
    return s or "untitled"


def package_dirname(source_path: Path | str, sha256_hex: str) -> str:
    """패키지 폴더명 = ``{원본stem_slug}_{sha256 앞 12자}``."""
    return f"{slugify(Path(source_path).stem)}_{sha256_hex[:_SHA_PREFIX]}"


def _index_path(root: Path) -> Path:
    return root / _INDEX_NAME


def load_index(root: Path) -> dict:
    """``converted/_index.json``을 읽는다. 없으면 빈 색인을 돌려준다."""
    p = _index_path(root)
    if not p.exists():
        return {"index_version": _INDEX_VERSION, "entries": {}}
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def save_index(root: Path, index: dict) -> None:
    """``_index.json``을 쓴다. entries는 폴더명 키로 정렬(파일 안정)."""
    entries = index.get("entries", {})
    ordered = {
        "index_version": index.get("index_version", _INDEX_VERSION),
        "entries": {k: entries[k] for k in sorted(entries)},
    }
    root.mkdir(parents=True, exist_ok=True)
    with _index_path(root).open("w", encoding="utf-8", newline="\n") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def _build_entry(
    source_path: Path,
    sha256_hex: str,
    converter_version: str,
    conversion_params: dict | None,
    generated_at: str,
    annotation_key: str | None,
    review_status: str | None,
) -> dict:
    """§6 색인 항목 — 필드 순서 고정(원본명·sha256·경로·버전·변환파라미터·주석키·리뷰·시각).

    conversion_params(max_rows·full_names)는 결정론 산출을 좌우하므로 캐시 키의
    일부다. 같은 sha·같은 converter_version이어도 옵션이 다르면 다른 패키지이며,
    probe가 이 값을 대조해 stale hit(옛 옵션 패키지 재사용)을 막는다.
    """
    return {
        "source_filename": source_path.name,
        "sha256": sha256_hex,
        "package_path": package_dirname(source_path, sha256_hex),
        "converter_version": converter_version,
        "conversion_params": conversion_params,
        "annotation_key": annotation_key,  # convert 시 None → annotate가 갱신(§6)
        "review_status": review_status,  # convert 시 None → annotate/review가 갱신
        "generated_at": generated_at,  # 가변 — 재현성 비교(V3) 제외
    }


@dataclass(frozen=True)
class CacheProbe:
    """추출 캐시 조회 결과.

    reason: hit이면 ``match``. miss면 ``force``/``absent``/``version_changed``/
    ``params_changed``/``sha_mismatch``/``folder_missing`` 중 하나 — cli가 stderr/로그로
    사유를 밝힐 때 쓴다.
    """

    hit: bool
    reason: str
    sha256: str
    package_dir: str  # 폴더명
    package_path: Path  # root / 폴더명
    entry: dict | None  # 색인에 있던 기존 항목(없으면 None)


def probe(
    root: Path,
    source_path: Path | str,
    *,
    converter_version: str | None = None,
    conversion_params: dict | None = None,
    force: bool = False,
) -> CacheProbe:
    """원본을 캐시에 대조한다. hit이면 재생성 없이 기존 폴더를 재사용하면 된다.

    conversion_params(max_rows·full_names)를 주면 색인 항목의 값과 대조해, 다르면
    ``params_changed`` miss로 재생성한다(옵션이 바뀌면 산출이 달라지므로). 주지 않으면
    이 대조를 건너뛴다(하위호환 — cli는 항상 현재 옵션을 넘긴다).
    """
    src = Path(source_path)
    cv = converter_version if converter_version is not None else _converter_version()
    sha = _source_sha256(src)
    dirname = package_dirname(src, sha)
    pkg = root / dirname
    entry = load_index(root)["entries"].get(dirname)

    if force:
        return CacheProbe(False, "force", sha, dirname, pkg, entry)
    if entry is None:
        return CacheProbe(False, "absent", sha, dirname, pkg, None)
    if entry.get("converter_version") != cv:
        return CacheProbe(False, "version_changed", sha, dirname, pkg, entry)
    if conversion_params is not None and entry.get("conversion_params") != conversion_params:
        return CacheProbe(False, "params_changed", sha, dirname, pkg, entry)
    if entry.get("sha256") != sha:  # 12자 접두 충돌 방어
        return CacheProbe(False, "sha_mismatch", sha, dirname, pkg, entry)
    if not pkg.is_dir():
        return CacheProbe(False, "folder_missing", sha, dirname, pkg, entry)
    return CacheProbe(True, "match", sha, dirname, pkg, entry)


def annotation_key(
    file_sha256: str, annotator_version: str, model: str, prompt_sha: str
) -> str:
    """주석 캐시 키(§6) = sha256(file + annotator_version + model + prompt_sha) hex.

    파일·어노테이터·모델·프롬프트 중 하나라도 바뀌면 키가 달라져 재주석 대상이 된다.
    성분 원문은 meta.source.sha256과 semantics.generator에 남으므로 키는 해시로 압축한다.
    """
    h = hashlib.sha256()
    h.update("\n".join([file_sha256, annotator_version, model, prompt_sha]).encode("utf-8"))
    return h.hexdigest()


_UNSET = object()  # "이 인자는 건드리지 않음"을 명시적 None(=값을 None으로 설정)과 구분


def update_annotation(
    root: Path,
    dirname: str,
    *,
    annotation_key=_UNSET,
    review_status=_UNSET,
) -> dict | None:
    """기존 색인 항목의 해석 계층 필드만 갱신한다(결정론 필드는 불변).

    annotate/review가 semantics를 바꿀 때 `_index.json`을 맞춘다. 항목이 없으면
    None(convert 없이 만들어진 패키지 등 — 호출자가 경고). **인자를 주면(=명시적으로
    None이라도) 덮어쓰고, 생략하면 건드리지 않는다** — annotation_key=None을 넘겨
    '완료되지 않은 주석'의 키를 clear할 수 있게 하기 위함(partial 실패 캐시 오염 방지).
    """
    index = load_index(root)
    entry = index.get("entries", {}).get(dirname)
    if entry is None:
        return None
    if annotation_key is not _UNSET:
        entry["annotation_key"] = annotation_key
    if review_status is not _UNSET:
        entry["review_status"] = review_status
    save_index(root, index)
    return entry


def record(
    root: Path,
    source_path: Path | str,
    *,
    sha256: str | None = None,
    converter_version: str | None = None,
    conversion_params: dict | None = None,
    generated_at: str | None = None,
    annotation_key: str | None = None,
    review_status: str | None = None,
) -> dict:
    """변환 성공 후 색인에 항목을 upsert하고, 그 항목을 반환한다.

    sha256/converter_version은 ``probe``에서 이미 계산했으면 넘겨 재해시를 피한다.
    conversion_params는 이 변환에 실제로 쓴 옵션(max_rows·full_names)을 그대로 기록한다.
    """
    src = Path(source_path)
    sha = sha256 if sha256 is not None else _source_sha256(src)
    cv = converter_version if converter_version is not None else _converter_version()
    ts = generated_at if generated_at is not None else _now_iso()
    entry = _build_entry(src, sha, cv, conversion_params, ts, annotation_key, review_status)

    index = load_index(root)
    index.setdefault("entries", {})[entry["package_path"]] = entry
    save_index(root, index)
    return entry
