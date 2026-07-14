"""Bounded structural validation for untrusted XLSX archive bytes.

The deterministic loader still owns workbook extraction.  This module is the smaller trust
boundary used before an uploaded ZIP is handed to that loader: it rejects ambiguous outer ZIP
framing, unsafe archive metadata, corrupt members, non-XLSX package roots, macros, and XML entity
constructs without extracting any member to the filesystem.
"""
from __future__ import annotations

import posixpath
import struct
from io import BytesIO
from pathlib import PurePosixPath
from typing import Final
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from defusedxml import ElementTree as SafeElementTree


MAX_XLSX_MEMBERS = 10_000
MAX_XLSX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 200

_LOCAL_FILE_HEADER: Final = b"PK\x03\x04"
_EOCD_SIGNATURE: Final = b"PK\x05\x06"
_EOCD_SIZE: Final = 22
_MAX_ZIP_COMMENT_BYTES: Final = 65_535
_READ_CHUNK_BYTES: Final = 1024 * 1024

_CONTENT_TYPES = "[Content_Types].xml"
_ROOT_RELATIONSHIPS = "_rels/.rels"
_WORKBOOK = "xl/workbook.xml"
_WORKBOOK_RELATIONSHIPS = "xl/_rels/workbook.xml.rels"
_REQUIRED_PARTS = frozenset(
    {_CONTENT_TYPES, _ROOT_RELATIONSHIPS, _WORKBOOK, _WORKBOOK_RELATIONSHIPS}
)

_XLSX_WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)
_CONTENT_TYPES_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/content-types"
)
_PACKAGE_RELATIONSHIPS_NAMESPACE = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)
_WORKBOOK_NAMESPACES = (
    "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "http://purl.oclc.org/ooxml/spreadsheetml/main",
)
_RELATIONSHIP_BASES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
)
_OFFICE_DOCUMENT_RELATIONSHIPS = frozenset(
    f"{base}/officeDocument" for base in _RELATIONSHIP_BASES
)
_SHEET_RELATIONSHIPS = frozenset(
    f"{base}/{kind}"
    for base in _RELATIONSHIP_BASES
    for kind in ("worksheet", "chartsheet", "dialogsheet")
)
_RELATIONSHIP_ID_ATTRIBUTES = tuple(
    f"{{{base}}}id" for base in _RELATIONSHIP_BASES
)
_XML_MEMBER_SUFFIXES = (".xml", ".rels", ".vml")

_MESSAGES = {
    "CONTRACT_MISMATCH": "XLSX archive 계약이 유효하지 않습니다.",
    "LIMIT_EXCEEDED": "XLSX archive 안전 상한을 초과했습니다.",
}


class XlsxSafetyError(ValueError):
    """Fixed, input-free failure raised by :func:`validate_xlsx_archive`."""

    def __init__(self, code: str) -> None:
        safe_code = code if code in _MESSAGES else "CONTRACT_MISMATCH"
        self.code = safe_code
        super().__init__(_MESSAGES[safe_code])


def _fail(code: str = "CONTRACT_MISMATCH") -> XlsxSafetyError:
    return XlsxSafetyError(code)


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _qualified(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _find_eocd(data: bytes) -> tuple[int, tuple[int, int, int, int, int, int, int]]:
    """Return the one EOCD record whose declared comment ends exactly at EOF."""

    start = max(0, len(data) - _EOCD_SIZE - _MAX_ZIP_COMMENT_BYTES)
    cursor = len(data)
    while True:
        offset = data.rfind(_EOCD_SIGNATURE, start, cursor)
        if offset < 0:
            raise _fail()
        if offset + _EOCD_SIZE <= len(data):
            fields = struct.unpack_from("<4H2IH", data, offset + 4)
            comment_length = fields[-1]
            if offset + _EOCD_SIZE + comment_length == len(data):
                return offset, fields
        cursor = offset


def _validate_outer_zip(data: bytes) -> tuple[int, int, int]:
    if not isinstance(data, bytes) or not data.startswith(_LOCAL_FILE_HEADER):
        raise _fail()
    eocd_offset, fields = _find_eocd(data)
    (
        disk_number,
        central_disk_number,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        _comment_length,
    ) = fields
    if (
        disk_number != 0
        or central_disk_number != 0
        or entries_on_disk != total_entries
        or total_entries < 1
        or total_entries > MAX_XLSX_MEMBERS
        or entries_on_disk == 0xFFFF
        or total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
        or central_offset + central_size != eocd_offset
    ):
        code = "LIMIT_EXCEEDED" if total_entries > MAX_XLSX_MEMBERS else "CONTRACT_MISMATCH"
        raise _fail(code)
    return total_entries, central_offset, central_size


def _validate_member_metadata(archive: ZipFile, *, expected_entries: int) -> set[str]:
    members = archive.infolist()
    if len(members) != expected_entries or len(members) > MAX_XLSX_MEMBERS:
        raise _fail("LIMIT_EXCEEDED")
    names: set[str] = set()
    total_uncompressed = 0
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        if (
            not name
            or len(name) > 512
            or "\x00" in name
            or "\\" in name
            or path.is_absolute()
            or ".." in path.parts
            or name in names
            or member.flag_bits & 0x1
            or member.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
        ):
            raise _fail()
        names.add(name)
        if member.is_dir():
            continue
        if member.file_size > MAX_XLSX_MEMBER_BYTES:
            raise _fail("LIMIT_EXCEEDED")
        total_uncompressed += member.file_size
        if total_uncompressed > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise _fail("LIMIT_EXCEEDED")
        if (
            member.file_size > 0
            and (
                member.compress_size <= 0
                or member.file_size
                > member.compress_size * MAX_XLSX_COMPRESSION_RATIO
            )
        ):
            raise _fail("LIMIT_EXCEEDED")
    if not _REQUIRED_PARTS.issubset(names):
        raise _fail()
    return names


def _read_every_member(archive: ZipFile) -> None:
    """Drain every member so ZIP decompression and CRC checks actually run."""

    for member in archive.infolist():
        observed = 0
        with archive.open(member, "r") as source:
            while True:
                chunk = source.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > member.file_size:
                    raise _fail()
        if observed != member.file_size:
            raise _fail()


def _validate_xml_members(archive: ZipFile) -> None:
    """Reject malformed XML plus DTD/entity/external-reference constructs in every XML part."""

    for member in archive.infolist():
        if member.is_dir() or not member.filename.lower().endswith(_XML_MEMBER_SUFFIXES):
            continue
        with archive.open(member, "r") as source:
            for _event, element in SafeElementTree.iterparse(
                source,
                events=("end",),
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            ):
                element.clear()


def _xml_root(archive: ZipFile, name: str):
    return SafeElementTree.fromstring(
        archive.read(name),
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )


def _validate_content_types(archive: ZipFile, names: set[str]) -> None:
    root = _xml_root(archive, _CONTENT_TYPES)
    if root.tag != _qualified(_CONTENT_TYPES_NAMESPACE, "Types"):
        raise _fail()
    workbook_types: list[str] = []
    for element in root:
        local_name = _local_name(element.tag)
        if local_name not in {"Default", "Override"}:
            continue
        if element.tag != _qualified(_CONTENT_TYPES_NAMESPACE, local_name):
            raise _fail()
        content_type = element.get("ContentType")
        if not isinstance(content_type, str) or not content_type:
            raise _fail()
        lowered = content_type.lower()
        if "macroenabled" in lowered or "vbaproject" in lowered:
            raise _fail()
        if (
            local_name == "Override"
            and element.get("PartName") == "/xl/workbook.xml"
        ):
            workbook_types.append(content_type)
    if workbook_types != [_XLSX_WORKBOOK_CONTENT_TYPE]:
        raise _fail()
    if any(name.lower().endswith("vbaproject.bin") for name in names):
        raise _fail()


def _resolve_relationship_target(source_part: str, target: object) -> str:
    if (
        not isinstance(target, str)
        or not target
        or "\x00" in target
        or "\\" in target
        or "?" in target
        or "#" in target
    ):
        raise _fail()
    raw = target[1:] if target.startswith("/") else target
    raw_path = PurePosixPath(raw)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        raise _fail()
    if target.startswith("/"):
        resolved = posixpath.normpath(raw)
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source_part), raw))
    if not resolved or resolved in {".", ".."} or resolved.startswith("../"):
        raise _fail()
    return resolved


def _relationship_elements(root) -> list[object]:
    if root.tag != _qualified(_PACKAGE_RELATIONSHIPS_NAMESPACE, "Relationships"):
        raise _fail()
    relationships = [
        item
        for item in root
        if item.tag == _qualified(_PACKAGE_RELATIONSHIPS_NAMESPACE, "Relationship")
    ]
    if not relationships:
        raise _fail()
    return relationships


def _validate_relationships(archive: ZipFile, names: set[str]) -> None:
    root_relationships = _relationship_elements(_xml_root(archive, _ROOT_RELATIONSHIPS))
    office_documents = [
        item
        for item in root_relationships
        if item.get("Type") in _OFFICE_DOCUMENT_RELATIONSHIPS
    ]
    if len(office_documents) != 1:
        raise _fail()
    office_document = office_documents[0]
    if (
        not office_document.get("Id")
        or office_document.get("TargetMode") not in {None, "Internal"}
        or _resolve_relationship_target("", office_document.get("Target")) != _WORKBOOK
    ):
        raise _fail()

    workbook_root = _xml_root(archive, _WORKBOOK)
    if workbook_root.tag not in {
        _qualified(namespace, "workbook") for namespace in _WORKBOOK_NAMESPACES
    }:
        raise _fail()
    sheet_tags = {
        _qualified(namespace, "sheet") for namespace in _WORKBOOK_NAMESPACES
    }
    sheet_relationship_ids: list[str] = []
    for element in workbook_root.iter():
        if element.tag not in sheet_tags:
            continue
        relationship_id = next(
            (
                element.get(attribute)
                for attribute in _RELATIONSHIP_ID_ATTRIBUTES
                if element.get(attribute) is not None
            ),
            None,
        )
        if not isinstance(relationship_id, str) or not relationship_id:
            raise _fail()
        sheet_relationship_ids.append(relationship_id)
    if not sheet_relationship_ids or len(set(sheet_relationship_ids)) != len(
        sheet_relationship_ids
    ):
        raise _fail()

    workbook_relationships = _relationship_elements(
        _xml_root(archive, _WORKBOOK_RELATIONSHIPS)
    )
    by_id: dict[str, object] = {}
    for relationship in workbook_relationships:
        relationship_id = relationship.get("Id")
        if (
            not isinstance(relationship_id, str)
            or not relationship_id
            or relationship_id in by_id
        ):
            raise _fail()
        by_id[relationship_id] = relationship
    for relationship_id in sheet_relationship_ids:
        relationship = by_id.get(relationship_id)
        if (
            relationship is None
            or relationship.get("Type") not in _SHEET_RELATIONSHIPS
            or relationship.get("TargetMode") not in {None, "Internal"}
        ):
            raise _fail()
        target = _resolve_relationship_target(_WORKBOOK, relationship.get("Target"))
        if target not in names or target.endswith("/"):
            raise _fail()


def validate_xlsx_archive(data: bytes) -> None:
    """Validate one complete XLSX byte string without extracting or trusting its filename.

    ``LIMIT_EXCEEDED`` identifies the fixed archive expansion envelope.  Every other malformed,
    ambiguous, corrupt, macro-enabled, or unsafe package fails as ``CONTRACT_MISMATCH``.
    """

    try:
        expected_entries, central_offset, _central_size = _validate_outer_zip(data)
        with ZipFile(BytesIO(data), "r") as archive:
            if archive.start_dir != central_offset:
                raise _fail()
            names = _validate_member_metadata(archive, expected_entries=expected_entries)
            _read_every_member(archive)
            _validate_xml_members(archive)
            _validate_content_types(archive, names)
            _validate_relationships(archive, names)
    except XlsxSafetyError:
        raise
    except Exception:
        raise _fail() from None


__all__ = [
    "MAX_XLSX_COMPRESSION_RATIO",
    "MAX_XLSX_MEMBERS",
    "MAX_XLSX_MEMBER_BYTES",
    "MAX_XLSX_UNCOMPRESSED_BYTES",
    "XlsxSafetyError",
    "validate_xlsx_archive",
]
