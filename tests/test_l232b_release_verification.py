# SPDX-License-Identifier: AGPL-3.0-only
"""Effective configuration verification with simulated systemd, real files."""
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock
from tests import support
from tests import test_l232b_bootstrap_files as fixture

ROOT=Path(__file__).resolve().parents[1]
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    loaded=importlib.util.module_from_spec(spec); spec.loader.exec_module(loaded)
    return loaded

config=load('release_configuration','deploy/mise-a-jour/configuration-release.py')
verification=load('release_verification','deploy/mise-a-jour/verification-release.py')


class Authority:
    allowed=True
    def validate(self,*args): return self.allowed


class Gate:
    allowed=True
    @contextmanager
    def guard(self,request):
        if not self.allowed: raise ValueError('no live fence')
        yield


class System:
    """Simulated systemd: records every fixed command, never touches the host."""
    def __init__(self):
        self.calls=[]; self.verify_code=0; self.reload_code=0; self.overrides={}
    def verify(self,files,seconds):
        self.calls.append(('verify',[str(path) for path in files],seconds)); return self.verify_code
    def reload(self,seconds):
        self.calls.append(('reload',seconds)); return self.reload_code
    def show(self,unit,properties,seconds):
        self.calls.append(('show',unit,seconds))
        values=dict(FragmentPath='',DropInPaths='',LoadState='loaded',
                    NeedDaemonReload='no',ActiveState='active')
        values.update(self.overrides.get(unit,{}))
        for name,value in properties.items(): values.setdefault(name,value)
        return values


class ReleaseVerificationTest(unittest.TestCase):
    def setUp(self):
        base=fixture.BootstrapFilesTest(); base.setUp(); self.addCleanup(base.doCleanups)
        self.base=base; self.root=base.root
        self.authority=Authority(); self.gate=Gate()
        self.destinations=dict(unit='/etc/systemd/system/fixture.service',guard='/etc/ameesh/guard.json')
        for logical in self.destinations.values():
            (self.root/logical.lstrip('/')).parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.unit=self.root/self.destinations['unit'].lstrip('/')
        self.guard=self.root/self.destinations['guard'].lstrip('/')
        self.unit.write_text('prior unit\n'); self.unit.chmod(0o640)
        self.runner='ameesh-runner-agent@fixture.service'
        self.release=dict(commit='a'*40,configuration=dict(
            unit=dict(content='[Unit]\nDescription=fixture\n',mode=0o644),
            guard=dict(content='{"guard":true}',mode=0o600)))
        for unit in (self.runner,'ameesh-guard.service'):
            path=self.root/('usr/lib/systemd/system/'+unit)
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            path.write_text('[Unit]\n'); path.chmod(0o644)
        self.manifest=dict(host='fixture-host',commit='a'*40,release=self.identity(),database='fixture-db',
            units=[self.runner],policy='b'*64,rollback_until=2000000000)
        self.files=config.ConfigurationFiles(Path(base.temp.name)/'configuration',self.destinations,
            authority=self.authority,gate=self.gate,root=self.root,clock=lambda:1900000000)
        self.card=dict(units=[self.runner],
            properties={self.runner:{'CapabilityBoundingSet':'CAP_NET_BIND_SERVICE','Slice':'ameesh-agents.slice'},
                        'ameesh-guard.service':{'NoNewPrivileges':'yes'}},
            guard='ameesh-guard.service',seconds=10)
        self.system=System()
        self.system.overrides={self.runner:{'FragmentPath':'/usr/lib/systemd/system/'+self.runner},
                               'ameesh-guard.service':{'FragmentPath':'/usr/lib/systemd/system/ameesh-guard.service'}}
        self.engine=verification.HostConfiguration(self.files,self.card,system=self.system)
        self.op='release-fixture'

    def identity(self):
        return self.release['commit']+'-'+hashlib.sha256(
            json.dumps(self.release,sort_keys=True).encode()).hexdigest()

    def request(self,step):
        return dict(operation=self.op,manifest_digest=config.protocol.identity(self.manifest),
                    action_id=self.op+':'+step,manifest=self.manifest,step=step)

    def files_effect(self,step): return self.files.perform(self.request(step),self.release)
    def perform(self,step='configure'): return self.engine.perform(self.request(step),self.release)
    def published(self):
        self.files_effect('pin'); self.files_effect('configure')

    def kinds(self): return [call[0] for call in self.system.calls]

    def test_configure_reconciles_syntax_reload_and_effective_properties(self):
        self.published()
        receipt=self.perform()
        self.assertEqual(receipt,dict(units=1,guard='ameesh-guard.service',reloaded=True,
                                      syntax_checked=1,documents_checked=1))
        self.assertEqual(self.kinds(),['verify','reload','show','show'])
        self.assertEqual(self.system.calls[0][1],[str(self.unit)])
        self.assertEqual(self.system.calls[0][2],10)
        self.assertEqual(self.system.calls[2][1],self.runner)

    def test_rollback_verifies_effective_units_and_skips_absent_documents(self):
        self.published(); self.files_effect('rollback')
        receipt=self.perform('rollback')
        self.assertEqual(receipt['syntax_checked'],1)
        self.assertEqual(receipt['documents_checked'],0)
        self.assertEqual(self.kinds(),['verify','reload','show','show'])
        self.assertFalse(self.guard.exists())

    def test_syntax_failure_is_refused_before_reload(self):
        self.published(); self.system.verify_code=1
        with self.assertRaisesRegex(verification.VerificationError,'syntax'): self.perform()
        self.assertEqual(self.kinds(),['verify'])

    def test_reload_failure_is_refused_before_effective_checks(self):
        self.published(); self.system.reload_code=1
        with self.assertRaisesRegex(verification.VerificationError,'reload'): self.perform()
        self.assertEqual(self.kinds(),['verify','reload'])

    def test_unreconciled_daemon_reload_is_refused(self):
        self.published(); self.system.overrides[self.runner]['NeedDaemonReload']='yes'
        with self.assertRaisesRegex(verification.VerificationError,'reconciled'): self.perform()

    def test_unsafe_or_unknown_fragment_path_is_refused(self):
        self.published()
        evil=self.root/'home/evil'; evil.mkdir(mode=0o700,parents=True); evil.chmod(0o777)
        (evil/'fixture.service').write_text('[Unit]\n'); (evil/'fixture.service').chmod(0o644)
        self.system.overrides[self.runner]['FragmentPath']='/home/evil/fixture.service'
        with self.assertRaisesRegex(verification.VerificationError,'unsafe|unavailable'): self.perform()

    def test_unexpected_dropin_directory_is_refused(self):
        self.published()
        dropin=self.root/'etc/systemd/system/other.service.d/x.conf'
        dropin.parent.mkdir(mode=0o700,parents=True); dropin.write_text('[Service]\n'); dropin.chmod(0o644)
        self.system.overrides[self.runner]['DropInPaths']='/etc/systemd/system/other.service.d/x.conf'
        with self.assertRaisesRegex(verification.VerificationError,'drop-in'): self.perform()

    def test_declared_capability_mismatch_is_refused(self):
        self.published()
        self.system.overrides[self.runner]['CapabilityBoundingSet']='CAP_SYS_ADMIN'
        with self.assertRaisesRegex(verification.VerificationError,'declared'): self.perform()

    def test_inactive_guard_is_refused(self):
        self.published(); self.system.overrides['ameesh-guard.service']['ActiveState']='inactive'
        with self.assertRaisesRegex(verification.VerificationError,'guard'): self.perform()

    def test_manifest_unit_outside_enrolled_card_is_refused(self):
        self.published(); self.manifest['units']=['other@fixture.service']
        with self.assertRaisesRegex(verification.VerificationError,'host card'): self.perform()
        self.assertEqual(self.kinds(),[])

    def test_invalid_json_document_is_refused(self):
        self.release['configuration']['guard']['content']='{not json'
        self.manifest['release']=self.identity()
        self.published()
        with self.assertRaisesRegex(verification.VerificationError,'JSON'): self.perform()

    def test_configure_requires_published_unit_bytes(self):
        self.published(); self.unit.unlink()
        with self.assertRaisesRegex(verification.VerificationError,'missing'): self.perform()
        self.assertEqual(self.kinds(),[])

    def test_changed_unit_bytes_after_publication_are_refused(self):
        self.published(); self.unit.write_text('external change\n'); self.unit.chmod(0o644)
        with self.assertRaisesRegex(verification.VerificationError,'changed'): self.perform()

    def test_authority_is_rechecked_after_the_slow_reload(self):
        self.published()
        def expiring(seconds): self.authority.allowed=False; return 0
        self.system.reload=expiring
        with self.assertRaises(config.ConfigurationError): self.perform()
        self.assertEqual(self.kinds(),['verify'])

    def test_enrolled_destination_must_be_the_effective_fragment(self):
        self.published()
        other=self.root/'usr/lib/systemd/system/fixture.service'
        other.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        other.write_text('[Unit]\n'); other.chmod(0o644)
        card=dict(self.card,units=['fixture.service'],
                  properties={'fixture.service':{'Slice':'ameesh-agents.slice'}})
        engine=verification.HostConfiguration(self.files,card,system=self.system)
        self.manifest['units']=['fixture.service']
        self.system.overrides['fixture.service']={'FragmentPath':'/usr/lib/systemd/system/fixture.service'}
        with self.assertRaisesRegex(verification.VerificationError,'effective fragment'):
            engine.perform(self.request('configure'),self.release)
        self.system.overrides['fixture.service']['FragmentPath']='/etc/systemd/system/fixture.service'
        self.assertEqual(engine.perform(self.request('configure'),self.release)['units'],1)

    def test_unknown_card_fields_are_refused_without_any_system_command(self):
        bad=[dict(self.card,extra=True),dict(self.card,seconds=4),dict(self.card,seconds=True),
             dict(self.card,units=['not-a-unit']),dict(self.card,guard='guard'),
             dict(self.card,properties={'unknown.service':{'Slice':'x.slice'}}),
             dict(self.card,properties={self.runner:{'NotAProperty':'yes'}}),
             dict(self.card,properties={self.runner:{'Slice':'/etc/passwd'}})]
        for card in bad:
            with self.subTest(card=card):
                with self.assertRaises(verification.VerificationError):
                    verification.HostConfiguration(self.files,card,system=self.system)
        self.assertEqual(self.kinds(),[])

    def test_real_adapter_uses_fixed_commands_and_scrubbed_environment(self):
        binary=Path(self.base.temp.name)/'bin'; binary.mkdir(mode=0o700)
        record=binary/'record'
        body=('#!/bin/sh\nprintf "%s|%s|%s\\n" "$PATH" "$LANG" "$LC_ALL" > "'+str(record)+'"\n'
              'for arg do printf "%s\\n" "$arg" >> "'+str(record)+'"; done\n'
              'printf "FragmentPath=\\nDropInPaths=\\nLoadState=loaded\\nNeedDaemonReload=no\\nActiveState=active\\n"\n')
        for name in ('systemctl','systemd-analyze'):
            path=binary/name; path.write_text(body); path.chmod(0o755)
        adapter=verification.Systemd(systemctl=str(binary/'systemctl'),analyze=str(binary/'systemd-analyze'))
        self.assertEqual(adapter.reload(7),0)
        lines=(binary/'record').read_text().splitlines()
        self.assertEqual(lines[0],'/usr/bin:/bin|C|C')
        self.assertEqual(lines[1:],['daemon-reload'])
        self.assertEqual(adapter.verify(['/etc/systemd/system/x.service'],7),0)
        lines=(binary/'record').read_text().splitlines()
        self.assertEqual(lines[0],'/usr/bin:/bin|C|C')
        self.assertEqual(lines[1:],['verify','/etc/systemd/system/x.service'])
        shown=adapter.show('x.service',{'NoNewPrivileges':'yes'},7)
        self.assertEqual(shown['LoadState'],'loaded')
        self.assertIn('-p',(binary/'record').read_text().splitlines())

    def test_oversized_or_unavailable_system_output_is_generic(self):
        binary=Path(self.base.temp.name)/'bin'; binary.mkdir(mode=0o700)
        noisy=binary/'systemd-analyze'
        noisy.write_text('#!/bin/sh\nprintf "PRIVATE-CANARY"\nfor i in $(seq 1 5000); do printf "0123456789abcdef"; done\n')
        noisy.chmod(0o755)
        adapter=verification.Systemd(analyze=str(noisy))
        with self.assertRaises(verification.VerificationError) as caught:
            adapter.verify(['/etc/systemd/system/x.service'],7)
        self.assertNotIn('canary',str(caught.exception).lower())
        broken=verification.Systemd(analyze=str(binary/'absent'))
        with self.assertRaises(verification.VerificationError): broken.verify(['/etc/systemd/system/x.service'],7)


if __name__=='__main__':
    unittest.main()
