"""Prepare an existing checked trainer artifact and an explicitly supplied frozen QA split."""
import argparse,csv,gzip,hashlib,json,random,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from director_core.model_format import DECISION_FEATURES,validate_forest
from model_runtime import probability,FEATURE_HASH
from device_identity import canonical
p=argparse.ArgumentParser();p.add_argument('--model',type=Path,required=True);p.add_argument('--csv',type=Path,required=True);p.add_argument('--split',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
raw=a.model.read_bytes();forest=validate_forest(json.loads(gzip.decompress(raw)))
rows=list(csv.DictReader(a.csv.open(encoding='utf-8-sig',newline='')));split=json.loads(a.split.read_text())
test=[rows[i] for i in split['final_test']];ys=[int(r['label']) for r in test];pred=[int(probability(forest,[float(r[f]) for f in DECISION_FEATURES])>=.5) for r in test]
recalls=[sum(y==c and prediction==c for y,prediction in zip(ys,pred))/max(1,ys.count(c)) for c in (0,1)]
rng=random.Random(78);vectors=[[0.]*8]+[[float(i==j) for i in range(8)] for j in range(8)]+[[rng.random() for _ in range(8)] for _ in range(15)]
metadata={'name':'Initial RF baseline / fixed QA split','artifactSha256':hashlib.sha256(raw).hexdigest(),'evaluation':{'suiteVersion':'qa-fixed-split-v1','splitHash':split['split_hash'],'testRows':len(test),'positiveRows':sum(ys),'balancedAccuracy':sum(recalls)/2,'notes':'Existing unified-trainer RF baseline. Synthetic fixed grouped QA split only; no claim of competitive-match benefit. Production efficacy review remains separate.'},'vectors':[{'features':v,'probability':probability(forest,v)} for v in vectors]}
a.out.mkdir(parents=True,exist_ok=True);(a.out/'baseline.json.gz').write_bytes(raw);(a.out/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
(a.out/'feature-manifest.json').write_bytes(canonical({'feature_version':'pov-v1','features':DECISION_FEATURES}))
(a.out/'provenance.json').write_text(json.dumps({'model_sha256':hashlib.sha256(raw).hexdigest(),'feature_manifest_sha256':FEATURE_HASH,'dataset_file_sha256':hashlib.sha256(a.csv.read_bytes()).hexdigest(),'split_hash':split['split_hash'],'trees':len(forest['trees']),'test_rows':len(test),'balanced_accuracy':sum(recalls)/2,'effect_claim':'format/vector/QA baseline; real-match benefit unverified'},indent=2)+'\n')
print('Prepared baseline:',len(forest['trees']),'trees,',len(test),'frozen final-test rows,',len(vectors),'prediction vectors')
