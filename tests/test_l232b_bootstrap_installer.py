# SPDX-License-Identifier: AGPL-3.0-only
"""Bootstrap fixture filesystem is real; account/service effects are simulated."""
from tests import support
from tests import test_l232b_bootstrap_files as file_fixture
from tests import test_l232b_account as account_fixture
import importlib.util
import os
from pathlib import Path
import stat
import unittest
from unittest import mock

SPEC=importlib.util.spec_from_file_location('bootstrap_installer',
    Path(__file__).resolve().parents[1]/'deploy/mise-a-jour/installateur-bootstrap.py')
installer=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(installer)


class Host(account_fixture.Accounts):
    def __init__(self,root):
        super().__init__(); self.root=root; self.is_ready=True; self.reloads=0
    def preflight(self,bundle): return self.machine()==bundle['machine_id']
    def layout(self,bundle):
        for logical,mode in installer.DIRECTORIES.items():
            target=self.root/logical.lstrip('/')
            target.mkdir(parents=True,mode=mode,exist_ok=True)
        return True
    def validate_configuration(self,bundle): return True
    def validate_restored_configuration(self,bundle): return True
    def ready(self,bundle): return self.is_ready
    def activate_ssh_configuration(self,bundle): self.reloads+=1; return True


class InstallerTest(unittest.TestCase):
    def setUp(self):
        base=file_fixture.BootstrapFilesTest(); base.setUp()
        self.addCleanup(base.doCleanups)
        def writable():
            for directory,dirs,names in os.walk(base.root): Path(directory).chmod(0o700)
        self.addCleanup(writable)
        self.root=base.root; self.operation='bootstrap-fixture'
        stage=self.root/('var/lib/ameesh-release-bootstrap/'+self.operation)
        (stage/'runtime/bin').mkdir(parents=True,mode=0o700)
        program=stage/'runtime/bin/python'; program.write_bytes(b'synthetic executable, never launched'); program.chmod(0o700)
        (stage/'sources').mkdir(mode=0o700)
        (stage/'sources/hote-release.py').write_text('# synthetic pinned source\n')
        plan=dict(base.bundle['plan'],runtime_tree_sha256=installer.runtime.inventory(stage/'runtime')['digest'],
                  source_tree_sha256=installer.runtime.inventory(stage/'sources')['digest'])
        self.bundle=installer.bootstrap.delegation.render(plan,account_ids=dict(uid=987,gid=987))
        self.authority=base.authority; self.system=Host(self.root)
        self.adapter=installer.BootstrapHostAdapter(Path(base.temp.name)/'effects',authority=self.authority,
                                                   system=self.system,root=self.root)
        self.journal=Path(base.temp.name)/'controller'
    def run_bootstrap(self):
        return installer.bootstrap.run(self.journal,self.operation,self.bundle,authority=self.authority,adapter=self.adapter)

    def test_real_tree_and_file_publication_with_simulated_account_and_reload(self):
        self.assertEqual(self.run_bootstrap(),'installed')
        self.assertEqual(self.system.calls,['group','user'])
        self.assertEqual(self.system.reloads,1)
        self.assertEqual(installer.runtime.inventory(self.root/'opt/ameesh-release/runtime')['digest'],self.bundle['plan']['runtime_tree_sha256'])
        self.assertTrue((self.root/'etc/ameesh-release/authorized_keys').exists())
        self.assertEqual(self.run_bootstrap(),'installed')
        self.assertEqual(self.system.calls,['group','user']); self.assertEqual(self.system.reloads,1)

    def test_missing_independent_service_holds_key_and_resumes_without_recopied_runtime(self):
        self.system.is_ready=False
        with self.assertRaisesRegex(installer.bootstrap.BootstrapError,'uncertain'): self.run_bootstrap()
        program=self.root/'opt/ameesh-release/runtime/bin/python'; inode=program.stat().st_ino
        self.assertFalse((self.root/'etc/ameesh-release/authorized_keys').exists())
        self.assertEqual(self.system.reloads,0)
        self.system.is_ready=True
        self.assertEqual(self.run_bootstrap(),'installed')
        self.assertEqual(program.stat().st_ino,inode)
        self.assertEqual(self.system.calls,['group','user'])

    def test_signed_revoke_removes_access_and_preserves_runtime_account_and_journals(self):
        self.run_bootstrap()
        request=dict(operation=self.operation,step='revoke',action_id=self.operation+':revoke',bundle=self.bundle,
            scope_digest=installer.bootstrap.scopes(self.operation,self.bundle)['revoke'])
        self.assertTrue(self.adapter.perform('revoke',request)['ok'])
        self.assertFalse((self.root/'etc/ameesh-release/authorized_keys').exists())
        self.assertTrue((self.root/'opt/ameesh-release/runtime/bin/python').exists())
        self.assertIsNotNone(self.system.account_value)
        self.assertTrue(self.journal.exists())

    def test_linux_readiness_absent_or_mismatched_is_false_without_actual_system_calls(self):
        with mock.patch.object(installer.account.os,'geteuid',return_value=0): host=installer.LinuxHost()
        with mock.patch.object(installer.runtime,'inventory',side_effect=OSError('fixture missing runtime')):
            self.assertFalse(host.ready(self.bundle))

    def test_private_roots_read_by_the_adapter_are_in_the_reviewed_layout(self):
        # The adapter reads the staged trees from this exact root (filled by the
        # administrator before preflight runs), private at 0700; the pinned
        # factory traverses its versioned release store at 0711 (not listable,
        # not writable) to run the active release's own runner. Both belong to
        # the signed inventory, never to ad hoc paths.
        self.assertEqual(installer.DIRECTORIES.get('/var/lib/ameesh-release-bootstrap'), 0o700)
        self.assertEqual(installer.DIRECTORIES.get('/opt/ameesh-release/store'), 0o711)
        self.assertNotIn('/var/lib/ameesh-release/bootstrap', installer.DIRECTORIES)
        self.assertEqual(self.bundle['directories']['/var/lib/ameesh-release-bootstrap'], 0o700)
        self.assertEqual(self.bundle['directories']['/opt/ameesh-release/store'], 0o711)
        stage = self.root/'var/lib/ameesh-release-bootstrap'/self.operation
        self.assertTrue((stage/'runtime').is_dir())
        self.assertTrue((stage/'sources').is_dir())
        (self.root/'var/lib/ameesh-release-bootstrap').chmod(0o700)
        self.assertEqual(self.run_bootstrap(), 'installed')
        for logical,mode in (('/var/lib/ameesh-release-bootstrap',0o700),
                             ('/opt/ameesh-release/store',0o711)):
            root = self.root/logical.lstrip('/')
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), mode, logical)
            self.assertEqual(root.stat().st_uid, os.geteuid(), logical)


    def test_real_layout_publishes_reviewed_modes_under_a_restrictive_umask(self):
        # L1631: the production layout, not only the fixture above, must publish
        # the reviewed modes under a restrictive umask. mkdir(2) masks the mode,
        # so a fresh install under umask 077 used to fail the private-root check
        # on the tree it had just created; an existing directory is never
        # repaired, even a more restrictive one.
        sandbox=Path(self.root)/'sandbox'; sandbox.mkdir(mode=0o700)
        existing=sandbox/'usr/local/libexec'; existing.mkdir(parents=True,mode=0o700)
        real_stat=Path.stat
        def root_owned(path,*,follow_symlinks=True):
            # The sandbox is not root-owned; only the ownership check is faked.
            info=real_stat(path,follow_symlinks=follow_symlinks)
            fields=list(info); fields[4]=0
            return os.stat_result(fields)
        def sandboxed(logical):
            text=str(logical)
            return Path(str(sandbox)+text if text.startswith('/') else text)
        previous=os.umask(0o077)
        try:
            with mock.patch.object(installer.account.os,'geteuid',return_value=0):
                host=installer.LinuxHost()
            with mock.patch.object(installer,'Path',side_effect=sandboxed), \
                    mock.patch.object(Path,'stat',root_owned), \
                    mock.patch.object(installer.runtime,'trusted',
                                      side_effect=lambda path: Path(path)):
                self.assertTrue(host.layout(self.bundle))
        finally:
            os.umask(previous)
        for logical,mode in installer.DIRECTORIES.items():
            created=sandbox/logical.lstrip('/')
            expected=0o700 if created==existing else mode
            self.assertEqual(stat.S_IMODE(created.stat().st_mode),expected,logical)

    def test_lost_activation_reply_rechecks_configuration_and_never_recreates_account(self):
        original=self.system.activate_ssh_configuration; lost=[False]
        def lose(bundle):
            result=original(bundle)
            if not lost[0]: lost[0]=True; raise OSError('lost SSH daemon reload response')
            return result
        self.system.activate_ssh_configuration=lose
        with self.assertRaises(installer.bootstrap.BootstrapError): self.run_bootstrap()
        self.assertFalse((self.root/'etc/ameesh-release/authorized_keys').exists())
        self.assertEqual(self.run_bootstrap(),'installed')
        self.assertEqual(self.system.calls,['group','user'])
        self.assertEqual(self.system.reloads,2)
