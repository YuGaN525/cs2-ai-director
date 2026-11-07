"""Bounded archive inspection and stdout extraction; never trust archive paths.

Run inside the import parser sandbox. This module is not an OS sandbox. The
caller supplies a pinned 7-Zip executable and applies process/disk budgets.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import threading
import unicodedata


class ImportFailure(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    directory: bool


def safe_name(value):
    value = unicodedata.normalize('NFC', value)
    if (not value or len(value) > 512 or any(ord(c) < 32 for c in value)
            or any(c in value for c in '\\:\x7f') or value.startswith('/')
            or len(value.split('/')) > 16):
        raise ImportFailure('archive_path')
    for part in value.split('/'):
        if (part in ('', '.', '..') or part.endswith((' ', '.'))
                or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part, re.I)):
            raise ImportFailure('archive_path')
    return value


def bounded_run(command, output, limit, seconds):
    """Consume stdout incrementally; kill a hung child even while read blocks."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    expired = threading.Event()
    def timeout():
        expired.set()
        try:
            process.kill()
        except OSError:
            pass
    timer = threading.Timer(seconds, timeout)
    timer.daemon = True
    timer.start()
    total = 0
    try:
        while True:
            block = process.stdout.read(65536)
            if not block:
                break
            total += len(block)
            if total > limit:
                raise ImportFailure('archive_output_limit')
            output.write(block)
        code = process.wait()
        if expired.is_set():
            raise ImportFailure('archive_timeout')
        if code != 0:
            raise ImportFailure('archive_invalid_or_encrypted')
        return total
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def blocks(text):
    # A hostile newline/duplicate metadata field cannot be silently overwritten.
    result, current = [], {}
    for line in text.replace('\r\n', '\n').split('\n'):
        if not line:
            if current:
                result.append(current)
                current = {}
            continue
        if ' = ' not in line:
            continue  # tool header and separators only
        key, value = line.split(' = ', 1)
        if key in current:
            raise ImportFailure('archive_ambiguous_metadata')
        current[key] = value
    if current:
        result.append(current)
    return result


def inspect_archive(archive, kind, executable, *, max_entries, max_expanded, seconds=60):
    import io
    archive = Path(archive).resolve(strict=True)
    if kind not in ('zip', 'rar', '7z'):
        raise ImportFailure('archive_format')
    magic = archive.open('rb')
    with magic:
        header = magic.read(8)
    valid = {'zip': header[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x06\x06'),
             'rar': header.startswith(b'Rar!\x1a\x07'),
             '7z': header.startswith(b'7z\xbc\xaf\x27\x1c')}
    if not valid[kind]:
        raise ImportFailure('archive_magic')
    listing = io.BytesIO()
    bounded_run([str(executable), 'l', '-slt', '-sccUTF-8', '-p', '--', str(archive)],
                listing, min(8 * 1024 * 1024, max_entries * 4096 + 65536), seconds)
    try:
        text = listing.getvalue().decode('utf-8', errors='strict')
    except UnicodeError as error:
        raise ImportFailure('archive_encoding') from error
    if '\n----------\n' not in text.replace('\r\n', '\n'):
        raise ImportFailure('archive_listing')
    before, after = text.replace('\r\n', '\n').split('\n----------\n', 1)
    summary = {k: v for record in blocks(before) for k, v in record.items()}
    if summary.get('Type', '').lower() not in {'zip': ('zip',), 'rar': ('rar', 'rar5'), '7z': ('7z',)}[kind]:
        raise ImportFailure('archive_format')
    if (summary.get('Volumes', '1') != '1' or summary.get('Multivolume', '-') != '-'
            or summary.get('Volume', '-') != '-' or summary.get('Offset', '0') != '0'):
        raise ImportFailure('archive_split_or_embedded')
    records = blocks(after)
    if not records or len(records) > max_entries:
        raise ImportFailure('archive_entry_limit')
    entries, names, expanded = [], set(), 0
    for item in records:
        supplied_path = item.get('Path', '')
        # Windows 7-Zip renders ordinary archive separators as backslashes.
        if os.name == 'nt':
            supplied_path = supplied_path.replace('\\', '/')
        path = safe_name(supplied_path.rstrip('/'))
        if path.casefold() in names:
            raise ImportFailure('archive_duplicate_path')
        names.add(path.casefold())
        if (item.get('Encrypted', '-') != '-' or item.get('Split Before', '-') != '-'
                or item.get('Split After', '-') != '-' or item.get('Symbolic Link')
                or item.get('Hard Link') or item.get('Alternate Stream', '-') != '-'):
            raise ImportFailure('archive_encrypted_split_or_link')
        attributes = item.get('Attributes', '')
        mode = item.get('Mode', '')
        if 'l' in attributes or mode.startswith(('l', 'b', 'c', 'p', 's')):
            raise ImportFailure('archive_link_or_special')
        directory = item.get('Folder') == '+' or attributes.startswith('D') or mode.startswith('d')
        try:
            size = int(item.get('Size', '0'))
        except ValueError as error:
            raise ImportFailure('archive_size') from error
        if size < 0 or directory and size != 0:
            raise ImportFailure('archive_size')
        expanded += size
        if expanded > max_expanded or expanded > max(archive.stat().st_size * 1000, 64 * 1024 * 1024):
            raise ImportFailure('archive_expansion_limit')
        entries.append(Entry(path, size, directory))
    # A file cannot double as another entry's parent directory.
    files = {entry.path.casefold() for entry in entries if not entry.directory}
    for entry in entries:
        parts = entry.path.casefold().split('/')
        if any('/'.join(parts[:i]) in files for i in range(1, len(parts))):
            raise ImportFailure('archive_path_collision')
    return entries


def extract_entry(archive, entry, target, executable, seconds):
    if entry.directory:
        raise ImportFailure('archive_directory')
    # No filename from the archive is ever used as an output path. -spd avoids
    # wildcard expansion; the final -- separates literal source/member names.
    with Path(target).open('xb') as output:
        actual = bounded_run([str(executable), 'x', '-so', '-spd', '-p', '-sccUTF-8', '--',
                              str(Path(archive).resolve()), entry.path], output, entry.size, seconds)
        output.flush()
        os.fsync(output.fileno())
    if actual != entry.size:
        raise ImportFailure('archive_length')
    with Path(target).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()
