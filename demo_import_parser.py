"""Credential-free import subprocess. Produces deterministic retrainable CSV parts."""
import argparse
import csv
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil

from demo_import_archive import ImportFailure, inspect_archive, extract_entry

PART_LIMIT = 8 * 1024 * 1024


def package_csv(path, root, max_bytes):
    """Each gzip member is an independently readable CSV with its own header."""
    parts, buffer, count, total = [], io.StringIO(newline=''), 0, 0
    with Path(path).open('r', encoding='utf-8-sig', newline='') as incoming:
        reader = csv.DictReader(incoming)
        columns = reader.fieldnames
        if not columns or len(columns) != len(set(columns)):
            raise ImportFailure('result_columns')
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        def flush():
            nonlocal buffer, writer, count, total
            if not count:
                return
            content = gzip.compress(buffer.getvalue().encode('utf-8'), compresslevel=6, mtime=0)
            if len(content) > PART_LIMIT:
                raise ImportFailure('result_part_limit')
            total += len(content)
            if total > max_bytes:
                raise ImportFailure('result_budget')
            digest = hashlib.sha256(content).hexdigest()
            target = root / digest
            if not target.exists():
                with target.open('xb') as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
            parts.append(dict(index=len(parts), sha256=digest, bytes=len(content), rows=count))
            buffer = io.StringIO(newline='')
            writer = csv.DictWriter(buffer, fieldnames=columns)
            writer.writeheader()
            count = 0
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ImportFailure('result_row_shape')
            writer.writerow(row)
            count += 1
            # At most ~2 MiB UTF-8 payload, far below compressed part limit.
            if buffer.tell() >= 512 * 1024:
                flush()
        flush()
    return parts


def analyze(folder, executable):
    folder = Path(folder).resolve(strict=True)
    spec = json.loads((folder / 'spec.json').read_text('utf-8'))
    original = folder / 'input.raw'
    limit = int(spec['maxMemoryMb']) * 1024 * 1024
    if os.name != 'nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CPU, (spec['maxSeconds'], spec['maxSeconds'] + 2))
        resource.setrlimit(resource.RLIMIT_FSIZE, (spec['maxExpandedBytes'], spec['maxExpandedBytes']))
    if original.stat().st_size != spec['bytes']:
        raise ImportFailure('input_length')
    with original.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != spec['sha256']:
            raise ImportFailure('input_hash')
    if shutil.disk_usage(folder).free < min(spec['maxExpandedBytes'], spec['bytes'] * 2) + 64 * 1024 * 1024:
        raise ImportFailure('scratch_capacity')
    results = folder / 'results'
    results.mkdir(exist_ok=True)
    entries = []
    if spec['kind'] == 'dem':
        from demo_import_archive import Entry
        plan = [Entry('input.dem', spec['bytes'], False)]
    else:
        plan = inspect_archive(original, spec['kind'], executable,
                               max_entries=spec['maxEntries'], max_expanded=spec['maxExpandedBytes'])
        retained = sum(e.size for e in plan if not e.directory and e.path.lower().endswith('.dem'))
        temporary = max((e.size for e in plan if not e.directory and not e.path.lower().endswith('.dem')), default=0)
        if shutil.disk_usage(folder).free < retained + temporary + spec['maxDerivedBytes'] + 64 * 1024 * 1024:
            raise ImportFailure('scratch_capacity')
        # Validate CRC and actual lengths for every entry before parsing ANY Demo.
        # No archive member is extracted under its supplied name.
        for index, item in enumerate(plan):
            if item.directory:
                continue
            target = folder / f'entry-{index}.raw'
            extract_entry(original, item, target, executable, spec['maxSeconds'])
            if not item.path.lower().endswith('.dem'):
                target.unlink()
    derived_bytes = 0
    for index, item in enumerate(plan):
        if item.directory:
            continue
        result = dict(path=item.path, state='ignored', rawHash=None, rawBytes=0,
                      parserVersion='', rows=0, positive=0, parts=[], report={}, errorCode='not_demo')
        if not item.path.lower().endswith('.dem'):
            entries.append(result)
            continue
        path = original if spec['kind'] == 'dem' else folder / f'entry-{index}.raw'
        csv_path = folder / f'entry-{index}.csv'
        try:
            if item.size < 16 or item.size >= 50_000_000_000:
                raise ImportFailure('demo_size')
            with path.open('rb') as stream:
                if stream.read(8) != b'PBDEMS2\0':
                    raise ImportFailure('demo_magic')
            from demo_live_dataset import build_live_csv
            report = build_live_csv(path, csv_path)
            parts = package_csv(csv_path, results, spec['maxDerivedBytes'] - derived_bytes)
            if sum(p['rows'] for p in parts) != report['rows']:
                raise ImportFailure('result_row_count')
            derived_bytes += sum(p['bytes'] for p in parts)
            result.update(state='succeeded', rawHash=report['source_sha256'], rawBytes=item.size,
                          parserVersion=report['parser'], rows=report['rows'], positive=report['positive'],
                          parts=parts, report=report, errorCode='')
        except Exception as error:
            code = str(error) if isinstance(error, ImportFailure) else 'demo_parse_failed'
            result.update(state='failed', errorCode=code)
        finally:
            if csv_path.exists():
                csv_path.unlink()
            if spec['kind'] != 'dem':
                path.unlink(missing_ok=True)
        entries.append(result)
    return dict(entries=entries, errorCode=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--seven-zip', type=Path, required=True)
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    if args.probe:
        # Invoked through the exact sandbox command before the transport claims
        # any work. Finding bwrap on PATH alone does not prove namespaces work.
        if not args.seven_zip.is_file() or not args.folder.is_dir():
            raise RuntimeError('import_sandbox_paths')
        print('director-import-sandbox-ready')
        return
    try:
        result = analyze(args.folder, args.seven_zip)
    except Exception as error:
        result = dict(entries=[], errorCode=str(error) if isinstance(error, ImportFailure) else 'analysis_failed')
    # Publish the full result atomically only after whole-archive security checks.
    target = args.folder / 'receipt.json'
    temp = target.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=True, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, target)


if __name__ == '__main__':
    main()
