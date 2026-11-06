import unittest,random
from director_core.scheduler import schedule,ShotOption,choose_compatible
from director_core import choose_slot,DEFAULT_CONFIG
from demo_live_dataset import live_windows
from evaluate_programme import compare

class SchedulerWorkerTests(unittest.TestCase):
 def test_periodic_sampling_never_uses_deaths_or_crosses_round_end(self):
  samples=live_windows([0,2000],[1800,4000],horizon=192,step=128)
  self.assertTrue(samples);self.assertTrue(all(tick+192<=end for _,tick,end in samples));self.assertEqual(samples[0][1],128)
 def test_missing_round_end_is_censored(self):self.assertEqual(live_windows([100],[]),[])
 def test_overlapping_round_restart_is_censored(self):self.assertTrue(all(round_no==2 for round_no,_,_ in live_windows([0,1000],[2000])))
 def test_compatibility_wrapper_has_identical_actions(self):
  for t in range(100):self.assertEqual(choose_slot([],None,t,None,None,DEFAULT_CONFIG),choose_compatible([],None,t,None,None,DEFAULT_CONFIG))
 def test_manual_pause_and_input_floor_take_priority(self):
  options=[ShotOption('POV',2,10,100)]
  self.assertEqual(schedule(options,1,20,10,[],manual=True).reason,'manual_takeover')
  self.assertEqual(schedule(options,1,20,10,[],paused=True).reason,'pause')
  self.assertEqual(schedule(options,1,20,19.9,[],dead=True).reason,'hard_input_floor')
 def test_death_can_override_dwell_and_cut_budget(self):self.assertEqual(schedule([ShotOption('POV',2,1,100)],1,20,19,[19]*25,dead=True).target,2)
 def test_stale_candidate_never_executes(self):self.assertEqual(schedule([ShotOption('BOMB',None,1,10)],1,20,0,[]).reason,'no_fresh_candidate')
 def test_cuts_and_dwell_constraints_are_explained(self):
  self.assertEqual(schedule([ShotOption('POV',2,1,100)],1,20,19,[]).reason,'min_dwell')
  self.assertEqual(schedule([ShotOption('POV',2,1,100)],1,20,0,[10]*20).reason,'max_cut_rate')
 def test_synthetic_gain_cannot_enable_new_default(self):
  events=[{'at':3,'kind':'kill','attacker_slot':2,'identity_verified':True}];a=[{'at':0,'action':'hold','observed_slot':1}];b=[{'at':0,'action':'switch','slot':2}]
  result=compare(events,a,b,{'split_hash':'test','final_used_for_selection':False,'real_match_data':False})
  self.assertEqual(result['coverage_gain_pp'],100);self.assertFalse(result['quality_gate_met']);self.assertEqual(result['default_strategy'],'legacy')
 def test_unverified_identity_does_not_count_as_capture(self):
  r=compare([{'at':3,'kind':'kill','attacker_slot':2,'identity_verified':False}],[{'at':0,'action':'hold','observed_slot':2}],[{'at':0,'action':'hold','observed_slot':2}],{'split_hash':'test','final_used_for_selection':False})
  self.assertEqual(r['baseline']['eligible_kills'],0);self.assertIsNone(r['coverage_gain_pp'])

 def test_real_flag_without_complete_gsi_replay_cannot_enable_default(self):
  events=[{'at':i+3,'kind':'kill','attacker_slot':2,'identity_verified':True} for i in range(31)]
  r=compare(events,[{'at':0,'action':'hold','observed_slot':1}],[{'at':0,'action':'switch','slot':2}],{'split_hash':'frozen','final_used_for_selection':False,'real_match_data':True})
  self.assertFalse(r['quality_gate_met']);self.assertEqual(r['default_strategy'],'legacy')
 def test_input_download_obeys_cancellation_heartbeat(self):
  import tempfile,io,hashlib
  from pathlib import Path
  from unittest.mock import Mock
  from cloud_worker import WorkerClient
  with tempfile.TemporaryDirectory() as directory:
   payload=b'x'*150000;client=WorkerClient('http://127.0.0.1:5136','isolated-test-worker-key');client.opener=Mock();client.opener.open.return_value=io.BytesIO(payload);path=Path(directory)/'input.dem';calls=[]
   def keepalive():
    calls.append(True)
    if len(calls)==2:raise ValueError('cancelled')
   with self.assertRaisesRegex(ValueError,'cancelled'):client.download({'jobId':'job','fence':1,'leaseToken':'lease'},{'kind':'demo','id':'demo','artifactHash':hashlib.sha256(payload).hexdigest()},path,keepalive)
   self.assertLess(path.stat().st_size,len(payload));self.assertTrue(calls)
