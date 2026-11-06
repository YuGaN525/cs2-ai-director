import csv,gzip,hashlib,json,re,time,sys
from pathlib import Path
from demo_live_dataset import build_live_csv
from train_decision_model import train_from_csv

root=Path('build/task11-real');root.mkdir(parents=True,exist_ok=True);chosen={}
for path in sorted(Path('D:/demo').rglob('*.dem'),key=lambda p:p.stat().st_size):
    name=re.sub(r'_m\d+$','',path.stem);series=name.rsplit('_',1)[0]
    if path.stat().st_size<512*1024*1024:chosen.setdefault(series,path)
selected=list(chosen.items())[:12]
if len(selected)<8:raise ValueError('not_enough_independent_series')
report=[];output=root/'live-dataset.csv';fields=None;count=0;started=time.monotonic()
with output.open('w',encoding='utf-8',newline='') as stream:
    writer=None
    for index,(series,path) in enumerate(selected):
        print('PARSE',index+1,path.name,flush=True);target=root/('demo-'+str(index)+'.csv')
        stats=build_live_csv(path,target,step=128)
        with path.open('rb') as original:digest=hashlib.file_digest(original,'sha256').hexdigest()
        stats.update(source_name=path.name,source_sha256=digest,canonical_series_sha256=hashlib.sha256(series.encode()).hexdigest());report.append(stats)
        with target.open(encoding='utf-8-sig',newline='') as source:
            reader=csv.DictReader(source)
            if writer is None:writer=csv.DictWriter(stream,fieldnames=reader.fieldnames);writer.writeheader()
            for row in reader:row['match_source_id']=stats['canonical_series_sha256'];writer.writerow(row);count+=1
        print('ROWS',stats['rows'],'positive',stats['positive'],flush=True)
(root/'sources.json').write_text(json.dumps({'sources':report,'rows':count,'parse_seconds':time.monotonic()-started,'scope':'one recording per independently named match series; no maps from same series across partitions'},indent=2))
print('TRAIN',count,'rows,',len(selected),'independent series',flush=True)
metrics=train_from_csv(output,root/'real-candidate.pkl',root/'real-metrics.json',min_samples=100,trees=120,max_depth=8)
card={'dataset_rows':count,'independent_series':len(selected),'model_sha256':hashlib.sha256((root/'real-candidate.json.gz').read_bytes()).hexdigest(),'split_hash':metrics['split_hash'],'dataset_hash':metrics['dataset_hash'],'comparison':metrics['comparison'],'final_test':metrics.get('final_test'),'candidate_status':'local-only; not auto-published','programme_coverage':'not measured without same-match GSI/programme timeline; AUC is not camera coverage','gsi_alignment':'not measured','default_scheduler':'legacy','source_limitations':['missing GSI fields explicitly marked','64tick time assumption','filename series conservatively used for isolation; verify event IDs before production dataset publication']}
(root/'model-card.json').write_text(json.dumps(card,indent=2));print('PASS real Demo parsing and fixed-split model evaluation completed',flush=True)
