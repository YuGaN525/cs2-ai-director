import copy
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from client_update import decide, public_origin, UpdateProtocolError
from client_update_state import UpdateState


def envelope(version='1.14.0', channel='stable'):
    ident = '10000000-0000-0000-0000-000000000001'
    return {'success': True, 'code': 'OK', 'data': [dict(id=ident, version=version,
        channel=channel, enabled=True, artifactHash='a'*64, artifactBytes=200000000,
        revision=1, notes='更新说明\n第二行', downloadPath=f'/api/director/client-releases/{ident}/file')]}


class Decisions(unittest.TestCase):
    def test_numeric_order_and_no_downgrade(self):
        for current, target, state in [('1.9.0','1.10.0','update_available'),
                                      ('1.13.0','1.11.0','ahead_of_channel'),
                                      ('1.13.0','01.13.0','up_to_date')]:
            self.assertEqual(decide(envelope(target), current, 'stable').state, state)

    def test_empty_failure_conflict_distinct(self):
        value=envelope();value['data']=[]
        self.assertEqual(decide(value,'1.13.0','stable').state,'no_release')
        for value in [dict(success=False,code='DISABLED',data=[]),{'success':True,'data':[]}]:
            with self.assertRaises(UpdateProtocolError):decide(value,'1.13.0','stable')
        value=envelope();value['data']*=2
        with self.assertRaises(UpdateProtocolError):decide(value,'1.13.0','stable')
        self.assertEqual(decide(envelope('1.13.0'),'1.13.0','stable','b'*64).state,'same_version_conflict')

    def test_reject_wrong_channel_and_untrusted_fields(self):
        for key,value in [('channel','beta'),('enabled',False),('artifactHash','bad'),
                          ('artifactBytes',True),('revision',False),('notes','x'*2001),
                          ('id','not-a-uuid'),('version','1.14.0-beta'),
                          ('downloadPath','https://evil.invalid/update.exe')]:
            row=envelope();row['data'][0][key]=value
            with self.subTest(key=key),self.assertRaises(UpdateProtocolError):decide(row,'1.13.0','stable')

    def test_https_origin_only(self):
        self.assertEqual(public_origin('https://EXAMPLE.org/'),'https://example.org')
        for value in ['http://example.org','https://user@example.org','https://example.org/x',
                      'https://example.org/?x=1','https://example.org/#x','https://example.org:bad',
                      'https://example.org\\@evil.org','https://example.org\n']:
            with self.subTest(value=value),self.assertRaises(UpdateProtocolError):public_origin(value)


class Cache(unittest.TestCase):
    def test_only_one_window_reminds_and_snooze_expires(self):
        with tempfile.TemporaryDirectory() as root:
            now=[100.];state=UpdateState(root,lambda:now[0])
            self.assertTrue(state.reminder('k','release','first')['remind'])
            self.assertFalse(state.reminder('k','release','second')['remind'])
            self.assertTrue(state.reminder('k','release','first')['remind'])
            state.merge('k',reminder_window=None,reminder_until=200)
            self.assertFalse(state.reminder('k','release','first')['remind'])
            now[0]=201
            self.assertTrue(state.reminder('k','release','second')['remind'])
            state.merge('k',ignored='release')
            self.assertEqual(state.reminder('k','release','second'),{'ignored':True,'remind':False})
            self.assertTrue(state.reminder('k','new-release','first')['remind'])

    def test_claim_contention_expiry_and_late_result(self):
        with tempfile.TemporaryDirectory() as root:
            now=[100.];state=UpdateState(root,lambda:now[0]);key=state.key('https://example.org','stable')
            with ThreadPoolExecutor(max_workers=8) as pool:
                owners=list(pool.map(lambda _:state.claim(key),range(8)))
            self.assertEqual(sum(o is not None for o in owners),1)
            old=next(o for o in owners if o)
            now[0]+=16;new=state.claim(key)
            self.assertFalse(state.complete(key,old,result='old'))
            state.release(key,old)
            self.assertTrue(state.complete(key,new,result='new'))
            self.assertEqual(state.read(key)['result'],'new')

    def test_merge_preserves_preferences_and_clock_rollback_recovers(self):
        with tempfile.TemporaryDirectory() as root:
            now=[100.];a=UpdateState(root,lambda:now[0]);b=UpdateState(root,lambda:now[0])
            a.merge('key',ignored='release');b.merge('key',last_success=99)
            self.assertEqual(a.read('key'),{'ignored':'release','last_success':99})
            old=a.claim('key');now[0]=90;new=b.claim('key')
            self.assertTrue(new);self.assertFalse(a.complete('key',old,result='old'))


if __name__ == '__main__':
    unittest.main()
