# SPDX-License-Identifier: AGPL-3.0-only
"""#5666 : manifeste verrouillé du venv de staging, production et vérification.

Aucun réseau, root, hôte ni installation réelle de staging : les roues sont
synthétiques et le venv jetable est créé par un Python du banc. La production
est faite avec le Python système root quand il existe (`/usr/bin/python3`),
car la garde de #5666 refuse un home non root ; sinon le Python du banc sert
et seuls les contrôles de propriétaire sont élargis à son propriétaire réel
(`_accepter_le_proprietaire_du_banc`). `InterpreteurTest` vérifie le refus d'un
home non root dans les deux cas. La construction de la roue ameesh est
remplacée par la roue du wheelhouse pour le test d'intégration ;
`_comparer_roues` est éprouvée séparément sur de vraies archives ZIP.
`_preparer_outil` est aussi remplacé : le chemin pip réel est exercé par
l'installation du wheelhouse dans le venv jetable.
"""
from tests import support  # noqa: F401 — environnement isolé du poste

import base64
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/mise-a-jour/venv-staging.py'


def charger():
    spec = importlib.util.spec_from_file_location('venv_staging_5666', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = charger()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _roue(dossier, nom, version, *, requires=(), scripts=(), data=(),
          module_name=None):
    """Une roue pure Python minimale, déterministe et installable par pip."""
    module_name = module_name or nom.replace('-', '_')
    dist = '%s-%s.dist-info' % (nom.replace('-', '_'), version.replace('-', '_'))
    fichiers = {'%s/__init__.py' % module_name: b'def main():\n    return 0\n'}
    if scripts:
        fichiers[dist + '/entry_points.txt'] = (
            '[console_scripts]\n'
            + ''.join('%s = %s:main\n' % (script, module_name)
                      for script in scripts)).encode('ascii')
    for chemin, contenu in data:
        fichiers['%s.data/data/%s' % (dist, chemin)] = contenu
    metadata = ['Metadata-Version: 2.1', 'Name: ' + nom, 'Version: ' + version]
    metadata += ['Requires-Dist: ' + require for require in requires]
    fichiers[dist + '/METADATA'] = ('\n'.join(metadata) + '\n\n').encode()
    fichiers[dist + '/WHEEL'] = (
        b'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\n'
        b'Tag: py3-none-any\n')
    record = []
    for chemin in sorted(fichiers):
        contenu = fichiers[chemin]
        record.append('%s,sha256=%s,%d' % (chemin, _b64(hashlib.sha256(
            contenu).digest()), len(contenu)))
    record.append('%s/RECORD,,' % dist)
    fichiers[dist + '/RECORD'] = ('\n'.join(record) + '\n').encode()
    chemin = Path(dossier) / ('%s-%s-py3-none-any.whl' % (
        nom.replace('-', '_'), version.replace('-', '_')))
    with zipfile.ZipFile(chemin, 'w', zipfile.ZIP_DEFLATED) as archive:
        for nom_membre in sorted(fichiers):
            info = zipfile.ZipInfo(nom_membre, (1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, fichiers[nom_membre])
    chemin.chmod(0o644)
    donnees = chemin.read_bytes()
    return dict(path=chemin, filename=chemin.name, name=nom,
                version=version, sha256=_hash(donnees), bytes=len(donnees))


def _lock(root, roues):
    chemin = Path(root) / 'requirements-staging.lock'
    lignes = []
    for roue in roues:
        lignes.append('%s==%s \\\n    --hash=sha256:%s'
                      % (roue['name'], roue['version'], roue['sha256']))
    chemin.write_text('# paquet opérateur de test\n' + '\n'.join(lignes) + '\n')
    chemin.chmod(0o644)
    return chemin


def _depot(root):
    depot = Path(root) / 'depot'
    depot.mkdir(mode=0o700)
    env = dict(
        os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.invalid',
        GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.invalid')

    def git(*arguments):
        return subprocess.run(
            ['git', '-C', str(depot), *arguments], check=True,
            capture_output=True, env=env).stdout.decode().strip()

    git('init', '-q')
    (depot / 'pyproject.toml').write_text(
        '[project]\nname = "ameesh"\nversion = "1.0"\n')
    (depot / 'src' / 'ameesh').mkdir(parents=True)
    (depot / 'src' / 'ameesh' / '__init__.py').write_text('VALUE = 1\n')
    git('add', '.')
    git('commit', '-q', '-m', 'fixture')
    return depot, git('rev-parse', 'HEAD')


class CommitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='5666-commit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.depot, self.sha = _depot(self.root)

    def git(self, *arguments):
        return subprocess.run(
            ['git', '-C', str(self.depot), *arguments], check=True,
            capture_output=True).stdout.decode().strip()

    def test_tag_et_sha_resolvent_le_meme_commit(self):
        self.git('tag', 'v1')
        tag_sha, tag_epoch = module._resoudre_commit(self.depot, tag='v1')
        sha, epoch = module._resoudre_commit(self.depot, sha=self.sha)
        self.assertEqual(tag_sha, self.sha)
        self.assertEqual(sha, self.sha)
        self.assertEqual(tag_epoch, epoch)

    def test_refuse_tag_absent_et_sha_court(self):
        with self.assertRaises(module.Erreur):
            module._resoudre_commit(self.depot, tag='absent')
        with self.assertRaises(module.Erreur):
            module._resoudre_commit(self.depot, tag='..')
        with self.assertRaises(module.Erreur):
            module._resoudre_commit(self.depot, sha=self.sha[:12])


PYTHONS_ROOT = ('/usr/bin/python3', '/usr/local/bin/python3')

SONDE_ROOT = (
    'import platform, sys, venv, ensurepip; '
    'print(platform.python_version()); '
    'print("%d.%d" % sys.version_info[:2]); '
    'print(sys._base_executable)')
SONDE_VERSION = 'import platform; print(platform.python_version())'


def _fichier_root(chemin):
    """Un fichier root, non modifiable par autrui."""
    try:
        info = Path(chemin).lstat()
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and info.st_uid == 0
            and not info.st_mode & 0o022 and not info.st_mode & 0o7000)


def _dossier_root(chemin):
    try:
        info = Path(chemin).lstat()
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and not Path(chemin).is_symlink()
            and info.st_uid == 0
            and not info.st_mode & 0o022 and not info.st_mode & 0o7000)


def _environnement_sonde(home):
    """L'environnement de la production, sans celui du banc.

    setup-python exporte LD_LIBRARY_PATH vers la libpython du runner : hérité,
    il fait charger une autre libpython au Python système et le fait annoncer
    à la version du banc. La production, elle, tourne dans cet environnement
    propre ; la sonde doit mesurer exactement comme elle.
    """
    return module._construction()._base_env(Path(home))


def _mesurer_python(python, code):
    """Les lignes imprimées par ``python -I -c code``, ou None.

    L'appel est isolé (-I) et tourne dans l'environnement de la production.
    """
    with tempfile.TemporaryDirectory(prefix='5666-sonde-') as home:
        try:
            sonde = subprocess.run(
                [str(python), '-I', '-c', code], capture_output=True,
                timeout=120, check=False,
                env=_environnement_sonde(Path(home)))
        except (OSError, subprocess.TimeoutExpired):
            return None
    if sonde.returncode != 0:
        return None
    return sonde.stdout.decode('utf-8', 'replace').split()


def _python_root():
    """Le Python système root du banc, ou None.

    Même version mineure que le banc : le lock synthétique du test suppose le
    pip de ``ensurepip`` de cette version (aucun setuptools avant 3.12).
    """
    for candidat in PYTHONS_ROOT:
        chemin = Path(candidat)
        try:
            resolu = chemin.resolve(strict=True)
        except OSError:
            continue
        if not (_dossier_root(chemin.parent) and _fichier_root(resolu)):
            continue
        lignes = _mesurer_python(chemin, SONDE_ROOT)
        if lignes is None or len(lignes) != 3:
            continue
        version, mineure, base_brute = lignes
        if mineure != '%d.%d' % sys.version_info[:2]:
            continue
        try:
            base = Path(base_brute).resolve(strict=True)
        except OSError:
            continue
        if not (base.is_absolute() and _dossier_root(base.parent)
                and _fichier_root(base)):
            continue
        return str(chemin), version
    return None


def _version_python(python):
    """La version que la production mesurera pour cet interpréteur, ou None."""
    lignes = _mesurer_python(python, SONDE_VERSION)
    if lignes is None or len(lignes) != 1:
        return None
    return lignes[0]


def _python_du_banc():
    """(python, version, vérifie_root) des productions jetables du test.

    La version vient d'une sonde dans l'environnement de la production : celle
    du banc (``platform.python_version()``) peut mentir quand setup-python a
    exporté LD_LIBRARY_PATH.
    """
    trouve = _python_root()
    if trouve is not None:
        return trouve[0], trouve[1], True
    return (sys.executable,
            _version_python(sys.executable) or platform.python_version(),
            False)


@contextlib.contextmanager
def _accepter_le_proprietaire_du_banc():
    """Le Python du banc remplace un Python root absent, production et
    vérification jetables.

    La garde de production n'est pas modifiée : seuls les deux contrôles de
    propriétaire root (home et binaire de l'interpréteur) sont élargis au
    propriétaire réel du banc. Le refus d'un home non root reste éprouvé par
    `InterpreteurTest`, sans ce remplacement.
    """
    vrai_dossier = module._dossier_protege
    vrai_sha = module._sha256_regulier

    def dossier(chemin, *, proprietaires=None):
        if proprietaires == frozenset((0,)):
            proprietaires = frozenset((0, Path(chemin).lstat().st_uid))
        return vrai_dossier(chemin, proprietaires=proprietaires)

    def sha(chemin, limite, *, proprietaires=None, libelle='roue'):
        if proprietaires == frozenset((0,)):
            proprietaires = frozenset((0, Path(chemin).lstat().st_uid))
        return vrai_sha(chemin, limite, proprietaires=proprietaires,
                        libelle=libelle)

    with mock.patch.multiple(module, _dossier_protege=dossier,
                             _sha256_regulier=sha):
        yield


class Paquet:
    """Un dépôt, un lock et un wheelhouse synthétiques, prêts à produire."""

    def __init__(self, root):
        self.root = Path(root)
        self.python, self.python_version, self.verifie_root = _python_du_banc()
        self.version_courte = '%d.%d' % tuple(
            int(partie) for partie in self.python_version.split('.')[:2])
        self.wheelhouse = self.root / 'wheelhouse'
        self.wheelhouse.mkdir(mode=0o700)
        self.ameesh = _roue(
            self.wheelhouse, 'ameesh', '1.0', requires=('tiny-dep>=1.0',),
            scripts=('ameesh', 'agent-mail'), module_name='ameesh',
            data=(('share/ameesh/mise-a-jour/helper.py', b'HELPER = 1\n'),))
        self.setuptools = _roue(self.wheelhouse, 'setuptools', '99.0')
        self.tiny = _roue(self.wheelhouse, 'tiny-dep', '1.0',
                          module_name='tiny_dep')
        self.lock = _lock(self.root, (self.ameesh, self.setuptools, self.tiny))
        self.depot, self.sha = _depot(self.root)
        self.sortie = self.root / 'sorties'
        self.sortie.mkdir(mode=0o700)
        self._build = dict(
            path=self.ameesh['path'], name=self.ameesh['filename'],
            version=self.ameesh['version'], sha256=self.ameesh['sha256'])

    def garde(self):
        """Le remplacement de propriétaire si le banc n'est pas root."""
        return (contextlib.nullcontext() if self.verifie_root
                else _accepter_le_proprietaire_du_banc())

    def produire(self, nom, travail):
        with self.garde():
            return module._produire(
                self.depot, sha=self.sha, tag=None, lock_path=self.lock,
                wheelhouse=self.wheelhouse, python=self.python,
                output=self.sortie / (nom + '.json'),
                travail=self.root / travail)


def _patches(ameesh):
    def preparer(python, setuptools, travail, env):
        return Path(travail) / 'outil'

    def construire(outil, source, sortie, env):
        return dict(path=ameesh['path'], name=ameesh['filename'],
                    version=ameesh['version'], sha256=ameesh['sha256'])

    return (
        mock.patch.object(module, '_preparer_outil', side_effect=preparer),
        mock.patch.object(module, '_construire_roue', side_effect=construire))


class LockTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='5666-lock-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def ecrire(self, texte, nom='lock.txt'):
        chemin = self.root / nom
        chemin.write_bytes(texte.encode() if isinstance(texte, str) else texte)
        chemin.chmod(0o644)
        return chemin

    def base(self):
        return ('ameesh==1.0 \\\n    --hash=sha256:' + 'a' * 64 + '\n'
                'setuptools==99.0 \\\n    --hash=sha256:' + 'b' * 64 + '\n')

    def test_lock_exact_avec_extras_et_commentaires(self):
        texte = ('# lock\n' + self.base()
                 + 'psycopg[binary,pool]==3.3.6 '
                   '--hash=sha256:' + 'c' * 64 + '\n')
        lock = module._lire_lock(self.ecrire(texte))
        self.assertEqual(lock['digest'], _hash(texte.encode()))
        self.assertEqual(set(lock['records']), {'ameesh', 'psycopg',
                                                'setuptools'})
        self.assertEqual(lock['records']['psycopg']['version'], '3.3.6')

    def test_lock_refuse_non_exact_non_hache_et_double(self):
        cas = {
            'ameesh==1.0\nsetuptools==99.0 --hash=sha256:' + 'b' * 64 + '\n': 'SHA-256',
            'ameesh==1.0 --hash=sha256:' + 'a' * 64
            + '\nsetuptools==99.0 --hash=sha256:' + 'b' * 64
            + '\npsycopg>=3.3 --hash=sha256:' + 'c' * 64 + '\n': 'exact',
            'ameesh==1.0 --hash=sha256:' + 'a' * 64
            + '\nsetuptools==99.0 --hash=sha256:' + 'b' * 64
            + '\n-r autre.txt\n': 'non exacte',
            'ameesh==1.0 --hash=sha256:' + 'a' * 64
            + '\nsetuptools==99.0 --hash=sha256:' + 'b' * 64
            + '\npsycopg==3.3.6; python_version < "3.13" --hash=sha256:'
            + 'c' * 64 + '\n': 'non exacte',
            'ameesh==1.0 --hash=sha256:' + 'a' * 64
            + '\nsetuptools==99.0 --hash=sha256:' + 'b' * 64
            + '\npsycopg==3.3.6 --hash=sha512:' + 'c' * 64 + '\n': 'seul',
            'ameesh==1.0 --hash=sha256:' + 'a' * 64
            + '\nsetuptools==99.0 --hash=sha256:' + 'b' * 64
            + '\nameesh==1.0 --hash=sha256:' + 'c' * 64 + '\n': 'double',
        }
        for texte, motif in cas.items():
            with self.subTest(motif=motif):
                with self.assertRaisesRegex(module.Erreur, motif):
                    module._lire_lock(self.ecrire(texte))

    def test_lock_obligatoire_et_lien(self):
        without = ('ameesh==1.0 --hash=sha256:' + 'a' * 64 + '\n')
        with self.assertRaisesRegex(module.Erreur, 'setuptools'):
            module._lire_lock(self.ecrire(without))
        base = self.ecrire(self.base())
        lien = self.root / 'lien.txt'
        lien.symlink_to(base)
        with self.assertRaises(module.Erreur):
            module._lire_lock(lien)

    def test_lock_non_modifiable_par_autrui(self):
        chemin = self.ecrire(self.base())
        chemin.chmod(0o664)
        with self.assertRaisesRegex(module.Erreur, 'non sûr'):
            module._lire_lock(chemin)


class RoueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='5666-roues-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wheelhouse = self.root / 'wheelhouse'
        self.wheelhouse.mkdir(mode=0o700)
        self.ameesh = _roue(self.wheelhouse, 'ameesh', '1.0',
                            module_name='ameesh')
        self.setuptools = _roue(self.wheelhouse, 'setuptools', '99.0')
        self.lock = module._lire_lock(
            _lock(self.root, (self.ameesh, self.setuptools)))

    def test_analyse_et_appariement(self):
        roues = module._lire_wheelhouse(self.wheelhouse)
        self.assertEqual([roue['name'] for roue in roues],
                         ['ameesh', 'setuptools'])
        par_nom = module._apparier_lock_wheelhouse(self.lock, roues)
        self.assertEqual(par_nom['ameesh']['sha256'], self.ameesh['sha256'])

    def test_roue_inconnue_absente_ou_hash_different(self):
        _roue(self.wheelhouse, 'intrus', '1.0')
        roues = module._lire_wheelhouse(self.wheelhouse)
        with self.assertRaisesRegex(module.Erreur, 'absente du lock'):
            module._apparier_lock_wheelhouse(self.lock, roues)
        (self.wheelhouse / 'intrus-1.0-py3-none-any.whl').unlink()
        roues = module._lire_wheelhouse(self.wheelhouse)
        (self.wheelhouse / 'ameesh-1.0-py3-none-any.whl').unlink()
        with self.assertRaisesRegex(module.Erreur, 'absente du wheelhouse'):
            module._apparier_lock_wheelhouse(
                self.lock, module._lire_wheelhouse(self.wheelhouse))
        # Une roue au nom valide mais au contenu différent du lock.
        self.ameesh['path'].write_bytes(b'not a wheel')
        self.ameesh['path'].chmod(0o644)
        with self.assertRaisesRegex(module.Erreur, 'SHA-256'):
            module._apparier_lock_wheelhouse(
                self.lock, module._lire_wheelhouse(self.wheelhouse))

    def test_wheelhouse_refuse_lien_sous_dossier_et_mode_large(self):
        lien = self.wheelhouse / 'lien-1.0-py3-none-any.whl'
        lien.symlink_to(self.ameesh['path'])
        with self.assertRaises(module.Erreur):
            module._lire_wheelhouse(self.wheelhouse)
        lien.unlink()
        (self.wheelhouse / 'sous-dossier').mkdir()
        with self.assertRaisesRegex(module.Erreur, 'inconnue'):
            module._lire_wheelhouse(self.wheelhouse)
        (self.wheelhouse / 'sous-dossier').rmdir()
        (self.wheelhouse / 'note.txt').write_text('x')
        with self.assertRaisesRegex(module.Erreur, 'inconnue'):
            module._lire_wheelhouse(self.wheelhouse)
        (self.wheelhouse / 'note.txt').unlink()
        self.ameesh['path'].chmod(0o664)
        with self.assertRaisesRegex(module.Erreur, 'non sûre'):
            module._lire_wheelhouse(self.wheelhouse)

    def test_analyse_refuse_noms_ambigus(self):
        for nom in ('ameesh-1.0.whl', 'ameesh-1.0-py3-none.whl',
                    'ameesh-1.0-a-b-c-d-e.whl', 'ameesh-1.0-py3-none-any.tar.gz'):
            with self.subTest(nom=nom):
                with self.assertRaises(module.Erreur):
                    module._analyser_roue(nom)

    def test_comparer_roues_contenu_et_mode(self):
        copie = self.root / 'copie.whl'
        shutil.copyfile(self.ameesh['path'], copie)
        copie.chmod(0o644)
        gauche = dict(path=self.ameesh['path'], name=self.ameesh['filename'])
        droite = dict(path=copie, filename=self.ameesh['filename'])
        module._comparer_roues(gauche, droite)
        # Même contenu, mode d'un membre différent.
        with zipfile.ZipFile(copie) as archive:
            membres = [(info, archive.read(info.filename))
                       for info in archive.infolist()]
        with zipfile.ZipFile(copie, 'w', zipfile.ZIP_DEFLATED) as archive:
            for info, contenu in membres:
                nouveau = zipfile.ZipInfo(info.filename, info.date_time)
                nouveau.external_attr = (
                    (0o600 if info.filename.endswith('__init__.py')
                     else (info.external_attr >> 16) & 0o777) << 16)
                archive.writestr(nouveau, contenu)
        copie.chmod(0o644)
        with self.assertRaisesRegex(module.Erreur, 'mode'):
            module._comparer_roues(gauche, droite)
        # Même mode, contenu différent.
        shutil.copyfile(self.ameesh['path'], copie)
        with zipfile.ZipFile(copie) as archive:
            membres = [(info, archive.read(info.filename))
                       for info in archive.infolist()]
        with zipfile.ZipFile(copie, 'w', zipfile.ZIP_DEFLATED) as archive:
            for info, contenu in membres:
                if info.filename.endswith('__init__.py'):
                    contenu = contenu + b'# change\n'
                nouveau = zipfile.ZipInfo(info.filename, info.date_time)
                nouveau.external_attr = info.external_attr
                archive.writestr(nouveau, contenu)
        copie.chmod(0o644)
        with self.assertRaisesRegex(module.Erreur, 'contenu'):
            module._comparer_roues(gauche, droite)


class ArbreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='5666-arbre-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proprietaires = frozenset((0, os.geteuid()))

    def test_mesure_deterministe_et_sensible(self):
        arbre = self.root / 'arbre'
        arbre.mkdir(mode=0o700)
        (arbre / 'a').mkdir(mode=0o700)
        (arbre / 'a' / 'f').write_text('un\n')
        (arbre / 'a' / 'f').chmod(0o644)
        mesure = module._mesurer_arbre(
            arbre, proprietaires=self.proprietaires)
        self.assertEqual(mesure, module._mesurer_arbre(
            arbre, proprietaires=self.proprietaires))
        (arbre / 'a' / 'f').write_text('deux\n')
        (arbre / 'a' / 'f').chmod(0o644)
        self.assertNotEqual(mesure['tree_sha256'], module._mesurer_arbre(
            arbre, proprietaires=self.proprietaires)['tree_sha256'])
        (arbre / 'a' / 'g').write_text('x\n')
        (arbre / 'a' / 'g').chmod(0o644)
        self.assertNotEqual(mesure['files'], module._mesurer_arbre(
            arbre, proprietaires=self.proprietaires)['files'])

    def test_mode_large_et_lien_refuses(self):
        arbre = self.root / 'arbre'
        arbre.mkdir(mode=0o700)
        fichier = arbre / 'f'
        fichier.write_text('x\n')
        fichier.chmod(0o664)
        with self.assertRaisesRegex(module.Ecart, 'non sûr'):
            module._mesurer_arbre(arbre, proprietaires=self.proprietaires)
        fichier.chmod(0o644)
        lien = arbre / 'lien'
        lien.symlink_to(fichier)
        with self.assertRaises(module.Ecart):
            module._mesurer_arbre(arbre, proprietaires=self.proprietaires)

    def test_record_normalise_par_entree(self):
        entrees = {'ameesh': dict(sha256='a' * 64, taille=10)}
        gauche = (b'../../../bin/ameesh,sha256=old,42\n'
                  b'pkg/x.py,sha256=abc,3\n')
        droite = (b'../../../bin/ameesh,sha256=other,77\n'
                  b'pkg/x.py,sha256=abc,3\n')
        self.assertEqual(module._normaliser_record(gauche, entrees),
                         module._normaliser_record(droite, entrees))

    def test_entree_normalisee_directe_et_shell(self):
        for lanceur in ('#!/tmp/venv-a/bin/python3\nprint(1)\n',
                        '#!/tmp/venv-b/bin/python3\nprint(1)\n'):
            info = module._entree_normalisee(
                lanceur.encode(), '/tmp/venv-a' if 'venv-a' in lanceur
                else '/tmp/venv-b', 0o755)
            self.assertEqual(info['launcher'], 'direct')
            self.assertEqual(info['sha256'], module._entree_normalisee(
                b'#!/tmp/venv-a/bin/python3\nprint(1)\n',
                '/tmp/venv-a', 0o755)['sha256'])
        shell = (b'#!/bin/sh\n'
                 b"'''exec' \"/tmp/venv-a/bin/python3\" \"$0\" \"$@\"\n"
                 b"' '''\nprint(1)\n")
        info = module._entree_normalisee(shell, '/tmp/venv-a', 0o755)
        self.assertEqual(info['launcher'], 'sh-preamble')
        self.assertNotEqual(info['sha256'], module._entree_normalisee(
            shell, '/tmp/venv-a', 0o700)['sha256'])
        with self.assertRaises(module.Ecart):
            module._entree_normalisee(b'print(1)\n', '/tmp/venv-a', 0o755)


class InterpreteurTest(unittest.TestCase):
    """Le refus d'un home non root survit au choix du Python du banc."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='5666-interpreteur-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @unittest.skipIf(os.geteuid() == 0, 'le banc ne tourne pas root')
    def test_home_non_root_refuse(self):
        venv = self.root / 'venv'
        venv.mkdir(mode=0o700)
        (venv / 'bin').mkdir(mode=0o700)
        home = self.root / 'home'
        home.mkdir(mode=0o700)
        executable = home / 'python3'
        executable.write_text('#!/bin/sh\nexit 1\n')
        executable.chmod(0o755)
        for nom in ('python', 'python3'):
            (venv / 'bin' / nom).symlink_to(executable)
        cfg = venv / 'pyvenv.cfg'
        cfg.write_text(
            'home = %s\ninclude-system-site-packages = false\n'
            'version = %s\nexecutable = %s\n'
            % (home, platform.python_version(), executable))
        cfg.chmod(0o644)
        with self.assertRaisesRegex(
                module.Ecart, 'home de l’interpréteur non root'):
            module._interpreteur(venv)


class SondeTest(unittest.TestCase):
    """La sonde mesure comme la production, pas comme le banc.

    En CI, setup-python exporte LD_LIBRARY_PATH vers la libpython du runner ;
    hérité, il faisait annoncer 3.12.15 au Python système 3.12.3, alors que la
    production, dans un environnement propre, mesurait 3.12.3.
    """

    def test_environnement_de_la_sonde_sans_celui_du_banc(self):
        with tempfile.TemporaryDirectory(prefix='5666-sonde-') as home:
            with mock.patch.dict(os.environ, {
                    'LD_LIBRARY_PATH': '/outil/lib',
                    'PYTHONHOME': '/outil',
                    'PYTHONPATH': 'src'}):
                env = _environnement_sonde(Path(home))
        self.assertNotIn('LD_LIBRARY_PATH', env)
        self.assertNotIn('PYTHONHOME', env)
        self.assertNotIn('PYTHONPATH', env)


class ProductionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='5666-production-')
        cls.root = Path(cls.temp.name)
        cls.paquet = Paquet(cls.root)
        # La vérification lit le même interpréteur que la production : quand
        # le banc n'a pas de Python root, l'élargissement vaut pour la classe.
        cls.pile = contextlib.ExitStack()
        cls.addClassCleanup(cls.pile.close)
        cls.pile.enter_context(cls.paquet.garde())
        patch_preparer, patch_construire = _patches(cls.paquet.ameesh)
        with patch_preparer, patch_construire:
            cls.document1 = cls.paquet.produire('m1', 'travail1')
            cls.document2 = cls.paquet.produire('m2', 'travail2')
        cls.manifeste1 = (cls.paquet.sortie / 'm1.json').read_bytes()
        cls.manifeste2 = (cls.paquet.sortie / 'm2.json').read_bytes()
        cls.venv = cls.root / 'travail1' / 'venv'
        cls.site = module._dossier_site(cls.venv)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_deux_productions_donnent_le_meme_manifeste(self):
        self.assertEqual(self.manifeste1, self.manifeste2)
        document = json.loads(self.manifeste1)
        self.assertEqual(document, self.document1)
        module._valider_manifeste(document)
        # Le manifeste ne dépend pas du chemin du venv : le second venv, créé
        # dans un autre répertoire, se vérifie avec le même manifeste.
        module._verifier(self.paquet.sortie / 'm2.json',
                         self.root / 'travail2' / 'venv')

    def test_manifeste_contient_les_attendus(self):
        document = self.document1
        self.assertEqual(document['sha'], self.paquet.sha)
        self.assertEqual(document['python']['version'],
                         self.paquet.python_version)
        self.assertTrue(document['python']['home'].startswith('/'))
        self.assertTrue(document['python']['executable'].startswith('/'))
        self.assertEqual(len(document['python']['sha256']), 64)
        self.assertEqual([item['name'] for item in document['distributions']],
                         ['ameesh', 'pip', 'setuptools', 'tiny-dep'])
        self.assertEqual([item['name'] for item in document['installed']['entry_points']],
                         ['agent-mail', 'ameesh', 'pip', 'pip3',
                          'pip' + self.paquet.version_courte])
        self.assertTrue((self.venv / 'share/ameesh/mise-a-jour/helper.py').is_file())
        self.assertEqual(len(document['wheels']), 3)

    def test_verification_reussie_et_lien_de_venv(self):
        module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        lien = self.root / 'venv-courant'
        lien.symlink_to(self.venv, target_is_directory=True)
        module._verifier(self.paquet.sortie / 'm1.json', lien)

    def test_ecart_fichier_ajoute_modifie_ou_supprime(self):
        site = self.site
        cible = site / 'tiny_dep' / '__init__.py'
        original = cible.read_bytes()
        try:
            cible.write_bytes(original + b'# modification\n')
            with self.assertRaisesRegex(module.Ecart, 'arbre installé'):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            cible.write_bytes(original)
        bytecode = site / 'tiny_dep' / '__pycache__'
        bytecode.mkdir(mode=0o700)
        (bytecode / 'x.pyc').write_bytes(b'x')
        (bytecode / 'x.pyc').chmod(0o644)
        try:
            with self.assertRaisesRegex(module.Ecart, 'bytecode'):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            (bytecode / 'x.pyc').unlink()
            bytecode.rmdir()
        ajout = site / 'ajout.txt'
        try:
            ajout.write_text('x')
            ajout.chmod(0o644)
            with self.assertRaises(module.Ecart):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            ajout.unlink()
        cible.unlink()
        try:
            with self.assertRaises(module.Ecart):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            cible.write_bytes(original)
            cible.chmod(0o644)
        module._verifier(self.paquet.sortie / 'm1.json', self.venv)

    def test_ecart_mode_entree_et_pyvenv(self):
        script = self.venv / 'bin' / 'ameesh'
        original = script.read_bytes()
        try:
            script.write_bytes(original + b'# modification\n')
            with self.assertRaisesRegex(module.Ecart, 'scripts d’entrée'):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            script.write_bytes(original)
            script.chmod(0o755)
        script.chmod(0o700)
        try:
            with self.assertRaisesRegex(module.Ecart, 'scripts d’entrée'):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            script.chmod(0o755)
        cfg = self.venv / 'pyvenv.cfg'
        original = cfg.read_bytes()
        try:
            cfg.write_bytes(original.replace(b'include-system-site-packages = false',
                                             b'include-system-site-packages = true'))
            with self.assertRaises(module.Ecart):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            cfg.write_bytes(original)
            cfg.chmod(0o644)
        module._verifier(self.paquet.sortie / 'm1.json', self.venv)

    def test_pyvenv_reecrit_vers_un_autre_interpreteur(self):
        version = self.document1['python']['version']
        faux = self.root / 'faux-interpreteur'
        faux.mkdir(mode=0o700)
        binaire = faux / 'python3'
        binaire.write_text('#!/bin/sh\necho "Python %s"\n' % version)
        binaire.chmod(0o755)
        cfg = self.venv / 'pyvenv.cfg'
        original_cfg = cfg.read_bytes()
        lien = self.venv / 'bin' / 'python3'
        original_lien = os.readlink(lien)
        try:
            cfg.write_text(
                re.sub(r'^home = .*$', 'home = ' + str(faux),
                       original_cfg.decode(), flags=re.M)
                .replace('executable = ' + self.document1['python']['executable'],
                         'executable = ' + str(binaire)))
            cfg.chmod(0o644)
            lien.unlink()
            lien.symlink_to(binaire)
            with self.assertRaisesRegex(module.Ecart, 'interpréteur'):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            cfg.write_bytes(original_cfg)
            cfg.chmod(0o644)
            if lien.is_symlink() or lien.exists():
                lien.unlink()
            lien.symlink_to(original_lien)
        module._verifier(self.paquet.sortie / 'm1.json', self.venv)

    def test_sha_interpreteur_compare_avant_execution(self):
        document = json.loads(self.manifeste1)
        document['python']['sha256'] = 'f' * 64
        chemin = self.root / 'm-sha.json'
        chemin.write_text(json.dumps(document) + '\n')
        chemin.chmod(0o644)
        with mock.patch.object(module, '_version_executable',
                               side_effect=AssertionError('exécuté')):
            with self.assertRaisesRegex(module.Ecart, 'interpréteur'):
                module._verifier(chemin, self.venv)

    def test_pip_check_sans_bytecode_du_venv(self):
        resultat = subprocess.CompletedProcess(args=[], returncode=0,
                                               stdout=b'', stderr=b'')
        with mock.patch.object(module.subprocess, 'run',
                               return_value=resultat) as appel:
            module._pip_check(self.venv)
        argv = appel.call_args.args[0]
        self.assertIn('-B', argv)
        self.assertIn('-X', argv)
        self.assertTrue(any(argument.startswith('pycache_prefix=')
                            for argument in argv))
        self.assertEqual(argv[-3:], ['-m', 'pip', 'check'])
        self.assertEqual(appel.call_args.kwargs['env']['PYTHONDONTWRITEBYTECODE'],
                         '1')

    def test_ecart_distribution_et_pip_check(self):
        site = self.site
        metadata = site / 'tiny_dep-1.0.dist-info' / 'METADATA'
        original = metadata.read_bytes()
        try:
            metadata.write_bytes(original.replace(b'Version: 1.0',
                                                  b'Version: 2.0'))
            with self.assertRaises(module.Ecart):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)
        finally:
            metadata.write_bytes(original)
            metadata.chmod(0o644)
        with mock.patch.object(module, '_pip_check',
                               side_effect=module.Ecart('pip check échoue')):
            with self.assertRaisesRegex(module.Ecart, 'pip check'):
                module._verifier(self.paquet.sortie / 'm1.json', self.venv)

    def test_code_de_sortie_cli(self):
        self.assertEqual(module.main([
            'verifier', '--manifeste', str(self.paquet.sortie / 'm1.json'),
            '--venv', str(self.venv)]), 0)
        mauvais = self.root / 'mauvais.json'
        mauvais.write_text('{"format": "autre"}\n')
        mauvais.chmod(0o644)
        self.assertEqual(module.main([
            'verifier', '--manifeste', str(mauvais), '--venv', str(self.venv)]), 2)


class ManifesteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='5666-manifeste-')
        cls.root = Path(cls.temp.name)
        cls.paquet = Paquet(cls.root)
        patch_preparer, patch_construire = _patches(cls.paquet.ameesh)
        with patch_preparer, patch_construire:
            cls.document = cls.paquet.produire('m', 'travail')
        cls.chemin = cls.paquet.sortie / 'm.json'

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def copie(self):
        return copy.deepcopy(self.document)

    def test_validation_accepte_le_manifeste_produit(self):
        self.assertEqual(module._lire_manifeste(self.chemin), self.document)

    def test_refuse_champs_digest_et_listes(self):
        cas = []
        document = self.copie()
        document['inconnu'] = 1
        cas.append((document, 'champs exacts'))
        document = self.copie()
        document['sha'] = 'A' * 40
        cas.append((document, 'SHA'))
        document = self.copie()
        document['source_tree_sha256'] = 'f' * 63
        cas.append((document, 'source_tree_sha256'))
        document = self.copie()
        document['bootstrap'] = []
        cas.append((document, 'bootstrap vide'))
        document = self.copie()
        del document['lock']['distributions'][0]
        cas.append((document, 'lock'))
        document = self.copie()
        document['wheels'] = document['wheels'][:-1]
        cas.append((document, 'roue'))
        document = self.copie()
        document['installed']['entry_points'] = list(
            reversed(document['installed']['entry_points']))
        cas.append((document, 'trié'))
        document = self.copie()
        document['installed']['entry_points'][0]['launcher'] = 'autre'
        cas.append((document, 'entry_points.launcher'))
        document = self.copie()
        document['installed']['files'] = True
        cas.append((document, 'installed.files'))
        document = self.copie()
        document['python'] = '3.12.3'
        cas.append((document, 'python'))
        document = self.copie()
        del document['python']['home']
        cas.append((document, 'python'))
        document = self.copie()
        document['python']['sha256'] = 'f' * 63
        cas.append((document, 'python.sha256'))
        document = self.copie()
        document['python']['home'] = 'usr/bin'
        cas.append((document, 'python.home'))
        for document, motif in cas:
            with self.subTest(motif=motif):
                with self.assertRaisesRegex(module.Erreur, motif):
                    module._valider_manifeste(document)

    def test_ecriture_idempotente_et_refus_d_un_autre_manifeste(self):
        sortie = self.root / 'ecrit.json'
        self.assertTrue(module._ecrire_manifeste(sortie, self.document))
        self.assertFalse(module._ecrire_manifeste(sortie, self.document))
        autre = self.copie()
        autre['python']['version'] = '3.12.99'
        with self.assertRaisesRegex(module.Erreur, 'différent'):
            module._ecrire_manifeste(sortie, autre)

    def test_lecture_refuse_lien_et_json_invalide(self):
        lien = self.root / 'lien.json'
        lien.symlink_to(self.chemin)
        with self.assertRaises(module.Erreur):
            module._lire_manifeste(lien)
        invalide = self.root / 'invalide.json'
        invalide.write_text('{')
        invalide.chmod(0o644)
        with self.assertRaises(module.Erreur):
            module._lire_manifeste(invalide)

    def test_refuse_les_flags_incoherents(self):
        document = self.copie()
        document['distributions'] = [
            item for item in document['distributions'] if item['name'] != 'pip']
        with self.assertRaisesRegex(module.Erreur, 'bootstrap'):
            module._valider_manifeste(document)
        document = self.copie()
        document['lock']['distributions'] = [
            item for item in document['lock']['distributions']
            if item['name'] != 'ameesh']
        with self.assertRaisesRegex(module.Erreur, 'ameesh'):
            module._valider_manifeste(document)


if __name__ == '__main__':
    unittest.main()
