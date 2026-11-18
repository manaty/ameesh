# SPDX-License-Identifier: AGPL-3.0-only
"""Systemd's tmpfs credential ACL does not weaken enrollment validation."""
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ameesh import vm_adapter as vm


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "credential ACL is read with os.getxattr (Linux)")
class CredentialTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'scaleway.json'
        self.path.write_text('{"secret_key":"test-only"}')
        self.path.chmod(0o400)
        self.metadata = dict(st_mode=stat.S_IFREG | 0o440, st_uid=0,
                             st_nlink=1, st_size=self.path.stat().st_size)
        self.acl_entries = [(1, 4, 0xffffffff), (2, 4, 1234),
                            (4, 0, 0xffffffff), (16, 4, 0xffffffff),
                            (32, 0, 0xffffffff)]
        self.enterContext(patch.dict(os.environ, CREDENTIALS_DIRECTORY=self.temp.name))
        self.enterContext(patch.object(vm.os, 'geteuid', return_value=1234))

    def read(self, **changes):
        metadata = self.metadata | changes
        acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *e)
                                            for e in self.acl_entries)
        with patch.object(vm.os, 'fstat', return_value=SimpleNamespace(**metadata)), \
                patch.object(vm.os, 'getxattr', return_value=acl):
            return vm.credential_json(self.path)

    def test_euid_private_ramfs_copy(self):
        self.assertEqual(self.read(st_uid=1234, st_mode=stat.S_IFREG | 0o400),
                         {'secret_key': 'test-only'})

    def test_root_acl_tmpfs_copy(self):
        self.assertEqual(self.read(), {'secret_key': 'test-only'})

    def test_refused_metadata(self):
        for changes in ({'st_mode': stat.S_IFREG | 0o444},
                        {'st_mode': stat.S_IFREG | 0o460},
                        {'st_mode': stat.S_IFREG | 0o450},
                        {'st_mode': stat.S_IFIFO | 0o440},
                        {'st_nlink': 2}, {'st_size': 65537}, {'st_uid': 2345},
                        {'st_uid': 1234}):
            with self.subTest(changes=changes), self.assertRaises(vm.Refused):
                self.read(**changes)

    def test_root_outside_credential_directory(self):
        with patch.dict(os.environ, CREDENTIALS_DIRECTORY=self.temp.name + '/other'):
            with self.assertRaisesRegex(vm.Refused, 'directly under'):
                self.read()

    def test_only_scaleway_name(self):
        with self.assertRaisesRegex(vm.Refused, 'directly under'):
            vm.credential_json(self.path.with_name('enrollment.json'))

    def test_symlink_is_not_followed(self):
        target = self.path.with_name('target')
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            self.read()

    def test_open_keeps_nofollow_and_nonblock(self):
        original = os.open
        with patch.object(vm.os, 'open', wraps=original) as opened:
            self.read()
        self.assertEqual(opened.call_args.args[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    def test_enrollment_remains_strict(self):
        with patch.object(vm.os, 'fstat', return_value=SimpleNamespace(**self.metadata)):
            with self.assertRaisesRegex(vm.Refused, 'uid must match'):
                vm.private_json(self.path)

    def test_acl_rejects_other_readers_and_malformed_data(self):
        for index, entry in [(1, (2, 4, 2345)), (2, (4, 4, 0xffffffff)),
                             (1, (2, 6, 1234)), (4, (32, 4, 0xffffffff))]:
            with self.subTest(entry=entry):
                original = self.acl_entries[index]
                self.acl_entries[index] = entry
                with self.assertRaisesRegex(vm.Refused, 'ACL'):
                    self.read()
                self.acl_entries[index] = original
        self.acl_entries.append((2, 4, 2345))
        with self.assertRaisesRegex(vm.Refused, 'ACL'):
            self.read()

    def test_missing_acl_refused(self):
        with patch.object(vm.os, 'fstat', return_value=SimpleNamespace(**self.metadata)), \
                patch.object(vm.os, 'getxattr', side_effect=OSError):
            with self.assertRaisesRegex(vm.Refused, 'ACL required'):
                vm.credential_json(self.path)
