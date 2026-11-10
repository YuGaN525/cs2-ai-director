"""Offline contract reference only; never imported by runtime upload/worker code."""
import csv
import hashlib
import io
import json
import math
from decimal import Decimal, DecimalException
from pathlib import Path
import re
import zlib

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'director-training-dataset-v1'
PART_BYTES = 262144
FEATURES = ['aim_self','aim_enemy','win','enemy_low_hp','self_low_hp','proximity','enemy_damaged','hp_advantage']
BASE = ['timestamp','map','round','action','target_slot','current_slot','rule_score','candidate_count','rule_gap','is_ambiguous','group','group_size','distance','target_hp','enemy_hp',*FEATURES,'label','label_damage','label_kill']
EXTRA = ['match_source_id','source_tick','label_horizon_ticks','sampling_weight','missing_fields','parser_version','sampling_method','sample_step_ticks','tickrate_assumption','label_definition']
LABEL = 'damage_or_kill_in_(tick,tick+horizon]'
MISSING = 'armor_helmet_flash_defuse_bomb_ammo_approx'
LIMITS = dict(maxManifestBytes=1048576,maxInitializeBytes=2097152,maxSources=256,maxFiles=1024,maxRows=200000,maxCompressedBytes=134217728,maxExpandedBytes=536870912,maxFileBytes=8388608,maxExpandedFileBytes=33554432,maxFileRows=50000,partBytes=PART_BYTES,maxClientConcurrency=2,maxActiveSessionsPerAccount=2,maxReservedBytesPerAccount=268435456,sessionLifetimeSeconds=604800,expiredRetentionSeconds=604800)
HEX = r'[0-9a-f]{64}'
UUID = r'(?!00000000-0000-0000-0000-000000000000$)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
NUMBER = r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'


class Invalid(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise Invalid(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def fields(obj, names):
    require(type(obj) is dict and set(obj) == set(names.split()), 'FIELDS')


def integer(v, low=0, high=9007199254740991):
    require(type(v) is int and low <= v <= high, 'INTEGER')


def pattern(v, regex):
    require(type(v) is str and re.fullmatch(regex,v) is not None, 'STRING')


def limited(value, depth=0):
    require(depth <= 16, 'JSON_DEPTH')
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        integer(value)
    elif type(value) is str:
        require(all(32 <= ord(c) <= 126 for c in value), 'ASCII')
    elif type(value) is list:
        for v in value: limited(v,depth+1)
    elif type(value) is dict:
        for k,v in value.items(): limited(k,depth+1); limited(v,depth+1)
    else:
        raise Invalid('JSON_TYPE')


def canonical(value):
    limited(value)
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode('ascii')


def loads(raw):
    def pairs(items):
        out={}
        for k,v in items:
            require(k not in out,'DUPLICATE_KEY');out[k]=v
        return out
    def integral(s):
        require(re.fullmatch(r'0|[1-9][0-9]*',s) is not None,'INTEGER_TOKEN')
        v=int(s);integer(v);return v
    def forbidden(_):raise Invalid('INTEGER_TOKEN')
    require(not raw.startswith(b'\xef\xbb\xbf'),'BOM')
    # Limit lexical nesting before recursive parsing. Braces in strings are ignored.
    depth=0;quoted=False;escape=False
    for c in raw:
        if quoted:
            if escape:escape=False
            elif c==92:escape=True
            elif c==34:quoted=False
        elif c==34:quoted=True
        elif c in (91,123):
            depth+=1;require(depth<=16,'JSON_DEPTH')
        elif c in (93,125):depth-=1
    try:
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_int=integral,parse_float=forbidden,parse_constant=forbidden)
    except (UnicodeError,json.JSONDecodeError,RecursionError) as error:
        raise Invalid('JSON') from error
    limited(value);return value


def csv_bytes(rows, columns):
    s=io.StringIO(newline='');w=csv.DictWriter(s,fieldnames=columns,lineterminator='\n')
    w.writeheader();w.writerows(rows);return s.getvalue().encode('ascii')


def decimal_text(value):
    text=format(value,'f')
    if '.' in text:text=text.rstrip('0').rstrip('.')
    return '0' if text in ('-0','') else text


def number(text, required=False):
    if text=='':
        require(not required,'CSV_NUMBER');return None
    require(re.fullmatch(NUMBER,text) is not None,'CSV_NUMBER')
    try:
        value=Decimal(text)
        require(value.is_finite() and value.copy_abs()<=Decimal('1e12'),'CSV_NUMBER')
        return value
    except DecimalException as error:raise Invalid('CSV_NUMBER') from error


def int_cell(text,low,high,required=False):
    if text=='':require(not required,'CSV_INTEGER');return None
    require(re.fullmatch(r'0|[1-9][0-9]*',text) is not None,'CSV_INTEGER')
    value=int(text);require(low<=value<=high,'CSV_INTEGER');return value


def validate_source(source):
    fields(source,'sourceId kind rowSchema rawDemoSha256 recordingId declaredMatchGroup identityQuality parserVersion sampling rows positiveRows')
    pattern(source['sourceId'],UUID);pattern(source['recordingId'],HEX)
    if source['declaredMatchGroup'] is not None:pattern(source['declaredMatchGroup'],HEX)
    integer(source['rows'],1,LIMITS['maxRows']);integer(source['positiveRows'],0,source['rows'])
    if source['kind']=='demo-derived':
        require(source['rowSchema']=='demo-live36-v1','SOURCE_VERSION')
        pattern(source['rawDemoSha256'],HEX)
        require(source['recordingId']==source['rawDemoSha256'],'SOURCE_IDENTITY')
        require(source['identityQuality'] in ('recording-only','filename-inferred','user-declared'),'SOURCE_IDENTITY')
        require(source['parserVersion']=='demo-v2-live.1','SOURCE_VERSION')
        p=source['sampling'];fields(p,'method stepTicks horizonTicks tickrateAssumption labelDefinition missingFields')
        require(p['method']=='periodic-live' and p['tickrateAssumption']==64 and p['labelDefinition']==LABEL and p['missingFields']==MISSING,'SOURCE_SAMPLING')
        integer(p['stepTicks'],1,1000000);integer(p['horizonTicks'],1,1000000)
    else:
        require(source['kind'] in ('legacy-offline','legacy-selected-only') and source['rowSchema']=='legacy26-v1','SOURCE_VERSION')
        require(source['rawDemoSha256'] is None and source['parserVersion'] is None and source['sampling'] is None and source['identityQuality']=='unknown','SOURCE_IDENTITY')


def validate_init(raw):
    require(len(raw)<=LIMITS['maxInitializeBytes'],'INITIALIZE_SIZE')
    data=loads(raw);fields(data,'schema clientDatasetId runId manifestSha256 manifest')
    require(data['schema']==SCHEMA,'SCHEMA');pattern(data['clientDatasetId'],UUID);pattern(data['runId'],UUID);pattern(data['manifestSha256'],HEX)
    require(len(canonical(data))<=LIMITS['maxInitializeBytes'],'INITIALIZE_SIZE')
    m=data['manifest'];fields(m,'format version producerVersion featureVersion sources files totals')
    require(m['format']==SCHEMA and type(m['version']) is int and m['version']==1 and m['featureVersion']=='pov-v1','SCHEMA')
    pattern(m['producerVersion'],r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)')
    encoded=canonical(m);require(len(encoded)<=LIMITS['maxManifestBytes'],'MANIFEST_SIZE')
    require(digest(encoded)==data['manifestSha256'],'MANIFEST_HASH')
    require(type(m['sources']) is list and 1<=len(m['sources'])<=LIMITS['maxSources'],'SOURCE_COUNT')
    require(type(m['files']) is list and 1<=len(m['files'])<=LIMITS['maxFiles'],'FILE_COUNT')
    sources={}
    for s in m['sources']:
        validate_source(s);require(s['sourceId'] not in sources,'SOURCE_DUPLICATE');sources[s['sourceId']]=s
    t=m['totals'];fields(t,'sources files rows positiveRows compressedBytes expandedBytes')
    for k,limit in [('sources','maxSources'),('files','maxFiles'),('rows','maxRows'),('compressedBytes','maxCompressedBytes'),('expandedBytes','maxExpandedBytes')]:integer(t[k],1,LIMITS[limit])
    integer(t['positiveRows'],0,t['rows'])
    require(t['sources']==len(sources) and t['files']==len(m['files']),'TOTALS')
    per={sid:[0,0,0] for sid in sources};tot=[0,0,0,0];seen=set()
    for index,f in enumerate(m['files']):
        fields(f,'index sourceId sha256 contentSha256 bytes expandedBytes rows positiveRows partCount')
        integer(f['index'],index,index);pattern(f['sourceId'],UUID);require(f['sourceId'] in sources,'FILE_SOURCE')
        pattern(f['sha256'],HEX);pattern(f['contentSha256'],HEX)
        integer(f['bytes'],1,LIMITS['maxFileBytes']);integer(f['expandedBytes'],1,LIMITS['maxExpandedFileBytes']);integer(f['rows'],1,LIMITS['maxFileRows']);integer(f['positiveRows'],0,f['rows'])
        integer(f['partCount'],1,32);require(f['partCount']==(f['bytes']+PART_BYTES-1)//PART_BYTES,'PART_COUNT')
        key=(f['sourceId'],f['sha256']);require(key not in seen,'FILE_DUPLICATE');seen.add(key)
        per[f['sourceId']][0]+=f['rows'];per[f['sourceId']][1]+=f['positiveRows'];per[f['sourceId']][2]+=1
        tot=[a+f[k] for a,k in zip(tot,['rows','positiveRows','bytes','expandedBytes'])]
    require(tot==[t[k] for k in ('rows','positiveRows','compressedBytes','expandedBytes')],'TOTALS')
    for sid,(count,pos,files_count) in per.items():require(files_count>0 and count==sources[sid]['rows'] and pos==sources[sid]['positiveRows'],'SOURCE_TOTALS')
    return data


def unpack(raw, info):
    require(len(raw)==info['bytes'],'FILE_LENGTH');require(digest(raw)==info['sha256'],'FILE_HASH')
    try:
        decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
        expanded=decoder.decompress(raw,LIMITS['maxExpandedFileBytes']+1)
        require(len(expanded)<=LIMITS['maxExpandedFileBytes'],'EXPANDED_LIMIT')
        require(decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,'GZIP')
    except zlib.error as error:raise Invalid('GZIP') from error
    require(len(expanded)<=LIMITS['maxExpandedFileBytes'],'EXPANDED_LIMIT')
    require(len(expanded)==info['expandedBytes'],'EXPANDED_LENGTH');require(digest(expanded)==info['contentSha256'],'CONTENT_HASH')
    return expanded


def validate_csv(raw, source, info, groups):
    columns=BASE+EXTRA if source['rowSchema']=='demo-live36-v1' else BASE
    require(raw.endswith(b'\n') and b'\r' not in raw and not raw.startswith(b'\xef\xbb\xbf'),'CSV_ENCODING')
    require(all(len(line)<=65536 for line in raw.splitlines(keepends=True)),'CSV_LINE')
    try:
        table=list(csv.reader(io.StringIO(raw.decode('ascii'),newline=''),strict=True))
    except (UnicodeError,csv.Error) as error:raise Invalid('CSV') from error
    require(table and table[0]==columns,'CSV_COLUMNS')
    require(1<=len(table)-1<=LIMITS['maxFileRows'],'CSV_ROWS')
    rows=[];positive=0
    for cells in table[1:]:
        require(len(cells)==len(columns) and all(len(x)<=2048 and all(32<=ord(c)<=126 for c in x) for x in cells),'CSV_SHAPE')
        row=dict(zip(columns,cells));rows.append(row);demo=source['kind']=='demo-derived'
        pattern(row['map'],r'[a-zA-Z0-9_-]{1,64}')
        require(row['action'] in (('demo',) if demo else ('hold','switch') if source['kind']=='legacy-selected-only' else ('demo','hold','switch')),'CSV_ACTION')
        pattern(row['group'],r'g_'+HEX)
        for k in FEATURES:number(row[k],True)
        for k in ('timestamp','rule_score','rule_gap','distance'):
            v=number(row[k]);require(v is None or k not in ('timestamp','distance') or v>=0,'CSV_NUMBER')
        int_cell(row['round'],0,1000000,demo)
        target=int_cell(row['target_slot'],1,10,demo);int_cell(row['current_slot'],1,10)
        if not demo:require(row['target_slot']=='' and row['current_slot']=='','LEGACY_SLOT_UNKNOWN')
        for k in ('target_hp','enemy_hp'):int_cell(row[k],0,100)
        for k in ('is_ambiguous','label_damage','label_kill'):int_cell(row[k],0,1)
        candidate_count=int_cell(row['candidate_count'],1,10)
        label=int_cell(row['label'],0,1,True);positive+=label
        size=int_cell(row['group_size'],1,10,True)
        if source['kind']=='legacy-selected-only':require(size==1,'GROUP_SIZE')
        if candidate_count is not None:require(candidate_count>=size,'GROUP_SIZE')
        if demo:
            p=source['sampling'];tick=int_cell(row['source_tick'],0,9007199254740991,True)
            require(row['timestamp']==decimal_text(Decimal(tick)/64),'CSV_TIME')
            expected=dict(match_source_id=source['recordingId'],label_horizon_ticks=str(p['horizonTicks']),sampling_weight='1',missing_fields=p['missingFields'],parser_version=source['parserVersion'],sampling_method=p['method'],sample_step_ticks=str(p['stepTicks']),tickrate_assumption='64',label_definition=p['labelDefinition'],label_damage=row['label'],label_kill='')
            require(all(row[k]==v for k,v in expected.items()),'CSV_METADATA')
        if demo:
            key=(source['sourceId'],row['group']);groups.setdefault(key,[]).append((size,target,row.get('source_tick')))
    require(len(rows)==info['rows'] and positive==info['positiveRows'],'CSV_COUNTS')
    require(csv_bytes(rows,columns)==raw,'CSV_CANONICAL')


def validate_package(raw, payloads):
    data=validate_init(raw);m=data['manifest'];sources={s['sourceId']:s for s in m['sources']};groups={}
    require(set(payloads)==set(range(len(m['files']))),'FILES_MISSING')
    for f in m['files']:validate_csv(unpack(payloads[f['index']],f),sources[f['sourceId']],f,groups)
    for key,entries in groups.items():
        require({v[0] for v in entries}=={len(entries)},'GROUP_SIZE')
        targets=[v[1] for v in entries if v[1] is not None]
        require(len(targets)==len(set(targets)),'GROUP_TARGET')
        require(len({v[2] for v in entries})==1,'GROUP_TICK')
    return dict(manifestSha256=data['manifestSha256'],requestHash=digest(canonical(data)),rows=m['totals']['rows'],initialGovernanceStatus='pending-review')
