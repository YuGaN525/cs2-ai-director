"""Real-duration local load observation with synthetic game/input/OBS only.

Never connects to a real account or sends OS input. This is NOT CS2/HLAE/OBS
programme-output acceptance. A stop file permits a clean, recorded cancellation.
"""
import argparse
import hashlib
import http.client
import json
import os
import sys
import threading
import time
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace

parser=argparse.ArgumentParser()
parser.add_argument('--repo',type=Path,required=True)
parser.add_argument('--out',type=Path,required=True)
parser.add_argument('--load-seconds',type=float,default=14400)
parser.add_argument('--observe-seconds',type=float,default=86400)
args=parser.parse_args()
args.out.mkdir(parents=True,exist_ok=True)
os.environ['APPDATA']=str(args.out/'isolated-appdata')
sys.path.insert(0,str(args.repo))
import psutil
import cs2_ai_director as d
from director_story import ShotTimeline
from training_data_runtime import TrainingDataRuntime
from upload_protocol import DECISION_FEATURES
from test_cs2_ai_director import player,payload

class NoInput:
    def __init__(self):self.slot=9;self.pov=0;self.hlae=0
    def press_slot(self,slot):self.pov+=1;self.slot=slot;time.sleep(.03);return True
    def send_hotkey(self,key,double=False):self.hlae+=1;time.sleep(.49 if double else .41);return True
    def release_held_keys(self):pass
    def is_self_press(self,*args):return False

class NoWatcher:
    mode='synthetic-no-input'
    def __init__(self,*args):pass
    def start(self):pass
    def stop(self):pass

class QuietLog:
    def __init__(self):self.errors=0;self.messages=0
    def info(self,*args):self.messages+=1
    def warning(self,*args):self.messages+=1
    def exception(self,*args):self.errors+=1

log=QuietLog()
d.ManualKeyWatcher=NoWatcher
cfg=dict(d.DEFAULT_CONFIG,gsi_port=0,ai_enabled=False,demo_model_enabled=False,
         decision_calibration_enabled=False,hlae_spawn_ct_key='alt+c',hlae_spawn_t_key='alt+t',
         hlae_bomb_a_key='alt+a',hlae_bomb_b_key='alt+b',bomb_cam_policy='always')
timeline=ShotTimeline(cfg,args.out/'timeline',log.info,async_writes=True)
runtime=TrainingDataRuntime(root=args.out/'training-data')
engine=d.DirectorEngine(cfg,log,timeline=timeline)
engine.input=NoInput()
started=datetime.now(timezone.utc).isoformat()
source_hashes={str(p.relative_to(args.repo)):hashlib.sha256(p.read_bytes()).hexdigest()
               for p in list(args.repo.glob('*.py'))+list((args.repo/'director_adapters').glob('*.py'))+list((args.repo/'director_core').glob('*.py'))}
proc=psutil.Process()
start=time.monotonic();last=start;active=0.;next_report=0.;samples=0;gaps=[];cancelled=False
phase='load';errors=[];connection=None
def report(final=False):
    memory=proc.memory_info();io=proc.io_counters()
    result={'started_at':started,'updated_at':datetime.now(timezone.utc).isoformat(),
        'scope':'synthetic-local-GSI-and-persistence-no-real-game-no-real-input-no-OBS',
        'phase':phase,'active_seconds':active,'wall_seconds':time.monotonic()-start,
        'load_seconds_required':args.load_seconds,'observation_seconds_required':args.observe_seconds,
        'samples':samples,'rss_bytes':memory.rss,'threads':proc.num_threads(),
        'handles':proc.num_handles(),'cpu_seconds':sum(proc.cpu_times()[:2]),
        'io_read_bytes':io.read_bytes,'io_write_bytes':io.write_bytes,
        'pov_requests':engine.input.pov,'hlae_requests':engine.input.hlae,
        'timeline':timeline.persistence_status(),'recorder':runtime.recorder.stats(),
        'loop_errors':log.errors,'errors':errors,'suspension_gaps':gaps[-100:],
        'cancelled':cancelled,'final':final,'source_hashes':source_hashes,
        'real_cs2_mulnx_obs_accepted':False}
    temp=args.out/'status.tmp'
    temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    temp.replace(args.out/'status.json')
    compact={k:v for k,v in result.items() if k not in ('source_hashes','suspension_gaps')}
    with (args.out/'metrics.jsonl').open('a',encoding='utf-8') as stream:
        stream.write(json.dumps(compact,ensure_ascii=False)+'\n')

try:
    engine.start()
    port=engine.server._httpd.server_address[1]
    connection=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    report()
    while active<args.load_seconds+args.observe_seconds:
        if (args.out/'STOP').exists():cancelled=True;break
        now=time.monotonic();delta=now-last;last=now
        if delta>5:gaps.append({'at':now-start,'seconds':delta})
        active+=min(delta,.5)
        phase='load' if active<args.load_seconds else 'observation'
        cycle=samples%400
        if cycle==0:engine.input.slot=9
        body=payload([player(1,'CT',hp=0 if 150<=cycle<180 else 100,pos=(0,0,0)),
                      player(2,'T',hp=40,pos=(400,0,0),fwd=(-1,0,0)),
                      player(9,'CT',pos=(5000,5000,0))],phase='freezetime' if cycle<12 else 'live',
                     round_no=samples//400+1,player_slot=engine.input.slot,
                     bomb={'state':'planted','countdown':'6','position':'-600,-2200,0'} if 300<=cycle<340 else None)
        connection.request('POST','/',body=json.dumps(body),headers={'Content-Type':'application/json'})
        response=connection.getresponse();response.read()
        if response.status!=200:raise RuntimeError('GSI response '+str(response.status))
        if samples%10==0:
            runtime.recorder.offer({**dict.fromkeys(DECISION_FEATURES,'0.5'),'label':'1','action':'hold','map':'de_mirage'})
        samples+=1
        if active>=next_report:
            report();next_report=active+60
        time.sleep(max(0,.1-(time.monotonic()-now)))
except Exception as error:
    errors.append(type(error).__name__+': '+str(error))
finally:
    if connection:connection.close()
    try:engine.stop()
    except Exception as error:errors.append('stop: '+str(error))
    if not timeline.close(time.monotonic()):errors.append('timeline_not_flushed')
    if not runtime.close():errors.append('recorder_not_flushed')
    phase='cancelled' if cancelled else ('failed' if errors or log.errors else 'complete')
    report(final=True)
sys.exit(1 if errors or log.errors else 0)
