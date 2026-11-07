"""Deterministic synthetic engine scenarios; never sends real keyboard/OBS input."""
import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

parser=argparse.ArgumentParser()
parser.add_argument('--repo',type=Path,required=True)
parser.add_argument('--out',type=Path,required=True)
parser.add_argument('--samples',type=int,default=10000)
args=parser.parse_args()
sys.path.insert(0,str(args.repo))
import cs2_ai_director as d
from test_cs2_ai_director import player,payload

cfg=dict(d.DEFAULT_CONFIG,ai_enabled=False,demo_model_enabled=False,
         decision_calibration_enabled=False,shot_timeline_enabled=False,
         hlae_spawn_ct_key='alt+c',hlae_spawn_t_key='alt+t',
         hlae_bomb_a_key='alt+a',hlae_bomb_b_key='alt+b',bomb_cam_policy='always')
clock=[100.]
d.time=SimpleNamespace(monotonic=lambda:clock[0],sleep=lambda _:None)
engine=d.DirectorEngine(cfg,Mock())
engine.input=Mock()
actions=[]
observed=[9]
focused=[True]
def press(slot):
    actions.append([round(clock[0],3),'pov',slot,focused[0]])
    if focused[0]: observed[0]=slot
    return focused[0]
def hotkey(key,double=False):
    actions.append([round(clock[0],3),'hlae',key,double,focused[0]])
    return focused[0]
engine.input.press_slot.side_effect=press
engine.input.send_hotkey.side_effect=hotkey
times=[]; trace=[]; reasons=Counter(); fixture=hashlib.sha256()
cpu_start=time.process_time()
for i in range(args.samples):
    clock[0]=100+i*.05
    cycle=i%400
    focused[0]=not 200<=cycle<230
    players=[player(1,'CT',hp=0 if 150<=cycle<180 else 100,pos=(0,0,0),round_totaldmg=(cycle%100)//5),
             player(2,'T',hp=40,pos=(400,0,0),fwd=(-1,0,0)),
             player(9,'CT',pos=(5000,5000,0))]
    if cycle==0: observed[0]=9
    if cycle==240: engine._on_manual_key('2')
    bomb={'state':'planted','countdown':'6','position':'-600,-2200,0'} if 300<=cycle<340 else None
    data=payload(players,phase='freezetime' if cycle<12 else 'live',round_no=i//400+1,
                 player_slot=observed[0],bomb=bomb)
    fixture.update(json.dumps(data,sort_keys=True).encode())
    snap=d.parse_snapshot(data,clock[0]-(10 if 360<=cycle<370 else 0))
    engine.store.update(snap)
    started=time.perf_counter_ns()
    engine._tick()
    times.append((time.perf_counter_ns()-started)/1e6)
    st=engine.status()
    reasons[st.get('reason','')]+=1
    trace.append([st.get('target'),st.get('reason'),st.get('score'),st.get('gsi')])
ordered=sorted(times)
assert any(a[1]=='pov' for a in actions),'Fixture must exercise actual decision requests'
assert any(a[1]=='hlae' for a in actions),'Fixture must exercise HLAE requests'
result={'scope':'synthetic-engine-with-simulated-input-and-feedback',
        'samples':len(times),'p50_ms':statistics.median(times),
        'p95_ms':ordered[int(len(times)*.95)-1],'p99_ms':ordered[int(len(times)*.99)-1],
        'max_ms':max(times),'cpu_seconds':time.process_time()-cpu_start,
        'input_sha256':fixture.hexdigest(),'config_sha256':hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest(),
        'trace_sha256':hashlib.sha256(json.dumps(trace,sort_keys=True).encode()).hexdigest(),
        'actions_sha256':hashlib.sha256(json.dumps(actions,sort_keys=True).encode()).hexdigest(),
        'action_count':len(actions),'pov_requests':sum(a[1]=='pov' for a in actions),
        'hlae_requests':sum(a[1]=='hlae' for a in actions),
        'actions':actions,'reason_counts':dict(reasons),'python':sys.version,'platform':platform.platform(),
        'real_programme_latency_measured':False,'network_and_persistence_included':False}
args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in result.items() if k not in ('actions','reason_counts')},ensure_ascii=False,indent=2))
