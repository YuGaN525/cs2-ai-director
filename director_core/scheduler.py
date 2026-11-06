"""Pure constrained programme scheduler; legacy remains the formal default."""
from dataclasses import dataclass
from .legacy import choose_slot


@dataclass(frozen=True)
class ShotOption:
    kind:str
    target:int|None
    score:float
    expires_at:float
    event_priority:int=0


@dataclass(frozen=True)
class ShotDecision:
    kind:str
    target:int|None
    reason:str


def schedule(options,current,now,last_switch,cuts,*,manual=False,paused=False,dead=False,min_dwell=1.5,max_cuts=20,cost=.12):
    if manual:return ShotDecision('HOLD',None,'manual_takeover')
    if paused:return ShotDecision('HOLD',None,'pause')
    fresh=[o for o in options if o.kind in ('POV','HOLD','UTILITY','BOMB','SPAWN') and o.expires_at>now]
    if not fresh:return ShotDecision('HOLD',None,'no_fresh_candidate')
    if last_switch is not None and now-last_switch<.2:return ShotDecision('HOLD',None,'hard_input_floor')
    if not dead and last_switch is not None and now-last_switch<min_dwell:return ShotDecision('HOLD',None,'min_dwell')
    if not dead and sum(now-60<t<=now for t in cuts)>=max_cuts:return ShotDecision('HOLD',None,'max_cut_rate')
    choice=max(fresh,key=lambda o:(o.event_priority,o.score-(cost if o.target!=current else 0),o.kind,str(o.target)))
    if choice.target==current and choice.kind=='POV':return ShotDecision('HOLD',None,'already_observed')
    return ShotDecision(choice.kind,choice.target,'death_priority' if dead else 'event_score_minus_switch_cost')


def choose_compatible(candidates,current,now,last_at,last_slot,config):
    # No effect evidence yet: shadow/legacy both return the exact established action.
    return choose_slot(candidates,current,now,last_at,last_slot,config)
