from __future__ import annotations

import struct
import warnings
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from excel_to_skill.audit import xlsx_safety
from excel_to_skill.audit.workbook_inspection import (
    WorkbookInspectionError,
    _validate_xlsx_archive,
)
from excel_to_skill.audit.xlsx_safety import XlsxSafetyError, validate_xlsx_archive


FIXTURES = Path(__file__).parent / "fixtures"
_STANDARD_WORKBOOK_TYPE = (
    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)
_MACRO_WORKBOOK_TYPE = b"application/vnd.ms-excel.sheet.macroEnabled.main+xml"


def _base() -> bytes:
    return (FIXTURES / "fx1_merge_formula.xlsx").read_bytes()


def _rewrite(
    data: bytes,
    *,
    replacements: dict[str, bytes] | None = None,
    omitted: set[str] | None = None,
    additions: list[tuple[str, bytes, int]] | None = None,
    comment: bytes | None = None,
) -> bytes:
    replacements = {} if replacements is None else replacements
    omitted = set() if omitted is None else omitted
    additions = [] if additions is None else additions
    output = BytesIO()
    with ZipFile(BytesIO(data), "r") as source, ZipFile(output, "w") as target:
        for member in source.infolist():
            if member.filename in omitted:
                continue
            content = replacements.get(member.filename, source.read(member))
            target.writestr(member.filename, content, compress_type=member.compress_type)
        for name, content, compression in additions:
            target.writestr(name, content, compress_type=compression)
        if comment is not None:
            target.comment = comment
    return output.getvalue()


def _assert_code(code: str, data: bytes) -> XlsxSafetyError:
    with pytest.raises(XlsxSafetyError) as caught:
        validate_xlsx_archive(data)
    assert caught.value.code == code
    assert "fx1" not in str(caught.value)
    return caught.value


def test_all_committed_xlsx_fixtures_pass_the_shared_safety_boundary() -> None:
    fixtures = sorted(FIXTURES.glob("*.xlsx"))
    assert [path.name for path in fixtures] == [
        "fx1_merge_formula.xlsx",
        "fx2_refs.xlsx",
        "fx3_slots_hidden.xlsx",
        "fx4_defined_names.xlsx",
    ]
    for path in fixtures:
        assert validate_xlsx_archive(path.read_bytes()) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: b"%PDF-1.7\n" + data,
        lambda data: b"<html>not an xlsx envelope</html>" + data,
        lambda data: data + b"JUNK",
        lambda data: data + data,
    ],
    ids=("pdf-prefix", "html-prefix", "trailing-bytes", "concatenated-zip"),
)
def test_outer_zip_framing_rejects_prefix_suffix_and_concatenation(mutate) -> None:
    _assert_code("CONTRACT_MISMATCH", mutate(_base()))


def test_outer_zip_accepts_an_exact_eocd_comment_but_rejects_central_offset_drift() -> None:
    commented = _rewrite(_base(), comment=b"bounded-comment")
    validate_xlsx_archive(commented)

    drifted = bytearray(_base())
    eocd = drifted.rfind(b"PK\x05\x06")
    assert eocd >= 0
    central_offset = struct.unpack_from("<I", drifted, eocd + 16)[0]
    struct.pack_into("<I", drifted, eocd + 16, central_offset + 1)
    _assert_code("CONTRACT_MISMATCH", bytes(drifted))


def test_every_member_is_drained_and_crc_checked() -> None:
    with_payload = _rewrite(
        _base(),
        additions=[("custom/payload.bin", b"crc-must-be-read", ZIP_STORED)],
    )
    damaged = bytearray(with_payload)
    with ZipFile(BytesIO(with_payload), "r") as archive:
        member = archive.getinfo("custom/payload.bin")
        header = member.header_offset
        name_length, extra_length = struct.unpack_from("<HH", damaged, header + 26)
        payload_offset = header + 30 + name_length + extra_length
    damaged[payload_offset] ^= 0x01
    _assert_code("CONTRACT_MISMATCH", bytes(damaged))


@pytest.mark.parametrize(
    "part",
    [
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
    ],
)
def test_required_ooxml_parts_cannot_be_missing(part: str) -> None:
    _assert_code("CONTRACT_MISMATCH", _rewrite(_base(), omitted={part}))


def test_requires_standard_xlsx_content_type_and_rejects_macros() -> None:
    with ZipFile(BytesIO(_base()), "r") as archive:
        content_types = archive.read("[Content_Types].xml")
    assert _STANDARD_WORKBOOK_TYPE in content_types
    macro = content_types.replace(_STANDARD_WORKBOOK_TYPE, _MACRO_WORKBOOK_TYPE)
    _assert_code(
        "CONTRACT_MISMATCH",
        _rewrite(_base(), replacements={"[Content_Types].xml": macro}),
    )
    _assert_code(
        "CONTRACT_MISMATCH",
        _rewrite(
            _base(),
            additions=[("xl/vbaProject.bin", b"macro", ZIP_STORED)],
        ),
    )

    wrong_namespace = content_types.replace(
        b"http://schemas.openxmlformats.org/package/2006/content-types",
        b"https://invalid.example/content-types",
    )
    assert wrong_namespace != content_types
    _assert_code(
        "CONTRACT_MISMATCH",
        _rewrite(_base(), replacements={"[Content_Types].xml": wrong_namespace}),
    )


def test_root_office_document_and_workbook_sheet_targets_must_resolve() -> None:
    with ZipFile(BytesIO(_base()), "r") as archive:
        root_relationships = archive.read("_rels/.rels")
        workbook_relationships = archive.read("xl/_rels/workbook.xml.rels")
        workbook = archive.read("xl/workbook.xml")

    broken_root = root_relationships.replace(
        b'Target="xl/workbook.xml"', b'Target="xl/missing.xml"'
    )
    assert broken_root != root_relationships
    _assert_code(
        "CONTRACT_MISMATCH",
        _rewrite(_base(), replacements={"_rels/.rels": broken_root}),
    )

    broken_target = workbook_relationships.replace(b"sheet1.xml", b"missing.xml")
    assert broken_target != workbook_relationships
    _assert_code(
        "CONTRACT_MISMATCH",
        _rewrite(
            _base(),
            replacements={"xl/_rels/workbook.xml.rels": broken_target},
        ),
    )

    broken_id = workbook.replace(b'r:id="rId1"', b'r:id="missing"')
    assert broken_id != workbook
    _assert_code(
        "CONTRACT_MISMATCH",
        _rewrite(_base(), replacements={"xl/workbook.xml": broken_id}),
    )


@pytest.mark.parametrize(
    "xml",
    [
        b'<!DOCTYPE x SYSTEM "file:///private/client"><x/>',
        b'<!DOCTYPE x [<!ENTITY secret "expanded">]><x>&secret;</x>',
    ],
    ids=("external-dtd", "entity"),
)
def test_dtd_and_entities_are_rejected_in_any_xml_member(xml: bytes) -> None:
    unsafe = _rewrite(
        _base(),
        additions=[("custom/unsafe.xml", xml, ZIP_DEFLATED)],
    )
    error = _assert_code("CONTRACT_MISMATCH", unsafe)
    assert "private/client" not in str(error)


@pytest.mark.parametrize("name", ["../escape.bin", "/absolute.bin", "bad\\name.bin"])
def test_unsafe_member_paths_remain_rejected(name: str) -> None:
    unsafe = _rewrite(
        _base(),
        additions=[(name, b"unsafe", ZIP_STORED)],
    )
    _assert_code("CONTRACT_MISMATCH", unsafe)


def test_duplicate_encrypted_and_unsupported_compression_members_remain_rejected() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        duplicate = _rewrite(
            _base(),
            additions=[("[Content_Types].xml", b"duplicate", ZIP_STORED)],
        )
    _assert_code("CONTRACT_MISMATCH", duplicate)

    encrypted = bytearray(_base())
    central = encrypted.find(b"PK\x01\x02")
    assert central >= 0
    flags = struct.unpack_from("<H", encrypted, central + 8)[0]
    struct.pack_into("<H", encrypted, central + 8, flags | 0x1)
    _assert_code("CONTRACT_MISMATCH", bytes(encrypted))

    unsupported = _rewrite(
        _base(),
        additions=[("custom/unsupported.bin", b"payload", ZIP_BZIP2)],
    )
    _assert_code("CONTRACT_MISMATCH", unsupported)


def test_archive_limits_are_inclusive_and_each_overflow_is_a_limit_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _rewrite(
        _base(),
        additions=[("custom/compressible.bin", b"A" * 4_096, ZIP_DEFLATED)],
    )
    with ZipFile(BytesIO(data), "r") as archive:
        files = [member for member in archive.infolist() if not member.is_dir()]
        member_count = len(archive.infolist())
        largest = max(member.file_size for member in files)
        total = sum(member.file_size for member in files)
        ratio = max(
            (member.file_size + member.compress_size - 1) // member.compress_size
            for member in files
            if member.file_size and member.compress_size
        )

    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_MEMBERS", member_count)
    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_MEMBER_BYTES", largest)
    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_UNCOMPRESSED_BYTES", total)
    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_COMPRESSION_RATIO", ratio)
    validate_xlsx_archive(data)

    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_MEMBERS", member_count - 1)
    _assert_code("LIMIT_EXCEEDED", data)
    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_MEMBERS", member_count)

    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_MEMBER_BYTES", largest - 1)
    _assert_code("LIMIT_EXCEEDED", data)
    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_MEMBER_BYTES", largest)

    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_UNCOMPRESSED_BYTES", total - 1)
    _assert_code("LIMIT_EXCEEDED", data)
    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_UNCOMPRESSED_BYTES", total)

    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_COMPRESSION_RATIO", ratio - 1)
    _assert_code("LIMIT_EXCEEDED", data)


def test_inspection_wrapper_preserves_its_existing_error_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(WorkbookInspectionError) as malformed:
        _validate_xlsx_archive(b"not a zip")
    assert malformed.value.code == "SOURCE_CONTRACT_MISMATCH"

    data = _base()
    with ZipFile(BytesIO(data), "r") as archive:
        member_count = len(archive.infolist())
    monkeypatch.setattr(xlsx_safety, "MAX_XLSX_MEMBERS", member_count - 1)
    with pytest.raises(WorkbookInspectionError) as limited:
        _validate_xlsx_archive(data)
    assert limited.value.code == "SOURCE_LIMIT_EXCEEDED"


def test_public_boundary_rejects_non_bytes_and_exposes_only_fixed_codes() -> None:
    for invalid in (None, bytearray(_base()), memoryview(_base())):
        with pytest.raises(XlsxSafetyError) as caught:
            validate_xlsx_archive(invalid)  # type: ignore[arg-type]
        assert caught.value.code == "CONTRACT_MISMATCH"
    assert xlsx_safety.MAX_XLSX_MEMBER_BYTES == 64 * 1024 * 1024
    assert xlsx_safety.MAX_XLSX_UNCOMPRESSED_BYTES == 256 * 1024 * 1024
    assert xlsx_safety.MAX_XLSX_COMPRESSION_RATIO == 200
