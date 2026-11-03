#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Manifeste verrouillé du venv de staging (#5666).

Le §5 du plan L2 V4 exige, avant la pose d'un venv de staging, un paquet
opérateur protégé (``requirements-staging.lock`` et ``wheelhouse``) et un
manifeste qualifié : distributions et versions exactes, SHA-256 de chaque roue,
SHA-256 de l'arbre installé. Ce script produit ce manifeste depuis un commit
de release, puis vérifie un venv installé contre lui, sans réseau, sans geste
d'hôte et sans secret.

    venv-staging.py produire --clone DÉPÔT --sha SHA|--tag TAG \\
        --lock requirements-staging.lock --wheelhouse WHEELHOUSE \\
        --python /usr/bin/python3 --output manifeste.json
    venv-staging.py verifier --manifeste manifeste.json --venv VENV

La production :

* résout un tag (ou exige un SHA complet) et lit le commit par ``git archive``,
  jamais par le checkout ;
* n'accepte qu'un lock exact, ``nom==version`` avec au moins un
  ``--hash=sha256:`` par ligne, sans URL, marqueur, option ni sdist ;
* vérifie que le wheelhouse contient exactement une roue par distribution du
  lock, que son SHA-256 est celui du lock et que chaque roue est régulière,
  non liée et non modifiable par un autre compte ;
* reconstruit la roue ameesh depuis le commit qualifié avec le setuptools
  verrouillé du wheelhouse (construction sans isolation ni réseau) et refuse
  toute différence de nom, de contenu ou de mode avec la roue du wheelhouse ;
  le SHA-256 du manifeste reste celui de la roue réellement installée ;
* installe le venv dans un répertoire jetable avec la commande du plan
  (``pip install --no-index --find-links ... --require-hashes
  --only-binary=:all:``), exécute ``pip check`` puis mesure l'arbre ;
* n'écrit qu'un manifeste canonique, sans date, chemin temporaire, nom d'hôte
  ni secret. Deux exécutions sur le même SHA, le même lock et le même
  wheelhouse produisent les mêmes octets.

La vérification est en lecture seule et fail closed : elle épingle
l'interpréteur (``version``, ``home``, ``executable`` et SHA-256 du binaire
résolu) et le compare avant de l'exécuter, puis contrôle le ``pyvenv.cfg``
(``include-system-site-packages``), l'inventaire des distributions, l'arbre
installé (hors ``bin/``, ``pyvenv.cfg`` et bytecode, couverts séparément), les
scripts d'entrée (contenu, lanceur et mode), les scripts d'activation, puis
``pip check`` sans charger le bytecode de ``pip``. Un fichier de plus, de moins, modifié
ou un mode élargi est un écart.

Le manifeste ne remplace ni la qualification du commit à la forge, ni la
relecture du paquet opérateur, ni le feu de pose. Aucune installation sur un
hôte n'est faite par ce script.
"""
from __future__ import annotations

import argparse
import base64
import csv
from functools import lru_cache
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

FORMAT = 'ameesh-venv-staging/2'
MAX_LOCK_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 10 * 1024 * 1024
MAX_PYTHON_BYTES = 64 * 1024 * 1024
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_WHEEL_BYTES = 512 * 1024 * 1024
MAX_READ = 64 * 1024
SHA = re.compile(r'[0-9a-f]{40}\Z')
HEX256 = re.compile(r'[0-9a-f]{64}\Z')
VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9.+!_~-]*\Z')
CLEAN_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')
WHEEL_FILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+~-]*\.whl\Z')
WHEEL_COMPONENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._+~]*\Z')
REQUIREMENT = re.compile(
    r'([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[([A-Za-z0-9._,-]+)\])?'
    r'==([A-Za-z0-9][A-Za-z0-9.+!_~-]*)\Z')
HASH_OPTION = re.compile(r'--hash=sha256:([0-9a-f]{64})\Z')
ACTIVATIONS = ('Activate.ps1', 'activate', 'activate.csh', 'activate.fish')
PYTHON_LINK = re.compile(r'python[0-9.]*\Z')


class Erreur(ValueError):
    """Entrée, outil ou configuration invalide ; le geste est refusé."""


class Ecart(Erreur):
    """Un venv ne correspond pas au manifeste ; refus fail closed."""


@lru_cache(maxsize=1)
def _construction():
    spec = importlib.util.spec_from_file_location(
        'ameesh_construction_runtime',
        Path(__file__).with_name('construction-runtime.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_name(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _canonical(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'))


def _proprietaires(chemin):
    """Les propriétaires acceptés pour un arbre : root ou son propriétaire."""
    return frozenset((0, Path(chemin).lstat().st_uid))


def _lire_regulier(chemin, limite, *, proprietaires=None):
    """Lire un fichier régulier sans suivre un lien ni subir une course."""
    chemin = Path(chemin)
    if proprietaires is None:
        proprietaires = frozenset((0, os.geteuid()))
    try:
        fd = os.open(chemin, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Erreur('fichier illisible ou lié : %s' % chemin.name) from None
    with os.fdopen(fd, 'rb') as flux:
        avant = os.fstat(flux.fileno())
        if (not stat.S_ISREG(avant.st_mode) or avant.st_nlink != 1
                or avant.st_uid not in proprietaires
                or avant.st_mode & 0o022 or avant.st_mode & 0o7000
                or avant.st_size > limite):
            raise Erreur('fichier non sûr ou trop grand : %s' % chemin.name)
        donnees = flux.read(limite + 1)
        apres = os.fstat(flux.fileno())
        if (len(donnees) != avant.st_size or len(donnees) > limite
                or (avant.st_size, avant.st_mtime_ns, avant.st_ctime_ns)
                != (apres.st_size, apres.st_mtime_ns, apres.st_ctime_ns)):
            raise Erreur('fichier changé pendant sa lecture : %s' % chemin.name)
    return donnees


def _sha256_regulier(chemin, limite, *, proprietaires=None, libelle='roue'):
    """SHA-256 et taille d'un fichier régulier, sans le charger en mémoire."""
    chemin = Path(chemin)
    if proprietaires is None:
        proprietaires = frozenset((0, os.geteuid()))
    try:
        fd = os.open(chemin, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise Erreur('fichier illisible ou lié : %s' % chemin.name) from None
    with os.fdopen(fd, 'rb') as flux:
        avant = os.fstat(flux.fileno())
        if (not stat.S_ISREG(avant.st_mode) or avant.st_nlink != 1
                or avant.st_uid not in proprietaires
                or avant.st_mode & 0o022 or avant.st_mode & 0o7000
                or avant.st_size > limite):
            raise Erreur('%s non conforme ou trop grand : %s' % (libelle, chemin.name))
        digest = hashlib.sha256()
        taille = 0
        while True:
            morceau = flux.read(MAX_READ)
            if not morceau:
                break
            taille += len(morceau)
            if taille > limite:
                raise Erreur('%s trop grand : %s' % (libelle, chemin.name))
            digest.update(morceau)
        apres = os.fstat(flux.fileno())
        if (taille != avant.st_size
                or (avant.st_size, avant.st_mtime_ns, avant.st_ctime_ns)
                != (apres.st_size, apres.st_mtime_ns, apres.st_ctime_ns)):
            raise Erreur('%s changé pendant sa lecture : %s' % (libelle, chemin.name))
    return digest.hexdigest(), taille


def _dossier_protege(chemin, *, proprietaires=None):
    chemin = Path(chemin)
    if proprietaires is None:
        proprietaires = frozenset((0, os.geteuid()))
    try:
        info = chemin.lstat()
    except OSError:
        raise Erreur('dossier absent : %s' % chemin) from None
    if (not stat.S_ISDIR(info.st_mode) or chemin.is_symlink()
            or info.st_uid not in proprietaires
            or info.st_mode & 0o022 or info.st_mode & 0o7000):
        raise Erreur('dossier non sûr : %s' % chemin)


def _git_env(home):
    return _construction()._git_env(Path(home))


def _pip_env(home, epoch):
    return _construction()._pip_env(Path(home), epoch)


def _resoudre_commit(depot, *, sha=None, tag=None):
    """Le SHA complet et son horodatage, lus dans le clone, pas le checkout."""
    construction = _construction()
    depot = Path(depot).absolute()
    if not depot.is_dir():
        raise Erreur('clone absent : %s' % depot)
    if not construction.GIT.is_file():
        raise Erreur('git absent')
    env = _git_env(depot)
    try:
        haut = construction._git(depot, 'rev-parse', '--show-toplevel',
                                 env=env).decode().strip()
    except construction.BuildError:
        raise Erreur('clone Git invalide : %s' % depot) from None
    if not haut or Path(haut).resolve() != depot.resolve():
        raise Erreur('la racine du clone est attendue : %s' % depot)
    if tag is not None:
        if (not tag or tag.startswith('-') or '..' in tag
                or any(ord(c) < 32 or ord(c) == 127 for c in tag)):
            raise Erreur('nom de tag refusé')
        try:
            construction._git(depot, 'check-ref-format', 'refs/tags/' + tag,
                              env=env)
            sha = construction._git(
                depot, 'rev-parse', '--verify', '--end-of-options',
                'refs/tags/%s^{commit}' % tag, env=env).decode().strip()
        except construction.BuildError:
            raise Erreur('tag absent ou invalide : %s' % tag) from None
    if not isinstance(sha, str) or SHA.fullmatch(sha) is None:
        raise Erreur('SHA complet de 40 hexadécimaux attendu')
    try:
        resolu = construction._git(
            depot, 'rev-parse', '--verify', '--end-of-options', sha + '^{commit}',
            env=env).decode().strip()
        horodatage = construction._git(
            depot, 'show', '-s', '--format=%ct', sha, env=env).decode().strip()
        epoch = int(horodatage)
    except (construction.BuildError, ValueError):
        raise Erreur('commit absent du clone : %s' % sha) from None
    if resolu != sha:
        raise Erreur('le commit demandé n’est pas le SHA complet attendu')
    if epoch <= 0:
        raise Erreur('horodatage de commit illisible')
    return sha, epoch


def _lire_lock(chemin):
    """Le lock exact : nom==version, au moins un SHA-256, aucun autre token."""
    _dossier_protege(Path(chemin).parent)
    donnees = _lire_regulier(chemin, MAX_LOCK_BYTES)
    try:
        texte = donnees.decode('utf-8')
    except UnicodeDecodeError:
        raise Erreur('lock non UTF-8') from None
    if '\x00' in texte:
        raise Erreur('lock binaire refusé')
    lignes, suite = [], ''
    for brute in texte.splitlines():
        ligne = (suite + brute) if suite else brute
        if ligne.rstrip().endswith('\\'):
            suite = ligne.rstrip()[:-1] + ' '
            continue
        suite = ''
        lignes.append(ligne)
    if suite:
        raise Erreur('continuation de ligne non terminée dans le lock')
    records = {}
    for ligne in lignes:
        ligne = re.sub(r'(^|\s)#.*$', r'\1', ligne).strip()
        if not ligne:
            continue
        if (ligne.startswith('-') or '://' in ligne or '@' in ligne
                or ';' in ligne or ligne.endswith('\\')):
            raise Erreur('ligne non exacte ou non hachée dans le lock')
        tokens = ligne.split()
        correspondance = REQUIREMENT.fullmatch(tokens[0])
        if correspondance is None:
            raise Erreur('épinglage exact nom==version attendu : %s' % tokens[0])
        nom, extras, version = correspondance.groups()
        hashes = []
        for token in tokens[1:]:
            digest = HASH_OPTION.fullmatch(token)
            if digest is None:
                raise Erreur('seul --hash=sha256 est accepté dans le lock')
            hashes.append(digest.group(1))
        if not hashes:
            raise Erreur('au moins un SHA-256 est requis par épinglage')
        nom = _canonical_name(nom)
        if nom in records:
            raise Erreur('distribution en double dans le lock : %s' % nom)
        records[nom] = dict(name=nom, version=version, extras=extras,
                            hashes=tuple(hashes))
    if not records:
        raise Erreur('lock vide refusé')
    for obligatoire in ('ameesh', 'setuptools'):
        if obligatoire not in records:
            raise Erreur('le lock doit épingler %s' % obligatoire)
    return dict(records=records,
                digest=hashlib.sha256(donnees).hexdigest())


def _analyser_roue(nom_fichier):
    """Nom, version et tags d'une roue PEP 427 ; refuse toute forme ambiguë."""
    if not WHEEL_FILE.fullmatch(nom_fichier):
        raise Erreur('fichier de roue attendu : %s' % nom_fichier)
    morceaux = nom_fichier[:-4].split('-')
    if len(morceaux) == 5:
        nom, version, python, abi, plateforme = morceaux
    elif len(morceaux) == 6:
        nom, version, build, python, abi, plateforme = morceaux
        if not re.fullmatch(r'[0-9][A-Za-z0-9.]*', build):
            raise Erreur('tag de construction invalide : %s' % nom_fichier)
    else:
        raise Erreur('nom de roue ambigu : %s' % nom_fichier)
    if (not CLEAN_NAME.fullmatch(nom) or not VERSION.fullmatch(version)
            or not WHEEL_COMPONENT.fullmatch(python)
            or not WHEEL_COMPONENT.fullmatch(abi)
            or not WHEEL_COMPONENT.fullmatch(plateforme)):
        raise Erreur('composants de roue invalides : %s' % nom_fichier)
    return dict(filename=nom_fichier, name=_canonical_name(nom),
                version=version)


def _lire_wheelhouse(chemin):
    """Les roues régulières du dossier, triées, sans lien ni entrée inconnue."""
    chemin = Path(chemin).absolute()
    _dossier_protege(chemin)
    with os.scandir(chemin) as contenu:
        entrees = sorted(contenu, key=lambda item: item.name)
    roues = []
    for entree in entrees:
        if entree.name.startswith('.') or not entree.name.endswith('.whl'):
            raise Erreur('entrée inconnue dans le wheelhouse : %s' % entree.name)
        info = entree.stat(follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or entree.is_symlink()
                or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022 or info.st_mode & 0o7000
                or info.st_nlink != 1):
            raise Erreur('roue non sûre dans le wheelhouse : %s' % entree.name)
        roue = _analyser_roue(entree.name)
        digest, taille = _sha256_regulier(entree.path, MAX_WHEEL_BYTES)
        roue.update(path=Path(entree.path), sha256=digest, bytes=taille)
        roues.append(roue)
    if not roues:
        raise Erreur('wheelhouse vide refusé')
    return roues


def _apparier_lock_wheelhouse(lock, roues):
    """Exactement une roue par distribution du lock, au bon hash et version."""
    par_nom = {}
    for roue in roues:
        if roue['name'] in par_nom:
            raise Erreur('plusieurs roues pour %s dans le wheelhouse'
                         % roue['name'])
        par_nom[roue['name']] = roue
    attendues = set(lock['records'])
    vues = set(par_nom)
    if vues - attendues:
        raise Erreur('roue absente du lock : %s'
                     % ', '.join(sorted(vues - attendues)))
    if attendues - vues:
        raise Erreur('roue absente du wheelhouse : %s'
                     % ', '.join(sorted(attendues - vues)))
    for nom in sorted(attendues):
        record, roue = lock['records'][nom], par_nom[nom]
        if (roue['version'].lower().replace('-', '_')
                != record['version'].lower().replace('-', '_')):
            raise Erreur('version de roue différente du lock : %s' % nom)
        if roue['sha256'] not in record['hashes']:
            raise Erreur('SHA-256 de roue absent du lock : %s' % nom)
    return par_nom


def _extraire_source(depot, sha, destination, env):
    construction = _construction()
    try:
        return construction.extract_source(depot, sha, destination, env=env)
    except construction.BuildError as erreur:
        raise Erreur('source du commit inutilisable : %s' % erreur) from None


def _preparer_outil(python, setuptools, travail, env):
    """Un venv d'outillage séparé, avec le setuptools exact du wheelhouse."""
    construction = _construction()
    outil = Path(travail) / 'outil'
    try:
        construction.create_venv(outil, python=python, with_pip=True, env=env)
        construction.pip(outil, 'install', '--no-index', '--no-deps',
                         '--no-cache-dir', str(setuptools['path']), env=env)
    except construction.BuildError as erreur:
        raise Erreur('outillage de construction indisponible : %s' % erreur) from None
    return outil


def _construire_roue(outil, source, sortie, env):
    construction = _construction()
    try:
        return construction.build_wheel(outil, source, sortie, env=env)
    except construction.BuildError as erreur:
        raise Erreur('construction de la roue ameesh échouée : %s' % erreur) from None


def _comparer_roues(construite, attendue):
    """La reconstruction depuis le commit doit avoir le même contenu et les
    mêmes modes que la roue du wheelhouse ; l'ordre et les métadonnées ZIP,
    qui ne survivent pas à l'installation, ne sont pas comparés."""
    if construite.get('name') != attendue['filename']:
        raise Erreur('nom de la roue ameesh reconstruite différent du wheelhouse')

    def membres(archive):
        infos = archive.infolist()
        noms = [info.filename for info in infos]
        if len(noms) != len(set(noms)):
            raise Erreur('entrée en double dans une roue')
        resultat = {}
        for info in infos:
            nom = info.filename
            if (not nom or nom.startswith('/') or '\\' in nom
                    or '..' in nom.split('/')):
                raise Erreur('chemin de roue non sûr')
            resultat[nom] = info
        return resultat

    try:
        with zipfile.ZipFile(construite['path']) as gauche, \
                zipfile.ZipFile(attendue['path']) as droite:
            a, b = membres(gauche), membres(droite)
            if set(a) != set(b):
                raise Erreur('contenu de la roue ameesh différent du wheelhouse')
            for nom in sorted(a):
                ia, ib = a[nom], b[nom]
                if ia.file_size != ib.file_size:
                    raise Erreur('contenu de roue différent (taille) : %s' % nom)
                if ((ia.external_attr >> 16) & 0o777
                        != (ib.external_attr >> 16) & 0o777):
                    raise Erreur(
                        'mode de roue différent (construire avec umask 022) : %s'
                        % nom)
                with gauche.open(ia) as fa, droite.open(ib) as fb:
                    while True:
                        ca, cb = fa.read(65536), fb.read(65536)
                        if ca != cb:
                            raise Erreur('contenu de roue différent : %s' % nom)
                        if not ca:
                            break
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile,
            RuntimeError, NotImplementedError, EOFError):
        raise Erreur('roue ameesh illisible') from None


def _creer_venv(chemin, *, python, env):
    construction = _construction()
    try:
        return construction.create_venv(chemin, python=python,
                                        with_pip=True, env=env)
    except construction.BuildError as erreur:
        raise Erreur('création du venv impossible : %s' % erreur) from None


def _pip(venv, *arguments, env):
    construction = _construction()
    try:
        return construction.pip(venv, *arguments, env=env)
    except construction.BuildError as erreur:
        raise Erreur('pip a refusé le geste : %s' % erreur) from None


def _dossier_site(venv):
    venv = Path(venv)
    lib = venv / 'lib'
    if not lib.is_dir() or lib.is_symlink():
        raise Ecart('lib/ absent du venv')
    sites = sorted(lib.glob('python*/site-packages'))
    if len(sites) != 1 or not sites[0].is_dir() or sites[0].is_symlink():
        raise Ecart('un seul site-packages attendu dans le venv')
    return sites[0]


def _lire_metadata(chemin, proprietaires):
    donnees = _lire_regulier(chemin, MAX_METADATA_BYTES,
                             proprietaires=proprietaires)
    try:
        texte = donnees.decode('utf-8')
    except UnicodeDecodeError:
        raise Ecart('METADATA non UTF-8 : %s' % Path(chemin).name) from None
    noms, versions = [], []
    for ligne in texte.splitlines():
        if ligne.startswith('Name:'):
            noms.append(ligne.split(':', 1)[1].strip())
        elif ligne.startswith('Version:'):
            versions.append(ligne.split(':', 1)[1].strip())
        elif not ligne.strip():
            break
    if len(noms) != 1 or len(versions) != 1:
        raise Ecart('METADATA sans nom ou version unique : %s' % Path(chemin).name)
    nom, version = _canonical_name(noms[0]), versions[0]
    if not CLEAN_NAME.fullmatch(nom) or not VERSION.fullmatch(version):
        raise Ecart('nom ou version invalide dans METADATA : %s' % Path(chemin).name)
    return dict(name=nom, version=version)


def _distributions(venv, proprietaires):
    site = _dossier_site(venv)
    if any(site.glob('*.egg-info')):
        raise Ecart('egg-info refusé dans le venv')
    distributions = []
    for dist_info in sorted(site.glob('*.dist-info')):
        if not dist_info.is_dir() or dist_info.is_symlink():
            raise Ecart('dist-info invalide : %s' % dist_info.name)
        distributions.append(_lire_metadata(dist_info / 'METADATA', proprietaires))
    if not distributions:
        raise Ecart('aucune distribution installée')
    noms = [item['name'] for item in distributions]
    if len(set(noms)) != len(noms):
        raise Ecart('distribution installée en double')
    return sorted(distributions, key=lambda item: item['name'])


def _entree_normalisee(donnees, venv, mode):
    """Type de lanceur, ligne, mode et corps canoniques d'un script."""
    if not donnees:
        raise Ecart('script d’entrée vide')
    prefixe = os.fsencode(str(Path(venv).resolve()))
    lignes = donnees.splitlines(keepends=True)
    if lignes[0].startswith(b'#!/bin/sh'):
        if len(lignes) < 3:
            raise Ecart('préambule shell incomplet')
        preambule = b''.join(lignes[:3])
        if prefixe not in preambule:
            raise Ecart('préambule shell sans l’interpréteur du venv')
        type_lanceur = 'sh-preamble'
        lanceur = preambule.replace(prefixe, b'<VENV>')
        corps = b''.join(lignes[3:])
    elif lignes[0].startswith(b'#!'):
        if prefixe + b'/bin/python' not in lignes[0]:
            raise Ecart('shebang sans l’interpréteur du venv')
        type_lanceur = 'direct'
        lanceur = lignes[0].replace(prefixe, b'<VENV>')
        corps = b''.join(lignes[1:])
    else:
        raise Ecart('script d’entrée sans shebang')
    canonique = (type_lanceur.encode('ascii') + b'\0' + lanceur + b'\0'
                 + str(mode).encode('ascii') + b'\0' + corps)
    return dict(launcher=type_lanceur,
                sha256=hashlib.sha256(canonique).hexdigest(),
                taille=len(lanceur) + len(corps))


def _controler_bytecode(site):
    """Seul le bytecode du pip de bootstrap est toléré dans site-packages."""
    for repertoire, enfants, noms in os.walk(site, followlinks=False):
        chemin = Path(repertoire)
        relatif = chemin.relative_to(site).as_posix()
        if relatif == '.':
            relatif = ''
        for nom in sorted(enfants):
            if nom == '__pycache__':
                chemin_rel = (relatif + '/' + nom).lstrip('/')
                if not chemin_rel.startswith('pip/'):
                    raise Ecart('bytecode inattendu dans le venv : %s' % chemin_rel)
        for nom in sorted(noms):
            if nom.endswith('.pyc'):
                chemin_rel = (relatif + '/' + nom).lstrip('/')
                if not chemin_rel.startswith('pip/'):
                    raise Ecart('bytecode inattendu dans le venv : %s' % chemin_rel)


def _entrees(venv, site, proprietaires):
    """Les scripts de bin/ déclarés par les RECORD, normalisés par le venv."""
    entrees = {}
    for record in sorted(site.glob('*.dist-info/RECORD')):
        try:
            texte = _lire_regulier(record, MAX_METADATA_BYTES,
                                   proprietaires=proprietaires).decode('utf-8')
        except UnicodeDecodeError:
            raise Ecart('RECORD non UTF-8 : %s' % record.name) from None
        for ligne in csv.reader(io.StringIO(texte)):
            if len(ligne) != 3:
                raise Ecart('ligne RECORD invalide : %s' % record.name)
            chemin = ligne[0]
            if not chemin.startswith('../../../bin/'):
                continue
            nom = chemin[len('../../../bin/'):]
            if (not nom or '/' in nom or nom in ('.', '..')
                    or not CLEAN_NAME.fullmatch(nom)):
                raise Ecart('script de bin/ invalide dans RECORD')
            if nom in entrees:
                raise Ecart('script de bin/ déclaré deux fois : %s' % nom)
            fichier = Path(venv) / 'bin' / nom
            donnees = _lire_regulier(fichier, MAX_METADATA_BYTES,
                                     proprietaires=proprietaires)
            mode = stat.S_IMODE(fichier.lstat().st_mode)
            entrees[nom] = _entree_normalisee(donnees, venv, mode)
    return entrees


def _activations(venv, proprietaires):
    """Les scripts d'activation, normalisés par le chemin du venv."""
    prefixe = os.fsencode(str(Path(venv).resolve()))
    resultat = []
    for nom in ACTIVATIONS:
        fichier = Path(venv) / 'bin' / nom
        if not fichier.exists() and not fichier.is_symlink():
            continue
        donnees = _lire_regulier(fichier, MAX_METADATA_BYTES,
                                 proprietaires=proprietaires)
        if nom == 'Activate.ps1':
            # Ce script PowerShell ne nomme pas le venv : il est déjà stable.
            normalisees = donnees
        elif prefixe in donnees:
            normalisees = donnees.replace(prefixe, b'<VENV>')
        else:
            raise Ecart('script d’activation sans le chemin du venv : %s' % nom)
        resultat.append(dict(name=nom, sha256=hashlib.sha256(
            normalisees).hexdigest()))
    if not resultat:
        raise Ecart('aucun script d’activation dans le venv')
    return resultat


def _interpreteur(venv):
    """Le Python de base épinglable, mesuré sans exécuter le venv."""
    venv = Path(venv)
    propriete = _proprietaires(venv)
    _dossier_protege(venv, proprietaires=propriete)
    cfg = venv / 'pyvenv.cfg'
    try:
        texte = _lire_regulier(cfg, 64 * 1024,
                               proprietaires=propriete).decode('utf-8')
    except UnicodeDecodeError:
        raise Ecart('pyvenv.cfg non UTF-8') from None
    valeurs = {}
    for ligne in texte.splitlines():
        if not ligne.strip():
            continue
        if '=' not in ligne:
            raise Ecart('ligne pyvenv.cfg invalide')
        cle, valeur = ligne.split('=', 1)
        cle, valeur = cle.strip(), valeur.strip()
        if cle in valeurs:
            raise Ecart('clé pyvenv.cfg en double : %s' % cle)
        valeurs[cle] = valeur
    for cle in ('home', 'include-system-site-packages', 'version', 'executable'):
        if cle not in valeurs:
            raise Ecart('pyvenv.cfg sans %s' % cle)
    if valeurs['include-system-site-packages'] != 'false':
        raise Ecart('include-system-site-packages doit être false')
    if not VERSION.fullmatch(valeurs['version']):
        raise Ecart('version Python invalide dans pyvenv.cfg')
    home = Path(valeurs['home'])
    executable = Path(valeurs['executable'])
    if not home.is_absolute() or not executable.is_absolute():
        raise Ecart('chemins absolus attendus dans pyvenv.cfg')
    try:
        home_resolu = home.resolve(strict=True)
        executable_resolu = executable.resolve(strict=True)
    except OSError:
        raise Ecart('interpréteur de base absent') from None
    try:
        _dossier_protege(home_resolu, proprietaires=frozenset((0,)))
    except Erreur as erreur:
        raise Ecart('home de l’interpréteur non root : %s' % erreur) from None
    try:
        digest, _taille = _sha256_regulier(
            executable_resolu, MAX_PYTHON_BYTES,
            proprietaires=frozenset((0,)), libelle='interpréteur')
    except Erreur as erreur:
        raise Ecart('interpréteur de base non sûr : %s' % erreur) from None
    for nom in ('python', 'python3'):
        lien = venv / 'bin' / nom
        if not lien.is_symlink() or not lien.exists():
            raise Ecart('bin/python doit être un lien vers le Python de base')
        if not os.path.samefile(lien.resolve(), executable_resolu):
            raise Ecart('bin/%s ne pointe pas vers l’interpréteur de base' % nom)
    for nom in sorted((venv / 'bin').iterdir()):
        if nom.name.startswith('python'):
            if not nom.is_symlink() or not nom.exists():
                raise Ecart('lien Python invalide dans bin/ : %s' % nom.name)
            if not os.path.samefile(nom.resolve(), executable_resolu):
                raise Ecart('lien Python étranger dans bin/ : %s' % nom.name)
    return dict(version=valeurs['version'], home=str(home_resolu),
                executable=str(executable_resolu), sha256=digest)


def _version_executable(interpreteur):
    """La version réellement imprimée par le binaire épinglé, après contrôle."""
    with tempfile.TemporaryDirectory(prefix='venv-staging-version-') as home:
        environnement = dict(
            PATH='/usr/bin:/bin', HOME=home,
            LANG='C', LC_ALL='C', TZ='UTC', PYTHONHASHSEED='0',
            PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        try:
            resultat = subprocess.run(
                [interpreteur['executable'], '-I', '-S', '--version'],
                env=environnement, cwd=home, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Ecart('interpréteur de base non exécutable') from None
    sortie = (resultat.stdout + resultat.stderr).decode('utf-8', 'replace').strip()
    correspondance = re.fullmatch(
        r'Python ([0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.+_-]*)', sortie)
    if resultat.returncode != 0 or correspondance is None:
        raise Ecart('version de l’interpréteur de base illisible')
    if correspondance.group(1) != interpreteur['version']:
        raise Ecart('version de l’interpréteur de base différente de pyvenv.cfg')
    return correspondance.group(1)


def _controler_bin(venv, entrees, activations):
    bin_dir = Path(venv) / 'bin'
    _dossier_protege(bin_dir, proprietaires=_proprietaires(venv))
    noms_activation = {item['name'] for item in activations}
    for entree in sorted(bin_dir.iterdir(), key=lambda item: item.name):
        if entree.is_symlink():
            if not PYTHON_LINK.fullmatch(entree.name):
                raise Ecart('lien inattendu dans bin/ : %s' % entree.name)
            if not entree.exists():
                raise Ecart('lien Python cassé : %s' % entree.name)
        elif entree.is_dir():
            raise Ecart('dossier inattendu dans bin/ : %s' % entree.name)
        elif not entree.is_file():
            raise Ecart('entrée non régulière dans bin/ : %s' % entree.name)
        elif entree.name in entrees:
            info = entree.stat()
            if not info.st_mode & 0o111:
                raise Ecart('script de bin/ non exécutable : %s' % entree.name)
        elif entree.name not in noms_activation:
            raise Ecart('script inattendu dans bin/ : %s' % entree.name)


def _normaliser_record(donnees, entrees):
    """Un RECORD stable malgré le chemin du venv dans les lignes de bin/."""
    try:
        texte = donnees.decode('utf-8')
    except UnicodeDecodeError:
        raise Ecart('RECORD non UTF-8') from None
    lignes = []
    for ligne in csv.reader(io.StringIO(texte)):
        if len(ligne) != 3:
            raise Ecart('ligne RECORD invalide')
        chemin = ligne[0]
        if chemin.startswith('../../../bin/'):
            nom = chemin[len('../../../bin/'):]
            if nom not in entrees:
                raise Ecart('RECORD cite un script inconnu : %s' % nom)
            info = entrees[nom]
            digest = base64.urlsafe_b64encode(
                bytes.fromhex(info['sha256'])).rstrip(b'=').decode('ascii')
            ligne = [chemin, 'sha256=' + digest, str(info['taille'])]
        lignes.append(ligne)
    sortie = io.StringIO()
    csv.writer(sortie, lineterminator='\n').writerows(lignes)
    return sortie.getvalue().encode('utf-8')


def _mesurer_arbre(racine, *, proprietaires, ignorer=(), exclure_pycache=False,
                   entrees=None, venv=None):
    """SHA-256 canonique d'un arbre : chemins relatifs, modes et contenus."""
    racine = Path(racine)
    _dossier_protege(racine, proprietaires=proprietaires)
    digest = hashlib.sha256()
    fichiers = dossiers = octets = 0
    for repertoire, enfants, noms in os.walk(racine, topdown=True,
                                             followlinks=False):
        chemin = Path(repertoire)
        relatif = chemin.relative_to(racine).as_posix()
        if relatif == '.':
            relatif = ''
        gardes = []
        for nom in sorted(enfants):
            if relatif == '' and nom in ignorer:
                continue
            if exclure_pycache and nom == '__pycache__':
                continue
            gardes.append(nom)
        enfants[:] = gardes
        for nom in sorted(noms):
            if relatif == '' and nom in ignorer:
                continue
            if venv is not None and relatif == '' and nom == 'pyvenv.cfg':
                continue
            if exclure_pycache and (nom.endswith('.pyc') or nom.endswith('.pyo')):
                continue
            fichier = chemin / nom
            relative = fichier.relative_to(racine).as_posix()
            info = fichier.lstat()
            if (not stat.S_ISREG(info.st_mode) or fichier.is_symlink()
                    or info.st_nlink != 1 or info.st_uid not in proprietaires
                    or info.st_mode & 0o022 or info.st_mode & 0o7000):
                raise Ecart('fichier non sûr dans l’arbre : %s' % relative)
            donnees = _lire_regulier(fichier, MAX_TREE_BYTES,
                                     proprietaires=proprietaires)
            apres_info = fichier.lstat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_ctime_ns) != (
                    apres_info.st_dev, apres_info.st_ino, apres_info.st_size,
                    apres_info.st_mtime_ns, apres_info.st_ctime_ns):
                raise Ecart('fichier changé pendant la mesure : %s' % relative)
            if (venv is not None and relative.endswith('/RECORD')
                    and '/site-packages/' in relative):
                if entrees is None:
                    raise Ecart('RECORD rencontré sans table d’entrées')
                donnees = _normaliser_record(donnees, entrees)
            mode = stat.S_IMODE(info.st_mode)
            digest.update(('F\0%s\0%o\0%s\0%d\n' % (
                relative, mode, hashlib.sha256(donnees).hexdigest(),
                len(donnees))).encode('utf-8'))
            fichiers += 1
            octets += len(donnees)
            if octets > MAX_TREE_BYTES:
                raise Ecart('arbre trop grand refusé')
        for nom in sorted(enfants):
            enfant = chemin / nom
            relative = enfant.relative_to(racine).as_posix()
            info = enfant.lstat()
            if (not stat.S_ISDIR(info.st_mode) or enfant.is_symlink()
                    or info.st_uid not in proprietaires
                    or info.st_mode & 0o022 or info.st_mode & 0o7000):
                raise Ecart('dossier non sûr dans l’arbre : %s' % relative)
            digest.update(('D\0%s\0%o\n' % (
                relative, stat.S_IMODE(info.st_mode))).encode('utf-8'))
            dossiers += 1
    if fichiers == 0:
        raise Ecart('arbre vide refusé')
    return dict(tree_sha256=digest.hexdigest(), files=fichiers,
                dirs=dossiers, bytes=octets)


def _mesurer_venv(venv, *, interpreteur=None):
    """Tout le venv : arbre hors bin/pyvenv, distributions, entrées, activation."""
    venv = Path(venv)
    if not venv.is_dir() or venv.is_symlink():
        raise Ecart('dossier de venv attendu')
    propriete = _proprietaires(venv)
    _dossier_protege(venv, proprietaires=propriete)
    lib64 = venv / 'lib64'
    if lib64.exists() or lib64.is_symlink():
        if not lib64.is_symlink() or (lib64.resolve() != (venv / 'lib').resolve()):
            raise Ecart('lib64 inattendu dans le venv')
    if interpreteur is None:
        interpreteur = _interpreteur(venv)
    site = _dossier_site(venv)
    distributions = _distributions(venv, propriete)
    _controler_bytecode(site)
    entrees = _entrees(venv, site, propriete)
    activations = _activations(venv, propriete)
    _controler_bin(venv, entrees, activations)
    arbre = _mesurer_arbre(
        venv, proprietaires=propriete, ignorer={'bin', 'lib64'},
        exclure_pycache=True, entrees=entrees, venv=venv)
    return dict(python=interpreteur, distributions=distributions, tree=arbre,
                entry_points=sorted(entrees.items()),
                activation=activations)


def _exiger(condition, message):
    if not condition:
        raise Erreur(message)


def _liste(items, champs, etiquette, cle):
    _exiger(isinstance(items, list) and bool(items),
            'manifeste : %s vide' % etiquette)
    valeurs = []
    for item in items:
        _exiger(isinstance(item, dict) and set(item) == set(champs),
                'manifeste : champs de %s' % etiquette)
        for champ, motif in champs.items():
            _exiger(isinstance(item[champ], str)
                    and motif.fullmatch(item[champ]) is not None,
                    'manifeste : %s.%s' % (etiquette, champ))
        valeurs.append(item[cle])
    _exiger(valeurs == sorted(valeurs) and len(valeurs) == len(set(valeurs)),
            'manifeste : %s trié et sans doublon' % etiquette)
    return {item[cle]: item for item in items}


def _valider_manifeste(document):
    if not isinstance(document, dict):
        raise Erreur('manifeste : objet JSON attendu')
    _exiger(set(document) == {'format', 'sha', 'source_tree_sha256', 'python',
                              'bootstrap', 'lock', 'distributions', 'wheels',
                              'installed'},
            'manifeste : champs exacts attendus')
    _exiger(document['format'] == FORMAT, 'manifeste : format inconnu')
    _exiger(isinstance(document['sha'], str)
            and SHA.fullmatch(document['sha']) is not None, 'manifeste : SHA')
    _exiger(isinstance(document['source_tree_sha256'], str)
            and HEX256.fullmatch(document['source_tree_sha256']) is not None,
            'manifeste : source_tree_sha256')
    python = document['python']
    _exiger(isinstance(python, dict)
            and set(python) == {'version', 'home', 'executable', 'sha256'},
            'manifeste : python')
    _exiger(isinstance(python['version'], str)
            and VERSION.fullmatch(python['version']) is not None,
            'manifeste : python.version')
    for champ in ('home', 'executable'):
        _exiger(isinstance(python[champ], str)
                and python[champ].startswith('/')
                and '\x00' not in python[champ],
                'manifeste : python.%s' % champ)
    _exiger(isinstance(python['sha256'], str)
            and HEX256.fullmatch(python['sha256']) is not None,
            'manifeste : python.sha256')
    lock = document['lock']
    _exiger(isinstance(lock, dict) and set(lock) == {'sha256', 'distributions'},
            'manifeste : lock')
    _exiger(isinstance(lock['sha256'], str)
            and HEX256.fullmatch(lock['sha256']) is not None,
            'manifeste : lock.sha256')
    distributions = _liste(document['distributions'],
                           {'name': CLEAN_NAME, 'version': VERSION},
                           'distributions', 'name')
    bootstrap = _liste(document['bootstrap'],
                       {'name': CLEAN_NAME, 'version': VERSION},
                       'bootstrap', 'name')
    lock_liste = _liste(lock['distributions'],
                        {'name': CLEAN_NAME, 'version': VERSION},
                        'lock.distributions', 'name')
    _exiger('ameesh' in lock_liste and 'setuptools' in lock_liste,
            'manifeste : le lock doit nommer ameesh et setuptools')
    _exiger('pip' in bootstrap, 'manifeste : pip bootstrap absent')
    for nom in set(bootstrap) & set(lock_liste):
        _exiger(bootstrap[nom]['version'] == lock_liste[nom]['version'],
                'manifeste : le lock remplacerait %s du bootstrap' % nom)
    _exiger(set(distributions) == set(bootstrap) | set(lock_liste),
            'manifeste : distributions != bootstrap + lock')
    wheels = _liste(document['wheels'],
                    {'filename': WHEEL_FILE, 'name': CLEAN_NAME,
                     'version': VERSION, 'sha256': HEX256},
                    'wheels', 'filename')
    par_nom = {}
    for roue in wheels.values():
        _exiger(roue['name'] in lock_liste, 'manifeste : roue hors lock')
        _exiger(roue['version'] == lock_liste[roue['name']]['version'],
                'manifeste : version de roue != lock')
        _exiger(roue['name'] not in par_nom,
                'manifeste : deux roues pour %s' % roue['name'])
        par_nom[roue['name']] = roue
    _exiger(set(par_nom) == set(lock_liste),
            'manifeste : une roue par distribution du lock')
    _exiger('ameesh' in par_nom, 'manifeste : roue ameesh absente')
    installed = document['installed']
    _exiger(isinstance(installed, dict) and set(installed) == {
        'tree_sha256', 'files', 'dirs', 'bytes', 'entry_points', 'activation'},
        'manifeste : installed')
    _exiger(isinstance(installed['tree_sha256'], str)
            and HEX256.fullmatch(installed['tree_sha256']) is not None,
            'manifeste : installed.tree_sha256')
    for champ in ('files', 'dirs', 'bytes'):
        _exiger(type(installed[champ]) is int and installed[champ] >= 0,
                'manifeste : installed.%s' % champ)
    _exiger(installed['files'] > 0 and installed['dirs'] > 0
            and installed['bytes'] > 0, 'manifeste : arbre installé vide')
    _liste(installed['entry_points'],
           {'name': CLEAN_NAME,
            'launcher': re.compile(r'direct|sh-preamble\Z'),
            'sha256': HEX256}, 'entry_points', 'name')
    _liste(installed['activation'],
           {'name': CLEAN_NAME, 'sha256': HEX256}, 'activation', 'name')
    return document


def _lire_manifeste(chemin):
    _dossier_protege(Path(chemin).parent)
    donnees = _lire_regulier(chemin, MAX_MANIFEST_BYTES)
    try:
        document = json.loads(donnees.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Erreur('manifeste JSON illisible') from None
    return _valider_manifeste(document)


def _ecrire_manifeste(chemin, document):
    chemin = Path(chemin)
    parent = chemin.parent
    _dossier_protege(parent)
    donnees = (_canonical(document) + '\n').encode('utf-8')
    if chemin.exists() or chemin.is_symlink():
        if chemin.is_symlink() or not chemin.is_file():
            raise Erreur('sortie non régulière refusée : %s' % chemin)
        if _lire_regulier(chemin, MAX_MANIFEST_BYTES) == donnees:
            return False
        raise Erreur('un manifeste différent existe déjà : %s' % chemin)
    temporaire = parent / ('.venv-staging-%d-%s'
                           % (os.getpid(), os.urandom(4).hex()))
    fd = os.open(temporaire, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o444)
    try:
        with os.fdopen(fd, 'wb') as flux:
            flux.write(donnees)
            flux.flush()
            os.fsync(flux.fileno())
        os.replace(temporaire, chemin)
        descripteur = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descripteur)
        finally:
            os.close(descripteur)
    finally:
        try:
            temporaire.unlink()
        except OSError:
            pass
    return True


def _pip_check(venv):
    venv = Path(venv)
    python = venv / 'bin' / 'python'
    if not python.exists():
        raise Ecart('python du venv absent pour pip check')
    with tempfile.TemporaryDirectory(prefix='venv-staging-pip-') as home:
        environnement = dict(
            PATH='/usr/bin:/bin', HOME=home,
            LANG='C', LC_ALL='C', TZ='UTC', PYTHONHASHSEED='0',
            PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
            PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT='1', PIP_NO_INDEX='1',
            PIP_NO_CACHE_DIR='1', PIP_DISABLE_PIP_VERSION_CHECK='1',
            TMPDIR=home)
        try:
            # -B et un cache de bytecode hors du venv : le pip de bootstrap
            # est relu depuis sa source vérifiée, jamais depuis ses `.pyc`.
            resultat = subprocess.run(
                [str(python), '-I', '-B', '-X',
                 'pycache_prefix=' + os.path.join(home, 'pycache'),
                 '-m', 'pip', 'check'], env=environnement, cwd=home,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise Ecart('pip check non exécutable') from None
    if resultat.returncode != 0:
        detail = (resultat.stdout + resultat.stderr).decode(
            'utf-8', 'replace').strip().splitlines()
        raise Ecart('pip check échoue : %s' % (' '.join(detail[-2:])[:400]))


def _produire(depot, *, sha, tag, lock_path, wheelhouse, python, output,
              travail=None):
    """Produire le manifeste ; ``travail`` (tests) est conservé tel quel."""
    if os.geteuid() == 0:
        raise Erreur('la production doit se faire hors root')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        if output.is_symlink() or not output.is_file():
            raise Erreur('sortie non régulière refusée : %s' % output)
    _dossier_protege(output.parent)
    lock_path = Path(lock_path).absolute()
    wheelhouse = Path(wheelhouse).absolute()
    if output.resolve() == lock_path.resolve():
        raise Erreur('la sortie ne peut pas être le lock')
    if wheelhouse in output.parents:
        raise Erreur('la sortie ne peut pas entrer dans le wheelhouse')
    sha, epoch = _resoudre_commit(depot, sha=sha, tag=tag)
    lock = _lire_lock(lock_path)
    roues = _lire_wheelhouse(wheelhouse)
    par_nom = _apparier_lock_wheelhouse(lock, roues)

    propre = travail is None
    if propre:
        racine = Path(tempfile.mkdtemp(prefix='ameesh-venv-staging-'))
    else:
        racine = Path(travail).absolute()
        if racine.exists():
            if not racine.is_dir() or any(racine.iterdir()):
                raise Erreur('répertoire de travail vide attendu')
        else:
            racine.mkdir(mode=0o700)
    ancien_masque = os.umask(0o022)
    try:
        home = racine / 'home'
        home.mkdir(mode=0o700)
        (home / 'tmp').mkdir(mode=0o700)
        env_git = _git_env(home)
        env_pip = _pip_env(home, epoch)
        source = racine / 'source'
        _extraire_source(Path(depot).absolute(), sha, source, env_git)
        try:
            source_arbre = _mesurer_arbre(
                source, proprietaires=frozenset((0, os.geteuid())))
        except Ecart as erreur:
            raise Erreur('source du commit non mesurable : %s' % erreur) from None
        outil = _preparer_outil(python, par_nom['setuptools'], racine, env_pip)
        sortie = racine / 'roue'
        sortie.mkdir(mode=0o700)
        construite = _construire_roue(outil, source, sortie, env_pip)
        _comparer_roues(construite, par_nom['ameesh'])
        venv = racine / 'venv'
        _creer_venv(venv, python=python, env=env_pip)
        try:
            bootstrap = _distributions(venv, frozenset((0, os.geteuid())))
        except Ecart as erreur:
            raise Erreur('venv de base non mesurable : %s' % erreur) from None
        base = {item['name']: item for item in bootstrap}
        for nom in sorted(set(base) & set(lock['records'])):
            if base[nom]['version'] != lock['records'][nom]['version']:
                raise Erreur('le lock remplacerait %s du bootstrap' % nom)
        _pip(venv, 'install', '--no-index', '--find-links', str(wheelhouse),
             '--require-hashes', '--only-binary=:all:', '--no-compile',
             '--no-cache-dir', '-r', str(lock_path), env=env_pip)
        _pip(venv, 'check', env=env_pip)
        try:
            mesure = _mesurer_venv(venv)
        except Ecart as erreur:
            raise Erreur('venv produit non mesurable : %s' % erreur) from None
        try:
            _version_executable(mesure['python'])
        except Ecart as erreur:
            raise Erreur('interpréteur du venv produit : %s' % erreur) from None
        attendues = dict(base)
        for record in lock['records'].values():
            attendues[record['name']] = dict(name=record['name'],
                                             version=record['version'])
        if set(attendues) != set(item['name'] for item in mesure['distributions']):
            raise Erreur('inventaire installé différent du bootstrap + lock')
        for item in mesure['distributions']:
            if item['version'] != attendues[item['name']]['version']:
                raise Erreur('version installée inattendue : %s' % item['name'])
        document = dict(
            format=FORMAT,
            sha=sha,
            source_tree_sha256=source_arbre['tree_sha256'],
            python=mesure['python'],
            bootstrap=sorted(bootstrap, key=lambda item: item['name']),
            lock=dict(sha256=lock['digest'],
                      distributions=sorted(
                          (dict(name=value['name'], version=value['version'])
                           for value in lock['records'].values()),
                          key=lambda item: item['name'])),
            distributions=mesure['distributions'],
            wheels=sorted(
                (dict(filename=roue['filename'], name=roue['name'],
                      version=lock['records'][roue['name']]['version'],
                      sha256=roue['sha256']) for roue in roues),
                key=lambda item: item['filename']),
            installed=dict(
                tree_sha256=mesure['tree']['tree_sha256'],
                files=mesure['tree']['files'],
                dirs=mesure['tree']['dirs'],
                bytes=mesure['tree']['bytes'],
                entry_points=[
                    dict(name=nom, launcher=value['launcher'],
                         sha256=value['sha256'])
                    for nom, value in mesure['entry_points']],
                activation=mesure['activation']),
        )
        _valider_manifeste(document)
        _ecrire_manifeste(output, document)
        return document
    finally:
        os.umask(ancien_masque)
        if propre:
            shutil.rmtree(racine, ignore_errors=True)


def _comparer(attendu, mesure, etiquette):
    if attendu == mesure:
        return
    if isinstance(attendu, dict) and isinstance(mesure, dict):
        champs = sorted(set(attendu) | set(mesure))
        differents = [champ for champ in champs
                      if attendu.get(champ) != mesure.get(champ)]
        raise Ecart('%s : champs différents : %s'
                    % (etiquette, ', '.join(differents)))
    if (isinstance(attendu, list) and isinstance(mesure, list)
            and attendu and isinstance(attendu[0], dict)
            and 'name' in attendu[0]):
        a = {item['name']: item for item in attendu}
        m = {item['name']: item for item in mesure}
        manquants = sorted(set(a) - set(m))
        superflus = sorted(set(m) - set(a))
        differents = sorted(nom for nom in set(a) & set(m) if a[nom] != m[nom])
        detail = []
        if manquants:
            detail.append('manquants: ' + ', '.join(manquants[:8]))
        if superflus:
            detail.append('en trop: ' + ', '.join(superflus[:8]))
        if differents:
            detail.append('différents: ' + ', '.join(differents[:8]))
        raise Ecart('%s : %s' % (etiquette, ' ; '.join(detail) or 'écart'))
    raise Ecart('%s : %s attendu, %s observé' % (etiquette, attendu, mesure))


def _verifier(manifest_path, venv):
    document = _lire_manifeste(manifest_path)
    venv = Path(venv)
    if venv.is_symlink():
        venv = venv.resolve()
    interpreteur = _interpreteur(venv)
    # L'interpréteur est comparé au manifeste AVANT toute exécution.
    _comparer(document['python'], interpreteur, 'interpréteur')
    _version_executable(interpreteur)
    mesure = _mesurer_venv(venv, interpreteur=interpreteur)
    _comparer(document['distributions'], mesure['distributions'], 'distributions')
    installed = document['installed']
    _comparer(installed['entry_points'],
              [dict(name=nom, launcher=value['launcher'],
                    sha256=value['sha256'])
               for nom, value in mesure['entry_points']],
              'scripts d’entrée')
    _comparer(installed['activation'], mesure['activation'],
              'scripts d’activation')
    _comparer(installed['tree_sha256'], mesure['tree']['tree_sha256'],
              'arbre installé')
    _comparer(installed['files'], mesure['tree']['files'], 'nombre de fichiers')
    _comparer(installed['dirs'], mesure['tree']['dirs'], 'nombre de dossiers')
    _comparer(installed['bytes'], mesure['tree']['bytes'], 'octets installés')
    _pip_check(venv)
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Manifeste verrouillé du venv de staging (#5666).')
    sous = parser.add_subparsers(dest='commande', required=True)
    produire = sous.add_parser('produire', help='produire un manifeste')
    produire.add_argument('--clone', required=True)
    groupe = produire.add_mutually_exclusive_group(required=True)
    groupe.add_argument('--sha')
    groupe.add_argument('--tag')
    produire.add_argument('--lock', required=True)
    produire.add_argument('--wheelhouse', required=True)
    produire.add_argument('--python', default='/usr/bin/python3')
    produire.add_argument('--output', required=True)
    verifier = sous.add_parser('verifier', help='vérifier un venv installé')
    verifier.add_argument('--manifeste', required=True)
    verifier.add_argument('--venv', required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.commande == 'produire':
            document = _produire(
                arguments.clone, sha=arguments.sha, tag=arguments.tag,
                lock_path=arguments.lock, wheelhouse=arguments.wheelhouse,
                python=arguments.python, output=arguments.output)
            print('manifeste: %s' % Path(arguments.output))
            print('sha: %s' % document['sha'])
            print('arbre installé: %s' % document['installed']['tree_sha256'])
            print('distributions: %d, roues: %d'
                  % (len(document['distributions']), len(document['wheels'])))
            return 0
        document = _verifier(arguments.manifeste, arguments.venv)
        print('venv conforme: %s' % Path(arguments.venv))
        print('sha: %s' % document['sha'])
        print('arbre installé: %s' % document['installed']['tree_sha256'])
        return 0
    except Ecart as erreur:
        print('venv-staging: ÉCART: %s' % erreur, file=sys.stderr)
        return 1
    except (Erreur, OSError) as erreur:
        print('venv-staging: %s' % erreur, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
