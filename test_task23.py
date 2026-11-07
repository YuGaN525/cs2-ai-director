import copy
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.fernet import Fernet
import device_identity as identity
from offline_runtime import CachedModel, create_local_engine
from training_splits import make_plan, validate_plan, SplitError
import train_decision_model as training
import ai_trainer
from director_core import replay as replay_module
from director_core.model_format import DECISION_FEATURES
from test_cs2_ai_director import player, payload
from test_ai_trainer import write_csv


class SplitsTests(unittest.TestCase):
    def setUp(self):
        self.groups = [f'g{i//8}' for i in range(240)]
        self.xs = [[float(i % 8)] * 8 for i in range(240)]
        self.ys = [i % 2 for i in range(240)]
        self.times = [1000 + i for i in range(240)]

    def plan(self, mode='group'):
        return make_plan(self.xs, self.ys, self.groups, self.times, mode)

    def test_group_isolation_all_partitions_and_cv(self):
        p = self.plan()
        validate_plan(p, self.xs, self.ys, self.groups, self.times)
        for a, b in p.cv:
            self.assertFalse({self.groups[p.train[i]] for i in a} & {self.groups[p.train[i]] for i in b})
        self.assertEqual(p.split_hash, self.plan().split_hash)

    def test_time_uses_past_in_outer_and_inner_folds(self):
        p = self.plan('time')
        for a, b in [(p.train, p.validation), (p.train+p.validation, p.final_test)]:
            self.assertLess(max(self.times[i] for i in a), min(self.times[i] for i in b))
        validate_plan(p, self.xs, self.ys, self.groups, self.times)

    def test_time_missing_does_not_fall_back_to_random(self):
        self.times[0] = 0
        with self.assertRaises(SplitError):
            self.plan('time')

    def test_time_overlapping_groups_rejected(self):
        self.times[0] = 999999
        with self.assertRaises(SplitError):
            self.plan('time')

    def test_insufficient_groups_rejected(self):
        self.groups = ['one'] * 240
        with self.assertRaises(SplitError):
            self.plan()

    def test_changed_dataset_rejects_reused_plan(self):
        p = self.plan(); self.xs[0][0] += 1
        with self.assertRaises(SplitError):
            validate_plan(p, self.xs, self.ys, self.groups, self.times)

    def test_saved_plan_roundtrip_and_hash_validation(self):
        from training_splits import SplitPlan
        p = self.plan()
        data = json.loads(json.dumps(p.manifest()))
        self.assertEqual(SplitPlan.from_manifest(data),p)
        data['validation'].reverse()
        with self.assertRaises(SplitError): SplitPlan.from_manifest(data)

    def test_comparison_requires_identical_split_hash(self):
        with self.assertRaises(SplitError):
            ai_trainer.choose_result({'split_hash':'a'}, {'split_hash':'b'})
        with self.assertRaises(SplitError):
            ai_trainer.choose_result({}, {})

    def test_training_reproducible_and_final_test_reserved(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); csv = root/'rows.csv'; write_csv(csv)
            kwargs = dict(min_samples=100, search_space={'n_estimators':[50], 'max_depth':[4],
                          'min_samples_leaf':[2], 'max_features':['sqrt'], 'class_weight':['balanced']},
                          n_iter=1, log=lambda s: None)
            a = training.train_from_csv(csv, root/'a.pkl', root/'a.json', **kwargs)
            b = training.train_from_csv(csv, root/'b.pkl', root/'b.json', **kwargs)
            self.assertEqual(a['split_hash'], b['split_hash'])
            self.assertEqual(a['auc'], b['auc'])
            self.assertFalse(a['final_test']['selection_used'])
            self.assertGreater(a['partitions']['final_test'], 0)

    def test_advisor_sees_training_only_and_cannot_change_split(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); csv = root/'rows.csv'; write_csv(csv)
            def advise(profile):
                self.assertLess(profile['valid'], 252)
                self.assertEqual(profile['scope'], 'training-partition-only')
                return ai_trainer.Advice(diagnosis=['test'], split_mode='time', n_iter=1,
                    search_space={'n_estimators':[50], 'max_depth':[4]})
            report = ai_trainer.run_ai_training(csv, root, min_samples=100, trees=50,
                advisor=advise, search_iters=1, log=lambda s: None)
            self.assertEqual(report['baseline']['split_hash'], report['candidate']['split_hash'])
            self.assertFalse(report['final_test']['selection_used'])


class CoreReplayTests(unittest.TestCase):
    def events(self):
        data = payload([player(0,'CT',pos=(0,0,0)),
                        player(1,'T',pos=(300,0,0),fwd=(-1,0,0),hp=30)], player_slot=8)
        return [{'at':at,'gsi':copy.deepcopy(data)} for at in (0, .1, 2, 5, 8)]

    def test_replay_repeatable_and_pov_feedback(self):
        events = self.events()
        a, b = replay_module.replay(events), replay_module.replay(events)
        self.assertEqual(a,b)
        self.assertEqual(a['trace'][0]['action'],'switch')
        self.assertEqual(a['trace'][1]['action'],'hold')
        self.assertEqual(a['trace'][1]['observed_slot'],a['trace'][0]['slot'])

    def test_core_import_has_no_ui_or_desktop_dependencies(self):
        code = "import sys; import director_core; import director_core.replay; assert not any(m in sys.modules for m in ['tkinter','cs2_ai_director','keyboard','pydirectinput','decision_model'])"
        subprocess.run([sys.executable,'-c',code],check=True)

    def test_legacy_demo_cli_remains_available(self):
        subprocess.run([sys.executable,'train_from_demos.py','--help'],check=True,stdout=subprocess.DEVNULL)

    def test_same_named_demos_get_content_identity_and_metadata(self):
        import demo_dataset
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); a=root/'a'/'same.dem';b=root/'b'/'same.dem'
            for path, data in ((a,b'first'),(b,b'second')):
                path.parent.mkdir();path.write_bytes(data)
            from test_demo_live_dataset import parser_fixture
            with mock.patch('demoparser2.DemoParser',return_value=parser_fixture()):
                demo_dataset.build_demo_csv([a,b],root/'out.csv',log=lambda s:None)
            import csv
            with (root/'out.csv').open(encoding='utf-8-sig',newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual(len(rows),8)
            self.assertEqual({row['match_source_id'] for row in rows},
                             {hashlib.sha256(path.read_bytes()).hexdigest() for path in (a,b)})
            self.assertTrue(all(row['map']=='de_dust2' for row in rows))
            self.assertEqual({row['source_tick'] for row in rows},{'128','256'})

    def test_moved_rules_equal_pre_migration_baseline(self):
        # The baseline is a local evidence file, never copied into the core package.
        path = Path('build/task23/legacy_core_source.py')
        if not path.exists():
            self.skipTest('pre-migration evidence only available on original checkout')
        spec = importlib.util.spec_from_file_location('baseline_director', path)
        old = importlib.util.module_from_spec(spec); sys.modules[spec.name] = old
        spec.loader.exec_module(old)
        import director_core as core
        for event in self.events():
            a = old.score_candidates(old.parse_snapshot(event['gsi'],event['at']),old.DEFAULT_CONFIG,None,0.)
            b = core.score_candidates(core.parse_snapshot(event['gsi'],event['at']),core.DEFAULT_CONFIG,None,0.)
            self.assertEqual([(c.slot,c.score,old._candidate_features(c)) for c in a],
                             [(c.slot,c.score,core._candidate_features(c)) for c in b])


class Protector:
    def __init__(self):
        self.crypto = Fernet(Fernet.generate_key())
    def protect(self, data): return self.crypto.encrypt(data)
    def unprotect(self, data): return self.crypto.decrypt(data)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now, self.mono = 1000000., 100.
        self.protector = Protector()
        self.signer = Ed25519PrivateKey.generate()
        self.trust = {'test': self.signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)}
        self.session = self.new_session()
        self.model = self.root/'model.json.gz'
        data = {'n_features':8, 'features':DECISION_FEATURES, 'feature_version':'pov-v1',
                'classes':[0,1], 'trees':[{'left':[-1],'right':[-1],'feature':[-2],
                                         'threshold':[-2.], 'value':[[.4,.6]]}]}
        with gzip.open(self.model,'wt') as stream: json.dump(data,stream)
        self.digest = hashlib.sha256(self.model.read_bytes()).hexdigest()

    def new_session(self, root=None):
        return identity.DeviceSession(root or self.root, 'https://test.invalid', self.trust,
            clock=lambda:self.now,monotonic=lambda:self.mono,protector=self.protector)

    def lease(self, **changes):
        device = identity.unb64(self.session.device_public_key())
        payload = {'schema':1,'key_id':'test','product':'cs2-ai-director','account_id':'account-a',
                   'server_origin':'https://test.invalid','device_key_thumbprint':hashlib.sha256(device).hexdigest(),
                   'capabilities':['direct'],'verified_at':self.now,'offline_expires_at':self.now+604800,
                   'offline_max_seconds':604800,'model_sha256':self.digest}
        payload.update(changes)
        return {'payload':payload,'signature':identity.b64(self.signer.sign(identity.PURPOSE+identity.canonical(payload)))}

    def install(self, **changes):
        self.session.install_verified_lease(self.lease(**changes),'account-a')

    def check(self, **kw):
        return self.session.check(kw.get('account','account-a'),kw.get('digest',self.digest))

    def advance(self, seconds): self.now+=seconds; self.mono+=seconds

    def test_missing_login_denied(self): self.assertFalse(self.check().allowed)

    def test_valid_login_is_protected_and_restart_keeps_deadline(self):
        self.install(); self.advance(600)
        self.assertNotIn(b'account-a',self.session.path.read_bytes())
        self.session = self.new_session()
        self.assertEqual(self.check().remaining_seconds,604200)

    def test_exact_168_hour_expiry(self):
        self.install(); self.advance(604799.999)
        self.assertTrue(self.check().allowed)
        self.advance(.001)
        self.assertEqual(self.check().reason,'offline_expired')
        self.advance(1); self.assertFalse(self.check().allowed)

    def test_reboot_does_not_extend_expired_lease(self):
        self.install(); self.advance(604801); self.session=self.new_session()
        self.assertFalse(self.check().allowed)

    def test_wrong_account_and_model_rejected(self):
        self.install()
        self.assertFalse(self.check(account='account-b').allowed)
        self.assertFalse(self.check(digest='').allowed)
        self.assertFalse(self.check(digest='f'*64).allowed)

    def test_tampered_signature_and_unknown_key_rejected(self):
        envelope=self.lease();envelope['payload']['offline_expires_at']+=1
        with self.assertRaises(Exception): self.session.install_verified_lease(envelope,'account-a')
        with self.assertRaises(Exception): self.session.install_verified_lease(self.lease(key_id='other'),'account-a')

    def test_copied_login_wrong_device_rejected(self):
        self.install(); other=self.new_session(self.root/'other')
        other.device_public_key()
        other.path.write_bytes(self.session.path.read_bytes())
        self.assertFalse(other.check('account-a',self.digest).allowed)

    def test_max_duration_and_future_start_rejected(self):
        with self.assertRaises(Exception): self.install(offline_expires_at=self.now+604801)
        with self.assertRaises(Exception): self.install(verified_at=self.now+10)

    def test_known_entitlement_shorter_deadline(self):
        self.install(offline_expires_at=self.now+60); self.advance(60)
        self.assertEqual(self.check().reason,'offline_expired')

    def test_replayed_renewal_rejected_successful_renewal_resets_deadline(self):
        original=self.lease();self.session.install_verified_lease(original,'account-a')
        self.advance(100)
        with self.assertRaises(Exception): self.session.install_verified_lease(original,'account-a')
        self.install(); self.assertEqual(self.check().remaining_seconds,604800)

    def test_clock_rollback_persists_and_requires_new_signed_renewal(self):
        self.install();self.advance(60);self.check();self.now-=120
        self.assertEqual(self.check().reason,'clock_rollback')
        self.session=self.new_session();self.assertFalse(self.check().allowed)
        self.now+=200;self.install();self.assertTrue(self.check().allowed)

    def test_forward_clock_then_rollback_cannot_revive(self):
        self.install();self.now+=700000;self.assertFalse(self.check().allowed)
        self.now-=700000;self.assertFalse(self.check().allowed)

    def test_logout_and_revocation_preserve_files_but_deny(self):
        for reason in ('signed_out','device_revoked'):
            self.advance(1);self.install();self.session.invalidate(reason)
            self.assertTrue(self.session.path.exists());self.assertFalse(self.check().allowed)

    def test_different_origin_cannot_read_session(self):
        self.install()
        other=identity.DeviceSession(self.root,'https://other.invalid',self.trust,protector=self.protector)
        self.assertFalse(other.check('account-a',self.digest).allowed)

    def test_corrupt_protected_file_is_denied(self):
        self.install();self.session.path.write_bytes(b'corrupt');self.assertFalse(self.check().allowed)

    def test_model_validation_and_modified_cache(self):
        model=CachedModel(self.model);self.assertEqual(model.verified_hash(),self.digest)
        self.model.write_bytes(b'broken');self.assertEqual(model.current_hash(),'')
        self.assertEqual(model.verified_hash(),'')

    def test_model_feature_order_version_cycle_and_nonfinite_rejected(self):
        from director_core.model_format import validate_forest
        with gzip.open(self.model,'rt') as stream:
            original=json.load(stream)
        for case in ('order','version','cycle','nonfinite'):
            data=copy.deepcopy(original)
            if case=='order': data['features'].reverse()
            if case=='version': data['feature_version']='unknown'
            if case=='cycle':
                data['trees'][0].update(left=[0],right=[0],feature=[0])
            if case=='nonfinite': data['trees'][0]['value']=[[float('nan'),.6]]
            with self.subTest(case=case),self.assertRaises(ValueError):
                validate_forest(data)

    def test_signed_runtime_blocks_input_and_obs_at_expiry(self):
        import cs2_ai_director as d
        import director_pro as pro
        self.install()
        cfg=dict(d.DEFAULT_CONFIG,dry_run=True)
        pd=pro.ProDirector(cfg)
        engine,gate=create_local_engine(self.session,'account-a',self.model,cfg,d.LogBus(),pro=pd)
        self.assertTrue(engine.input.press_slot(0))
        self.advance(604800)
        self.assertFalse(engine.input.press_slot(0))
        self.assertFalse(engine.input.send_hotkey('alt+1'))
        pd.obs.client=mock.Mock()
        with self.assertRaises(RuntimeError): pd.obs._call('set_current_program_scene','scene')
        pd.obs.client.set_current_program_scene.assert_not_called()
        engine._tick()
        self.assertEqual(gate.last_state.reason,'offline_expired')

    def test_gate_releases_only_held_automatic_keys(self):
        import cs2_ai_director as d
        self.install();gate=identity.AuthorizationGate(self.session,'account-a',lambda:self.digest)
        controller=d.InputController(dict(d.DEFAULT_CONFIG),d.LogBus(),gate)
        controller._held_keys.add(('direct','1'))
        with mock.patch.object(d,'pydirectinput') as backend:
            self.advance(604800);self.assertFalse(controller.press_slot(0))
            backend.keyUp.assert_called_once_with('1')
            backend.keyDown.assert_not_called()

    @unittest.skipUnless(os.name=='nt','Windows DPAPI only')
    def test_real_dpapi_round_trip_and_tamper(self):
        protector=identity.WindowsProtector();raw=protector.protect(b'device secret')
        self.assertNotIn(b'device secret',raw)
        self.assertEqual(protector.unprotect(raw),b'device secret')
        with self.assertRaises(identity.IdentityError): protector.unprotect(raw[:-5]+b'xxxxx')


if __name__=='__main__': unittest.main()
