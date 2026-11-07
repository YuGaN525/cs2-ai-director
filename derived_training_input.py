"""Versioned derived input reader: no raw Demo needed after successful ingestion."""
import csv
import gzip
import hashlib
import json
from pathlib import Path
import re


def manifest(path, expected_hash):
    path = Path(path)
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('derived_manifest_limit')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError('derived_manifest_hash')
    data = json.loads(raw)
    if (data.get('format') != 'director-derived-v1' or data.get('version') != 1
            or data.get('parserVersion') != 'demo-v2-live.1' or data.get('featureVersion') != 'pov-v1'
            or not re.fullmatch('[a-f0-9]{64}', data.get('rawHash', ''))
            or not isinstance(data.get('parts'), list) or not 0 < len(data['parts']) <= 20000):
        raise ValueError('derived_manifest_version')
    count, total = 0, 0
    for index, part in enumerate(data['parts']):
        if (part.get('index') != index or not re.fullmatch('[a-f0-9]{64}', part.get('sha256', ''))
                or type(part.get('bytes')) is not int or not 0 < part['bytes'] <= 8 * 1024 * 1024
                or type(part.get('rows')) is not int or part['rows'] < 1):
            raise ValueError('derived_part_manifest')
        count += part['rows']
        total += part['bytes']
    if count != data.get('rows') or total > 10_000_000_000:
        raise ValueError('derived_total')
    return data


def rows(path, item, columns):
    path = Path(path)
    data = manifest(path, item['artifactHash'])
    total = 0
    for part in data['parts']:
        target = path.parent / (item['id'] + '-' + part['sha256'] + '.csv.gz')
        if target.stat().st_size != part['bytes']:
            raise ValueError('derived_part_length')
        with target.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != part['sha256']:
                raise ValueError('derived_part_hash')
        count = 0
        expanded = 0
        with gzip.open(target, 'rt', encoding='utf-8-sig', newline='') as stream:
            def bounded_lines():
                nonlocal expanded
                while True:
                    line = stream.readline(65537)
                    if not line:
                        return
                    expanded += len(line.encode('utf-8'))
                    if len(line) > 65536 or expanded > 32 * 1024 * 1024:
                        raise ValueError('derived_csv_expansion_limit')
                    yield line
            reader = csv.DictReader(bounded_lines())
            if not reader.fieldnames or not set(columns).issubset(reader.fieldnames):
                raise ValueError('derived_columns')
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError('derived_csv_shape')
                count += 1
                total += 1
                if count > part['rows'] or total > data['rows']:
                    raise ValueError('derived_row_count')
                yield row
        if count != part['rows']:
            raise ValueError('derived_row_count')
    if total != data['rows']:
        raise ValueError('derived_row_count')
