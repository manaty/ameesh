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
externes sont simulées : la base (aucune requête servie), la ligne de rôle du
registre (`registry.get`, fixture) et, pour les lectures du registre des
agents (`list`, `show`, `doctor`, `--version`) et les sondes de base de vm.sh,
les faux de test_1252. Toute autre commande lancée par vm.sh va au vrai CLI
N-1 : un contrôle qu'il ne connaît pas, ou qu'il juge rouge, rend le préflight
rouge ici avant de le rendre rouge en staging.

L'hôte est candidat du rôle `auditeur` (celui de l'incident) et pas de
`mesh-design`, dont les passes dépendent du canon et des réglages GitHub, hors
de ce banc : son contrôle tourne quand même, sur la branche « non candidat ».

Le tag N-1 est le plus récent `v*` atteignable depuis HEAD et qui ne le
contient pas. Un clone sans tags (CI en profondeur 1) rend ce module sauté,
sauf sous AMEESH_TEST_N1_REQUIS=1 (job « préflight N-1 » de tests.yml, clone
complet) où l'absence est un échec : la barrière ne se perd pas en silence.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

from ameesh import canon

from tests import test_940_agregat_bash as t940
from tests.test_1252_vm_agents_install import AMEESH, DB_PYTHON, ROOT, SCRIPT, VmAgentsHarness

REQUIS = os.environ.get('AMEESH_TEST_N1_REQUIS') == '1'


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


TAG = release_precedente()

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
    """Aucune requête servie : un contrôle qui lit la base échoue ici, nommé."""
    def close(self):
        pass
    def __getattr__(self, name):
        raise RuntimeError('banc #5947 : base simulée, %s non servi' % name)

roles = {r['name']: r for r in json.loads((state / 'roles.json').read_text())}
db_mod.connect = lambda *a, **k: FakeDb()
registry.get = lambda db, name: roles.get(name)
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


@unittest.skipUnless(TAG or REQUIS, 'aucun tag de release atteignable (clone sans tags)')
class PreflightCliPrecedentTest(VmAgentsHarness):
    cli = TAG  # la release dont le code est le CLI installé

    def setUp(self):
        if not self.cli:
            self.fail('AMEESH_TEST_N1_REQUIS=1 : aucun tag v* atteignable depuis HEAD')
        super().setUp()
        self.n1 = self.extraire(self.cli, 'n1')
        (self.state / 'boot.py').write_text(BOOT)
        (self.state / 'roles.json').write_text(json.dumps([
            dict(name='mesh-design', releve='election', host='agents-autre',
                 candidats=['agents-autre'], responsible='human:test'),
            dict(name='auditeur', releve='election', host='host-agents',
                 candidats=['host-agents', 'agents-autre'], responsible='human:test')]))
        (self.vmhome / '.config/ameesh/config.json').write_text('{}\n')
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

    def test_le_preflight_est_vert_avec_le_cli_de_la_release_precedente(self):
        # État laissé par la release N-1 : ses minuteries, posées par SON vm.sh.
        pose = self.run_vm_de(self.n1 / 'deploy/mise-a-jour/vm.sh', 'timers')
        self.assertEqual(pose.returncode, 0, pose.stdout + pose.stderr)
        # Préflight de la montée vers N : vm.sh de N, CLI N-1 encore installé.
        self.assert_vert(self.run_vm('verifier'))

    def test_le_controle_apres_retour_arriere_vers_la_release_precedente_est_vert(self):
        # Minuteries posées par N (installation), puis retour au code N-1 :
        # `retour_agents` finit par controler_agents, rejoué ici tel quel.
        self.installer_cli(ROOT / 'src')
        pose = self.run_vm_de(SCRIPT, 'timers')
        self.assertEqual(pose.returncode, 0, pose.stdout + pose.stderr)
        self.installer_cli(self.n1 / 'src')
        self.assert_vert(self.run_vm('controler'))


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
        self.assertTrue(corps.rstrip().endswith('redemarrer_agents\n  controler_agents'),
                        corps[-200:])
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


if __name__ == '__main__':
    unittest.main()
