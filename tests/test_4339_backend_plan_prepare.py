# SPDX-License-Identifier: AGPL-3.0-only
"""#4339: the dry run tells the truth about account/layout refusals and a
prepare replay repairs a lost or altered question without re-rendering the
reviewed bundle. The fixtures are #4323's and #4327's: real disposable files,
simulated accounts, no root effect.
"""
from tests import test_4327_comptes_canaux as channel_fixture
from tests.test_4323_backend_administrateur import Fixture,backend_mod,bootstrap
import hashlib
import json
import stat
import unittest


class PlanPrepareTest(Fixture,unittest.TestCase):
    def plan(self):
        return self.backend.plan(self.operation,7,authority=self.authority,
                                 revoke_authority=self.authority)

    def account_effects(self):
        steps={item['step']:item['effects'] for item in self.plan()['steps']}
        return steps['account']

    def layout_effects(self):
        steps={item['step']:item['effects'] for item in self.plan()['steps']}
        return steps['layout']

    def intent(self,phase='complete',receipt=None):
        ids=self.bundle['account']
        request=dict(operation=self.operation,machine_id=self.bundle['machine_id'],
                     uid=ids['uid'],gid=ids['gid'],
                     scope_digest=backend_mod.installer.account.scope_digest(
                         self.operation,self.bundle['machine_id'],ids['uid'],ids['gid']))
        path=self.backend.admin/'effects/account'/(self.operation+'.json')
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        backend_mod.protocol.save(path,dict(request=request,phase=phase,receipt=receipt))
        return path

    def reviewed(self):
        ids=self.bundle['account']
        value=backend_mod.installer.account.expected(ids['uid'],ids['gid'])
        self.system.group_value=dict(value['group'])
        self.system.account_value=dict(value['account'])
        return value

    def test_account_plan_refuses_a_mismatched_identity_instead_of_present(self):
        self.intent()
        value=self.reviewed()
        effects=self.account_effects()
        self.assertEqual((effects['group'],effects['account'],effects['phase']),
                         ('present','present','complete'))
        for name,bad in (('home','/tmp/other'),('shell','/bin/bash'),
                         ('password_disabled',False),('supplementary_groups',[0]),
                         ('uid',1234),('gid',1234)):
            with self.subTest(name=name):
                self.system.account_value=dict(value['account'],**{name:bad})
                effects=self.account_effects()
                self.assertEqual(effects['account'],'conflict')
        self.system.account_value=dict(value['account'])
        self.system.group_value=dict(value['group'],members=['someone'])
        effects=self.account_effects()
        self.assertEqual((effects['group'],effects['account']),('conflict','conflict'))

    def test_account_plan_shows_resume_states_from_the_durable_intent(self):
        value=self.reviewed()
        self.system.account_value=None
        self.intent(phase='group')
        effects=self.account_effects()
        self.assertEqual((effects['group'],effects['account']),('present','create'))
        self.system.account_value=dict(value['account'])
        effects=self.account_effects()
        self.assertEqual((effects['group'],effects['account']),('present','conflict'))
        self.intent(phase='user')
        self.system.account_value=None
        effects=self.account_effects()
        self.assertEqual((effects['group'],effects['account']),('present','conflict'))
        self.system.account_value=dict(value['account'])
        self.assertEqual(self.account_effects()['account'],'present')

    def test_account_plan_never_announces_create_over_an_unowned_or_taken_identity(self):
        ids=self.bundle['account']
        self.system.account_value=dict(name='ameesh-release',uid=ids['uid'],gid=ids['gid'],
            home='/tmp/other',shell='/bin/sh',password_disabled=True,supplementary_groups=[])
        effects=self.account_effects()
        self.assertEqual((effects['group'],effects['account']),('conflict','conflict'))
        self.assertIn('existing account',effects['refused'])
        self.system.account_value=None; self.system.available=False
        effects=self.account_effects()
        self.assertEqual((effects['group'],effects['account']),('conflict','conflict'))
        self.assertIn('taken uid/gid',effects['refused'])

    def test_account_plan_refuses_an_untrusted_intent_record(self):
        path=self.intent(phase='group')
        record=backend_mod.protocol.load(path); record['phase']='unknown'
        backend_mod.protocol.save(path,record)
        with self.assertRaisesRegex(backend_mod.BackendError,'account intent unavailable'):
            self.plan()

    def test_layout_plan_refuses_an_untrusted_ancestor_before_create(self):
        parent=self.root/'opt/ameesh-release'
        parent.mkdir(parents=True,mode=0o700,exist_ok=True)
        self.addCleanup(lambda: parent.is_symlink() and parent.unlink())
        parent.chmod(0o777)
        effects=self.layout_effects()
        self.assertEqual(effects['/opt/ameesh-release'],'refuse untrusted')
        self.assertEqual(effects['/opt/ameesh-release/store'],'refuse untrusted')
        outside=self.root/'opt/trusted-target'; outside.mkdir(mode=0o755,exist_ok=True)
        parent.rmdir(); parent.symlink_to(outside,target_is_directory=True)
        effects=self.layout_effects()
        self.assertEqual(effects['/opt/ameesh-release'],'refuse untrusted')
        self.assertEqual(effects['/opt/ameesh-release/store'],'refuse untrusted')

    def test_prepare_replay_repairs_a_lost_or_altered_question_only(self):
        value=dict(plan=self.bundle['plan'],account_ids=dict(uid=987,gid=987),expires_at=2000000000)
        plan_path=self.stage/'plan.json'; plan_path.write_text(json.dumps(value)); plan_path.chmod(0o600)
        bundle=self.stage/'bundle.json'; question=self.stage/'question.txt'
        before=(bundle.stat().st_ino,bundle.stat().st_mtime_ns,
                hashlib.sha256(bundle.read_bytes()).hexdigest())
        for altered in (None,self.question.replace('"install"','"revoke"'),
                        bootstrap.question(self.operation,self.bundle,expires_at=1999999999)):
            with self.subTest(altered=altered is not None):
                if altered is None: question.unlink(missing_ok=True)
                else: question.write_text(altered); question.chmod(0o600)
                result=self.backend.prepare(self.operation)
                self.assertEqual(result['question'],self.question)
                self.assertEqual(question.read_text(),self.question)
                self.assertEqual(stat.S_IMODE(question.stat().st_mode),0o600)
                self.assertEqual(self.backend.prepared(self.operation).question,self.question)
                after=(bundle.stat().st_ino,bundle.stat().st_mtime_ns,
                       hashlib.sha256(bundle.read_bytes()).hexdigest())
                self.assertEqual(after,before)
        self.assertTrue(all(item['bundle_digest']==self.bundle['digest']
                            for item in self.backend.gestes.entries() if item['phase']=='prepared'))


class ChannelAccountPlanTest(channel_fixture.Fixture,unittest.TestCase):
    def effects(self):
        plan=self.backend.plan(self.operation,7,authority=self.authority,
                               revoke_authority=self.authority)
        return {item['step']:item['effects'] for item in plan['steps']}

    def test_channel_numeric_collision_is_conflict_not_create(self):
        self.system.accounts['other']=dict(name='other',uid=982,gid=1)
        steps=self.effects()
        self.assertEqual(steps['account_handoff']['account'],'create')
        self.assertEqual(steps['account_secrets']['account'],'conflict')
        self.assertIn('taken uid/gid',steps['account_secrets']['refused'])

    def test_channel_resume_compares_the_complete_identity(self):
        name,home='ameesh-handoff','/etc/ameesh-handoff'
        machine=self.system.machine_id
        request=dict(operation=self.operation,machine_id=machine,uid=981,gid=981,
                     scope_digest=channel_fixture.channels.account.scope_digest(
                         self.operation,machine,981,981,name=name,home=home))
        path=self.backend.admin/'effects/account'/(self.operation+'.'+name+'.json')
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        backend_mod.protocol.save(path,dict(request=request,phase='complete',receipt={}))
        self.system.groups[name]=dict(name=name,gid=981,members=[])
        self.system.accounts[name]=dict(name=name,uid=981,gid=981,home=home,shell='/bin/sh',
                                        password_disabled=True,supplementary_groups=[])
        self.assertEqual(self.effects()['account_handoff']['account'],'present')
        for key,value in (('home','/tmp/other'),('shell','/bin/bash'),
                          ('supplementary_groups',[0]),('password_disabled',False)):
            with self.subTest(key=key):
                self.system.accounts[name][key]=value
                self.assertEqual(self.effects()['account_handoff']['account'],'conflict')
                self.system.accounts[name][key]=dict(home=home,shell='/bin/sh',
                                                     supplementary_groups=[],password_disabled=True)[key]


if __name__=='__main__':
    unittest.main()
