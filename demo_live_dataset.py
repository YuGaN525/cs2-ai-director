"""Demo v2: periodic live-round sampling, future labels and past-only features."""
import bisect,csv,hashlib
from pathlib import Path
import director_core as core
from train_from_demos import build_snapshot
from upload_protocol import COLUMNS

PARSER_VERSION = 'demo-v2-live.1'
SAMPLING_METHOD = 'periodic-live'
DEFAULT_HORIZON = 192
DEFAULT_STEP = 128
LIVE_COLUMNS = COLUMNS + ['match_source_id', 'source_tick', 'label_horizon_ticks',
                          'sampling_weight', 'missing_fields', 'parser_version',
                          'sampling_method', 'sample_step_ticks', 'tickrate_assumption',
                          'label_definition']


def validate_sampling(horizon, step):
    if any(type(value) is not int or value <= 0 for value in (horizon, step)):
        raise ValueError('demo_sampling_ticks_must_be_positive_integers')


def live_windows(starts,ends,horizon=DEFAULT_HORIZON,step=DEFAULT_STEP,max_samples=12000):
    validate_sampling(horizon, step)
    if type(max_samples) is not int or max_samples <= 0:raise ValueError('demo_sample_budget')
    starts=sorted(set(int(x) for x in starts));ends=sorted(set(int(x) for x in ends));samples=[]
    for number,start in enumerate(starts):
        stop=next((e for e in ends if e>start),None)
        if stop is None or number+1<len(starts) and stop>=starts[number+1]:continue
        for tick in range(start+step,stop-horizon+1,step):
            if len(samples)>=max_samples:raise ValueError('demo_sample_budget')
            samples.append((number+1,tick,stop))
    return samples


def build_live_csv(path,out,horizon=DEFAULT_HORIZON,step=DEFAULT_STEP):
    validate_sampling(horizon, step)
    try:
        return _build_live_csv(path, out, horizon, step)
    except BaseException as error:
        # PyO3 parser panics derive directly from BaseException. Convert only
        # those failures so the GUI reports an error and resets its busy state.
        if type(error).__module__ == 'pyo3_runtime' and type(error).__name__ == 'PanicException':
            raise ValueError('demo_parser_panic: invalid or unsupported Demo') from error
        raise


def _build_live_csv(path,out,horizon,step):
    from demoparser2 import DemoParser
    parser=DemoParser(str(path));header=parser.parse_header();map_name=str(header.get('map_name') or 'unknown')
    starts=parser.parse_event('round_freeze_end');ends=parser.parse_event('round_end')
    if starts is None or ends is None:raise ValueError('demo_round_boundaries_missing')
    samples=live_windows(starts['tick'],ends['tick'],horizon,step)
    round_starts=sorted(set(int(tick) for tick in starts['tick']))
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
            values=victims.get(str(sid),[]);i=bisect.bisect_left(values,max(tick-102,round_starts[round_no-1]))
            return i<len(values) and values[i]<=tick
        candidates=core.score_candidates(snap,core.DEFAULT_CONFIG,recent,0.)
        for c in candidates:
            values=attacks.get(str(c.steamid),[]);i=bisect.bisect_right(values,tick);label=int(i<len(values) and values[i]<=tick+horizon)
            row={key:'' for key in COLUMNS};row.update(timestamp=str(tick/64),map=map_name,round=str(round_no),action='demo',target_slot=str(c.slot),group=source+'#'+str(tick),group_size=str(len(candidates)),rule_score=str(c.score),label=str(label),label_damage=str(label))
            row.update({key:str(float(v)) for key,v in zip(core.DECISION_FEATURES,core._candidate_features(c))})
            row.update(match_source_id=source,source_tick=tick,label_horizon_ticks=horizon,sampling_weight='1',missing_fields='armor_helmet_flash_defuse_bomb_ammo_approx')
            row.update(parser_version=PARSER_VERSION,sampling_method=SAMPLING_METHOD,
                       sample_step_ticks=step,tickrate_assumption=64,
                       label_definition='damage_or_kill_in_(tick,tick+horizon]')
            rows.append(row);counts[label]+=1
    if not rows:raise ValueError('demo_no_samples')
    with Path(out).open('w',newline='',encoding='utf-8') as output:
        writer=csv.DictWriter(output,fieldnames=LIVE_COLUMNS);writer.writeheader();writer.writerows(rows)
    return {'parser':PARSER_VERSION,'sampling_method':SAMPLING_METHOD,'source_sha256':source,'sample_windows':len(samples),'rows':len(rows),'positive':counts[1],'negative':counts[0],'missing_windows':missing,'horizon_ticks':horizon,'step_ticks':step,'tickrate_assumption':64,'label_definition':'damage_or_kill_in_(tick,tick+horizon]','features':list(core.DECISION_FEATURES),'missing_fields':'armor_helmet_flash_defuse_bomb_ammo_approx','limitations':'unavailable GSI fields explicitly marked; real Demo/GSI alignment requires same-match evidence'}
