# SPDX-License-Identifier: AGPL-3.0-only
"""L232c tranche 2 : intention durable, transport SSH du canal secrets, dispatcher, délégation.

Aucun SSH, aucun root, aucun réseau : le pair SSH est simulé par l'Installeur
local, et la délégation n'est qu'un rendu.
"""
from tests import support  # environnement isolé
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_l232c_coffre import (CANARY, GROUPE_ROOT, HOST, REFERENCE, UTILISATEUR,
                                     Materiel, Transport, coffre)

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy/mise-a-jour'


def charger(nom, label):
    spec = importlib.util.spec_from_file_location(label, DEPLOY / nom)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


transport = charger('transport-secrets.py', 'l232c_transport')
delegation = charger('delegation-secrets.py', 'l232c_delegation')
restart_delegation = charger('delegation-release.py', 'l232c_restart_delegation')
HOTE = DEPLOY / 'hote-secrets.py'


class Perte(Transport):
    """Le helper agit, puis la réponse se perd (une fois)."""

    def __init__(self, installeur, pertes=1):
        super().__init__(installeur)
        self.pertes = pertes

    def exchange(self, payload, timeout):
        receipt = super().exchange(payload, timeout)
        if self.pertes:
            self.pertes -= 1
            raise OSError('réponse perdue')
        return receipt


class IntentionTest(Materiel):
    def setUp(self):
        super().setUp()
        self.journal = coffre.Journal(self.root / 'journal')

    def livraison(self, transport_, **kw):
        return coffre.Livraison(self.policy, transport_, self.journal, **kw)

    def intentions(self):
        return sorted((self.root / 'journal' / 'intents').glob('*.json'))

    def test_reponse_perdue_rejouee_a_la_meme_version(self):
        perte = Perte(self.installeur)
        livraison = self.livraison(perte)
        with self.assertRaises(coffre.Uncertain):
            livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(self.key.read_bytes(), CANARY)
        [intention] = self.intentions()
        self.assertEqual(stat.S_IMODE(intention.stat().st_mode), 0o600)
        self.assertNotIn(CANARY, intention.read_bytes())
        receipt = livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertTrue(receipt['ok'], receipt)
        self.assertTrue(receipt['replayed'])
        versions = {coffre.unframe(p)[0]['version'] for p in perte.payloads}
        self.assertEqual(len(versions), 1)
        self.assertEqual(self.intentions(), [])
        categories = [json.loads(l)['category'] for l in
                      (self.root / 'journal' / 'rotations.jsonl').read_text().splitlines()]
        self.assertEqual(categories, ['intent', 'transport', 'retry', 'installed'])
        # soldée : une nouvelle livraison tire une autre version, refusée juste.
        receipt = livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(receipt['category'], 'rotation_required')

    def test_envoi_jamais_parti_installe_au_rejeu(self):
        class Coupure:
            def exchange(self, payload, timeout):
                raise OSError('connexion refusée')

        with self.assertRaises(coffre.Uncertain):
            self.livraison(Coupure()).deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertFalse(self.key.exists())
        receipt = self.livraison(Transport(self.installeur)).deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertTrue(receipt['ok'])
        self.assertFalse(receipt['replayed'])

    def test_livraisons_concurrentes_ne_perdent_pas_l_intention(self):
        perte = Perte(self.installeur)
        first = self.livraison(perte)
        second = self.livraison(Transport(self.installeur))
        exchange = perte.exchange

        def interleave(payload, timeout):
            with self.assertRaises(coffre.Refused) as caught:
                second.deliver(REFERENCE, HOST, UTILISATEUR)
            self.assertEqual(caught.exception.category, 'delivery_busy')
            return exchange(payload, timeout)

        with mock.patch.object(perte, 'exchange', side_effect=interleave):
            with self.assertRaises(coffre.Uncertain):
                first.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(len(self.intentions()), 1)
        self.assertTrue(second.deliver(REFERENCE, HOST, UTILISATEUR)['replayed'])
        self.assertEqual(coffre.unframe(perte.payloads[0])[0]['version'],
                         coffre.unframe(second.transport.payloads[0])[0]['version'])

    def test_autre_operation_refusee_tant_qu_une_intention_court(self):
        with self.assertRaises(coffre.Uncertain):
            self.livraison(Perte(self.installeur)).deliver(REFERENCE, HOST, UTILISATEUR)
        livraison = self.livraison(Transport(self.installeur), boundary=lambda h, a: True)
        with self.assertRaises(coffre.Refused) as caught:
            livraison.deliver(REFERENCE, HOST, UTILISATEUR, operation='rotate')
        self.assertEqual(caught.exception.category, 'pending_delivery')

    def test_rejeu_de_rotation_tient_la_frontiere(self):
        self.livraison(Transport(self.installeur)).deliver(REFERENCE, HOST, UTILISATEUR)
        libre = {'ok': True}
        livraison = self.livraison(Perte(self.installeur),
                                   boundary=lambda h, a: libre['ok'])
        with self.assertRaises(coffre.Uncertain):
            livraison.deliver(REFERENCE, HOST, UTILISATEUR, operation='rotate')
        libre['ok'] = False
        with self.assertRaises(coffre.Refused) as caught:
            livraison.deliver(REFERENCE, HOST, UTILISATEUR, operation='rotate')
        self.assertEqual(caught.exception.category, 'busy')
        libre['ok'] = True
        receipt = livraison.deliver(REFERENCE, HOST, UTILISATEUR, operation='rotate')
        self.assertTrue(receipt['replayed'])

    def test_recu_ambigu_garde_l_intention(self):
        class Tronque:
            def exchange(self, payload, timeout):
                return b'{"protocol":"ameesh-secret/1"}'

        with self.assertRaises(coffre.Uncertain):
            self.livraison(Tronque()).deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(len(self.intentions()), 1)

    def test_intention_alteree_refusee(self):
        with self.assertRaises(coffre.Uncertain):
            self.livraison(Perte(self.installeur)).deliver(REFERENCE, HOST, UTILISATEUR)
        [intention] = self.intentions()
        entry = json.loads(intention.read_text())
        entry['host'] = 'autre'
        intention.write_text(json.dumps(entry))
        with self.assertRaises(coffre.Refused) as caught:
            self.livraison(Transport(self.installeur)).deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(caught.exception.category, 'intent_record')

    def test_parent_des_intentions_refuse_avant_ecriture_et_transport(self):
        for kind in ('public', 'link', 'dangling', 'file', 'owner'):
            with self.subTest(kind=kind):
                base = self.root / kind
                base.mkdir(mode=0o700)
                parent = base / 'journal'
                elsewhere = base / 'elsewhere'
                elsewhere.mkdir(mode=0o700)
                if kind == 'public':
                    parent.mkdir(mode=0o755)
                    parent.chmod(0o755)
                elif kind in ('link', 'dangling'):
                    parent.symlink_to(elsewhere if kind == 'link' else base / 'absent')
                elif kind == 'file':
                    parent.write_text('ordinary file')
                else:
                    parent.mkdir(mode=0o700)
                journal = coffre.Journal(parent, uid=os.geteuid() + 1 if kind == 'owner'
                                        else os.geteuid())
                peer = Transport(self.installeur)
                with self.assertRaises(coffre.Refused) as caught:
                    coffre.Livraison(self.policy, peer, journal).deliver(
                        REFERENCE, HOST, UTILISATEUR)
                self.assertEqual(caught.exception.category, 'intent_root')
                self.assertEqual(peer.payloads, [])
                self.assertFalse(self.key.exists())
                self.assertFalse((parent / 'intents').exists())
                self.assertEqual(list(elsewhere.iterdir()), [])

    def test_lien_mourant_des_intentions_refuse_explicitement(self):
        self.journal.root.mkdir(mode=0o700)
        (self.journal.root / 'intents').symlink_to(self.root / 'absent')
        peer = Transport(self.installeur)
        with self.assertRaises(coffre.Refused) as caught:
            self.livraison(peer).deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertEqual(caught.exception.category, 'intent_root')
        self.assertEqual(peer.payloads, [])
        self.assertFalse((self.journal.root / 'rotations.jsonl').exists())

    def test_groupe_root_refuse_au_pont(self):
        document = self.destination_document(group=GROUPE_ROOT)
        helper = dict(self.helper_document, destinations=[document])
        installeur = coffre.Installeur(helper, read_as=None, uid=0, isolate=True)
        payload, _ = self.trame(destination=document)
        receipt = json.loads(installeur.handle(payload))
        self.assertEqual(receipt['category'], 'destination_bridge')


class TransportSecretsTest(Materiel):
    def setUp(self):
        super().setUp()
        root = Path(self.temp.name)
        # Même modèle que tests/test_l232b_ssh_transport : ancêtres d'un /tmp
        # partagé considérés sûrs, sans affaiblir le contrôle des fichiers.
        original_lstat = Path.lstat
        ancestors = set(root.parents)

        def enrolled_lstat(path):
            info = original_lstat(path)
            if path in ancestors and stat.S_ISDIR(info.st_mode):
                fields = list(info)
                fields[0] &= ~0o022
                fields[4] = os.geteuid()
                return os.stat_result(fields)
            return info

        patch = mock.patch.object(Path, 'lstat', enrolled_lstat)
        patch.start()
        self.addCleanup(patch.stop)
        self.hosts = root / 'known_hosts'
        self.identity = root / 'identity'
        self.hosts.write_text('hote-test ssh-ed25519 synthetic-public-key\n')
        self.identity.write_text('synthetic-private-key-canary')
        self.hosts.chmod(0o600)
        self.identity.chmod(0o600)
        self.enrollment = dict(host=HOST, address='host.example.test', port=2222,
                               user='ameesh-secrets', host_key_alias=HOST,
                               known_hosts=str(self.hosts), identity_file=str(self.identity),
                               known_hosts_sha256=hashlib.sha256(self.hosts.read_bytes()).hexdigest(),
                               seconds=20)

    def test_compte_dedie_exige(self):
        with self.assertRaises(transport.TransportError):
            transport.SSHSecretTransport(dict(self.enrollment, user='ameesh-release'))

    def test_trame_par_stdin_seulement(self):
        engine = transport.SSHSecretTransport(self.enrollment)
        payload, _ = self.trame()
        with mock.patch.object(transport.release, 'exchange', return_value=b'{}') as call:
            self.assertEqual(engine.exchange(payload, 30), b'{}')
        args, sent, seconds = call.call_args.args
        self.assertEqual(sent, payload)
        self.assertEqual(seconds, 20)
        self.assertNotIn(CANARY.decode(), repr(args))
        self.assertNotIn('synthetic-private-key-canary', repr(args))
        self.assertEqual(args[args.index('-l') + 1], 'ameesh-secrets')
        self.assertIn('ProxyCommand=none', args)

    def test_cle_d_hote_changee_refusee_avant_connexion(self):
        engine = transport.SSHSecretTransport(self.enrollment)
        self.hosts.write_text('autre clé')
        with mock.patch.object(transport.release, 'exchange') as call:
            with self.assertRaises(transport.TransportError):
                engine.exchange(self.trame()[0], 30)
            call.assert_not_called()

    def test_livraison_de_bout_en_bout_avec_reponse_perdue(self):
        perdues = [1]

        def pair(args, payload, seconds):
            receipt = self.installeur.handle(payload)
            if perdues[0]:
                perdues[0] -= 1
                raise transport.TransportError('SSH action unavailable')
            return receipt

        journal = coffre.Journal(self.root / 'journal')
        livraison = coffre.Livraison(self.policy, transport.SSHSecretTransport(self.enrollment),
                                     journal)
        with mock.patch.object(transport.release, 'exchange', side_effect=pair):
            with self.assertRaises(coffre.Uncertain) as caught:
                livraison.deliver(REFERENCE, HOST, UTILISATEUR)
            self.assertEqual(caught.exception.category, 'transport')
            receipt = livraison.deliver(REFERENCE, HOST, UTILISATEUR)
        self.assertTrue(receipt['replayed'])
        self.assertEqual(self.key.read_bytes(), CANARY)
        self.assertNotIn(CANARY, (self.root / 'journal' / 'rotations.jsonl').read_bytes())


class DispatcherTest(unittest.TestCase):
    def test_modules_controles_avant_execution(self):
        dispatcher = charger('hote-secrets.py', 'l2034_dispatcher')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lib = root / 'lib'
            lib.mkdir(mode=0o700)
            marker = root / 'executed'
            source = lib / 'hote-release.py'
            source.write_text('from pathlib import Path\n'
                              'Path(%r).write_text("executed")\n' % str(marker))
            source.chmod(0o600)
            original_lstat = Path.lstat

            for kind in ('file_mode', 'file_owner', 'parent_mode', 'parent_owner',
                         'link', 'dangling', 'parent_link', 'trusted'):
                with self.subTest(kind=kind):
                    marker.unlink(missing_ok=True)
                    def enrolled_lstat(path):
                        info = list(original_lstat(path))
                        info[4] = 0
                        if path in root.parents:
                            info[0] &= ~0o022  # temporary fixture ancestors
                        if (path == source and kind == 'file_mode'
                                or path == lib and kind == 'parent_mode'):
                            info[0] |= 0o020
                        if (path == source and kind == 'file_owner'
                                or path == lib and kind == 'parent_owner'):
                            info[4] = 12345
                        return os.stat_result(info)

                    fake_dispatcher = lib / 'hote-secrets.py'
                    module_name = 'hote-release'
                    if kind in ('link', 'dangling'):
                        (lib / 'linked.py').symlink_to(source if kind == 'link'
                                                       else lib / 'absent')
                        module_name = 'linked'
                    if kind == 'parent_link':
                        alias = root / 'alias'
                        alias.symlink_to(lib)
                        fake_dispatcher = alias / 'hote-secrets.py'
                    with mock.patch.object(dispatcher, '__file__', str(fake_dispatcher)), \
                            mock.patch.object(Path, 'lstat', enrolled_lstat):
                        if kind == 'trusted':
                            dispatcher.module(module_name, 'l2034_enrolled')
                            self.assertEqual(marker.read_text(), 'executed')
                        else:
                            with self.assertRaises((ValueError, OSError)):
                                dispatcher.module(module_name, 'l2034_untrusted')
                            self.assertFalse(marker.exists())
                    if kind in ('link', 'dangling'):
                        (lib / 'linked.py').unlink()

    def lancer(self, *args, stdin=b''):
        return subprocess.run([sys.executable, str(HOTE), *args], input=stdin,
                              capture_output=True, timeout=30)

    def test_arguments_refuses(self):
        done = self.lancer('--policy', '/tmp/x', stdin=b'{}\n' + CANARY)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout), {'error': 'host_action_unavailable'})
        self.assertNotIn(CANARY, done.stdout + done.stderr)

    def test_runtime_root_exige(self):
        # hors du runtime root isolé, rien n'est lu (ni enrôlement ni trame).
        done = self.lancer(stdin=b'{"protocol":"ameesh-secret/1"}\n' + CANARY)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout), {'error': 'host_action_unavailable'})
        self.assertNotIn(CANARY, done.stdout + done.stderr)


class DelegationSecretsTest(unittest.TestCase):
    def setUp(self):
        key = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + b'a' * 32
        self.plan = dict(host='fixture-host', machine_id='a' * 32,
                         controller_source='192.0.2.17/32',
                         controller_key='ssh-ed25519 ' + base64.b64encode(key).decode(),
                         host_key_sha256='b' * 64, runtime_tree_sha256='c' * 64,
                         source_tree_sha256='d' * 64)

    def test_rendu_pur_deterministe(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('exécution refusée')):
            bundle = delegation.render(self.plan)
        self.assertEqual(bundle, delegation.render(self.plan))
        for path, item in bundle['files'].items():
            self.assertEqual(item['sha256'], hashlib.sha256(item['content'].encode()).hexdigest())
            self.assertFalse(item['mode'] & 0o022)
            self.assertEqual(bundle['revoke']['remove_only_matching_sha256'][path], item['sha256'])

    def test_canal_distinct_du_restart(self):
        files = delegation.render(self.plan)['files']
        sudo = files['/etc/sudoers.d/ameesh-secrets']['content']
        self.assertEqual([l for l in sudo.splitlines() if 'NOPASSWD:' in l],
                         ['ameesh-secrets ALL=(root) NOPASSWD: /opt/ameesh-release/runtime/bin/python'
                          ' -I -B /opt/ameesh-release/lib/hote-secrets.py'])
        restart = restart_delegation.render(dict(self.plan, policy=dict(
            host='fixture-host', units=['ameesh-runner@fixture.service'],
            controller_unit='ameesh-release-controller.service', manager={'kind':'system'},restart_seconds=5)))['files']
        # aucun fichier commun : installer ou révoquer l'un ne touche pas l'autre.
        self.assertFalse(set(files) & set(restart))
        for item in files.values():
            self.assertNotIn('hote-release.py', item['content'])
            self.assertNotIn('ameesh-release ', item['content'])
        sshd = files[delegation.SSHD]['content']
        for line in ('Match User ameesh-secrets', 'AuthorizedKeysFile ' + delegation.KEYS,
                     'ForceCommand ' + delegation.WRAPPER, 'DisableForwarding yes',
                     'PermitTTY no', 'MaxSessions 1'):
            self.assertIn(line, sshd)

    def test_plan_de_restart_refuse(self):
        with self.assertRaises(delegation.DelegationError):
            delegation.render(dict(self.plan, policy={}))
        for name, value in (('machine_id', 'x'), ('controller_source', '192.0.2.17/24'),
                            ('host', 'a b'), ('runtime_tree_sha256', 'HEAD')):
            with self.subTest(name=name):
                with self.assertRaises(delegation.DelegationError):
                    delegation.render(dict(self.plan, **{name: value}))

    def test_wrapper_refuse_commande_distante(self):
        wrapper = delegation.render(self.plan)['files'][delegation.WRAPPER]['content']
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'wrapper'
            path.write_text(wrapper)
            for args, env in ((['x'], {}), ([], {'SSH_ORIGINAL_COMMAND': 'cat /etc/ameesh/db.env'})):
                done = subprocess.run(['/bin/sh', str(path), *args], env=env,
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 1)
                self.assertEqual(json.loads(done.stdout), {'error': 'host_action_unavailable'})

    def test_question_liee_au_bundle(self):
        bundle = delegation.render(self.plan)
        text = delegation.question(bundle)
        for value in (bundle['digest'], self.plan['machine_id'], 'hote-secrets.py',
                      'No installation is authorized', 'does not widen the restart delegation'):
            self.assertIn(value, text)
        changed = json.loads(json.dumps(bundle))
        changed['files'][delegation.WRAPPER]['content'] = 'exec evil'
        with self.assertRaisesRegex(delegation.DelegationError, 'changed'):
            delegation.question(changed)


if __name__ == '__main__':
    unittest.main()
