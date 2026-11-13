"""Read-only governance for ordinary capture; keeps the existing device entitlement."""
import json
import time
import urllib.request
from contextlib import closing

import dataset_protocol as p
from upload_worker import UploadFailure

SCHEMA='director-upload-governance-v1'
LABELS={'pending-review':'已接收，待管理员筛选','included':'已纳入，等待管理员主动训练','partially-included':'部分已纳入','excluded':'管理员未纳入'}


def validate(value,batch_id):
    p.fields(value,'schema clientBatchId transportStatus governanceStatus sources')
    p.require(value['schema']==SCHEMA and value['clientBatchId']==batch_id,'GOVERNANCE_MISMATCH')
    p.require(type(value['transportStatus']) is str and len(value['transportStatus'])<=32,'GOVERNANCE_MISMATCH')
    p.require(value['governanceStatus'] in LABELS,'GOVERNANCE_MISMATCH')
    p.require(type(value['sources']) is list and 0<len(value['sources'])<=500,'GOVERNANCE_MISMATCH')
    seen=set()
    for source in value['sources']:
        p.fields(source,'sourceId revision status blockers');p.pattern(source['sourceId'],p.UUID)
        p.require(source['sourceId'] not in seen,'GOVERNANCE_MISMATCH');seen.add(source['sourceId'])
        p.integer(source['revision'],0)
        p.require(source['status'] in ('pending-review','included','excluded'),'GOVERNANCE_MISMATCH')
        p.require(type(source['blockers']) is list and len(source['blockers'])<=2 and all(type(b) is str and b in ('SOURCE_UNVERIFIED','READER_UNAVAILABLE') for b in source['blockers']),'GOVERNANCE_MISMATCH')
    return value


class CaptureGovernance:
    def __init__(self,outbox,client):
        self.outbox,self.client=outbox,client;self.next_at=0
        with outbox.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS capture_governance(batch_id TEXT PRIMARY KEY,value TEXT NOT NULL,checked REAL NOT NULL)')

    def refresh(self,identity):
        now=time.monotonic()
        if now<self.next_at:return
        self.next_at=now+60
        binding=identity.binding
        with closing(self.outbox.connect()) as db:
            row=db.execute("SELECT b.id FROM batches b LEFT JOIN capture_governance g ON g.batch_id=b.id WHERE b.account=? AND b.origin=? AND b.device=? AND b.state='confirmed' AND b.descriptor IS NOT NULL ORDER BY COALESCE(g.checked,0),b.created DESC LIMIT 1",self.outbox._who(binding)).fetchone()
        if row is None:return
        base=binding.origin+'/api/director/data/uploads'
        def get(suffix):
            self.client._check_identity(identity)
            value=self.client._request(urllib.request.Request(base+suffix,headers={'Authorization':'Bearer '+identity.access_token}))
            self.client._check_identity(identity)
            return value
        try:
            caps=get('/capabilities')
            if caps!={'governance':True,'governanceSchema':SCHEMA,'authentication':'device-entitlement'}:return
            value=validate(get('/'+row['id']+'/governance'),row['id'])
            with self.outbox.transaction() as db:
                old=db.execute('SELECT value FROM capture_governance WHERE batch_id=?',(row['id'],)).fetchone()
                if old:
                    previous={s['sourceId']:s for s in json.loads(old[0])['sources']}
                    current={s['sourceId']:s for s in value['sources']}
                    p.require(previous.keys()==current.keys(),'GOVERNANCE_MISMATCH')
                    for key,source in current.items():
                        p.require(source['revision']>=previous[key]['revision'],'GOVERNANCE_ROLLBACK')
                        p.require(source['revision']!=previous[key]['revision'] or source==previous[key],'GOVERNANCE_CHANGED')
                db.execute('INSERT INTO capture_governance VALUES(?,?,?) ON CONFLICT(batch_id) DO UPDATE SET value=excluded.value,checked=excluded.checked',(row['id'],json.dumps(value,sort_keys=True),time.time()))
        except UploadFailure as error:
            from account_security import notify_account
            notify_account(getattr(self.client.identity_provider,'__self__',None),error,identity)
        except (ValueError,TypeError,KeyError):pass  # Old servers and network failures never alter receipts.

    def summary(self,binding):
        with closing(self.outbox.connect()) as db:
            rows=db.execute('SELECT g.value FROM capture_governance g JOIN batches b ON g.batch_id=b.id WHERE b.account=? AND b.origin=? AND b.device=? ORDER BY g.checked DESC LIMIT 100',self.outbox._who(binding)).fetchall()
        counts={key:0 for key in LABELS};blocked=0
        for row in rows:
            value=json.loads(row[0]);counts[value['governanceStatus']]+=1
            blocked+=any(source['blockers'] for source in value['sources'])
        lines=['普通采集仅含实际选中视角，保持原采集权限；接收不代表纳入训练。']
        if not rows:lines.append('暂无筛选状态，联网后自动查询；旧服务器不支持时仍保留上传回执。')
        else:
            lines.extend(f'{LABELS[key]}：{count} 批' for key,count in counts.items() if count)
            if blocked:lines.append(f'{blocked} 批需要管理员补核来源或读取器兼容。')
        return '\n'.join(lines)
