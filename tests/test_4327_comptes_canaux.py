# SPDX-License-Identifier: AGPL-3.0-only
"""Channel accounts bundle (#4327): renderer, signed ordering, adapter, backend.

Real disposable files under a private fixture root; accounts, sshd/visudo
and reloads are simulated. The signed refusals run the real channel
authorities on a disposable PostgreSQL schema. No real root, account, SSH or
systemd effect is executed.
"""
from tests import support
from tests import test_4323_backend_administrateur as backend_fixture
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import time
import unittest
from unittest import mock
from ameesh import authority,decisions,registry,signing,work

backend_mod=backend_fixture.backend_mod
channels=backend_mod.channels
delegation=channels.delegation
adapter_mod=backend_mod.channel_installer
tree_state=backend_fixture.tree_state
HANDOFF_KEY='/etc/ameesh-handoff/authorized_keys'
SECRETS_KEY='/etc/ameesh-secrets/authorized_keys'
IDS=dict(handoff=dict(uid=981,gid=981),secrets=dict(uid=982,gid=982))


def public_key(byte):
    wire=struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+bytes([byte])*32
    return 'ssh-ed25519 '+base64.b64encode(wire).decode()


def plan(runtime='c'*64,sources='d'*64):
    return dict(host='fixture-host',machine_id='a'*32,host_key_sha256='b'*64,
                runtime_tree_sha256=runtime,source_tree_sha256=sources,
                channels=dict(handoff=dict(controller_source='192.0.2.17/32',controller_key=public_key(1)),
                              secrets=dict(controller_source='192.0.2.18/32',controller_key=public_key(2))))


class Host:
    """Simulated multi-account host with the channel system interface."""
    machine_id='a'*32

    def __init__(self,root):
        self.root=root; self.groups={}; self.accounts={}; self.calls=[]
        self.reloads=0; self.trees=True; self.valid=True

    def machine(self): return self.machine_id
    def free(self,uid,gid):
        return (all(item['uid']!=uid for item in self.accounts.values())
                and all(item['gid']!=gid for item in self.groups.values()))
    def group(self,name): return self.groups.get(name)
    def account(self,name): return self.accounts.get(name)
    def create_group(self,gid,name):
        self.calls.append('group:'+name); self.groups[name]=dict(name=name,gid=gid,members=[])
    def create_account(self,uid,gid,name,home):
        self.calls.append('user:'+name)
        self.accounts[name]=dict(name=name,uid=uid,gid=gid,home=home,shell='/bin/sh',
                                 password_disabled=True,supplementary_groups=[])
    def preflight_channels(self,bundle): return self.machine()==bundle['machine_id'] and self.trees
    def trees_ready(self,bundle): return self.trees
    def layout_channels(self,bundle):
        for logical,mode in delegation.DIRECTORIES.items():
            target=self.root/logical.lstrip('/')
            target.mkdir(parents=True,mode=mode,exist_ok=True); target.chmod(mode)
        return True
    def validate_channels(self,bundle): return self.valid
    def validate_restored_configuration(self,bundle): return True
    def activate_ssh_configuration(self,bundle): self.reloads+=1; return True


class Authority:
    def __init__(self): self.denied=set()
    def validate(self,operation,digest,step): return step not in self.denied


class RendererTest(unittest.TestCase):
    def test_render_covers_both_accounts_roots_and_channels_exactly(self):
        bundle=delegation.render(plan(),account_ids=IDS)
        self.assertEqual(bundle,delegation.render(plan(),account_ids=IDS))
        self.assertEqual(bundle['kind'],'channels')
        self.assertEqual(set(bundle['files']),{
            '/usr/local/libexec/ameesh-handoff-ssh','/etc/sudoers.d/ameesh-handoff',HANDOFF_KEY,
            '/etc/ssh/sshd_config.d/62-ameesh-handoff.conf',
            '/usr/local/libexec/ameesh-secrets-ssh','/etc/sudoers.d/ameesh-secrets',SECRETS_KEY,
            '/etc/ssh/sshd_config.d/61-ameesh-secrets.conf'})
        for logical in ('/etc/ameesh-handoff','/etc/ameesh-secrets'):
            self.assertEqual(bundle['directories'][logical],0o755)
        for logical in ('/var/lib/ameesh-handoff','/var/lib/ameesh-secrets'):
            self.assertEqual(bundle['directories'][logical],0o700)
        self.assertEqual(bundle['accounts']['handoff'],dict(name='ameesh-handoff',system=True,shell='/bin/sh',
            home='/etc/ameesh-handoff',supplementary_groups=[],password_login=False,uid=981,gid=981))
        self.assertEqual(bundle['accounts']['secrets']['name'],'ameesh-secrets')
        # The secret channel is byte-identical to its existing reviewed bundle.
        secrets=delegation.secrets.render({key:value for key,value in plan().items() if key!='channels'}
                                          |plan()['channels']['secrets'])
        for path,info in secrets['files'].items():
            self.assertEqual(bundle['files'][path],info)
        handoff=bundle['files'][HANDOFF_KEY]['content']
        self.assertTrue(handoff.startswith('restrict,from="192.0.2.17/32",command="/usr/local/libexec/ameesh-handoff-ssh" '))
        self.assertIn('/opt/ameesh-release/lib/hote-handoff.py',
                      bundle['files']['/usr/local/libexec/ameesh-handoff-ssh']['content'])
        self.assertIn('ameesh-handoff ALL=(root) NOPASSWD: /opt/ameesh-release/runtime/bin/python -I -B '
                      '/opt/ameesh-release/lib/hote-handoff.py\n',bundle['files']['/etc/sudoers.d/ameesh-handoff']['content'])

    def test_render_refuses_partial_shared_or_extra_inputs(self):
        cases=[]
        value=plan(); del value['channels']['secrets']; cases.append((value,IDS))
        value=plan(); value['channels']['secrets']['controller_key']=public_key(1); cases.append((value,IDS))
        value=plan(); value['policy']={}; cases.append((value,IDS))
        value=plan(); value['channels']['handoff']['controller_source']='192.0.2.17'; cases.append((value,IDS))
        cases.append((plan(),dict(handoff=IDS['handoff'])))
        cases.append((plan(),dict(handoff=IDS['handoff'],secrets=dict(uid=981,gid=990))))
        cases.append((plan(),dict(handoff=IDS['handoff'],secrets=None)))
        cases.append((plan(),None))
        for value,ids in cases:
            with self.subTest(value=value,ids=ids),self.assertRaises(delegation.DelegationError):
                delegation.render(value,account_ids=ids)

    def test_question_detects_any_change_and_fits_the_signed_limit(self):
        bundle=delegation.render(plan(),account_ids=IDS)
        for change in (lambda b:b['accounts']['handoff'].update(uid=983),
                       lambda b:b['files'][HANDOFF_KEY].update(mode=0o666),
                       lambda b:b['directories'].update({'/var/lib/ameesh-handoff':0o755}),
                       lambda b:b.update(kind='release')):
            altered=json.loads(json.dumps(bundle)); change(altered)
            with self.subTest(),self.assertRaises(delegation.DelegationError):
                delegation.question(altered)
        prior={path:dict(sha256='e'*64,mode=0o644) for path in bundle['files']}
        full=delegation.render(plan(),account_ids=IDS,previous_files=prior)
        text=channels.question('c'*80,full,expires_at=2000000000)
        self.assertLessEqual(len(text.encode()),channels.MAX_QUESTION)
        self.assertIn('"handoff":"ameesh-handoff 981:981"',text)
        self.assertLessEqual(len(channels.revoke_question('x',full,expires_at=2000000000).encode()),
                             channels.MAX_QUESTION)

    def test_scopes_are_distinct_from_the_release_bootstrap(self):
        bundle=delegation.render(plan(),account_ids=IDS)
        scopes=channels.scopes('op',bundle)
        self.assertEqual(list(channels.STEPS),['preflight','layout','account_handoff','account_secrets',
                                               'configuration','validate','enable'])
        self.assertEqual(len(set(scopes.values())),len(channels.ACTIONS))
        self.assertEqual(scopes['account_handoff'],channels.account.scope_digest(
            'op','a'*32,981,981,name='ameesh-handoff',home='/etc/ameesh-handoff'))
        # The release account scope is unchanged by the parameterization.
        self.assertEqual(channels.account.scope_digest('op','a'*32,987,987),
            channels.protocol.identity(dict(version=1,operation='op',machine_id='a'*32,
                account=dict(name='ameesh-release',uid=987,gid=987,home='/var/lib/ameesh-release/ssh',
                             shell='/bin/sh',password_disabled=True,supplementary_groups=[]))))
        with self.assertRaises(channels.BootstrapError):
            channels.scopes('../op',bundle)


class LinuxChannelsTest(unittest.TestCase):
    """Fixed commands only; the commands themselves are simulated."""

    def host(self,outputs):
        with mock.patch.object(adapter_mod.installer.account.os,'geteuid',return_value=0):
            host=adapter_mod.LinuxChannels()
        calls=[]
        def command(args):
            calls.append(args)
            return outputs(args)
        host.command=command
        return host,calls

    def effective(self,args):
        if args[0].endswith('visudo'): return ''
        user=args[3].split(',')[0][len('user='):]
        channel=next(item for item in delegation.CHANNELS.values() if item['account']==user)
        return '\n'.join(['ForceCommand '+channel['wrapper'],'AuthorizedKeysFile '+channel['keys'],
            'AuthorizedKeysCommand none','AuthenticationMethods publickey','PasswordAuthentication no',
            'KbdInteractiveAuthentication no','DisableForwarding yes','PermitTTY no','PermitUserRC no',
            'MaxSessions 1'])

    def test_each_channel_account_is_validated_with_its_own_source(self):
        bundle=delegation.render(plan(),account_ids=IDS)
        host,calls=self.host(self.effective)
        with mock.patch.object(adapter_mod.Path,'is_file',return_value=True), \
                mock.patch.object(adapter_mod.runtime,'trusted',side_effect=lambda path:path):
            self.assertTrue(host.validate_channels(bundle))
        self.assertIn(['/usr/bin/visudo','-c','-f','/etc/sudoers.d/ameesh-handoff'],calls)
        self.assertIn(['/usr/bin/visudo','-c','-f','/etc/sudoers.d/ameesh-secrets'],calls)
        self.assertTrue(any(args[-1]=='user=ameesh-handoff,host=fixture-host,addr=192.0.2.17' for args in calls))
        self.assertTrue(any(args[-1]=='user=ameesh-secrets,host=fixture-host,addr=192.0.2.18' for args in calls))
        def swapped(args):
            text=self.effective(args)
            return text.replace('ameesh-secrets-ssh','ameesh-handoff-ssh')
        host,calls=self.host(swapped)
        with mock.patch.object(adapter_mod.Path,'is_file',return_value=True), \
                mock.patch.object(adapter_mod.runtime,'trusted',side_effect=lambda path:path), \
                self.assertRaisesRegex(adapter_mod.InstallError,'mismatch'):
            host.validate_channels(bundle)

    def test_release_trees_absent_or_mismatched_are_not_ready(self):
        bundle=delegation.render(plan(),account_ids=IDS)
        host,calls=self.host(lambda args:'')
        with mock.patch.object(adapter_mod.runtime,'inventory',side_effect=OSError('fixture missing tree')):
            self.assertFalse(host.trees_ready(bundle))
        with mock.patch.object(adapter_mod.runtime,'inventory',return_value=dict(digest='f'*64,dirs=[],files={})):
            self.assertFalse(host.trees_ready(bundle))
        self.assertEqual(calls,[])

    def test_dispatchers_must_belong_to_the_measured_source_tree(self):
        bundle=delegation.render(plan(),account_ids=IDS)
        host,calls=self.host(lambda args:'')
        def inventory(files):
            def measured(logical):
                field='runtime_tree_sha256' if logical.endswith('runtime') else 'source_tree_sha256'
                return dict(digest=bundle['plan'][field],dirs=[],
                            files={} if logical.endswith('runtime') else dict.fromkeys(files,'0'*64))
            return measured
        trusted=mock.patch.object(adapter_mod.runtime,'trusted',side_effect=lambda path:path)
        # Read-only measured files (the fixture never touches the real /opt).
        readonly=mock.patch.object(adapter_mod.Path,'stat',return_value=os.stat_result((0o100444,)+(0,)*9))
        with trusted,readonly,mock.patch.object(adapter_mod.runtime,'inventory',
                                       side_effect=inventory(['hote-handoff.py','hote-secrets.py'])):
            self.assertTrue(host.trees_ready(bundle))
        with trusted,readonly,mock.patch.object(adapter_mod.runtime,'inventory',
                                                side_effect=inventory(['hote-secrets.py'])):
            self.assertFalse(host.trees_ready(bundle))


class Fixture:
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'host'; self.root.mkdir(mode=0o700)
        original=Path.lstat; ancestors=set(Path(self.temp.name).parents)
        def trusted_ancestors(path):
            info=original(path)
            if path in ancestors and stat.S_ISDIR(info.st_mode):
                fields=list(info); fields[0]&=~0o022; fields[4]=os.geteuid(); return os.stat_result(fields)
            return info
        patch=mock.patch.object(Path,'lstat',trusted_ancestors); patch.start(); self.addCleanup(patch.stop)
        def writable():
            for directory,dirs,names in os.walk(self.root): Path(directory).chmod(0o700)
        self.addCleanup(writable)
        # The release bootstrap already installed its trees (read-only).
        runtime_tree=self.root/'opt/ameesh-release/runtime'; (runtime_tree/'bin').mkdir(parents=True,mode=0o700)
        (runtime_tree/'bin/python').write_bytes(b'synthetic executable, never launched')
        lib=self.root/'opt/ameesh-release/lib'; lib.mkdir(mode=0o700)
        for name in ('hote-handoff.py','hote-secrets.py'): (lib/name).write_text('# synthetic pinned source\n')
        inventory=adapter_mod.runtime.inventory
        self.plan=plan(inventory(runtime_tree)['digest'],inventory(lib)['digest'])
        for directory in ('etc/ssh','usr/local/libexec','etc/sudoers.d','etc/ssh/sshd_config.d'):
            (self.root/directory).mkdir(parents=True,mode=0o700,exist_ok=True)
        (self.root/'etc/machine-id').write_text('a'*32)
        key=self.root/'etc/ssh/ssh_host_ed25519_key.pub'
        key.write_text('ssh-ed25519 '+base64.b64encode(b'host').decode()+'\n')
        self.plan['host_key_sha256']=hashlib.sha256(b'host').hexdigest()
        self.operation='channels-fixture'
        self.stage=self.root/('var/lib/ameesh-release-bootstrap/'+self.operation)
        self.stage.mkdir(parents=True,mode=0o700); self.stage.parent.chmod(0o700)
        self.system=Host(self.root); self.authority=Authority()
        self.enrollment=dict(version=1,host='fixture-host',machine_id='a'*32,code_sha256='e'*64)
        self.backend=backend_mod.Backend(system=self.system,enrollment=self.enrollment,
                                         root=self.root,code='e'*64)
        self.stage_plan()
        self.backend.prepare(self.operation)
        self.bundle=json.loads((self.stage/'bundle.json').read_text())

    def stage_plan(self,**extra):
        value=dict(delegation='canaux',plan=self.plan,account_ids=IDS,expires_at=2000000000,**extra)
        path=self.stage/'plan.json'; path.unlink(missing_ok=True)
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def path(self,logical): return self.root/logical.lstrip('/')
    def gestes(self): return self.backend.gestes.entries()


class BackendChannelTest(Fixture,unittest.TestCase):
    def test_prepare_observes_the_inventory_and_writes_the_channel_question(self):
        self.assertEqual(self.bundle['kind'],'channels')
        self.assertTrue(all(value is None for value in self.bundle['previous_files'].values()))
        question=(self.stage/'question.txt').read_text()
        self.assertTrue(question.startswith(channels.PREFIX))
        self.assertEqual(self.backend.prepared(self.operation).question,question)
        self.assertEqual(self.backend.prepare(self.operation)['bundle_digest'],self.bundle['digest'])
        inventory=self.backend.inventory(self.operation)
        self.assertEqual(set(inventory['files']),set(self.bundle['files']))
        # Probe and seal documents belong to the release bundle only.
        for extra in (dict(runtime_seal={}),dict(probe_turn={}),):
            self.stage_plan(**extra)
            with self.subTest(extra=extra),self.assertRaisesRegex(backend_mod.BackendError,'exact staged plan'):
                self.backend.inventory(self.operation)
        self.stage_plan(); (self.stage/'plan.json').write_text(json.dumps(dict(
            delegation='autre',plan=self.plan,account_ids=IDS,expires_at=2000000000)))
        with self.assertRaisesRegex(backend_mod.BackendError,'exact staged plan'):
            self.backend.inventory(self.operation)

    def test_dry_run_renders_the_exact_channel_plan_without_any_effect(self):
        before=tree_state(self.root)
        refused=AssertionError('dry run must not execute')
        with mock.patch.object(adapter_mod.ChannelHostAdapter,'perform',side_effect=refused), \
                mock.patch.object(channels,'run',side_effect=refused), \
                mock.patch.object(channels.release,'_run',side_effect=refused):
            result=self.backend.plan(self.operation,7,authority=self.authority,revoke_authority=self.authority)
        self.assertEqual(tree_state(self.root),before)
        self.assertFalse((self.root/'var/lib/ameesh-release-bootstrap/.admin/effects').exists())
        self.assertEqual(result['delegation'],'canaux'); self.assertTrue(result['authorized'])
        self.assertEqual([item['step'] for item in result['steps']],list(channels.STEPS))
        steps={item['step']:item['effects'] for item in result['steps']}
        self.assertTrue(steps['preflight']['machine_id']); self.assertTrue(steps['preflight']['host_key'])
        self.assertTrue(steps['preflight']['/opt/ameesh-release/runtime'])
        self.assertTrue(steps['preflight']['/opt/ameesh-release/lib'])
        self.assertEqual(steps['preflight']['dispatchers'],dict(handoff=True,secrets=True))
        self.assertEqual(steps['layout']['/var/lib/ameesh-handoff'],'create 0700')
        self.assertEqual(steps['layout']['/etc/ameesh-secrets'],'create 0755')
        self.assertEqual(steps['account_handoff'],dict(group='create',account='create',
                                                       name='ameesh-handoff',uid=981,gid=981))
        self.assertEqual(steps['account_secrets']['name'],'ameesh-secrets')
        self.assertEqual(steps['enable'][HANDOFF_KEY],'write 0644'); self.assertEqual(steps['enable'][SECRETS_KEY],'write 0644')
        self.assertNotIn(HANDOFF_KEY,steps['configuration']); self.assertNotIn(SECRETS_KEY,steps['configuration'])
        self.assertEqual(self.system.calls,[]); self.assertEqual(self.system.reloads,0)
        text=json.dumps(result)
        for info in self.bundle['files'].values(): self.assertNotIn(info['content'],text)

    def test_install_creates_both_accounts_then_keys_last_and_replays_without_effect(self):
        order=[]
        original=self.system.activate_ssh_configuration
        def reload(bundle):
            order.append(('reload',self.path(HANDOFF_KEY).exists()))
            return original(bundle)
        self.system.activate_ssh_configuration=reload
        self.assertEqual(self.backend.install(self.operation,7,authority=self.authority)['outcome'],'installed')
        self.assertEqual(order,[('reload',False)])
        self.assertEqual(self.system.calls,['group:ameesh-handoff','user:ameesh-handoff',
                                            'group:ameesh-secrets','user:ameesh-secrets'])
        self.assertEqual(self.system.accounts['ameesh-handoff']['home'],'/etc/ameesh-handoff')
        for logical,info in self.bundle['files'].items():
            self.assertEqual(self.path(logical).read_text(),info['content'],logical)
            self.assertEqual(stat.S_IMODE(self.path(logical).stat().st_mode),info['mode'],logical)
        for logical,mode in delegation.DIRECTORIES.items():
            self.assertEqual(stat.S_IMODE(self.path(logical).stat().st_mode),mode,logical)
        entries=self.gestes()
        self.assertEqual([item['phase'] for item in entries],
                         ['prepared','start']+['intent','receipt']*len(channels.STEPS)+['installed'])
        self.assertEqual(self.backend.install(self.operation,7,authority=self.authority)['outcome'],'installed')
        self.assertEqual(len(self.system.calls),4); self.assertEqual(self.system.reloads,1)
        result=self.backend.plan(self.operation,7,authority=self.authority,revoke_authority=self.authority)
        self.assertTrue(result['complete']); self.assertTrue(all(item['done'] for item in result['steps']))
        self.assertEqual(result['revoke']['files'][HANDOFF_KEY],'remove')

    def test_missing_release_trees_or_invalid_channel_settings_hold_before_keys(self):
        self.system.trees=False
        with self.assertRaises(channels.BootstrapError): self.backend.install(self.operation,7,authority=self.authority)
        self.assertEqual(self.system.calls,[]); self.assertFalse(self.path('/etc/ameesh-handoff').exists())
        self.system.trees=True; self.system.valid=False
        with self.assertRaises(channels.BootstrapError): self.backend.install(self.operation,7,authority=self.authority)
        self.assertFalse(self.path(HANDOFF_KEY).exists()); self.assertFalse(self.path(SECRETS_KEY).exists())
        self.assertTrue(self.path('/etc/sudoers.d/ameesh-handoff').exists())
        self.assertEqual(self.system.reloads,0)
        self.system.valid=True
        self.assertEqual(self.backend.install(self.operation,7,authority=self.authority)['outcome'],'installed')
        self.assertEqual(len(self.system.calls),4)

    def test_install_without_authority_has_no_host_effect(self):
        self.authority.denied=set(channels.ACTIONS)
        with self.assertRaises(channels.BootstrapError):
            self.backend.install(self.operation,7,authority=self.authority)
        self.assertFalse(self.path('/etc/ameesh-handoff').exists())
        self.assertEqual(self.system.calls,[])
        self.assertEqual([item['phase'] for item in self.gestes()][-2:],['start','held'])

    def test_preexisting_account_or_numeric_identity_refuses(self):
        self.system.accounts['other']=dict(name='other',uid=982,gid=1)
        with self.assertRaises(channels.BootstrapError):
            self.backend.install(self.operation,7,authority=self.authority)
        self.assertEqual(self.system.calls,['group:ameesh-handoff','user:ameesh-handoff'])
        self.assertFalse(self.path(HANDOFF_KEY).exists())

    def test_signed_revoke_removes_both_keys_first_and_keeps_accounts_and_roots(self):
        self.backend.install(self.operation,7,authority=self.authority)
        removed=[]
        original=Path.unlink
        def unlink(path,*args,**kwargs):
            removed.append(str(path)[len(str(self.root)):]); return original(path,*args,**kwargs)
        with mock.patch.object(Path,'unlink',unlink):
            self.assertEqual(self.backend.revoke(self.operation,8,authority=self.authority)['outcome'],'revoked')
        published=[item for item in removed if item in self.bundle['files']]
        self.assertEqual(set(published[:2]),{HANDOFF_KEY,SECRETS_KEY})
        for logical in self.bundle['files']: self.assertFalse(self.path(logical).exists(),logical)
        self.assertEqual(set(self.system.accounts),{'ameesh-handoff','ameesh-secrets'})
        for logical in delegation.DIRECTORIES: self.assertTrue(self.path(logical).is_dir(),logical)
        self.assertEqual(self.system.reloads,2)
        question=self.backend.revoke_question(self.operation,2000000000)
        self.assertTrue(question['question'].startswith(channels.REVOKE_PREFIX))
        self.assertEqual(question['options'],channels.REVOKE_OPTIONS)

    def test_release_and_channel_bundles_never_cross(self):
        release=backend_fixture.installer_fixture.InstallerTest(); release.setUp(); self.addCleanup(release.doCleanups)
        with self.assertRaises(Exception): channels.scopes('op',release.bundle)
        with self.assertRaises(Exception): backend_mod.bootstrap.scopes('op',self.bundle)
        altered=(self.stage/'question.txt').read_text().replace(channels.PREFIX,backend_mod.bootstrap.PREFIX)
        (self.stage/'question.txt').write_text(altered)
        with self.assertRaisesRegex(backend_mod.BackendError,'question'):
            self.backend.plan(self.operation,7,authority=self.authority,revoke_authority=self.authority)


class SignedChannelRefusalTest(Fixture,support.PgTestCase):
    """Mandate absent, unsigned, expired, of the other kind or revoked key: refused."""

    def setUp(self):
        support.PgTestCase.setUp(self)
        Fixture.setUp(self)
        self.now=time.time(); self.seed=b'\x47'*32
        registry.upsert(self.db,'fixture-owner',harness='other',host=self.cfg.host)
        authority.register_key(self.db,'fixture-owner',signing.public_from_seed(self.seed),role='owner')
        registry.upsert(self.db,'fixture-controller',harness='other',host=self.cfg.host)
        self.lot=work.add(self.db,title='Synthetic channel bootstrap signature',assignee='fixture-controller')
        question=channels.question(self.operation,self.bundle,expires_at=int(self.now)+600)
        (self.stage/'question.txt').write_text(question)
        self.view=self.ask(question,channels.OPTIONS)
        self.backend.db=self.db

    def ask(self,question,options):
        return decisions.ask(self.cfg,self.db,requester='fixture-controller',lot=str(self.lot['id']),
            question=question,options=['%s=%s'%(item['key'],item['label']) for item in options],
            needs_signature=True)

    def sign(self,view=None,text='install'):
        view=view or self.view
        option=decisions.match_option(view,text)
        digest=decisions.answer_digest(view,text,option,'human:fixture-owner')
        authority.create_approval(self.db,self.seed,approver='fixture-owner',action=decisions.APPROVAL_ACTION,
            artifact_kind=decisions.APPROVAL_KIND,artifact_hash=digest,ttl=(self.now+900)-time.time())
        decisions.answer(self.cfg,self.db,view['id'],text,human='human:fixture-owner',channel='cli',signed=True)

    def signed(self,clock=None,view=None,revoke=False):
        kind=channels.SignedChannelRevokeAuthority if revoke else channels.SignedChannelAuthority
        return kind(self.db,(view or self.view)['id'],self.operation,self.bundle,clock=clock or (lambda:self.now))

    def assert_refused(self,decision,authority=None):
        with self.assertRaises(channels.BootstrapError):
            self.backend.install(self.operation,decision,authority=authority)
        self.assertFalse(self.path('/etc/ameesh-handoff').exists())
        self.assertEqual(self.system.calls,[])
        self.assertEqual(self.gestes()[-1]['phase'],'held')

    def test_absent_unsigned_or_expired_mandate_refuses_and_live_one_installs(self):
        self.assert_refused(self.view['id']+1000)
        self.assert_refused(self.view['id'])
        original=decisions.get(self.db,self.view['id'])
        unsigned=dict(original,state='answered',answer=dict(option='install',text='install',channel='cli'))
        with mock.patch.object(decisions,'get',return_value=unsigned):
            self.assert_refused(self.view['id'],self.signed())
        self.sign()
        self.assert_refused(self.view['id'],self.signed(clock=lambda:self.now+601))
        self.assertTrue(self.backend.plan(self.operation,self.view['id'],authority=self.signed())['authorized'])
        self.assertEqual(self.backend.install(self.operation,self.view['id'],authority=self.signed())['outcome'],
                         'installed')
        self.assertTrue(self.path(HANDOFF_KEY).exists())

    def test_release_decision_never_authorizes_the_channels(self):
        release=backend_mod.bootstrap.SignedBootstrapAuthority
        self.sign()
        scopes=channels.scopes(self.operation,self.bundle)
        with self.assertRaises(Exception): release(self.db,self.view['id'],self.operation,self.bundle)
        # A channel decision read with another prefix is not this question.
        class Other(channels.SignedChannelAuthority): prefix=backend_mod.bootstrap.PREFIX
        other=Other(self.db,self.view['id'],self.operation,self.bundle,clock=lambda:self.now)
        self.assertFalse(other.validate(self.operation,scopes['preflight'],'preflight'))
        self.assertTrue(self.signed().validate(self.operation,scopes['preflight'],'preflight'))

    def test_install_decision_never_authorizes_revoke_and_conversely(self):
        self.sign()
        self.backend.install(self.operation,self.view['id'],authority=self.signed())
        scopes=channels.scopes(self.operation,self.bundle)
        self.assertFalse(self.signed().validate(self.operation,scopes['revoke'],'revoke'))
        with self.assertRaises(Exception): self.backend.revoke(self.operation,self.view['id'])
        self.assertTrue(self.path(HANDOFF_KEY).exists())
        text=self.backend.revoke_question(self.operation,int(self.now)+600)['question']
        view=self.ask(text,channels.REVOKE_OPTIONS); self.sign(view,'revoke')
        revoke=self.signed(view=view,revoke=True)
        for step in channels.STEPS:
            self.assertFalse(revoke.validate(self.operation,scopes[step],step),step)
        with self.assertRaises(Exception):
            self.backend.revoke(self.operation,view['id'],
                                authority=self.signed(view=view,revoke=True,clock=lambda:self.now+601))
        self.assertTrue(self.path(HANDOFF_KEY).exists())
        self.assertEqual(self.backend.revoke(self.operation,view['id'])['outcome'],'revoked')
        self.assertFalse(self.path(HANDOFF_KEY).exists()); self.assertFalse(self.path(SECRETS_KEY).exists())

    def test_bureau_mention_installs_then_revokes_the_real_channels(self):
        # #4788: channel install and revoke are the same bounded native forms
        # as decisions.match_option; the full signed text stays recorded.
        install='install — saisi par le bureau, délégation §10'
        self.sign(self.view,install)
        answer=decisions.get(self.db,self.view['id'])['answer']
        self.assertEqual((answer['text'],answer['option']),(install,'install'))
        self.assertEqual(self.backend.install(self.operation,self.view['id'],authority=self.signed())['outcome'],
                         'installed')
        self.assertTrue(self.path(HANDOFF_KEY).exists())
        text=self.backend.revoke_question(self.operation,int(self.now)+600)['question']
        view=self.ask(text,channels.REVOKE_OPTIONS)
        revoke='revoke — saisi par le bureau, délégation §10'
        self.sign(view,revoke)
        self.assertEqual((decisions.get(self.db,view['id'])['answer']['text'],
                          decisions.get(self.db,view['id'])['answer']['option']),(revoke,'revoke'))
        self.assertEqual(self.backend.revoke(self.operation,view['id'],
                                             authority=self.signed(view=view,revoke=True))['outcome'],'revoked')
        self.assertFalse(self.path(HANDOFF_KEY).exists()); self.assertFalse(self.path(SECRETS_KEY).exists())

    def test_foreign_or_free_mention_never_authorizes_the_channels(self):
        cases=('install — saisi par le bureau',
               'install, mais seulement après la revue',
               'install — Réponse de l\'owner relayée par le bureau, source x',
               'install — réponse de l\'owner relayée par le bureau, source -x')
        for text in cases:
            with self.subTest(text=text):
                view=self.ask(channels.question(self.operation,self.bundle,expires_at=int(self.now)+600),
                              channels.OPTIONS)
                self.sign(view,text)
                self.assert_refused(view['id'],self.signed(view=view))

    def test_revoked_owner_key_refuses_install_and_rollback(self):
        self.sign()
        authority.revoke_key(self.db,'fixture-owner')
        self.assert_refused(self.view['id'])


if __name__=='__main__':
    unittest.main()
