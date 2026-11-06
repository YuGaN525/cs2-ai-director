"""Deterministic rule replay with simulated POV feedback; never imports input adapters."""
import argparse
import hashlib
import json
from .legacy import parse_snapshot, score_candidates, choose_slot, _candidate_features
from .config import DEFAULT_CONFIG


def replay(events, config=None):
    cfg = dict(DEFAULT_CONFIG, **(config or {}))
    current = last_slot = last_at = None
    previous = -1.0
    trace = []
    for event in events:
        now = float(event['at'])
        if now < previous or now < 0:
            raise ValueError('回放时间必须单调递增')
        previous = now
        snap = parse_snapshot(event['gsi'], now)
        if snap is None:
            raise ValueError('无效快照')
        if current is None:
            current = snap.observer_slot
        snap.observer_slot = current
        candidates = score_candidates(snap, cfg, None, 0.0)
        decision = choose_slot(candidates, current, now, last_at, last_slot, cfg)
        if not snap.is_spectating or snap.phase != 'live':
            decision.slot = None
            decision.reason = '非live观战状态'
        if decision.slot is not None:
            current = last_slot = decision.slot
            last_at = now
        trace.append({'at': now, 'action': 'switch' if decision.slot is not None else 'hold',
                      'slot': decision.slot, 'observed_slot': current, 'reason': decision.reason,
                      'candidates': [{'slot': c.slot, 'score': c.score, 'features': _candidate_features(c)}
                                     for c in candidates]})
    serial = json.dumps(trace, sort_keys=True, allow_nan=False, separators=(',', ':'))
    return {'core_version': '0.1.0', 'scope': 'legacy-rule-only', 'trace': trace,
            'trace_sha256': hashlib.sha256(serial.encode()).hexdigest(),
            'switches': sum(row['action'] == 'switch' for row in trace)}


def main():
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = replay(json.loads(args.input.read_text(encoding='utf-8')))
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
