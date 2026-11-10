"""Immutable, bounded training sample export. No credentials or HTTP in this module."""
from contextlib import ExitStack
import csv
from decimal import Decimal, DecimalException
import gzip
import hashlib
import json
from pathlib import Path
import re
import tempfile
import uuid

import dataset_protocol as p
from client_version import VERSION
from device_identity import atomic_write


def sha_file(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def csv_rows(path):
    with Path(path).open('r',encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream,strict=True)
        columns=reader.fieldnames or []
        p.require(len(columns)==len(set(columns)) and set(p.FEATURES+['label'])<=set(columns),'CSV_COLUMNS')
        for row in reader:
            p.require(None not in row and all(v is not None and len(v)<=2048 for v in row.values()),'CSV_SHAPE')
            yield row


def inspect_csv(path):
    path=Path(path)
    p.require(path.is_file() and path.stat().st_size<=p.LIMITS['maxExpandedBytes'],'INPUT_LIMIT')
    raw_hash=sha_file(path);count=0;demo=None;minimum=None;positive=0;sources=set()
    for row in csv_rows(path):
        count+=1;p.require(count<=p.LIMITS['maxRows'],'ROW_LIMIT')
        for name in p.FEATURES:p.number(row[name],True)
        positive+=p.int_cell(row['label'],0,1,True)
        current=all(row.get(k) not in (None,'') for k in p.EXTRA)
        if demo is None:demo=current
        p.require(demo==current,'MIXED_ROW_SCHEMAS')
        if demo:
            p.pattern(row['match_source_id'],p.HEX);sources.add(row['match_source_id'])
        else:
            value=p.number(row.get('timestamp',''))
            if value is not None:minimum=value if minimum is None else min(minimum,value)
    p.require(count>0,'NO_SAMPLES')
    p.require(len(sources)<=p.LIMITS['maxSources'],'SOURCE_COUNT')
    return dict(sha256=raw_hash,rows=count,positiveRows=positive,kind='demo-derived' if demo else 'legacy',relativeOrigin=None if minimum is None else str(minimum),sources=len(sources) if demo else 1)


def export_csv(path, destination, run_id, *, declared_groups=None, expected_hash=None, producer=VERSION):
    """Export to a NEW directory. Original CSV is untouched; crash leaves an unqueued folder."""
    path=Path(path);destination=Path(destination);p.pattern(run_id,p.UUID)
    info=inspect_csv(path)
    if expected_hash is not None:p.require(info['sha256']==expected_hash,'INPUT_CHANGED')
    destination.mkdir(parents=True,exist_ok=False)
    declared_groups=declared_groups or {}
    legacy_id=p.digest(('legacy-recording-v1:'+info['sha256']).encode('ascii'))
    source_map={};row_count=0
    with tempfile.TemporaryDirectory(prefix='.source-export-',dir=destination) as scratch, ExitStack() as handles:
        writers={}
        for original in csv_rows(path):
            demo=info['kind']=='demo-derived';columns=p.BASE+(p.EXTRA if demo else [])
            row={k:str(original.get(k,'')).strip() for k in columns}
            recording=row['match_source_id'] if demo else legacy_id
            # Distinguish offline legacy candidates from online selected views, never infer complete v2 telemetry.
            kind='demo-derived' if demo else ('legacy-offline' if row['action']=='demo' else 'legacy-selected-only')
            source_key=(recording,kind)
            if source_key not in source_map:
                p.require(len(source_map)<p.LIMITS['maxSources'],'SOURCE_COUNT')
                sid=str(uuid.uuid5(uuid.UUID(run_id),recording+':'+kind));sampling=None
                if demo:
                    sampling=dict(method=row['sampling_method'],stepTicks=p.int_cell(row['sample_step_ticks'],1,1000000,True),horizonTicks=p.int_cell(row['label_horizon_ticks'],1,1000000,True),tickrateAssumption=p.int_cell(row['tickrate_assumption'],64,64,True),labelDefinition=row['label_definition'],missingFields=row['missing_fields'])
                declared=declared_groups.get(recording) if demo else None
                if declared is not None:p.pattern(declared,p.HEX)
                source_map[source_key]=dict(sourceId=sid,kind=kind,rowSchema='demo-live36-v1' if demo else 'legacy26-v1',rawDemoSha256=recording if demo else None,recordingId=recording,declaredMatchGroup=declared,identityQuality=('filename-inferred' if declared else 'recording-only') if demo else 'unknown',parserVersion=row.get('parser_version') if demo else None,sampling=sampling,rows=0,positiveRows=0)
                stream=handles.enter_context((Path(scratch)/(sid+'.csv')).open('w',encoding='ascii',newline=''))
                writer=csv.DictWriter(stream,fieldnames=columns,lineterminator='\n');writer.writeheader();writers[source_key]=writer
            source=source_map[source_key]
            p.require(bool(row['group']),'MISSING_GROUP')
            row['group']='g_'+p.digest(('candidate-group-v1:'+recording+':'+row['group']).encode('utf-8'))
            row['map']=row['map'] if re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',row['map']) else 'unknown'
            if demo:
                tick=p.int_cell(row['source_tick'],0,9007199254740991,True)
                row['timestamp']=p.decimal_text(Decimal(tick)/64)
            else:
                row['target_slot']=row['current_slot']=''
                origin=info['relativeOrigin'];value=p.number(row['timestamp'])
                row['timestamp']=p.decimal_text(value-Decimal(origin)) if value is not None and origin is not None else ''
            # Only declared columns are serialized: player names, IDs, reason and paths never leave the device.
            p.require(all(all(32<=ord(ch)<=126 for ch in v) for v in row.values()),'ASCII')
            writers[source_key].writerow(row)
            source['rows']+=1;source['positiveRows']+=p.int_cell(row['label'],0,1,True);row_count+=1
        handles.close()
        p.require(sha_file(path)==info['sha256'],'INPUT_CHANGED')
        files=[]
        for source in source_map.values():
            columns=p.BASE+(p.EXTRA if source['rowSchema']=='demo-live36-v1' else [])
            pending=[];estimated=0
            def flush():
                nonlocal pending,estimated
                if not pending:return
                p.require(len(files)<p.LIMITS['maxFiles'],'FILE_COUNT')
                raw=p.csv_bytes(pending,columns);compressed=gzip.compress(raw,compresslevel=6,mtime=0)
                index=len(files)
                p.require(len(raw)<=p.LIMITS['maxExpandedFileBytes'] and len(compressed)<=p.LIMITS['maxFileBytes'],'FILE_LIMIT')
                atomic_write(p.file_path(destination,index),compressed)
                files.append(dict(index=index,sourceId=source['sourceId'],sha256=p.digest(compressed),contentSha256=p.digest(raw),bytes=len(compressed),expandedBytes=len(raw),rows=len(pending),positiveRows=sum(r['label']=='1' for r in pending),partCount=(len(compressed)+p.PART_BYTES-1)//p.PART_BYTES))
                pending=[];estimated=0
            for row in csv_rows(Path(scratch)/(source['sourceId']+'.csv')):
                pending.append(row);estimated+=sum(len(v) for v in row.values())+len(columns)*3
                if len(pending)>=5000 or estimated>=2*1024*1024:flush()
            flush()
    sources=list(source_map.values())
    manifest=dict(format=p.SCHEMA,version=1,producerVersion=producer,featureVersion='pov-v1',sources=sources,files=files,totals=dict(sources=len(sources),files=len(files),rows=row_count,positiveRows=sum(s['positiveRows'] for s in sources),compressedBytes=sum(f['bytes'] for f in files),expandedBytes=sum(f['expandedBytes'] for f in files)))
    data=dict(schema=p.SCHEMA,clientDatasetId=str(uuid.uuid5(uuid.UUID(run_id),'training-data')),runId=run_id,manifestSha256=p.digest(p.canonical(manifest)),manifest=manifest)
    atomic_write(destination/'initialize.json',p.canonical(data))
    p.validate_directory(destination)
    return data
