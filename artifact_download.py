"""Read-only verification of a browser download from this run."""
from __future__ import annotations

import hashlib
import io
import os
import struct
import zipfile
import zlib


# A downloaded delivery ZIP may contain a presentation ZIP. Inspect those two
# containers only, without extracting files, and bound every decompressed byte.
ZIP_MAX_MEMBERS = 2048
ZIP_MAX_MEMBER_BYTES = 64 * 1024 * 1024
ZIP_MAX_TOTAL_BYTES = 128 * 1024 * 1024
ZIP_REPORTED_MEMBERS = 20
ZIP_REPORTED_NAME_CHARS = 300
_OLE_MAGIC = bytes.fromhex('D0CF11E0A1B11AE1')
_ZIP_MAGICS = (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')


class _InspectionError(ValueError):
    pass


def _zip_preflight(stream):
    """Bound directory entries before ZipFile allocates a ZipInfo per entry.

    Read at most the EOCD/comment tail and one fixed-size directory header at
    a time. Scan the real headers as well: a forged low EOCD count must not
    bypass the bound when ZipFile later parses the directory by byte length.
    """
    previous = stream.tell()
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        tail_start = max(0, size - (65535 + 22))
        stream.seek(tail_start)
        tail = stream.read(65535 + 22)
        offset = tail.rfind(b'PK\x05\x06')
        if offset < 0 or offset + 22 > len(tail):
            raise _InspectionError('ZIP end-of-central-directory record is missing or truncated')
        _, disk, directory_disk, disk_entries, entries, directory_bytes, directory_offset, comment_bytes = (
            struct.unpack_from('<4s4H2IH', tail, offset))
        if offset + 22 + comment_bytes != len(tail):
            raise _InspectionError('ZIP end-of-central-directory comment is inconsistent')
        if (disk_entries == 0xffff or entries == 0xffff
                or directory_bytes == 0xffffffff or directory_offset == 0xffffffff):
            raise _InspectionError('ZIP64 directory is unsupported by bounded inspection')
        if disk or directory_disk or disk_entries != entries:
            raise _InspectionError('multipart ZIP directory is unsupported by bounded inspection')
        if entries > ZIP_MAX_MEMBERS:
            raise _InspectionError('ZIP member count exceeds the inspection limit')
        directory_end = directory_offset + directory_bytes
        if directory_end != tail_start + offset:
            raise _InspectionError('ZIP central directory does not match its outer archive')
        cursor, count = directory_offset, 0
        while cursor < directory_end:
            stream.seek(cursor)
            header = stream.read(46)
            if len(header) != 46 or header[:4] != b'PK\x01\x02':
                raise _InspectionError('ZIP central-directory header is invalid or truncated')
            count += 1
            if count > ZIP_MAX_MEMBERS:
                raise _InspectionError('ZIP member count exceeds the inspection limit')
            name_bytes, extra_bytes, member_comment_bytes = struct.unpack_from('<3H', header, 28)
            cursor += 46 + name_bytes + extra_bytes + member_comment_bytes
            if cursor > directory_end:
                raise _InspectionError('ZIP central-directory member is truncated')
        if count != entries:
            raise _InspectionError('ZIP declared member count differs from its central directory')
    finally:
        stream.seek(previous)


def _archive_members(package, budget):
    members = package.infolist()
    if len(members) > ZIP_MAX_MEMBERS:
        raise _InspectionError('ZIP member count exceeds the inspection limit')
    if members and min(member.header_offset for member in members) != 0:
        # ZipFile accepts prepended bytes, and can mistake a nested PPTX's EOCD
        # for the outer directory when a delivery ZIP was truncated. Our inputs
        # start with a ZIP header, so that first header must belong to this ZIP.
        raise _InspectionError('ZIP central directory does not match its outer archive')
    total = 0
    for member in members:
        if member.flag_bits & 1:
            raise _InspectionError('encrypted ZIP member cannot be verified')
        if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            # Standard Office/delivery ZIPs use these methods. Other decoders
            # can allocate an unbounded dictionary before yielding a chunk.
            raise _InspectionError('ZIP compression method is unsupported by bounded inspection')
        if member.file_size < 0 or member.file_size > ZIP_MAX_MEMBER_BYTES:
            raise _InspectionError('ZIP member exceeds the decompressed-size limit')
        total += member.file_size
    if total > budget['remaining']:
        raise _InspectionError('ZIP contents exceed the total decompressed-byte budget')
    return members


def _read_member(package, member, budget, *, capture=False):
    if member.file_size > budget['remaining']:
        raise _InspectionError('ZIP contents exceed the total decompressed-byte budget')
    data = bytearray() if capture else None
    total = 0
    with package.open(member) as stream:
        while True:
            chunk = stream.read(min(1024 * 1024, budget['remaining'] + 1))
            if not chunk:
                break
            total += len(chunk)
            budget['remaining'] -= len(chunk)
            if total > ZIP_MAX_MEMBER_BYTES or budget['remaining'] < 0:
                raise _InspectionError('ZIP contents exceed the decompressed-byte budget')
            if capture:
                data.extend(chunk)
    # Reading to EOF also checks ZipExtFile's CRC. Never trust suffixes or the
    # declared central-directory size in place of the saved member bytes.
    if total != member.file_size:
        raise _InspectionError('ZIP member size differs from its declared size')
    return bytes(data) if capture else None


def _office_kind(names):
    for kind, member in (('pptx', 'ppt/presentation.xml'),
                         ('docx', 'word/document.xml'), ('xlsx', 'xl/workbook.xml')):
        if {'[Content_Types].xml', member} <= names:
            return kind
    return None


def _document_bytes(data, budget):
    if data.startswith(b'%PDF-'):
        return 'pdf'
    if data.startswith(_OLE_MAGIC):
        return 'ppt'  # Preserve the existing legacy-PPT signature policy.
    if not data.startswith(_ZIP_MAGICS):
        return None
    # This is the one permitted inner container. A ZIP holding another ZIP is
    # not recursively treated as a presentation delivery.
    stream = io.BytesIO(data)
    _zip_preflight(stream)
    with zipfile.ZipFile(stream) as package:
        members = _archive_members(package, budget)
        kind = _office_kind({member.filename for member in members})
        if kind != 'pptx':
            return None
        for member in members:
            _read_member(package, member, budget)
        return kind


def inspect_download_format(path):
    """Read saved bytes, including one bounded delivery ZIP, without extraction.

    Returned bundle metadata describes the outer ZIP honestly. A valid member
    proves only that format is present, never its business identity or relevance.
    """
    try:
        with open(path, 'rb') as stream:
            prefix = stream.read(8)
        if prefix.startswith(b'%PDF-'):
            return {'format': 'pdf'}
        if prefix == _OLE_MAGIC:
            return {'format': 'ole'}
        if not prefix.startswith(_ZIP_MAGICS):
            return {}
        empty = {'format': 'zip', 'container': 'zip', 'contained_formats': [],
                 'verified_members': [], 'verified_member_count': 0}
        try:
            # Also bound central-directory input before ZipFile materializes it.
            if os.path.getsize(path) > ZIP_MAX_TOTAL_BYTES:
                raise _InspectionError('ZIP file exceeds the inspection-size limit')
            budget = {'remaining': ZIP_MAX_TOTAL_BYTES}
            with open(path, 'rb') as stream:
                _zip_preflight(stream)
                with zipfile.ZipFile(stream) as package:
                    members = _archive_members(package, budget)
                    office = _office_kind({member.filename for member in members})
                    if office:
                        for member in members:
                            _read_member(package, member, budget)
                        return {'format': office}
                    verified, formats, count = [], set(), 0
                    for member in members:
                        candidate = (not member.is_dir() and
                                     member.filename.lower().endswith(('.pptx', '.ppt', '.pdf')))
                        data = _read_member(package, member, budget, capture=candidate)
                        if not candidate:
                            continue
                        try:
                            kind = _document_bytes(data, budget)
                        except (OSError, ValueError, RuntimeError, NotImplementedError,
                                zipfile.BadZipFile, zlib.error):
                            kind = None
                        if not kind:
                            continue
                        count += 1
                        formats.add(kind)
                        if len(verified) < ZIP_REPORTED_MEMBERS:
                            record = {'name': member.filename[:ZIP_REPORTED_NAME_CHARS],
                                      'format': kind, 'bytes': len(data)}
                            if len(member.filename) > ZIP_REPORTED_NAME_CHARS:
                                record['name_truncated'] = True
                            verified.append(record)
            return {**empty, 'contents_status': 'verified' if count else 'no_verified_documents',
                'contained_formats': sorted(formats), 'verified_members': verified,
                'verified_member_count': count, 'verified_members_omitted': count - len(verified)}
        except (OSError, ValueError, RuntimeError, NotImplementedError,
                zipfile.BadZipFile, zlib.error) as exc:
            return {**empty, 'contents_status': 'unavailable',
                    'contents_reason': str(exc)[:240] or 'ZIP contents could not be verified'}
    except (OSError, ValueError):
        return {}


def verified_download(artifacts, *, format="any", consumed=()):
    """Return a current, unchanged browser artifact or ``None``.

    A page screenshot or an existing file in Downloads is not download proof.
    BrowserSession supplies the event, inode, size and digest at completion.
    """
    used = set(consumed)
    for artifact in reversed(artifacts or []):
        if not isinstance(artifact, dict) or not artifact.get('completed'):
            continue
        ident = artifact.get('artifact_id') or artifact.get('download_id')
        if not ident or ident in used or artifact.get('transport') != 'browser':
            continue
        path = artifact.get('path')
        expected_hash = artifact.get('sha256')
        identity = artifact.get('file_identity')
        if not path or not expected_hash or not identity:
            continue
        try:
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            stat = os.stat(path, follow_symlinks=False)
            if (stat.st_size <= 0 or stat.st_size != artifact.get('bytes')
                    or [stat.st_dev, stat.st_ino] != list(identity)):
                continue
            digest = hashlib.sha256()
            with open(path, 'rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected_hash:
                continue
            if format in {'pdf', 'ppt'}:
                content = inspect_download_format(path)
                actual = {content.get('format'), *content.get('contained_formats', [])}
                expected = {'pdf'} if format == 'pdf' else {'ppt', 'pptx', 'ole'}
                if not actual.intersection(expected):
                    continue
            # Format/container inspection must not hide a concurrent change to
            # the actual outer receipt file.
            after = os.stat(path, follow_symlinks=False)
            signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if os.path.islink(path) or signature(after) != signature(stat):
                continue
            return artifact
        except (OSError, ValueError, zipfile.BadZipFile):
            continue
    return None


def _prefix(path, value):
    with open(path, 'rb') as stream:
        return stream.read(len(value)) == value
