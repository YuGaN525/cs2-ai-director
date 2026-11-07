import threading
import time
import unittest
from unittest.mock import patch
import cs2_ai_director as d


class ArbitrationLifetimeTests(unittest.TestCase):
    def arbiter(self):
        cfg = dict(d.DEFAULT_CONFIG, ai_enabled=True, ai_result_ttl=.5)
        arb = d.AIArbiter(cfg, d.LogBus())
        from types import SimpleNamespace
        arb.cloud=SimpleNamespace(generation=0,allowed=lambda:True,ask=lambda req:1)
        return arb

    def test_stop_clears_result_and_cannot_be_revived(self):
        arb = self.arbiter()
        arb._result = (1, time.monotonic(), "test")
        arb.stop()
        self.assertIsNone(arb.fresh(time.monotonic()))
        self.assertIsNone(arb._result)

    def test_disabled_and_future_results_are_not_fresh(self):
        arb = self.arbiter()
        arb._result = (1, 10, "test")
        self.assertIsNone(arb.fresh(9))
        arb.cfg["ai_enabled"] = False
        self.assertIsNone(arb.fresh(10))

    def run_response(self, finished_at, stop_during_call=False):
        arb = self.arbiter()
        arb._q.put({"ts":10, "prompt":"test", "slots":{1},"cloud":{"generation":0,"requestId":"fixture"}})
        def respond(*args):
            if stop_during_call:
                arb._stop.set()
            return 1
        arb.cloud.ask = respond
        # Deterministically end the loop at its next queue read, without wall-clock sleeps.
        original_get = arb._q.get
        calls = [0]
        def get(*args, **kwargs):
            calls[0] += 1
            if calls[0] > 1:
                arb._stop.set()
                raise d.queue.Empty()
            return original_get(*args, **kwargs)
        arb._q.get = get
        with patch.object(d.time, "monotonic", side_effect=[10, finished_at, finished_at]):
            arb._worker()
        return arb

    def test_delayed_provider_response_is_discarded(self):
        self.assertIsNone(self.run_response(11)._result)

    def test_valid_result_retains_capture_timestamp(self):
        self.assertEqual((1, 10, "云端候选建议"), self.run_response(10.1)._result)

    def test_stop_during_provider_call_drops_response(self):
        self.assertIsNone(self.run_response(10.1, True)._result)

    def test_work_queued_before_previous_call_cannot_bypass_cooldown(self):
        arb=self.arbiter();arb._last_call=9
        arb.cloud.ask=lambda req:self.fail('Queued work bypassed cooldown')
        arb._q.put({'ts':10,'cloud':{'generation':0,'requestId':'fixture'}})
        original=arb._q.get
        calls=[0]
        def get(*args,**kwargs):
            calls[0]+=1
            if calls[0]>1:
                arb._stop.set();raise d.queue.Empty()
            return original(*args,**kwargs)
        arb._q.get=get
        with patch.object(d.time,'monotonic',return_value=10):arb._worker()
        self.assertEqual(arb.calls,0)
