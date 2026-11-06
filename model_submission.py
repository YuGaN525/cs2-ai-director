"""Export candidate evaluation metadata only after the final winner is selected."""
import gzip,hashlib,json
from pathlib import Path
from director_core.model_format import validate_forest,forest_probability
from device_identity import atomic_write


def write_submission(model_path,features,labels,split_hash):
    model_path=Path(model_path);raw=model_path.read_bytes()
    if len(raw)>20*1024*1024:raise ValueError('模型超过正式发布的压缩大小限制')
    forest=validate_forest(json.loads(gzip.decompress(raw)))
    xs=[[float(value) for value in row] for row in features];ys=[int(value) for value in labels]
    if not xs or len(xs)!=len(ys) or set(ys)!={0,1}:raise ValueError('最终测试集需包含两个类别')
    predictions=[int(forest_probability(forest,x)>=.5) for x in xs]
    score=sum(sum(y==c and p==c for y,p in zip(ys,predictions))/ys.count(c) for c in (0,1))/2
    vectors=xs[:16]+[[0.]*8]+[[float(i==j) for i in range(8)] for j in range(8)]
    payload={'name':'Local RF candidate','artifactSha256':hashlib.sha256(raw).hexdigest(),
             'evaluation':{'suiteVersion':'frozen-final-split-v1','splitHash':split_hash,'testRows':len(xs),'positiveRows':sum(ys),
                           'balancedAccuracy':score,'notes':'Frozen final split evaluated after winner selection. Candidate only; administrator review/publication required. No live-match benefit claim.'},
             'vectors':[{'features':x,'probability':forest_probability(forest,x)} for x in vectors]}
    output=model_path.with_suffix('').with_suffix('.submission.json')
    atomic_write(output,json.dumps(payload,ensure_ascii=True,indent=2,allow_nan=False).encode());return output
