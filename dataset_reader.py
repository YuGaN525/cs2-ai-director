"""Lease-scoped training-dataset-v1 reader. Selected source only; server owns BO identity."""
from pathlib import Path
import dataset_protocol as p

READER_VERSION='training-dataset-v1'


def manifest(path,item):
    p.pattern(item['id'],p.UUID);p.pattern(item['sourceId'],p.UUID);p.pattern(item['matchHash'],p.HEX)
    p.require(item.get('readerVersion')==READER_VERSION,'READER_VERSION')
    raw=p.read_bounded(path,p.LIMITS['maxManifestBytes'])
    p.require(p.digest(raw)==item['artifactHash'],'DATASET_MANIFEST_HASH')
    value=p.loads(raw);p.require(p.canonical(value)==raw,'DATASET_CANONICAL')
    # Reuse the full frozen manifest validator; wire request IDs are not training identities.
    p.validate_init(p.canonical(dict(schema=p.SCHEMA,clientDatasetId='00000000-0000-0000-0000-000000000001',runId='00000000-0000-0000-0000-000000000002',manifestSha256=item['artifactHash'],manifest=value)))
    selected=next((s for s in value['sources'] if s['sourceId']==item['sourceId']),None)
    p.require(selected is not None,'SOURCE_NOT_SELECTED')
    p.integer(item['rows'],selected['rows'],selected['rows'])
    return value,selected


def part_path(path,item,index):
    p.pattern(item['id'],p.UUID);p.integer(index,0,p.LIMITS['maxFiles']-1)
    return Path(path).parent/(item['id']+'-file-'+str(index)+'.csv.gz')


def rows(path,item):
    value,source=manifest(path,item);groups={};count=0
    for f in value['files']:
        if f['sourceId']!=source['sourceId']:continue
        raw=p.read_bounded(part_path(path,item,f['index']),p.LIMITS['maxFileBytes'])
        for row in p.validate_csv(p.unpack(raw,f),source,f,groups):
            count+=1
            # Keep recorded source identity for provenance, separately from server-verified training isolation.
            row['recording_source_id']=source['recordingId']
            yield row
    p.require(count==source['rows'],'DATASET_ROW_COUNT');p.validate_groups(groups)
