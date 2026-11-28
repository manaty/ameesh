# SPDX-License-Identifier: AGPL-3.0-only
"""#5947 — le préflight de vm.sh joué contre le CLI de la release précédente.

Incident du 05/11/2026 (#5937, suite de #5863) : `controler_agents` lançait un
contrôle que seul le CLI N connaît, alors qu'en préflight (`agents verifier`)
c'est le CLI N-1 encore installé qui répond ; la CI ne voyait que N, les deux
tentatives de staging 1.39.0 ont échoué sans aucune installation. Le même
défaut touchait le contrôle qui suit un retour arrière (`retour_agents` finit
par `controler_agents`, avec N-1 installé).

Ce module rejoue ces deux contrôles avec, comme CLI installé, le **code réel
du dernier tag de release** (`git archive`, aucun réseau). Le banc est celui de
test_1252 (faux ssh, runuser, systemctl, systemd-run) ; dans le venv installé,
`ameesh` et `python -c` exécutent le code du tag, seules les frontières
externes sont simulées : la base (seule la lecture de `agent_watches` y est
servie, vide, #8004 ; toute autre requête échoue, nommée), la ligne de rôle du
registre (`registry.get`, fixture) et, pour les lectures du registre des
agents (`list`, `show`, `doctor`, `--version`) et les sondes de base de vm.sh,
les faux de test_1252. Toute autre commande lancée par vm.sh va au vrai CLI
N-1 : un contrôle qu'il ne connaît pas, ou qu'il juge rouge, rend le préflight
rouge ici avant de le rendre rouge en staging.

L'hôte est candidat des deux rôles. Celui de l'auditeur est celui de
l'incident ; celui de `mesh-design`, le rôle par défaut de `role timers`, lit
le canon et `github.projects` : un canon et des réglages GitHub minimaux les
rendent jouables sous le CLI N-1 (#5954). Le rejeu de l'incident, dont le CLI
v1.38.0 précède #5853, remet `mesh-design` non candidat pour rester borné à
#5863.

Le tag N-1 est le plus récent `v*` atteignable depuis HEAD et qui ne le
contient pas. Un clone sans tags (CI en profondeur 1) rend ce module sauté,
sauf sous AMEESH_TEST_N1_REQUIS=1 (job « préflight N-1 » de tests.yml, clone
complet) où l'absence est un échec : la barrière ne se perd pas en silence.

#5954 : un clone dont les tags n'ont pas suivi la forge est aussi sauté hors
du job requis. Le clone dédié du composeur de paquets (`git pull --ff-only`,
sans `--tags`) ne récupère pas les tags de release : le banc y jouerait un N-1
de plusieurs releases en arrière et son rendu d'unités, différent, ferait
rougir le retour arrière pour une raison qui ne dit rien du vrai N-1. La
version du paquet au checkout est celle que la release a taguée : un tag plus
ancien qu'elle trahit ce clone, et le job CI (clone complet, REQUIS) reste le
seul juge.

#7985 : ce saut ne doit plus être muet hors CI. Un worktree d'agent ou le
clone du composeur y rapatrie une fois les tags (`git fetch --tags`, borné)
avant de juger ; s'ils restent absents ou en retard, le saut est nommé
« non concluant (canon §3) », jamais compté vert. Et le module entre dans
tests/parts/toujours.txt : la sélection d'un lot (impactés + contrat, §11 bis)
le joue au lieu de ne le voir qu'à la CI du paquet (#7933, 8 h 20 d'attente).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ameesh import canon

from tests import test_940_agregat_bash as t940
from tests.test_1252_vm_agents_install import AMEESH, DB_PYTHON, ROOT, SCRIPT, VmAgentsHarness

REQUIS = os.environ.get('AMEESH_TEST_N1_REQUIS') == '1'
#: #7985 : la CI clone ce dont chaque job a besoin (le job « préflight N-1 »
#: prend tous les tags). Hors CI — worktree d'agent, clone du composeur — le
#: module prépare lui-même ses tags ; s'il n'y arrive pas, le saut est nommé.
EN_CI = os.environ.get('GITHUB_ACTIONS') == 'true'


def release_precedente(root: Path = ROOT) -> str:
    """Le tag de la dernière release atteignable depuis HEAD, HEAD exclu."""
    try:
        out = subprocess.run(
            ['git', '-C', str(root), 'tag', '--merged', 'HEAD', '--no-contains', 'HEAD',
             '--sort=-v:refname', '--list', 'v[0-9]*'],
            check=True, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
    tags = [t for t in out.split() if re.fullmatch(r'v\d+\.\d+\.\d+', t)]
    return tags[0] if tags else ''


def version_du_paquet(root: Path = ROOT) -> str:
    """La version du paquet au checkout, préfixée `v` (vide si illisible)."""
    try:
        texte = (root / 'pyproject.toml').read_text(encoding='utf-8')
    except OSError:
        return ''
    trouve = re.search(r'(?m)^version\s*=\s*"(\d+\.\d+\.\d+)"', texte)
    return 'v' + trouve.group(1) if trouve else ''


def preparer_tags(root: Path = ROOT, *, timeout: float = 30.0) -> bool:
    """#7985 : rapatrier les tags de release d'un clone qui ne les a pas.

    Un worktree d'agent ou le clone du composeur (`git pull --ff-only`, sans
    `--tags`) peut n'avoir aucun tag `v*` ou des tags en retard : le banc N-1
    sautait alors sans bruit, et le rouge d'un lot ne se voyait qu'à la CI du
    paquet (#7933, 8 h 20). Hors CI, on rapatrie une fois, borné ; un échec
    (réseau, distant absent, verrou) laisse le saut nommé qui suit.

    #7985 : l'instantané d'offload est un dépôt neuf à commit racine, sans
    histoire ni distant : aucun tag n'y est atteignable et un `fetch` y
    attendrait 30 s pour rien à chaque import du guest (revue #44935). On le
    constate sans réseau.
    """
    try:
        parents = subprocess.run(
            ['git', '-C', str(root), 'rev-list', '--parents', '-n', '1', 'HEAD'],
            capture_output=True, text=True, timeout=min(float(timeout), 10.0))
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if parents.returncode != 0 or len(parents.stdout.split()) < 2:
        return False
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'fetch', '--quiet', '--tags',
             '--no-write-fetch-head', 'origin'],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def etat_tags(root: Path = ROOT) -> tuple[str, str, bool]:
    """(tag N-1, version du paquet, tag fiable)."""
    tag = release_precedente(root)
    version = version_du_paquet(root)
    return tag, version, not tag or not version or tag == version


TAG, VERSION, TAG_FIABLE = etat_tags()
if not REQUIS and not EN_CI and (not TAG or not TAG_FIABLE):
    # #7985 : hors CI, un clone sans tags ou en retard les rapatrie avant de
    # juger ; le saut qui reste est nommé « non concluant » (canon §3), jamais
    # un vert muet.
    if preparer_tags():
        TAG, VERSION, TAG_FIABLE = etat_tags()

# Amorce du CLI installé : le code d'un arbre `src` (N-1 ou N) sous l'interpréteur
# du banc, base et ligne de rôle simulées. `-I` : ni PYTHONPATH ni dossier
# courant ; l'arbre est mis en tête explicitement et l'import est vérifié.
BOOT = r'''
import json, os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
src = (state / 'cli-src').read_text().strip()
sys.path.insert(0, src)
import ameesh
assert Path(ameesh.__file__).resolve().is_relative_to(Path(src).resolve()), ameesh.__file__
from ameesh import db as db_mod, registry

class FakeDb:
    """La base de staging existe et sa table des veilles est vide (#8004).

    Depuis 1.67.0 (#7933), `role timers --check` y lit les veilles des rôles
    élus : refuser la lecture rendait le préflight N-1 rouge dès que N-1 lisait
    les veilles (#8004), alors qu'une table vide et lisible est un état sain.
    Seule cette lecture est servie ; toute autre requête échoue, nommée : le
    banc ne simule pas une base vivante.
    """
    def close(self):
        pass
    def query(self, sql, params=()):
        normalized = ' '.join(sql.lower().split())
        if normalized.startswith('select ') and 'from agent_watches' in normalized:
            return []
        raise RuntimeError('banc #5947 : base simulée, query non servi')
    def __getattr__(self, name):
        raise RuntimeError('banc #5947 : base simulée, %s non servi' % name)

roles = {r['name']: r for r in json.loads((state / 'roles.json').read_text())}
db_mod.connect = lambda *a, **k: FakeDb()
registry.get = lambda db, name: roles.get(name)
# #7661 : `role timers` résout le rôle d'observation sur les fiches du
# registre (`overview`), plus sur le nom par défaut ; le faux sert les lignes.
registry.overview = lambda db, *a, **k: list(roles.values())
argv = sys.argv[1:]
with (state / 'cli-n1.log').open('a') as f:
    f.write(' '.join(argv).replace('\n', ' ') + '\n')
if argv[:1] == ['-c']:
    sys.argv = ['-c'] + argv[2:]
    exec(compile(argv[1], '<string>', 'exec'), {'__name__': '__main__'})
else:
    from ameesh.main import main
    sys.argv = ['ameesh'] + argv
    raise SystemExit(main(argv))
'''

# Lanceurs du venv installé. Les lectures du registre des agents et les sondes
# de base de vm.sh restent aux faux de test_1252 ; tout le reste au vrai CLI.
LANCEUR = r'''#!@@SHEBANG@@
import os, sys
from pathlib import Path
state = Path(os.environ['FAKE_STATE'])
argv = sys.argv[1:]
faux = Path(__file__).with_name(@@FAUX@@)
if @@REGISTRE@@:
    os.execv(@@PY@@, [@@PY@@, str(faux)] + argv)
os.execv(@@PY@@, [@@PY@@, '-I', str(state / 'boot.py')] + argv)
'''
REGISTRE_AMEESH = "argv[:1] in (['list'], ['show'], ['doctor'], ['--version'], ['version'])"
# Les sondes de base de DB_PYTHON (test_1252), reconnues à leur texte.
REGISTRE_PYTHON = ("argv[:1] != ['-c'] or any(m in argv[1] for m in ("
                   "'schema_migrations', 'clock_timestamp', 'config.load().host', "
                   "'import psycopg'))")


#: #5954 : canon minimal du banc. La veille GitHub de `role timers --check`
#: (rôle par défaut `mesh-design`) lit le canon et les réglages GitHub de
#: l'hôte ; sans cette carte, l'hôte non candidat rendait le contrôle vert
#: sans jamais exécuter cette branche. Le passage de l'auditeur, lui, n'en
#: dépend pas.
CANON = '''---
type: Persona
title: veilleur
projects: [ameesh]
roles: [github-watch]
github_watch:
  ameesh:
    repos: [manaty/ameesh]
    interval: 900
---
Veilleur du banc #5954 : la carte rend le rôle mesh-design candidat sur
l'hôte du banc et fait lire le canon et `github.projects` par `role timers`.
'''


def motif_n1(tag: str, version: str) -> str:
    """Le motif d'un saut : nommé non concluant (canon §3), jamais un vert."""
    if not tag:
        cause = 'aucun tag de release atteignable (clone sans tags)'
    else:
        cause = ('tags du clone en retard : %s alors que le paquet est %s (#5954)'
                 % (tag, version or 'illisible'))
    return 'non concluant (canon §3) : ' + cause


MOTIF_N1 = '' if (TAG and TAG_FIABLE) else motif_n1(TAG, VERSION)


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "previous-release CLI harness fakes Linux ssh/systemd and /proc")
@unittest.skipUnless(REQUIS or (TAG and TAG_FIABLE), MOTIF_N1)
class PreflightCliPrecedentTest(VmAgentsHarness):
    cli = TAG  # la release dont le code est le CLI installé
    #: #5954 : l'hôte du banc est candidat du rôle `mesh-design` (le défaut de
    #: `role timers --check`) pour couvrir ses passes, pas seulement celles de
    #: l'auditeur. Le rejeu de l'incident le remet non candidat : son CLI
    #: v1.38.0 précède #5853 et rendrait ce scénario rouge pour une raison
    #: étrangère à #5863.
    mesh_design_candidat = True

    def setUp(self):
        if not self.cli:
            self.fail('AMEESH_TEST_N1_REQUIS=1 : aucun tag v* atteignable depuis HEAD')
        super().setUp()
        self.n1 = self.extraire(self.cli, 'n1')
        (self.state / 'boot.py').write_text(BOOT)
        self.canon = self.root / 'canon'
        (self.canon / 'agents').mkdir(parents=True)
        (self.canon / 'agents/veilleur.md').write_text(CANON, encoding='utf-8')
        mesh_design = dict(name='mesh-design', releve='election', host='host-agents',
                           candidats=['host-agents', 'agents-autre'],
                           responsible='human:test', roles=['conception'])
        if not self.mesh_design_candidat:
            mesh_design.update(host='agents-autre', candidats=['agents-autre'])
        (self.state / 'roles.json').write_text(json.dumps([
            mesh_design,
            dict(name='auditeur', releve='election', host='host-agents',
                 candidats=['host-agents', 'agents-autre'], responsible='human:test',
                 roles=['auditeur'])]))
        config = self.vmhome / '.config/ameesh/config.json'
        config.write_text(json.dumps({
            'canon': str(self.canon),
            'canon_untrusted': True,
            'github': {'projects': {'ameesh': 'manaty/ameesh'}},
        }) + '\n')
        # `role timers --install` lit et peut réécrire cette configuration :
        # elle refuse un fichier inscriptible par le groupe ou les autres.
        config.chmod(0o600)
        bin_ = self.venv / 'bin'
        for nom, faux, texte, registre in (('ameesh', 'ameesh-registre', AMEESH, REGISTRE_AMEESH),
                                           ('python', 'python-base', DB_PYTHON, REGISTRE_PYTHON)):
            (bin_ / faux).write_text(texte)
            (bin_ / nom).write_text(LANCEUR.replace('@@SHEBANG@@', sys.executable)
                                    .replace('@@PY@@', repr(sys.executable))
                                    .replace('@@FAUX@@', repr(faux))
                                    .replace('@@REGISTRE@@', registre))
            (bin_ / nom).chmod(0o755)
        self.installer_cli(self.n1 / 'src')

    def extraire(self, ref: str, nom: str) -> Path:
        """`src` et `deploy/mise-a-jour` de ``ref``, en dépôt d'un commit (vm.sh
        lit son dépôt, DEPOT)."""
        dest = self.root / nom
        dest.mkdir()
        archive = subprocess.run(['git', '-C', str(ROOT), 'archive', '--format=tar', ref,
                                  'src', 'deploy/mise-a-jour'],
                                 check=True, capture_output=True, timeout=120).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(dest, filter='data')
        subprocess.run(['git', 'init', '-q', str(dest)], check=True)
        self.git(dest, 'add', 'deploy')
        self.git(dest, '-c', 'user.email=t@invalid', '-c', 'user.name=test',
                 'commit', '-q', '-m', ref)
        return dest

    def installer_cli(self, src: Path):
        (self.state / 'cli-src').write_text(str(src) + '\n')

    def run_vm_de(self, script: Path, action: str):
        return subprocess.run(['bash', str(script), 'agents', action],
                              env=self.env, capture_output=True, text=True, timeout=300)

    def assert_vert(self, p):
        sortie = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, 'CLI installé %s :\n%s' % (self.cli, sortie))
        self.assertIn('contrôles verts', p.stdout)
        # le contrôle est bien passé par le CLI réel du tag, pas par un faux
        journal = (self.state / 'cli-n1.log').read_text()
        self.assertIn('role timers --check', journal)
        if self.mesh_design_candidat:
            # #5954 : le contrôle par défaut a bien pris la branche candidat
            # (canon et github.projects), pas la sortie immédiate « non
            # candidat » qui rendrait le préflight vert sans rien contrôler.
            self.assertNotIn('not a candidate of role mesh-design', sortie)

    def assert_mesh_design_controle(self, pose):
        """#5954 : les passes du rôle mesh-design sont posées et contrôlées.

        `vm.sh timers` pose les minuteries du rôle par défaut avec le CLI
        installé ; leur présence prouve que `role timers --install` a pris la
        branche candidat (canon + `github.projects`), et le préflight vert
        qui suit prouve que `role timers --check` la rejoue sous le CLI N-1.
        """
        if not self.mesh_design_candidat:
            return
        sortie = pose.stdout + pose.stderr
        self.assertEqual(pose.returncode, 0, sortie)
        self.assertNotIn('not a candidate of role mesh-design', pose.stdout)
        self.assertIn('role timers --install', (self.state / 'cli-n1.log').read_text())
        for nom in ('github-watch', 'issues', 'ci', 'coherence'):
            unite = (self.vmhome / '.config/systemd/user'
                     / ('ameesh-role-%s.timer' % nom))
            self.assertTrue(unite.is_file(), '%s absent après %r :\n%s'
                            % (nom, self.cli, sortie))

    def test_le_preflight_est_vert_avec_le_cli_de_la_release_precedente(self):
        # État laissé par la release N-1 : ses minuteries, posées par SON vm.sh.
        pose = self.run_vm_de(self.n1 / 'deploy/mise-a-jour/vm.sh', 'timers')
        self.assert_mesh_design_controle(pose)
        # Préflight de la montée vers N : vm.sh de N, CLI N-1 encore installé.
        self.assert_vert(self.run_vm('verifier'))

    def test_le_controle_apres_retour_arriere_vers_la_release_precedente_est_vert(self):
        # Minuteries posées par N (installation), puis retour au code N-1 :
        # `retour_agents` finit par controler_agents, rejoué ici tel quel.
        self.installer_cli(ROOT / 'src')
        pose = self.run_vm_de(SCRIPT, 'timers')
        self.assert_mesh_design_controle(pose)
        self.installer_cli(self.n1 / 'src')
        self.assert_vert(self.run_vm('controler'))
        # #6643 : le retour a bien reposé la minuterie avec le CLI N-1 avant
        # de la contrôler (l'unité de N lance `ameesh auditor tick`).
        self.assertIn('role timers --role auditeur --install',
                      (self.state / 'cli-n1.log').read_text())


# #5946 : l'incident lui-même. Le dernier tag connaît déjà la passe courte de
# l'auditeur (#5863) : le banc ci-dessus ne passe jamais par la branche « le
# code installé ne la connaît pas ». Ici, le CLI est v1.38.0, la dernière
# release sans #5863, l'état celui des VM du 05/11 (passe de l'auditeur posée,
# à la main alors, par le vm.sh de N ici) ; vm.sh de #5863 sans #5937
# (d0f31e7f3) doit être rouge sur ce contrôle, celui de la tête vert.
CLI_INCIDENT, VM_INCIDENT = 'v1.38.0', 'd0f31e7f3'


def _connu(ref: str) -> bool:
    return subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '-q', '--verify',
                           ref + '^{commit}'], capture_output=True).returncode == 0


@unittest.skipUnless(REQUIS or (_connu(CLI_INCIDENT) and _connu(VM_INCIDENT)),
                     'historique de l\'incident absent (clone sans tags)')
class IncidentRejoueTest(PreflightCliPrecedentTest):
    cli = CLI_INCIDENT
    #: #5954 : v1.38.0 précède #5853 (OnBootSec → OnActiveSec) ; un rôle
    #: mesh-design candidat posé par N puis contrôlé par v1.38.0 serait rouge
    #: pour cette raison, étrangère au scénario #5863. L'hôte reste non
    #: candidat comme avant ce lot.
    mesh_design_candidat = False

    def setUp(self):
        super().setUp()
        # passe de l'auditeur posée par N, CLI N-1 installé ensuite
        self.installer_cli(ROOT / 'src')
        pose = self.run_vm_de(SCRIPT, 'timers')
        self.assertEqual(pose.returncode, 0, pose.stdout + pose.stderr)
        self.installer_cli(self.n1 / 'src')

    def test_le_preflight_est_vert_avec_le_cli_de_la_release_precedente(self):
        p = self.run_vm('verifier')
        self.assert_vert(p)
        self.assertIn('le code installé ne la connaît pas', p.stdout)
        self.assertNotIn('role timers --role auditeur --check',
                         (self.state / 'cli-n1.log').read_text())

    def test_le_controle_apres_retour_arriere_vers_la_release_precedente_est_vert(self):
        self.assert_vert(self.run_vm('controler'))

    def test_le_vm_sh_de_l_incident_est_rouge(self):
        incident = self.extraire(VM_INCIDENT, 'incident')
        p = self.run_vm_de(incident / 'deploy/mise-a-jour/vm.sh', 'verifier')
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('contrôles en échec', p.stderr)
        self.assertIn('role timers --role auditeur --check',
                      (self.state / 'cli-n1.log').read_text())


class RetourFinitParLeControleTest(unittest.TestCase):
    """Le second cas ne vaut que si le retour arrière finit par controler_agents."""

    def test_retour_agents_finit_par_controler_agents(self):
        texte = SCRIPT.read_text(encoding='utf-8')
        debut = texte.index('retour_agents() {')
        corps = texte[debut:texte.index('\n}\n', debut)]
        # #8380 : le retour repose les unités avec le CLI installé avant de
        # les contrôler ; ce qui compte ici est que le contrôle reste le
        # dernier geste, après le redémarrage et la pose.
        self.assertTrue(corps.rstrip().endswith('controler_agents'), corps[-200:])
        self.assertLess(corps.index('redemarrer_agents'), corps.index('controler_agents'))
        self.assertRegex(texte, r'\n    verifier\|controler\) controler_agents ;;\n')



# #5946 : règle statique. Chaque appel du CLI installé (`ameesh_agents`,
# `python_agents`) dans controler_agents déclare les étapes où il tourne :
# `verifier` (préflight, CLI N-1 encore installé), `controler` (après
# l'installation, CLI N) et `retour` (après un retour arrière, CLI N-1). Un
# contrôle qui ne vaut que pour N (sans `verifier` ni `retour`) nomme la garde
# qui l'abrite, lue dans le corps avant l'appel. Un contrôle ajouté sans
# déclaration fait échouer ce test : son auteur doit dire s'il tient sous N-1.
TOUTES = ('verifier', 'controler', 'retour')
CONTROLES_CLI = {
    'ameesh_agents doctor --profiles': dict(etapes=TOUTES),
    'ameesh_agents doctor': dict(etapes=TOUTES),
    'ameesh_agents role timers --check': dict(etapes=TOUTES),
    # sonde du code installé (#5937) : connue de tout CLI, elle décide du suivant
    'python_agents -c': dict(etapes=TOUTES),
    'ameesh_agents role timers --role auditeur --check': dict(
        etapes=('controler',), garde='elif [ "$passe_auditeur" = - ]; then'),
    # #6643 : repose l'unité avec le CLI installé quand celui-ci précède la
    # passe déterministe (retour arrière) ; inoffensif sous N (garde fausse)
    # et sous N-1 (écrit l'unité que ce CLI attend).
    'ameesh_agents role timers --role auditeur --install': dict(etapes=TOUTES),
}


def appels_cli_de_controler(texte: str) -> list[tuple[str, int]]:
    debut = texte.index('controler_agents() {')
    corps = texte[debut:texte.index('\n}\n', debut)]
    appels = []
    for ligne in corps.splitlines():
        code = ligne.split('#', 1)[0] if ligne.lstrip().startswith('#') else ligne
        for m in re.finditer(r'\b(ameesh_agents|python_agents)((?: +[-\w]+)*)', code):
            appels.append((' '.join((m.group(1) + m.group(2)).split()), debut + corps.index(ligne)))
    return appels


class ControlesDeclaresTest(unittest.TestCase):
    """Tout contrôle de controler_agents déclare ses étapes (#5946)."""

    def test_chaque_appel_du_cli_installe_est_declare(self):
        texte = SCRIPT.read_text(encoding='utf-8')
        appels = appels_cli_de_controler(texte)
        self.assertTrue(appels)
        trouves = {a for a, _ in appels}
        self.assertEqual(sorted(trouves - set(CONTROLES_CLI)), [],
                         'contrôle de controler_agents sans étapes déclarées '
                         '(CONTROLES_CLI, #5946) : tient-il sous le CLI N-1 ?')
        self.assertEqual(sorted(set(CONTROLES_CLI) - trouves), [],
                         'déclaration sans contrôle : retirer de CONTROLES_CLI')
        for appel, pos in appels:
            decl = CONTROLES_CLI[appel]
            self.assertLessEqual(set(decl['etapes']), set(TOUTES), appel)
            if {'verifier', 'retour'} <= set(decl['etapes']):
                continue
            garde = decl.get('garde')
            self.assertTrue(garde, '%s ne tourne pas sous N-1 : garde à nommer' % appel)
            debut = texte.index('controler_agents() {')
            self.assertIn(garde, texte[debut:pos], appel)

    def test_la_regle_voit_un_controle_ajoute(self):
        texte = SCRIPT.read_text(encoding='utf-8').replace(
            '  ameesh_agents doctor || echec=1\n',
            '  ameesh_agents doctor || echec=1\n  ameesh_agents passe-nouvelle --check || echec=1\n', 1)
        self.assertIn('ameesh_agents passe-nouvelle --check',
                      {a for a, _ in appels_cli_de_controler(texte)})


class BarriereCiTest(unittest.TestCase):
    """La barrière tient en CI : job au clone complet, rouge = `tests` rouge."""

    @classmethod
    def setUpClass(cls):
        cls.jobs = canon.load_yaml(
            (ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8'))['jobs']

    def test_le_job_clone_les_tags_et_exige_le_tag(self):
        job = self.jobs['preflight-n1']
        self.assertEqual(job['env']['AMEESH_TEST_N1_REQUIS'], '1')
        checkout = next(s for s in job['steps'] if 'actions/checkout' in str(s.get('uses')))
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        self.assertTrue(any('tests.test_5947_preflight_cli_n_moins_1' in str(s.get('run'))
                            for s in job['steps']))
        self.assertIn("needs.plan.outputs.reuse != 'true'", job['if'])

    def test_l_agregat_requis_lit_son_resultat(self):
        job = self.jobs['tests']
        self.assertIn('preflight-n1', job['needs'])
        step = next(s for s in job['steps'] if s.get('name') == 'bilan des parts')
        self.assertIn('--preflight-n1 "${{ needs.preflight-n1.result }}"', step['run'])

    def classify(self, **over):
        values = dict(event='pull_request', plan='success', version='success',
                      version_only='false', docs_only='false', doc_readers='skipped',
                      psycopg='success', psycopg_parts='[1, 2]',
                      psql='success', psql_parts='[1]')
        values.update(over)
        return t940.agregat.classify(**values)

    def test_un_preflight_rouge_rougit_tests_un_non_concluant_se_consigne(self):
        agregat = t940.agregat
        self.assertEqual(self.classify(preflight_n1='success')['state'], agregat.GREEN)
        self.assertEqual(self.classify(preflight_n1='skipped')['state'], agregat.GREEN)
        rouge = self.classify(preflight_n1='failure')
        self.assertEqual(rouge['state'], agregat.RED)
        self.assertTrue(any('préflight N-1' in r for r in rouge['red']), rouge)
        annule = self.classify(preflight_n1='cancelled')
        self.assertEqual(annule['state'], agregat.GREEN, annule)
        self.assertEqual([c['part'] for c in annule['consigned']], ['préflight N-1'])


class ContratToujoursTest(unittest.TestCase):
    """#7985 : la sélection d'un lot joue le préflight N-1 (contrat)."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            'test_plan_7985', ROOT / 'scripts' / 'test-plan.py')
        cls.plan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.plan)
        cls.modules = sorted(p.stem for p in (ROOT / 'tests').glob('test*.py'))

    def test_le_preflight_n_1_est_dans_la_liste_toujours(self):
        toujours = self.plan.read_list(self.plan.ALWAYS_FILE, self.modules,
                                       required=True)
        self.assertIn('test_5947_preflight_cli_n_moins_1', toujours)

    def test_la_selection_d_un_lot_joue_le_preflight_n_1(self):
        toujours = self.plan.read_list(self.plan.ALWAYS_FILE, self.modules,
                                       required=True)
        classes = {module: ('P3', '') for module in self.modules}
        state = {'contract': toujours, 'map': {'used': True}, 'lot_safe': True}
        selection, why = self.plan.lot_selection(True, state, self.modules, classes)
        self.assertEqual(why, '')
        self.assertIn('test_5947_preflight_cli_n_moins_1', selection)


class DesignationTest(unittest.TestCase):
    """#7985 : la prévalidation ne bloque que sur les sauts du banc N-1."""

    def test_les_classes_du_banc_n_1_sont_designees(self):
        from ameesh import prevalidation as pv
        designes = pv.DESIGNATED_SKIPS['test_5947_preflight_cli_n_moins_1']
        for classe in (PreflightCliPrecedentTest, IncidentRejoueTest):
            prefixe = ('tests.test_5947_preflight_cli_n_moins_1.%s'
                       % classe.__name__)
            with self.subTest(classe=classe.__name__):
                self.assertIn(prefixe, designes)


class TagsPrepareTest(unittest.TestCase):
    """#7985 : sans tags, le module les rapatrie ; le saut reste nommé."""

    def _git(self, repo: Path, *args: str) -> None:
        subprocess.run(['git', '-C', str(repo), *args], check=True,
                       capture_output=True, text=True)

    def test_un_clone_sans_tag_rapatrie_les_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            remote, source, clone = (base / 'remote.git', base / 'source',
                                     base / 'clone')
            subprocess.run(['git', 'init', '-q', '--bare', '--initial-branch=main',
                            str(remote)], check=True, capture_output=True)
            subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(source)],
                           check=True, capture_output=True)
            self._git(source, 'config', 'user.email', 't@invalid')
            self._git(source, 'config', 'user.name', 'test')
            (source / 'a').write_text('a\n', encoding='utf-8')
            self._git(source, 'add', 'a')
            self._git(source, 'commit', '-q', '-m', 'v1')
            self._git(source, 'tag', 'v1.0.0')
            (source / 'b').write_text('b\n', encoding='utf-8')
            self._git(source, 'add', 'b')
            self._git(source, 'commit', '-q', '-m', 'tete')
            self._git(source, 'remote', 'add', 'origin', str(remote))
            self._git(source, 'push', '-q', 'origin', 'main', '--tags')
            subprocess.run(['git', 'clone', '-q', '--no-tags', str(remote), str(clone)],
                           check=True, capture_output=True)
            self.assertEqual(release_precedente(clone), '')
            self.assertTrue(preparer_tags(clone))
            self.assertEqual(release_precedente(clone), 'v1.0.0')

    def test_un_instantane_sans_histoire_ne_lance_aucun_fetch(self):
        """#7985 : le guest reçoit un dépôt à commit racine : pas de réseau."""
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / 'snapshot'
            subprocess.run(['git', 'init', '-q', '--initial-branch=main',
                            str(snapshot)], check=True, capture_output=True)
            self._git(snapshot, 'config', 'user.email', 't@invalid')
            self._git(snapshot, 'config', 'user.name', 'test')
            (snapshot / 'a').write_text('a\n', encoding='utf-8')
            self._git(snapshot, 'add', 'a')
            self._git(snapshot, 'commit', '-q', '-m', 'instantané')
            self._git(snapshot, 'remote', 'add', 'origin',
                      str(Path(tmp) / 'absent.git'))
            vrai = subprocess.run
            appels = []

            def espion(*args, **kwargs):
                appels.append(args)
                if args and 'fetch' in args[0]:
                    raise AssertionError(
                        'fetch réseau sur un instantané sans histoire')
                return vrai(*args, **kwargs)

            with mock.patch.object(subprocess, 'run', side_effect=espion):
                self.assertFalse(preparer_tags(snapshot))
            self.assertFalse([a for a in appels if 'fetch' in a[0]], appels)

    def test_le_motif_d_un_saut_est_nomme_non_concluant(self):
        self.assertTrue(motif_n1('', '').startswith('non concluant (canon §3)'))
        self.assertIn('v1.65.0', motif_n1('v1.65.0', 'v1.66.0'))


if __name__ == '__main__':
    unittest.main()
