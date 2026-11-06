"""Frozen compatibility CSV boundary, shared by director and trainer adapters."""
import csv
import gzip
import hashlib
import io
import json
import math
import re
import struct
import uuid
from datetime import datetime
from director_core.model_format import DECISION_FEATURES

API_VERSION = 'director-data-compat-v1'
MAX_UPLOAD = 8 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_ROWS = 50000
COLUMNS = ['timestamp', 'map', 'round', 'action', 'target_slot', 'current_slot', 'rule_score',
           'candidate_count', 'rule_gap', 'is_ambiguous', 'group', 'group_size', 'distance',
           'target_hp', 'enemy_hp', *DECISION_FEATURES, 'label', 'label_damage', 'label_kill']
INTEGERS = {'round','target_slot','current_slot','candidate_count','is_ambiguous','group_size',
            'target_hp','enemy_hp','label','label_damage','label_kill'}


class ProtocolError(ValueError): pass


def validate_row(row):
    result = {}
    for key in COLUMNS:
        value = str(row.get(key, '')).strip()
        if key == 'map':
            if len(value) > 64 or not re.fullmatch(r'[a-zA-Z0-9_\-/]*', value): value = ''
        elif key == 'action':
            if value not in ('switch','hold','demo'): value = ''
        elif key != 'group':
            if value:
                try: number = float(value)
                except (ValueError, OverflowError): raise ProtocolError('数值无效')
                if (not math.isfinite(number) or abs(number) > 1e12
                        or key in INTEGERS and number != math.trunc(number)
                        or key == 'label' and number not in (0,1)
                        or key == 'group_size' and number < 1):
                    raise ProtocolError('数值无效')
            elif key in DECISION_FEATURES or key == 'label': raise ProtocolError('特征或成熟标签缺失')
        result[key] = value
    return result


def parse_csv(data, compressed=False):
    if len(data) > MAX_UPLOAD: raise ProtocolError('上传文件超限')
    if compressed:
        try:
            if len(data)<18:raise ProtocolError('gzip损坏')
            size=struct.unpack('<I',data[-4:])[0]
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream: data = stream.read(MAX_EXPANDED+1)
            if len(data)!=size:raise ProtocolError('仅支持一个完整gzip成员')
        except (OSError, EOFError): raise ProtocolError('gzip损坏')
    if len(data) > MAX_EXPANDED: raise ProtocolError('解压数据超限')
    try:
        reader = csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True)
        header = next(reader)
        if len(header) > 100 or len(set(header)) != len(header) or not set(DECISION_FEATURES+['label']) <= set(header):
            raise ProtocolError('表头重复或缺少特征/label')
        rows = []
        for fields in reader:
            if not fields: continue
            if len(fields) != len(header) or any(len(v) > 2048 for v in fields) or len(rows) >= MAX_ROWS:
                raise ProtocolError('列数/字段长度/行数无效')
            rows.append(validate_row(dict(zip(header, fields))))
        if not rows: raise ProtocolError('没有成熟样本')
        return rows
    except (UnicodeError, csv.Error, StopIteration): raise ProtocolError('CSV损坏')


def encode_csv(rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator='\n', extrasaction='ignore')
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode('utf-8')


def sanitize_row(row, source_id):
    value = validate_row(row)
    if value['group']:
        value['group'] = 'g_' + hashlib.sha256((source_id+':'+value['group']).encode()).hexdigest()
    return value  # names, IDs, free text and paths are never included


def accept_candidate_preview(value):
    try:
        uuid.UUID(value['session_id']);uuid.UUID(value['source_id'])
        return (type(value['telemetry_schema']) is int and value['telemetry_schema'] == 2
                and value['feature_version'] == 'pov-v1' and value['feature_names'] == DECISION_FEATURES
                and type(value['sample_seq']) is int and 0 <= value['sample_seq'] <= 9223372036854775807
                and len(value['features']) == 8 and all(type(v) in (int,float) and math.isfinite(v) for v in value['features'])
                and value['label_state'] in ('pending','mature','censored'))
    except (KeyError, ValueError, TypeError): return False


def verify_receipt(receipt, batch):
    if 'clientBatchId' in receipt:
        try:
            uuid.UUID(receipt['uploadId']);uuid.UUID(receipt['clientBatchId'])
            description=json.loads(batch['descriptor'])
            expected=hashlib.sha256(json.dumps(description,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
            if (receipt['clientBatchId']!=batch['id'] or receipt['requestHash']!=expected or receipt['uploadHash']!=batch['upload_hash']
                    or receipt['source']!=batch['source'] or receipt['rowCount']!=batch['row_count']
                    or type(receipt['acceptedRows']) is not int or type(receipt['duplicateRows']) is not int
                    or receipt['acceptedRows']<0 or receipt['duplicateRows']<0 or receipt['acceptedRows']+receipt['duplicateRows']!=batch['row_count']
                    or receipt['quality']!=('legacy-selected-only' if batch['source']=='online' else 'legacy-offline')
                    or not receipt['createdAt'].endswith('Z')):raise ProtocolError('范围回执不匹配')
            if receipt['dataBatchId'] is not None:uuid.UUID(receipt['dataBatchId'])
            return receipt
        except (KeyError,ValueError,TypeError):raise ProtocolError('正式上传回执无效')
    try:
        uuid.UUID(receipt['id'])
        if not receipt['createdAt'].endswith('Z'):raise ProtocolError('回执时间必须为UTC')
        datetime.fromisoformat(receipt['createdAt'])
        expected_quality = 'legacy-selected-only' if batch['source'] == 'online' else 'legacy-offline'
        if (receipt['uploadHash'] != batch['upload_hash'] or receipt['source'] != batch['source']
                or receipt['channel'] not in ('automatic','manual')
                or receipt['quality'] != expected_quality
                or type(receipt['rowCount']) is not int or receipt['rowCount'] != batch['row_count']
                or type(receipt['positiveCount']) is not int or receipt['positiveCount'] != batch['positive_count']
                or type(receipt['duplicate']) is not bool
                or not re.fullmatch('[a-f0-9]{64}', receipt['contentHash'])):
            raise ProtocolError('回执与本批数据不匹配')
    except (KeyError, ValueError, TypeError): raise ProtocolError('回执缺失或格式不符')
    return receipt
