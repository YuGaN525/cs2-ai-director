"""Explicit historical derived sample export; original files stay untouched."""
import csv
import json
from pathlib import Path
import tempfile
import dataset_protocol as p
from dataset_export import export_csv

def export_derived(folder,destination,run_id,first_source=False):
    from derived_training_input import rows,manifest
    folder=Path(folder).resolve();spec=json.loads(p.read_bounded(folder/'spec.json',4*1024**2))
    items=spec['snapshot']['manifest']['items']
    if first_source:items=items[:1]
    p.require(1<=len(items)<=p.LIMITS['maxSources'],'SOURCE_COUNT')
    declared={};count=0
    with tempfile.TemporaryDirectory(prefix='dataset-derived-') as work:
        csv_path=Path(work)/'derived.csv'
        with csv_path.open('w',encoding='utf-8',newline='') as output:
            writer=csv.DictWriter(output,fieldnames=p.BASE+p.EXTRA,lineterminator='\n');writer.writeheader()
            for item in items:
                p.pattern(item['id'],p.UUID);p.pattern(item['artifactHash'],p.HEX);p.pattern(item['matchHash'],p.HEX)
                p.require(item['kind']=='derived','SOURCE_VERSION')
                path=folder/(item['id']+'.csv.gz');m=manifest(path,item['artifactHash'])
                declared[m['rawHash']]=item['matchHash']
                for row in rows(path,item,p.BASE+p.EXTRA):
                    writer.writerow({k:row[k] for k in p.BASE+p.EXTRA});count+=1
                    p.require(count<=p.LIMITS['maxRows'],'ROW_LIMIT')
        return export_csv(csv_path,destination,run_id,declared_groups=declared,producer='1.16.0')
