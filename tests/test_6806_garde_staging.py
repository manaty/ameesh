# SPDX-License-Identifier: AGPL-3.0-only
"""#6806 : la garde de démarrage du staging (ENV_SHA) est du code d'hôte.

Le banc vérifie les trois pièces versées au dépôt (garde, drop-in, contenu
public de `bascule.env`), le comportement de la garde hors service, puis
l'installateur : pose, refus d'une édition manuelle, chemin de mise à jour du
hash (`--accepter-bascule` / `pin`), idempotence, restauration sur échec et
saut attendu d'un hôte sans compte `ameesh-staging`. Enfin, il vérifie que
`vm.sh agents installer` appelle l'installateur depuis l'instantané du commit
installé et traite le code 3 comme un saut.

Aucun hôte réel, aucun compte, aucun service : les chemins sont temporaires,
l'uid attendu est celui du banc, le nodename est simulé (candidat AG1/AG2 ou
témoin hors candidats) et `runuser`/`systemctl` sont des exécuteurs factices.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/mise-a-jour'
GARDE = DEPLOY / 'staging-startup-guard.py'
DROPIN = DEPLOY / 'systemd' / 'ameesh-staging-10-native-actor.conf'
INSTALLATEUR = DEPLOY / 'garde-staging.py'
VM_SH = DEPLOY / 'vm.sh'

SHA_GARDE = '4bde802e931ab7017934f0ee2f42f07b33b2408905a8aabe3a98063ebeeffdd9'
SHA_DROPIN = 'ce3ed09c7a816b1f02358bd6301a1d8bdde0ee3110228a2fe10ea5e0a5a334cc'
SHA_ENV = '1223707fe4a80f5627744f969f4bbbc1662ac859dfb0aab9f2ef34e08c919fa1'


def charger(nom: str, chemin: Path):
    spec = importlib.util.spec_from_file_location(nom, chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


garde_staging = charger('garde_staging_6806', INSTALLATEUR)
garde = charger('garde_service_6806', GARDE)
PUBLIC_ENV = garde.PUBLIC_ENV
UID, GID = os.getuid(), os.getgid()

# Relevé avant toute simulation : le banc ne dépend jamais du nodename de la
# machine qui l'exécute (le runner de CI n'est ni AG1 ni AG2).
UNAME_REEL = os.uname()
HOTE_CANDIDAT = 'ameesh-agents-1'
HOTE_HORS_CANDIDATS = 'runner-ci-6806'


class Banc(unittest.TestCase):
    """Banc dont l'hôte simulé est un candidat, sauf témoin contraire.

    L'installateur ne fait tourner la garde que sur un nodename admis par la
    garde versionnée ; hors AG1/AG2 il se contente de poser. Le nodename est
    donc fixé explicitement ici, et le témoin hors candidats le remplace.
    """

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='6806-'))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / 'home'
        self.home.mkdir(mode=0o700)
        self.executeur = Executeur()
        self.simuler_hote(HOTE_CANDIDAT)

    def simuler_hote(self, nodename: str) -> None:
        """Fixe le nodename vu par l'installateur (`os.uname`)."""
        simule = SimpleNamespace(
            sysname=UNAME_REEL.sysname, nodename=nodename,
            release=UNAME_REEL.release, version=UNAME_REEL.version,
            machine=UNAME_REEL.machine)
        patcher = mock.patch.object(os, 'uname', return_value=simule)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bascule(self, contenu: bytes = PUBLIC_ENV) -> Path:
        (self.home / '.config').mkdir(mode=0o700, exist_ok=True)
        (self.home / '.config' / 'ameesh').mkdir(mode=0o700, exist_ok=True)
        chemin = self.home / '.config' / 'ameesh' / 'bascule.env'
        chemin.write_bytes(contenu)
        chemin.chmod(0o600)
        return chemin

    def source(self, env: bytes = PUBLIC_ENV) -> Path:
        """Garde versionnée pour CE home (les chemins installés en dépendent)."""
        contenu = garde_staging.rendre(GARDE.read_bytes(), self.home, env)
        chemin = self.base / 'staging-startup-guard.py'
        chemin.write_bytes(contenu)
        return chemin

    def dropin(self) -> Path:
        chemin = self.base / '10-native-actor.conf'
        chemin.write_bytes(
            b'[Service]\nEnvironment=AMEESH_OPERATEUR=ameesh:staging\n'
            b'ExecStartPre=/usr/bin/python3.12 -I -B '
            + str(self.home / garde_staging.GARDE_REL).encode('utf-8') + b'\n')
        return chemin

    def installer(self, **options):
        options.setdefault('executer', self.executeur)
        options.setdefault('uid_attendu', UID)
        return garde_staging.installer(
            self.home, UID, GID, 'banc', options.pop('source', self.source()),
            options.pop('dropin', self.dropin()), **options)


class Executeur:
    """`runuser` factice : journalise, rend le code demandé par la commande."""

    def __init__(self):
        self.appels = []
        self.code_reload = 0
        self.code_garde = 0

    def __call__(self, args, **options):
        self.appels.append(args)
        code = self.code_reload if 'daemon-reload' in args else self.code_garde
        return SimpleNamespace(returncode=code,
                               stderr='refus factice' if code else '')


class TestPiecesVersionnees(unittest.TestCase):
    def test_la_garde_est_exactement_celle_du_service(self):
        contenu = GARDE.read_bytes()
        self.assertEqual(hashlib.sha256(contenu).hexdigest(), SHA_GARDE)
        self.assertEqual(len(contenu), 2420)

    def test_le_dropin_est_exact(self):
        contenu = DROPIN.read_bytes()
        self.assertEqual(hashlib.sha256(contenu).hexdigest(), SHA_DROPIN)
        self.assertEqual(len(contenu), 154)

    def test_le_contenu_public_est_epingle(self):
        self.assertEqual(hashlib.sha256(PUBLIC_ENV).hexdigest(), SHA_ENV)
        self.assertEqual(garde_staging.empreinte(PUBLIC_ENV), SHA_ENV)

    def test_la_garde_vise_le_home_et_l_uid_du_service(self):
        self.assertEqual(garde_staging._home_versionnee(GARDE.read_bytes())[1],
                         '/home/ameesh-staging')
        self.assertEqual(garde_staging.UID_GARDE, 980)
        self.assertEqual(garde_staging._hotes_versionnes(GARDE.read_bytes()),
                         ('ameesh-agents-1', 'ameesh-agents-2'))

    def test_rendre_sans_changement_rend_les_memes_octets(self):
        self.assertEqual(
            garde_staging.rendre(GARDE.read_bytes(), Path('/home/ameesh-staging'),
                                 PUBLIC_ENV),
            GARDE.read_bytes())


class TestGardeHorsService(Banc):
    """La garde elle-même : mêmes contrôles que ceux du service."""

    def test_accepte_le_contenu_public_du_versionnement(self):
        chemin = self.bascule()
        self.assertEqual(chemin.read_bytes(), PUBLIC_ENV)
        garde.validate(self.home, UID, 'ameesh:staging')

    def test_refuse_une_edition_manuelle(self):
        modifie = PUBLIC_ENV.replace(b'ameesh-agents-2=root@78.232.60.1',
                                     b'ameesh-agents-2=root@78.232.60.9')
        self.bascule(modifie)
        with self.assertRaisesRegex(ValueError, 'Changed public environment'):
            garde.validate(self.home, UID, 'ameesh:staging')

    def test_refuse_un_autre_acteur(self):
        self.bascule()
        with self.assertRaisesRegex(ValueError, 'Wrong actor'):
            garde.validate(self.home, UID, 'ameesh:staging ')  # espace en trop

    def test_refuse_un_home_accessible_aux_autres(self):
        self.bascule()
        self.home.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'Wrong dedicated home'):
            garde.validate(self.home, UID, 'ameesh:staging')

    def test_refuse_un_bascule_env_partage(self):
        chemin = self.bascule()
        chemin.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'Wrong public environment metadata'):
            garde.validate(self.home, UID, 'ameesh:staging')

    @unittest.skipIf(os.getuid() == 980, 'le service tourne sous 980 sur cet hôte')
    def test_main_refuse_hors_du_contexte_du_service(self):
        resultat = subprocess.run(
            [sys.executable, '-I', '-B', str(GARDE)],
            env=dict(os.environ, AMEESH_OPERATEUR='ameesh:staging'),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(resultat.returncode, 1, resultat.stderr)
        self.assertEqual(
            resultat.stderr,
            'Elected staging startup refused: actor or protected configuration\n')


class TestInstallateur(Banc):
    def test_pose_la_garde_et_le_dropin_puis_verifie(self):
        self.bascule()
        rapport = self.installer()
        cible = self.home / garde_staging.GARDE_REL
        dropin = self.home / garde_staging.DROPIN_REL
        self.assertEqual(cible.read_bytes(), self.source().read_bytes())
        self.assertEqual(dropin.read_bytes(), self.dropin().read_bytes())
        self.assertEqual(stat.S_IMODE(cible.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(dropin.stat().st_mode), 0o644)
        self.assertEqual(cible.stat().st_uid, UID)
        self.assertEqual(dropin.stat().st_uid, UID)
        self.assertFalse(rapport['change'])
        self.assertTrue(rapport['candidat'])
        self.assertTrue(rapport['verifie'])
        self.assertEqual(len(self.executeur.appels), 2)  # daemon-reload, garde
        self.assertIn('daemon-reload', self.executeur.appels[0])
        self.assertEqual(self.executeur.appels[1][-1], str(cible))

    def test_un_hote_hors_candidats_ne_verifie_pas_la_garde(self):
        """Témoin : hors AG1/AG2, la pose réussit sans exécuter la garde."""
        self.bascule()
        self.simuler_hote(HOTE_HORS_CANDIDATS)
        rapport = self.installer()
        self.assertFalse(rapport['change'])
        self.assertFalse(rapport['candidat'])
        self.assertFalse(rapport['verifie'])
        self.assertTrue((self.home / garde_staging.GARDE_REL).exists())
        self.assertTrue((self.home / garde_staging.DROPIN_REL).exists())
        self.assertEqual(len(self.executeur.appels), 1)  # daemon-reload seul
        self.assertIn('daemon-reload', self.executeur.appels[0])
        self.assertFalse(any('staging-startup-guard.py' in ' '.join(appel)
                             for appel in self.executeur.appels))

    def test_est_idempotent(self):
        self.bascule()
        self.installer()
        cible = self.home / garde_staging.GARDE_REL
        avant = cible.read_bytes()
        rapport = self.installer()
        self.assertFalse(rapport['change'])
        self.assertEqual(cible.read_bytes(), avant)
        self.assertEqual(len(self.executeur.appels), 3)  # pas de 2e reload

    def test_refuse_une_bascule_env_editee_sans_accepter(self):
        self.bascule(PUBLIC_ENV + b'# edition manuelle\n')
        with self.assertRaisesRegex(garde_staging.Refus, '--accepter-bascule'):
            self.installer()
        self.assertFalse((self.home / garde_staging.GARDE_REL).exists())
        self.assertFalse((self.home / garde_staging.DROPIN_REL).exists())

    def test_accepte_la_bascule_revue_et_met_a_jour_le_hash(self):
        nouveau = PUBLIC_ENV.replace(b'VM=10.77.0.1', b'VM=10.77.0.2')
        self.bascule(nouveau)
        rapport = self.installer(accepter=True)
        self.assertTrue(rapport['change'])
        self.assertEqual(rapport['env_sha'], hashlib.sha256(nouveau).hexdigest())
        cible = self.home / garde_staging.GARDE_REL
        installee = charger('garde_installee_6806', cible)
        self.assertEqual(installee.PUBLIC_ENV, nouveau)
        # ENV_SHA est recalculé à l'import : la garde accepte le nouveau fichier.
        installee.validate(self.home, UID, 'ameesh:staging')

    def test_pin_ne_repose_pas_le_dropin(self):
        self.bascule()
        self.installer()
        dropin = self.home / garde_staging.DROPIN_REL
        self.executeur.appels.clear()
        rapport = self.installer(dropin=None, accepter=True)
        self.assertFalse(rapport['change'])
        self.assertEqual(rapport['dropin'], None)
        self.assertNotIn('daemon-reload', self.executeur.appels[0])
        # Le drop-in déjà posé n'a pas bougé.
        self.assertTrue(dropin.exists())

    def test_restaure_les_octets_precedents_si_la_verification_echoue(self):
        self.bascule()
        self.installer()
        cible = self.home / garde_staging.GARDE_REL
        dropin = self.home / garde_staging.DROPIN_REL
        cible.write_bytes(b'ancienne garde\n')
        dropin.write_bytes(b'ancien drop-in\n')
        self.executeur.code_garde = 1
        with self.assertRaisesRegex(garde_staging.Refus, 'refuse apres installation'):
            self.installer()
        self.assertEqual(cible.read_bytes(), b'ancienne garde\n')
        self.assertEqual(dropin.read_bytes(), b'ancien drop-in\n')

    def test_a_blanc_n_ecrit_rien(self):
        self.bascule()
        rapport = self.installer(appliquer=False)
        self.assertFalse((self.home / garde_staging.GARDE_REL).exists())
        self.assertFalse((self.home / garde_staging.DROPIN_REL).exists())
        self.assertEqual(self.executeur.appels, [])
        self.assertFalse(rapport['verifie'])

    def test_refuse_un_dropin_incoherent(self):
        self.bascule()
        mauvais = self.base / 'mauvais.conf'
        mauvais.write_bytes(b'[Service]\nExecStartPre=/bin/false\n')
        with self.assertRaisesRegex(garde_staging.Refus, 'incoherent'):
            self.installer(dropin=mauvais)

    def test_refuse_un_uid_autre_que_celui_du_service(self):
        self.bascule()
        with self.assertRaisesRegex(garde_staging.Refus, 'uid'):
            self.installer(uid_attendu=UID + 1)

    def test_refuse_un_lien_symbolique_a_la_place_de_bascule_env(self):
        self.bascule()
        chemin = self.home / '.config' / 'ameesh' / 'bascule.env'
        ailleurs = self.base / 'ailleurs.env'
        chemin.rename(ailleurs)
        chemin.symlink_to(ailleurs)
        with self.assertRaisesRegex(garde_staging.Refus, 'bascule.env illisible'):
            self.installer()

    def test_refuse_un_parent_de_configuration_inscriptible(self):
        self.bascule()
        (self.home / '.config').chmod(0o777)
        with self.assertRaisesRegex(garde_staging.Refus, 'dossier non sur'):
            self.installer()

    def test_refuse_une_cible_symbolique(self):
        self.bascule()
        cible = self.home / garde_staging.GARDE_REL
        (self.home / '.local').mkdir(mode=0o755)
        (self.home / '.local' / 'libexec').mkdir(mode=0o755)
        cible.symlink_to(self.base / 'ailleurs-garde')
        with self.assertRaisesRegex(garde_staging.Refus, 'cible non reguliere'):
            self.installer()

    def test_refuse_un_parent_de_bascule_symbolique(self):
        self.bascule()
        (self.home / '.config' / 'ameesh').rename(self.base / 'ailleurs-config')
        (self.home / '.config' / 'ameesh').symlink_to(self.base / 'ailleurs-config')
        with self.assertRaisesRegex(garde_staging.Refus, 'dossier non sur'):
            self.installer()

    def test_ecriture_relative_au_dossier_ouvert_malgre_substitution(self):
        # La course nommee par la revue : le parent est remplace par un lien
        # apres l'ouverture ; l'ecriture doit rester dans le dossier ouvert.
        self.bascule()
        (self.home / '.local').mkdir(mode=0o755)
        (self.home / '.local' / 'libexec').mkdir(mode=0o755)
        home_fd = garde_staging._ouvrir_home(self.home, UID)
        try:
            dir_fd = garde_staging._ouvrir_dossier(
                home_fd, ('.local', 'libexec'), UID, GID, False)
        finally:
            os.close(home_fd)
        dehors = self.base / 'dehors'
        dehors.mkdir()
        (self.home / '.local' / 'libexec').rename(self.base / 'ouvert')
        (self.home / '.local' / 'libexec').symlink_to(dehors)
        try:
            garde_staging._ecrire_atomique(dir_fd, 'preuve', b'ok\n',
                                           0o755, UID, GID)
        finally:
            os.close(dir_fd)
        self.assertEqual((self.base / 'ouvert' / 'preuve').read_bytes(), b'ok\n')
        self.assertFalse((dehors / 'preuve').exists())

    def test_verifier_ne_suit_pas_une_garde_symbolique(self):
        self.bascule()
        (self.home / '.local').mkdir(mode=0o755)
        (self.home / '.local' / 'libexec').mkdir(mode=0o755)
        cible = self.home / garde_staging.GARDE_REL
        cible.symlink_to(self.base / 'ailleurs-garde')
        compte = SimpleNamespace(pw_dir=str(self.home), pw_uid=UID, pw_gid=GID)
        with mock.patch.object(garde_staging.pwd, 'getpwnam',
                               return_value=compte):
            code = garde_staging.principal(['--compte', 'banc', 'verifier'])
        self.assertEqual(code, 2)

    def test_un_hote_sans_compte_sort_en_3(self):
        resultat = subprocess.run(
            [sys.executable, str(INSTALLATEUR), '--compte',
             'compte-absent-6806', 'installer'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(resultat.returncode, 3, resultat.stderr)
        self.assertIn('aucune garde de staging a poser', resultat.stdout)


class TestVmSh(unittest.TestCase):
    """Le crochet réel de `vm.sh agents installer`, isolé de son hôte."""

    TEXTE = VM_SH.read_text()

    def fonction(self, nom: str) -> str:
        trouve = re.search(r'^%s\(\) \{.*?^\}\n' % nom, self.TEXTE, re.M | re.S)
        self.assertIsNotNone(trouve, '%s absente de vm.sh' % nom)
        return trouve.group(0)

    def test_les_pieces_viennent_du_poste_et_non_de_l_arbre_du_compte(self):
        envoi = self.fonction('envoyer_garde_agents')
        self.assertIn('$ICI/$piece', envoi)
        self.assertIn('garde-staging.py', envoi)
        self.assertIn('base64 -w0', envoi)
        self.assertNotIn('/home/ameesh/.local/share/ameesh/src-', envoi)
        self.assertIn('GARDE_AGENTS_DIR=/root/.cache/ameesh/garde-$$', self.TEXTE)

    def test_installer_agents_appelle_l_installateur_apres_le_code(self):
        corps = re.search(r'^installer_agents\(\) \{.*?^\}\n', self.TEXTE,
                          re.M | re.S)
        self.assertIsNotNone(corps)
        self.assertIn('installer_garde_staging "$sha"', corps.group(0))
        # Après la bascule du venv, comme dernière étape de la pose.
        self.assertLess(corps.group(0).index('reinstaller_agents "$sha"'),
                        corps.group(0).index('installer_garde_staging "$sha"'))

    def lancer(self, pose: int = 0, envoi: int = 0, ici: Path | None = None,
               accepter: str | None = None):
        base = Path(tempfile.mkdtemp(prefix='6806-vm-'))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        log = base / 'ssh.log'
        reglage = '' if accepter is None else f'GARDE_ACCEPTER_BASCULE={accepter!r}'
        script = f'''set -euo pipefail
ICI={str(ici or DEPLOY)!r}
GARDE_AGENTS_DIR={str(base / 'garde')!r}
HOTE_AGENTS=ameesh-agents-1
{reglage}
etape() {{ printf '== %s\\n' "$*"; }}
ssh_agents() {{
  case "$*" in
    *" installer "*) printf 'installer %s\\n' "$*" >> {str(log)!r}; return {pose} ;;
    *"base64 -d"*) printf 'envoi\\n' >> {str(log)!r}; return {envoi} ;;
    *"rm -rf"*) return 0 ;;
    *"mkdir -p"*) return 0 ;;
  esac
  return 1
}}
{self.fonction('envoyer_garde_agents')}
{self.fonction('installer_garde_staging')}
installer_garde_staging 0123456789abcdef0123456789abcdef01234567
'''
        resultat = subprocess.run(['bash', '-c', script],
                                  capture_output=True, text=True, timeout=60)
        resultat.journal = log.read_text() if log.exists() else ''
        return resultat

    def test_pose_reussie(self):
        resultat = self.lancer(pose=0)
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertIn('garde installee sous le compte ameesh-staging', resultat.stdout)
        self.assertEqual(resultat.journal.count('envoi'), 3)
        self.assertIn('installer', resultat.journal)

    def test_bascule_revue_acceptee_explicitement(self):
        resultat = self.lancer(pose=0, accepter='1')
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertIn('--accepter-bascule', resultat.journal)
        self.assertIn('ENV_SHA sera mis a jour', resultat.stdout)

    def test_valeur_d_acceptation_invalide_refusee(self):
        resultat = self.lancer(pose=0, accepter='2')
        self.assertEqual(resultat.returncode, 1)
        self.assertIn('GARDE_ACCEPTER_BASCULE invalide', resultat.stderr)
        self.assertEqual(resultat.journal, '')

    def test_compte_absent_est_un_saut_attendu(self):
        resultat = self.lancer(pose=3)
        self.assertEqual(resultat.returncode, 0, resultat.stderr)
        self.assertIn('saut attendu', resultat.stdout)

    def test_echec_de_pose_arrete_l_installation(self):
        resultat = self.lancer(pose=2)
        self.assertEqual(resultat.returncode, 1)
        self.assertIn('garde de staging non installee (rc 2)', resultat.stderr)

    def test_envoi_incomplet_arrete_avant_d_executer_l_installateur(self):
        resultat = self.lancer(envoi=1)
        self.assertEqual(resultat.returncode, 1)
        self.assertIn('pieces de la garde non envoyees', resultat.stderr)
        self.assertNotIn('installer ', resultat.journal)

    def test_piece_absente_du_poste_refusee(self):
        vide = Path(tempfile.mkdtemp(prefix='6806-vide-'))
        self.addCleanup(shutil.rmtree, vide, ignore_errors=True)
        resultat = self.lancer(ici=vide)
        self.assertEqual(resultat.returncode, 1)
        self.assertIn('piece de la garde introuvable', resultat.stderr)


if __name__ == '__main__':
    unittest.main()
