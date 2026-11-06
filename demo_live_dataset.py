"""Demo v2: periodic live-round sampling, future labels and past-only features."""
import bisect,csv,hashlib
from pathlib import Path
import director_core as core
from train_from_demos import build_snapshot
from upload_protocol import COLUMNS


def live_windows(starts,ends,horizon=192,step=128,max_samples=12000):
    starts=sorted(set(int(x) for x in starts));ends=sorted(set(int(x) for x in ends));samples=[]
    for number,start in enumerate(starts):
        stop=next((e for e in ends if e>start),None)
        if stop is None or number+1<len(starts) and stop>=starts[number+1]:continue
        for tick in range(start+step,stop-horizon+1,step):samples.append((number+1,tick,stop))
    if len(samples)>max_samples:raise ValueError('demo_sample_budget')
    return samples


def build_live_csv(path,out,horizon=192,step=128):
    from demoparser2 import DemoParser
    parser=DemoParser(str(path));header=parser.parse_header();map_name=str(header.get('map_name') or 'unknown')
    starts=parser.parse_event('round_freeze_end');ends=parser.parse_event('round_end')
    if starts is None or ends is None:raise ValueError('demo_round_boundaries_missing')
    samples=live_windows(starts['tick'],ends['tick'],horizon,step)
    if not samples:raise ValueError('demo_no_complete_live_windows')
    hurts=parser.parse_event('player_hurt');deaths=parser.parse_event('player_death');attacks={};victims={}
    if hurts is None:raise ValueError('demo_damage_events_missing')
    for events in (hurts,deaths):
        if events is None:continue
        for r in events.to_dict('records'):
            tick=int(r['tick']);attacker=str(r.get('attacker_steamid') or '');victim=str(r.get('user_steamid') or '')
            if attacker and attacker not in ('0','nan'):attacks.setdefault(attacker,[]).append(tick)
            if victim and victim not in ('0','nan'):victims.setdefault(victim,[]).append(tick)
    for values in list(attacks.values())+list(victims.values()):values.sort()
    frame=parser.parse_ticks(['X','Y','Z','health','armor','team_num','is_alive','active_weapon_name','yaw','pitch','tick','steamid','name'],ticks=[x[1] for x in samples])
    by_tick={}
    for record in frame.to_dict('records'):by_tick.setdefault(int(record['tick']),[]).append(record)
    identities=sorted({str(r['steamid']) for records in by_tick.values() for r in records if int(r.get('team_num') or 0) in (2,3)})
    if len(identities)>10:raise ValueError('demo_ambiguous_player_mapping')
    slots={sid:i+1 for i,sid in enumerate(identities)}
    with Path(path).open('rb') as original:source=hashlib.file_digest(original,'sha256').hexdigest()
    rows=[];counts={0:0,1:0};missing=0
    for round_no,tick,stop in samples:
        records=by_tick.get(tick)
        if not records:missing+=1;continue
        # Old v1 builder used swapped numeric team enums. v2 explicitly maps CS2 2=T,3=CT.
        fixed=[dict(r,team_num=3 if int(r.get('team_num') or 0)==2 else 2 if int(r.get('team_num') or 0)==3 else 0) for r in records]
        snap=build_snapshot(fixed,map_name)
        if snap is None:missing+=1;continue
        snap.round_num=round_no
        for sid,p in snap.players.items():p.slot=slots[sid]
        def recent(sid):
            values=victims.get(str(sid),[]);i=bisect.bisect_left(values,tick-102)
            return i<len(values) and values[i]<=tick
        candidates=core.score_candidates(snap,core.DEFAULT_CONFIG,recent,0.)
        for c in candidates:
            values=attacks.get(str(c.steamid),[]);i=bisect.bisect_right(values,tick);label=int(i<len(values) and values[i]<=tick+horizon)
            row={key:'' for key in COLUMNS};row.update(timestamp=str(tick/64),map=map_name,round=str(round_no),action='demo',target_slot=str(c.slot),group=source+'#'+str(tick),group_size=str(len(candidates)),rule_score=str(c.score),label=str(label),label_damage=str(label))
            row.update({key:str(float(v)) for key,v in zip(core.DECISION_FEATURES,core._candidate_features(c))})
            row.update(match_source_id=source,source_tick=tick,label_horizon_ticks=horizon,sampling_weight='1',missing_fields='armor_helmet_flash_defuse_bomb_ammo_approx')
            rows.append(row);counts[label]+=1
    if not rows:raise ValueError('demo_no_samples')
    with Path(out).open('w',newline='',encoding='utf-8') as output:
        writer=csv.DictWriter(output,fieldnames=COLUMNS+['match_source_id','source_tick','label_horizon_ticks','sampling_weight','missing_fields']);writer.writeheader();writer.writerows(rows)
    return {'parser':'demo-v2-live','sample_windows':len(samples),'rows':len(rows),'positive':counts[1],'negative':counts[0],'missing_windows':missing,'horizon_ticks':horizon,'step_ticks':step,'tickrate_assumption':64,'limitations':'unavailable GSI fields explicitly marked; real Demo/GSI alignment requires same-match evidence'}
