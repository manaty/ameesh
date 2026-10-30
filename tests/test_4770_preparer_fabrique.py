# SPDX-License-Identifier: AGPL-3.0-only
"""Factory documents generated offline and read by the actual enrolled reader."""
from tests import support
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prepare_factory', ROOT / 'deploy/mise-a-jour/preparer-handoff.py')
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)
factory = prepare.module('fabrique-handoff')


class FactoryPreparationTest(unittest.TestCase):
    def setUp(self):
        if os.getuid() == 0:
            self.skipTest('non-root preparation fixture required')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'documents'
        patch = mock.patch.object(prepare.runtime, 'trusted', side_effect=lambda path: Path(path))
        patch.start()
        self.addCleanup(patch.stop)
        endpoint = dict(host='second-host', address='192.0.2.2', host_key_alias='second-host',
            user='ameesh-handoff', port=22, seconds=30,
            known_hosts='/etc/ameesh-handoff/known_hosts', known_hosts_sha256='a'*64,
            identity_file='/etc/ameesh-handoff/identity-second')
        self.request = dict(version=1, documents=dict(
            enrollment=dict(version=1, host='first-host'),
            hosts=dict(hosts={host: dict(deepseek=dict(accounts=['ambient'], tools=['git'], secrets=['api']))
                              for host in ('first-host', 'second-host')}),
            repositories=dict(repositories=dict(project='https://forge.example/acme/project.git')),
            secrets=dict(references=dict(api='provider.env#API_TOKEN')),
            transports=dict(transports={'second-host': endpoint})))

    def test_existing_reader_accepts_all_five_documents_without_opening_credentials(self):
        # These files intentionally do not exist: generation validates reference
        # grammar only and must not read credentials or open an SSH session.
        manifest = prepare.assemble_factory(self.request, self.output)
        reader = factory.Documents(self.output)
        for name, fields in factory.DOCUMENTS.items():
            path = self.output / (name + '.json')
            self.assertEqual(reader.json(name, exact=fields), self.request['documents'][name])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), self.request['documents'][name])
        self.assertEqual(len(manifest['files']), 5)
        self.assertEqual(manifest['host'], 'first-host')
        self.assertEqual(manifest['digest'], prepare.seal.identity({k:v for k,v in manifest.items() if k != 'digest'}))
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(FileExistsError):
            prepare.assemble_factory(self.request, self.output)

    def test_unknown_fields_are_refused_at_each_existing_document_boundary(self):
        paths = [(), ('documents',)] + [('documents', name) for name in factory.DOCUMENTS]
        paths += [('documents', 'hosts', 'hosts', 'first-host', 'deepseek'),
                  ('documents', 'transports', 'transports', 'second-host')]
        for path in paths:
            changed = copy.deepcopy(self.request)
            cursor = changed
            for key in path:
                cursor = cursor[key]
            cursor['unknown'] = 'not accepted'
            with self.subTest(path=path), self.assertRaises(ValueError):
                prepare.assemble_factory(changed, self.output)
            self.assertFalse(self.output.exists())

    def test_missing_host_transport_or_secret_reference_refuses_before_writing(self):
        cases = []
        changed = copy.deepcopy(self.request)
        changed['documents']['enrollment']['host'] = 'not-admitted'
        cases.append(changed)
        changed = copy.deepcopy(self.request)
        changed['documents']['transports']['transports'] = {}
        cases.append(changed)
        changed = copy.deepcopy(self.request)
        changed['documents']['secrets']['references'] = {}
        cases.append(changed)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare.assemble_factory(value, self.output)
            self.assertFalse(self.output.exists())

    def test_credentials_and_unenrolled_transport_paths_are_not_accepted_as_references(self):
        for value in ('raw-secret-value', 'provider.env#KEY#EXTRA', '../provider.env#KEY'):
            changed = copy.deepcopy(self.request)
            changed['documents']['secrets']['references']['api'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                prepare.assemble_factory(changed, self.output)
        for field, value in (('identity_file', '/home/runner/private-key'),
                             ('known_hosts', '/etc/ameesh-handoff/../known_hosts'),
                             ('user', 'root'), ('host', 'another-host')):
            changed = copy.deepcopy(self.request)
            changed['documents']['transports']['transports']['second-host'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                prepare.assemble_factory(changed, self.output)
        changed = copy.deepcopy(self.request)
        changed['documents']['repositories']['repositories']['project'] = 'https://TOKEN@forge.example/acme/repo.git'
        with self.assertRaises(ValueError):
            prepare.assemble_factory(changed, self.output)
        self.assertFalse(self.output.exists())

    def test_cli_uses_private_inputs_and_no_database(self):
        source = self.root / 'request.json'
        source.write_text(json.dumps(self.request))
        source.chmod(0o600)
        with mock.patch('ameesh.db.connect', side_effect=AssertionError('no database required')):
            with mock.patch('builtins.print'):
                self.assertEqual(prepare.main(['fabrique', str(source), str(self.output)]), 0)
        second = self.root / 'second'
        source.chmod(0o644)
        with mock.patch('builtins.print'):
            self.assertEqual(prepare.main(['fabrique', str(source), str(second)]), 2)
        self.assertFalse(second.exists())

    def test_output_symlink_and_consumer_rejection_never_reuse_partial_output(self):
        self.output.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(FileExistsError):
            prepare.assemble_factory(self.request, self.output)
        self.output.unlink()
        # Force a real consumer limit failure after output creation by reducing
        # its trusted-file read budget; cleanup removes only our new directory.
        with mock.patch.object(prepare, 'module', return_value=factory):
            with mock.patch.object(factory.Documents, 'json', side_effect=factory.FactoryError('document:fixture')):
                with self.assertRaises(ValueError):
                    prepare.assemble_factory(self.request, self.output)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
