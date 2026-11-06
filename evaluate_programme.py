"""Evaluate complete timed replay traces; classification AUC never stands in for coverage."""
import argparse,hashlib,json,statistics
from pathlib import Path


def evaluate(events,trace,latency=.2):
    if latency<0 or latency>5:raise ValueError('invalid_execution_latency')
    times=[float(x['at']) for x in trace]
    if times!=sorted(times) or len(set(times))!=len(times):raise ValueError('nonmonotonic_trace')
    previous=None;cuts=[];shots=[]
    for row in trace:
        if row.get('manual') and row.get('automatic_action'):raise ValueError('manual_takeover_bypassed')
        if row.get('action')=='switch':
            if previous is not None:shots.append(row['at']-previous)
            cuts.append(float(row['at']));previous=float(row['at'])
    captured=0;valid=0
    for event in events:
        if event.get('kind')!='kill' or not event.get('attacker_slot') or not event.get('identity_verified'):continue
        valid+=1;at=float(event['at']);active=None
        for row in trace:
            if float(row['at'])+latency>at:break
            if row.get('action')=='switch':active=row.get('slot')
            elif active is None:active=row.get('observed_slot')
        captured+=active==event['attacker_slot']
    duration=max(times[-1]-times[0],1) if times else 1
    return {'captured_kills':captured,'eligible_kills':valid,'kill_coverage':captured/valid if valid else None,'switches':len(cuts),
            'cuts_per_minute':len(cuts)*60/duration,'mean_pov_seconds':statistics.mean(shots) if shots else None,'latency_seconds':latency}


def compare(events,baseline,candidate,provenance,latency=.2):
    if not provenance.get('split_hash') or provenance.get('final_used_for_selection') is not False:raise ValueError('unfrozen_or_leaked_evaluation')
    a=evaluate(events,baseline,latency);b=evaluate(events,candidate,latency)
    if a['eligible_kills']!=b['eligible_kills']:raise ValueError('different_eligible_events')
    gain=None if a['kill_coverage'] is None else b['kill_coverage']-a['kill_coverage']
    meets=bool(provenance.get('real_match_data') and provenance.get('full_scheduler_replay') is True and provenance.get('same_match_gsi_aligned') is True and a['eligible_kills']>=30 and gain is not None and gain>=.02 and b['cuts_per_minute']<=a['cuts_per_minute']+1)
    return {'baseline':a,'candidate':b,'coverage_gain_pp':None if gain is None else gain*100,'quality_gate_met':meets,
            'default_strategy':'candidate' if meets else 'legacy','provenance':provenance,'content_sha256':hashlib.sha256(json.dumps([events,baseline,candidate],sort_keys=True,separators=(',',':')).encode()).hexdigest()}


def main():
    p=argparse.ArgumentParser();p.add_argument('input',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args();v=json.loads(a.input.read_text());r=compare(v['events'],v['baseline'],v['candidate'],v['provenance'],v.get('latency',.2));a.out.write_text(json.dumps(r,indent=2)+'\n')
if __name__=='__main__':main()
