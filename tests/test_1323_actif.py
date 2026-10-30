# SPDX-License-Identifier: AGPL-3.0-only
"""#1323 signal « staging actif » : preuves, idempotence, non-signal.

Deux bancs, aucun acces reel :

* `actif.py` isole (journal fabrique, envoi et relecture substitues) :
  l'attestation n'est ecrite et le rapporteur reveille que si l'activation est
  prouvee — sonde structuree et message reellement remis compris —, une seule
  fois par commit, meme apres une erreur d'ecriture posterieure a l'envoi ;
* `staging.sh` sur stubs : le signal est appele apres une sequence complete et
  saine, jamais apres un echec, jamais sur un sous-ensemble ni a blanc, et le
  journal porte la preuve de sonde en JSON.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy/mise-a-jour'

SHA = '0123456789abcdef0123456789abcdef01234567'
AUTRE_SHA = 'fedcba9876543210fedcba9876543210fedcba98'
DEPUIS = '2026-10-14T16:00:00Z'
TS = '2026-10-14T16:05:00Z'
HOTES = ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3', 'manaty-mesh-1', 'pc-smichea')
HOTES_AGENTS = ('ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3')
HOTES_VMS = (*HOTES_AGENTS, 'manaty-mesh-1')
DESTINATAIRE = 'quidu'
MESSAGE = 7

#: variables du runner qui supplanteraient le HOME, la config ou l'etat
#: temporaires : jamais heritees par le banc (meme garde que #1155/#1195).
ENV_PREFIXES = ('AMEESH_', 'AGENT_MESH_', 'AGENT_MAIL_')
ENV_EXTRA = ('XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME')
DSN_REFUSE = 'postgresql://refuse:refuse@127.0.0.1:1/refuse_1323'
ENV_BAC = ('AMEESH_CONFIG', 'AMEESH_DSN', 'AGENT_MESH_DSN', 'AMEESH_SCHEMA',
           'AGENT_MESH_SCHEMA', 'AMEESH_STATE', 'AGENT_MESH_STATE',
           'AMEESH_THREADS', 'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG')


def env_isole(environ=None):
    """Environnement du banc, debarrasse des reglages du runner."""
    source = os.environ if environ is None else environ
    return {k: v for k, v in source.items()
            if not k.startswith(ENV_PREFIXES) and k not in ENV_EXTRA}


def charger_actif():
    spec = importlib.util.spec_from_file_location('actif_1323', DEPLOY / 'actif.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def detail_sonde(hote, sha=SHA, verdict='ok', message=MESSAGE,
                 destinataire=DESTINATAIRE, delai=2.0, pourquoi=None,
                 personas=2, hote_destinataire=None):
    """Rapport JSON de sante-courrier.py, tel que staging.sh le journalise."""
    sonde = None
    if verdict == 'ok':
        sonde = {'message': message, 'destinataire': destinataire, 'delai_s': delai,
                 'hote_destinataire': hote_destinataire or hote}
    return json.dumps({'hote': hote, 'verdict': verdict, 'personas': personas,
                       'sha': sha, 'souffrance': [], 'boites_mortes': [],
                       'sonde': sonde, 'pourquoi': pourquoi})


def evenement(hote, etape, verdict='ok', detail=None, ts=TS, sha=SHA):
    return {'ts': ts, 'hote': hote, 'etape': etape, 'verdict': verdict,
            'sha': sha, 'precedent': None, 'detail': detail}


def journal_complet():
    """Journal d'une sequence complete : les cinq hotes actives."""
    out = []
    for hote in HOTES:
        out.append(evenement(hote, 'installer', detail='installe %s' % SHA[:9]))
        out.append(evenement(hote, 'redemarrer'))
        out.append(evenement(hote, 'controler'))
        out.append(evenement(hote, 'sante', detail=detail_sonde(hote)))
    out.append(evenement(None, 'sequence'))
    return out


def journal_serveur(hote_destinataire='ameesh-agents-1', personas=0,
                    verdict='ok'):
    """Topologie reelle : manaty-mesh-1 (serveur) ne porte aucune persona.

    Sa preuve est une sonde croisee, remise a une persona menee d'un autre
    hote canonique (avis R2 B1).
    """
    out = []
    for hote in HOTES:
        out.append(evenement(hote, 'installer', detail='installe %s' % SHA[:9]))
        out.append(evenement(hote, 'redemarrer'))
        out.append(evenement(hote, 'controler'))
        if hote == 'manaty-mesh-1':
            detail = detail_sonde(hote, personas=personas, verdict=verdict,
                                  hote_destinataire=hote_destinataire)
        else:
            detail = detail_sonde(hote)
        out.append(evenement(hote, 'sante', detail=detail))
    out.append(evenement(None, 'sequence'))
    return out


def journal_poste_absent(raison='poste absent, mise a jour a son retour',
                         avec_poste=False):
    """Sequence de quatre VM, poste reporte (trace `absent`/`reporte`, #3848)."""
    out = []
    for hote in HOTES:
        if hote == 'pc-smichea':
            out.append(evenement(hote, 'absent', verdict='reporte', detail=raison))
            continue
        out.append(evenement(hote, 'installer', detail='installe %s' % SHA[:9]))
        out.append(evenement(hote, 'redemarrer'))
        out.append(evenement(hote, 'controler'))
        if hote == 'manaty-mesh-1':
            detail = detail_sonde(hote, personas=0,
                                  hote_destinataire='ameesh-agents-1')
        else:
            detail = detail_sonde(hote)
        out.append(evenement(hote, 'sante', detail=detail))
    if avec_poste:
        out.append(evenement('pc-smichea', 'sante',
                             detail=detail_sonde('pc-smichea')))
    out.append(evenement(None, 'sequence'))
    return out


def journal_deux_vm_absents(raison_serveur='serveur sans acces root',
                            raison_poste='poste absent, mise a jour a son retour'):
    """Trois VM d'agents prouvees ; serveur et poste reportes (#147558/#3848)."""
    out = []
    for hote in HOTES_AGENTS:
        out.append(evenement(hote, 'installer', detail='installe %s' % SHA[:9]))
        out.append(evenement(hote, 'redemarrer'))
        out.append(evenement(hote, 'controler'))
        out.append(evenement(hote, 'sante', detail=detail_sonde(hote)))
    out.append(evenement('manaty-mesh-1', 'absent', verdict='reporte',
                         detail=raison_serveur))
    out.append(evenement('pc-smichea', 'absent', verdict='reporte',
                         detail=raison_poste))
    out.append(evenement(None, 'sequence'))
    return out


class ActifTest(unittest.TestCase):
    """actif.py : ce qui merite un signal, et ce qui n'en merite pas."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1323-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.etat = self.root / 'etat'
        self.journal = self.root / 'staging.jsonl'
        self.actif = charger_actif()
        #: la reconciliation reelle, pour les temoins qui exercent la recherche
        self.deja_emis_reel = self.actif.deja_emis
        self.envois = []
        #: double du transport : un envoi depose vraiment la cle, comme la base
        self.deposes = {}
        patcheur = mock.patch.object(self.actif, 'envoyer', side_effect=self.faux_envoi)
        patcheur.start(); self.addCleanup(patcheur.stop)
        patcheur = mock.patch.object(self.actif, 'deja_emis',
                                     side_effect=self.faux_deja_emis)
        patcheur.start(); self.addCleanup(patcheur.stop)
        patcheur = mock.patch.object(self.actif, 'verifier_remises')
        patcheur.start(); self.addCleanup(patcheur.stop)
        #: #3981 : le journal partage est un transport de plus ; les temoins
        #: locaux ne touchent pas la base et peuvent inspecter l'evenement.
        patcheur = mock.patch.object(self.actif, 'enregistrer_attestation')
        self.enregistre = patcheur.start(); self.addCleanup(patcheur.stop)

    def faux_envoi(self, dest, corps, charge):
        self.envois.append({'dest': dest, 'corps': corps, 'charge': charge})
        self.deposes[charge['emission']] = 4242
        return 4242

    def faux_deja_emis(self, nom, cle):
        return self.deposes.get(cle)

    def ecrire(self, evenements):
        self.journal.write_text(''.join(json.dumps(e) + '\n' for e in evenements),
                                encoding='utf-8')

    def lancer(self, *args):
        argv = ['--journal', str(self.journal), '--sha', SHA, '--depuis', DEPUIS,
                '--etat-dir', str(self.etat)]
        argv += [str(a) for a in args]
        sortie, erreur = io.StringIO(), io.StringIO()
        with redirect_stdout(sortie), redirect_stderr(erreur):
            code = self.actif.main(argv)
        return code, sortie.getvalue(), erreur.getvalue()

    def hotes(self, actifs=None, modes=None):
        valeurs = actifs or {h: SHA for h in HOTES}
        out = []
        for hote in HOTES:
            out += ['--hote', '%s=%s' % (hote, valeurs[hote])]
            if modes and hote in modes:
                out += ['--mode', '%s=%s' % (hote, modes[hote])]
        return out

    def hotes_sans_poste(self, actifs=None):
        """Les quatre VM seules ; manaty-mesh-1 en mode serveur (sonde croisee)."""
        valeurs = actifs or {h: SHA for h in HOTES_VMS}
        out = []
        for hote in HOTES_VMS:
            out += ['--hote', '%s=%s' % (hote, valeurs[hote])]
            out += ['--mode', '%s=%s' % (
                hote, 'serveur' if hote == 'manaty-mesh-1' else 'agents')]
        return out

    def hotes_agents(self):
        out = []
        for hote in HOTES_AGENTS:
            out += ['--hote', '%s=%s' % (hote, SHA), '--mode', '%s=agents' % hote]
        return out

    def attestations(self):
        return sorted(self.etat.glob('actif-*.json'))

    def echec_ecriture_apres_envoi(self):
        """Premier passage : envoi reussi, ecriture finale en echec."""
        reel = self.actif.ecrire_attestation
        appels = []

        def ecriture(chemin, attestation):
            appels.append(1)
            if len(appels) == 2:      # l'ecriture apres l'envoi
                raise OSError('disque plein')
            return reel(chemin, attestation)

        with mock.patch.object(self.actif, 'ecrire_attestation', side_effect=ecriture):
            return self.lancer(*self.hotes(), '--rapporteur', 'docs01')

    # --- B1 : le point d'entree doit s'ouvrir -----------------------------

    def test_aide_argparse_ne_leve_pas(self):
        """B1 : le help de --depuis ne doit pas etre lu comme un format."""
        sortie = io.StringIO()
        with redirect_stdout(sortie), self.assertRaises(SystemExit) as ctx:
            self.actif.main(['--help'])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn('--depuis', sortie.getvalue())

    def test_help_formatable_quelle_que_soit_la_version(self):
        """B1 : un % nu casse add_argument des Python 3.13 ; format_help()
        leve deja sur les versions qui retardent la validation."""
        texte = self.actif.analyseur().format_help()
        self.assertIn('--depuis', texte)
        self.assertIn('AAAA-MM-JJTHH:MM:SSZ', texte)

    def test_point_entree_aide(self):
        p = subprocess.run([sys.executable, str(DEPLOY / 'actif.py'), '--help'],
                           capture_output=True, text=True, env=env_isole())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('--depuis', p.stdout)

    # --- preuves et non-signal --------------------------------------------

    def test_sequence_complete_atteste_et_reveille_une_fois(self):
        self.ecrire(journal_complet())
        code, sortie, erreur = self.lancer(*self.hotes(), '--lot', '917',
                                           '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        fichiers = self.attestations()
        self.assertEqual(len(fichiers), 1, fichiers)
        attestation = json.loads(fichiers[0].read_text())
        self.assertEqual(attestation['schema'], 'ameesh-staging-actif/1')
        self.assertEqual(attestation['sha'], SHA)
        self.assertEqual(sorted(attestation['hotes']), sorted(HOTES))
        self.assertEqual(attestation['lots'], ['917'])
        for hote in HOTES:
            self.assertEqual(attestation['hotes'][hote]['actif'], SHA)
            self.assertEqual(attestation['hotes'][hote]['etapes'],
                             {e: 'ok' for e in ('installer', 'redemarrer', 'controler', 'sante')})
            self.assertEqual(attestation['hotes'][hote]['sonde'],
                             {'message': MESSAGE, 'destinataire': DESTINATAIRE,
                              'delai_s': 2.0, 'hote_destinataire': hote})
        self.assertEqual(attestation['emissions'], {})
        self.assertEqual(attestation['notifies']['docs01']['message'], 4242)
        self.assertEqual(len(self.envois), 1)
        self.assertEqual(self.envois[0]['dest'], 'docs01')
        self.assertIn(SHA[:12], self.envois[0]['charge']['emission'])
        for attendu in (SHA[:9], '#917', HOTES[0], 'Aucune cloture',
                        'sonde %s remise a %s' % (MESSAGE, DESTINATAIRE)):
            self.assertIn(attendu, self.envois[0]['corps'])
        # relance du meme commit : aucun second courrier, aucune duplication
        code, _, erreur = self.lancer(*self.hotes(), '--lot', '917',
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual(len(self.envois), 1)
        self.assertEqual(len(self.attestations()), 1)

    def test_attestation_est_aussi_partagee_dans_le_journal(self):
        """#3981 : la preuve locale part dans `ameesh_events` sous une forme
        compacte, lisible du poste qui fait la coupure sans ssh."""
        self.ecrire(journal_complet())
        self.lancer(*self.hotes(), '--lot', '917', '--rapporteur', 'docs01')
        self.enregistre.assert_called_once()
        evenement = self.enregistre.call_args.args[0]
        self.assertEqual(evenement['sha'], SHA)
        self.assertEqual(sorted(evenement['hotes']), sorted(HOTES))
        self.assertEqual(evenement['lots'], ['917'])
        for hote in HOTES:
            self.assertEqual(evenement['hotes'][hote]['actif'], SHA)
            self.assertNotIn('sante', evenement['hotes'][hote])
        self.assertNotIn('notifies', evenement)
        self.assertNotIn('emissions', evenement)

    def test_sans_retour_porte_la_mention_retour_automatique_desactive(self):
        """#4616 : attestation, preuve partagee et courrier le disent ; sans
        l'option, aucun champ nouveau."""
        self.ecrire(journal_complet())
        self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertNotIn('retour_automatique',
                         json.loads(self.attestations()[0].read_text()))
        self.assertNotIn('retour_automatique', self.enregistre.call_args.args[0])
        self.assertNotIn('Retour automatique', self.envois[0]['corps'])
        self.lancer(*self.hotes(), '--rapporteur', 'docs02', '--sans-retour')
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertEqual(attestation['retour_automatique'], 'desactive')
        self.assertEqual(self.enregistre.call_args.args[0]['retour_automatique'],
                         'desactive')
        self.assertIn('Retour automatique desactive', self.envois[-1]['corps'])

    def test_echec_du_partage_rend_le_code_echec_sans_perdre_la_preuve(self):
        """#3981 : un journal du mesh injoignable ne fait pas perdre le
        deploiement ; l'attestation locale reste et le code 4 le dit."""
        self.ecrire(journal_complet())
        with mock.patch.object(self.actif, 'enregistrer_attestation',
                               side_effect=RuntimeError('base absente')):
            code, _, erreur = self.lancer(*self.hotes())
        self.assertEqual(code, 4, erreur)
        self.assertIn('non partagee', erreur)
        self.assertEqual(len(self.attestations()), 1)

    def test_un_rapporteur_deja_prevenu_n_est_pas_reveille_pour_un_autre(self):
        self.ecrire(journal_complet())
        self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.lancer(*self.hotes(), '--rapporteur', 'docs01', '--rapporteur', 'bureau')
        self.assertEqual([e['dest'] for e in self.envois], ['docs01', 'bureau'])

    def test_retour_disqualifie_la_sequence(self):
        evenements = journal_complet()
        evenements.append(evenement('manaty-mesh-1', 'retour'))
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('retour/rollback', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_preuve_manquante_n_emet_rien(self):
        evenements = [e for e in journal_complet()
                      if not (e['hote'] == 'ameesh-agents-2' and e['etape'] == 'redemarrer')]
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('redemarrer', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_commit_actif_different_de_la_cible(self):
        self.ecrire(journal_complet())
        actifs = {h: SHA for h in HOTES}
        actifs['pc-smichea'] = 'deadbeefdeadbeef'
        code, _, erreur = self.lancer(*self.hotes(actifs), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('commit actif', erreur)
        self.assertEqual(self.envois, [])

    def test_commit_actif_court_du_poste_accepte(self):
        self.ecrire(journal_complet())
        actifs = {h: SHA for h in HOTES}
        actifs['pc-smichea'] = SHA[:7]
        code, _, erreur = self.lancer(*self.hotes(actifs), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual(len(self.attestations()), 1)

    # --- mode serveur sans executant (avis R2 B1) -------------------------

    def test_serveur_sans_persona_sonde_croisee_acceptee(self):
        """Topologie reelle : manaty-mesh-1 sans persona, prouve par une sonde
        croisee remise a une persona d'un autre hote canonique."""
        self.ecrire(journal_serveur())
        code, _, erreur = self.lancer(*self.hotes(modes={'manaty-mesh-1': 'serveur'}),
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        attestation = json.loads(self.attestations()[0].read_text())
        preuve = attestation['hotes']['manaty-mesh-1']
        self.assertEqual(preuve['redemarrage_ts'], TS)
        self.assertEqual(preuve['sonde'],
                         {'message': MESSAGE, 'destinataire': DESTINATAIRE,
                          'delai_s': 2.0, 'hote_destinataire': 'ameesh-agents-1'})
        self.assertIn('via ameesh-agents-1', self.envois[0]['corps'])

    def test_serveur_sans_mode_declare_refuse(self):
        """Porte fermee par defaut : sans `--mode serveur`, la remise croisee
        de la topologie reelle n'est pas acceptee."""
        self.ecrire(journal_serveur())
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('autre hote', erreur)

    def test_serveur_sans_persona_sonde_locale_refusee(self):
        self.ecrire(journal_serveur(hote_destinataire='manaty-mesh-1'))
        code, _, erreur = self.lancer(*self.hotes(modes={'manaty-mesh-1': 'serveur'}),
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('non croisee', erreur)
        self.assertEqual(self.attestations(), [])

    def test_serveur_sonde_croisee_hors_canon_refusee(self):
        self.ecrire(journal_serveur(hote_destinataire='ailleurs'))
        code, _, erreur = self.lancer(*self.hotes(modes={'manaty-mesh-1': 'serveur'}),
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('hors staging', erreur)

    def test_serveur_avec_personas_non_menee_mention_non_concluante(self):
        """Porte fermee : un serveur qui porte des executants sans persona
        menee reste un `sans-sonde`, jamais une sonde croisee ; #4126 : il est
        atteste avec la mention, sans preuve de remise."""
        self.ecrire(journal_serveur(personas=2, verdict='sans-sonde'))
        code, _, erreur = self.lancer(*self.hotes(modes={'manaty-mesh-1': 'serveur'}),
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertIsNone(attestation['hotes']['manaty-mesh-1']['sonde'])
        self.assertIn('manaty-mesh-1', attestation['non_concluants'])

    def test_serveur_avec_personas_sonde_croisee_refusee(self):
        """Une sonde croisee n'est acceptee que pour un serveur a 0 persona."""
        self.ecrire(journal_serveur(personas=2, hote_destinataire='ameesh-agents-1'))
        code, _, erreur = self.lancer(*self.hotes(modes={'manaty-mesh-1': 'serveur'}),
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('autre hote', erreur)

    def test_agents_sans_persona_sonde_croisee_refusee(self):
        """Porte fermee hors serveur : un hote a agents ne se prouve pas par
        une remise sur un autre hote, meme declare a 0 persona."""
        evenements = journal_serveur()
        for e in evenements:
            if e['hote'] == 'ameesh-agents-1' and e['etape'] == 'sante':
                e['detail'] = detail_sonde('ameesh-agents-1', personas=0,
                                           hote_destinataire='ameesh-agents-2')
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('autre hote', erreur)

    def test_serveur_remise_croisee_sans_horodatage_refusee(self):
        evenements = journal_serveur()
        for e in evenements:
            if e['hote'] == 'manaty-mesh-1' and e['etape'] == 'redemarrer':
                e['ts'] = None
        actifs = {h: SHA for h in HOTES}
        with self.assertRaises(self.actif.PreuveManquante) as ctx:
            self.actif.preuves(evenements, SHA, actifs, {'manaty-mesh-1': 'serveur'})
        self.assertIn('non horodate', str(ctx.exception))

    def test_mode_inconnu_refuse(self):
        self.ecrire(journal_complet())
        argv = ['--journal', str(self.journal), '--sha', SHA, '--depuis', DEPUIS,
                '--etat-dir', str(self.etat)] + self.hotes(modes={'manaty-mesh-1': 'srv'})
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            self.actif.main(argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_sequence_partielle_refusee(self):
        self.ecrire(journal_complet())
        code, _, erreur = self.lancer('--hote', 'ameesh-agents-1=%s' % SHA,
                                      '--hote', 'ameesh-agents-2=%s' % SHA,
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('partielle', erreur)
        self.assertEqual(self.envois, [])

    def test_poste_absent_atteste_les_quatre_vm_et_le_mentionne(self):
        """#3848 : une sequence de VM avec poste absent atteste, le courrier
        nomme le report et sa mise a jour au retour."""
        raison = 'poste absent (sequence lancee depuis ameesh-agents-1) : ' \
                 'mise a jour a son retour, jamais en dessous'
        self.ecrire(journal_poste_absent(raison))
        code, sortie, erreur = self.lancer(*self.hotes_sans_poste(),
                                           '--absent', 'pc-smichea=%s' % raison,
                                           '--lot', '917',
                                           '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        fichiers = self.attestations()
        self.assertEqual(len(fichiers), 1, fichiers)
        attestation = json.loads(fichiers[0].read_text())
        self.assertEqual(sorted(attestation['hotes']),
                         ['ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3', 'manaty-mesh-1'])
        self.assertNotIn('pc-smichea', attestation['hotes'])
        self.assertEqual(attestation['reportes']['pc-smichea']['raison'], raison)
        self.assertIn('a son retour', attestation['reportes']['pc-smichea']['mise_a_jour'])
        self.assertEqual(attestation['cle'], '%s-4hotes-1reporte' % SHA[:12])
        self.assertEqual(len(self.envois), 1)
        corps = self.envois[0]['corps']
        self.assertIn('4 hotes', corps)
        self.assertIn('pc-smichea : REPORTE', corps)
        self.assertIn(raison, corps)
        self.assertIn('mise a jour a son retour', corps)
        self.assertEqual(self.envois[0]['charge']['reportes'], ['pc-smichea'])

    def test_serveur_absent_atteste_les_trois_vm_et_le_mentionne(self):
        """#147558 : sans acces root, le serveur est reporte comme le poste ;
        les trois VM d'agents restent la preuve minimale."""
        self.ecrire(journal_deux_vm_absents(raison_serveur='sans-acces'))
        code, _, erreur = self.lancer(*self.hotes_agents(),
                                      '--absent', 'manaty-mesh-1=sans-acces',
                                      '--absent', 'pc-smichea=poste absent',
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        fichiers = self.attestations()
        self.assertEqual(len(fichiers), 1, fichiers)
        attestation = json.loads(fichiers[0].read_text())
        self.assertEqual(sorted(attestation['hotes']),
                         ['ameesh-agents-1', 'ameesh-agents-2', 'ameesh-agents-3'])
        self.assertEqual(sorted(attestation['reportes']),
                         ['manaty-mesh-1', 'pc-smichea'])
        self.assertIn('sans-acces', attestation['reportes']['manaty-mesh-1']['raison'])
        self.assertEqual(attestation['cle'], '%s-3hotes-2reporte' % SHA[:12])
        corps = self.envois[0]['corps']
        self.assertIn('manaty-mesh-1 : REPORTE', corps)
        self.assertIn('sans-acces', corps)

    def test_serveur_absent_sans_trace_refuse(self):
        self.ecrire([e for e in journal_deux_vm_absents()
                     if not (e['hote'] == 'manaty-mesh-1')])
        code, _, erreur = self.lancer(*self.hotes_agents(),
                                      '--absent', 'manaty-mesh-1=sans-acces',
                                      '--absent', 'pc-smichea=poste absent')
        self.assertEqual(code, 3, erreur)
        self.assertIn('absence', erreur)
        self.assertEqual(self.envois, [])

    def test_absence_non_tracee_refusee(self):
        """Une absence affirmee sans evenement `absent`/`reporte` ne prouve rien."""
        self.ecrire([e for e in journal_poste_absent()
                     if not (e['hote'] == 'pc-smichea')])
        code, _, erreur = self.lancer(*self.hotes_sans_poste(),
                                      '--absent', 'pc-smichea=absent',
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('absence', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_absence_contredite_par_une_preuve_refusee(self):
        self.ecrire(journal_poste_absent(avec_poste=True))
        code, _, erreur = self.lancer(*self.hotes_sans_poste(),
                                      '--absent', 'pc-smichea=absent',
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('contredisent', erreur)
        self.assertEqual(self.attestations(), [])

    def test_vm_declaree_absente_refusee(self):
        """Seul le poste est reportable : une VM absente n'atteste jamais."""
        self.ecrire(journal_poste_absent())
        code, _, erreur = self.lancer(*self.hotes_sans_poste(),
                                      '--absent', 'ameesh-agents-1=absente',
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('peuvent etre reportes', erreur)
        self.assertEqual(self.envois, [])

    def test_third_agent_is_required_in_inventory_and_evidence(self):
        for omitted_inventory in (True, False):
            with self.subTest(omitted_inventory=omitted_inventory):
                self.ecrire([e for e in journal_complet()
                             if e.get('hote') != 'ameesh-agents-3'])
                args = self.hotes()
                if omitted_inventory:
                    position = args.index('ameesh-agents-3=%s' % SHA)
                    del args[position - 1:position + 1]
                code, _, erreur = self.lancer(*args, '--rapporteur', 'docs01')
                self.assertEqual(code, 3, erreur)
                self.assertEqual(self.attestations(), [])
                self.assertEqual(self.envois, [])

    def test_third_agent_cannot_be_postponed(self):
        self.ecrire(journal_complet())
        code, _, erreur = self.lancer(*self.hotes(),
            '--absent', 'ameesh-agents-3=indisponible', '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('peuvent etre reportes', erreur)
        self.assertEqual(self.envois, [])

    def test_absent_sans_raison_refuse_par_argparse(self):
        self.ecrire(journal_poste_absent())
        argv = ['--journal', str(self.journal), '--sha', SHA, '--depuis', DEPUIS,
                '--etat-dir', str(self.etat)] + self.hotes_sans_poste()
        argv += ['--absent', 'pc-smichea=']
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            self.actif.main(argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_sans_sonde_refuse(self):
        self.ecrire(journal_complet())
        code, _, erreur = self.lancer(*self.hotes(), '--sans-sonde',
                                      '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('sonde', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_depuis_invalide_refuse(self):
        self.ecrire(journal_complet())
        argv = ['--journal', str(self.journal), '--sha', SHA, '--depuis', 'hier',
                '--etat-dir', str(self.etat)] + self.hotes()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            self.actif.main(argv)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_execution_precedente_du_meme_jour_ignoree(self):
        """Le journal est quotidien : un echec anterieur (retour) ne
        disqualifie pas l'execution courante, posterieure a --depuis."""
        anciens = [evenement(h, 'installer', ts='2026-10-14T09:00:00Z') for h in HOTES]
        anciens.append(evenement('manaty-mesh-1', 'retour', ts='2026-10-14T09:01:00Z'))
        self.ecrire(anciens + journal_complet())
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual(len(self.attestations()), 1)
        self.assertEqual(len(self.envois), 1)

    # --- B2 : la sonde doit prouver une remise reelle ---------------------

    def test_sonde_sans_remise_refusee(self):
        """B2 : un verdict `sans-sonde` (auto sans persona menee, ou
        SONDE_<HOTE>=jamais) n'atteste pas une activation ; #4126 : sur TOUS
        les hotes, aucune remise reelle ne prouve la chaine."""
        evenements = journal_complet()
        for e in evenements:
            if e['etape'] == 'sante':
                e['detail'] = detail_sonde(e['hote'], verdict='sans-sonde',
                                           pourquoi='aucune persona menee')
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('aucune sonde concluante', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_4126_deux_ok_un_sans_sonde_attestation_avec_mention(self):
        """#4126, temoin du pilote : un hote `sans-sonde` (cible nommee
        occupee, aucune libre) ne retient plus l'attestation ; elle porte la
        mention « sonde non concluante : <raison> », reportee au staging
        suivant, et le courrier la dit."""
        evenements = journal_complet()
        for e in evenements:
            if e['etape'] == 'sante' and e['hote'] == 'ameesh-agents-1':
                e['detail'] = detail_sonde(
                    'ameesh-agents-1', verdict='sans-sonde',
                    pourquoi='cible nommee ameesh-dev3 occupee (running)')
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertEqual(
            attestation['non_concluants'],
            {'ameesh-agents-1': 'sonde non concluante : cible nommee ameesh-dev3 '
                                'occupee (running)'})
        preuve = attestation['hotes']['ameesh-agents-1']
        self.assertIsNone(preuve['sonde'])
        self.assertEqual(preuve['report'], 'staging suivant')
        self.assertIsNotNone(attestation['hotes']['ameesh-agents-2']['sonde'])
        self.assertEqual(len(self.envois), 1)
        self.assertIn('sonde non concluante : cible nommee ameesh-dev3',
                      self.envois[0]['corps'])
        self.assertIn('reportee au staging suivant', self.envois[0]['corps'])

    def test_4126_trace_non_concluante_attestee_avec_sa_raison(self):
        """#4126 : la trace `non-concluant` de staging.sh (outil absent : pas
        de rapport ; base injoignable : rapport `non-concluant`) donne la
        mention, avec le texte de la trace ou l'erreur du rapport."""
        rapport = json.dumps({'hote': 'ameesh-agents-2', 'verdict': 'non-concluant',
                              'sha': SHA, 'erreur': 'OperationalError: base'})
        for detail, rapport_objet, attendu in (
                ('sonde de courrier non concluante (code 127)', None, 'code 127'),
                ('sonde de courrier non concluante (code 3)', json.loads(rapport),
                 'OperationalError: base')):
            with self.subTest(attendu=attendu):
                self.envois.clear(); self.deposes.clear()
                for chemin in self.attestations():
                    chemin.unlink()
                evenements = journal_complet()
                for e in evenements:
                    if e['etape'] == 'sante' and e['hote'] == 'ameesh-agents-2':
                        e.update(verdict='non-concluant', detail=detail)
                        if rapport_objet:
                            e['rapport'] = rapport_objet
                self.ecrire(evenements)
                code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
                self.assertEqual(code, 0, erreur)
                attestation = json.loads(self.attestations()[0].read_text())
                self.assertIn(attendu, attestation['non_concluants']['ameesh-agents-2'])
                self.assertEqual(attestation['hotes']['ameesh-agents-2']['etapes']['sante'],
                                 'non-concluant')

    def test_4126_un_hote_en_echec_aucune_attestation(self):
        """#4126, temoin du pilote : un negatif (sante ko) retient
        l'attestation, meme si un autre hote n'est que non concluant."""
        evenements = journal_complet()
        for e in evenements:
            if e['etape'] == 'sante' and e['hote'] == 'ameesh-agents-1':
                e['detail'] = detail_sonde('ameesh-agents-1', verdict='sans-sonde',
                                           pourquoi='occupee')
            if e['etape'] == 'sante' and e['hote'] == 'ameesh-agents-2':
                e['verdict'] = 'ko'
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_4126_rapport_non_concluant_d_un_autre_hote_refuse(self):
        """Un rapport `sans-sonde` d'un autre hote reste une preuve etrangere."""
        evenements = journal_complet()
        for e in evenements:
            if e['etape'] == 'sante' and e['hote'] == 'ameesh-agents-1':
                e['detail'] = detail_sonde('ameesh-agents-2', verdict='sans-sonde')
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('autre hote', erreur)

    def test_sonde_detail_textuel_ou_absent_refuse(self):
        """B2 : un detail libre ou absent ne prouve pas une remise reelle."""
        for detail in ('courrier ameesh-agents-1 : sonde remise a quidu en 2.0s (message 7)',
                       None):
            with self.subTest(detail=detail):
                self.envois.clear(); self.deposes.clear()
                evenements = journal_complet()
                for e in evenements:
                    if e['etape'] == 'sante' and e['hote'] == 'ameesh-agents-1':
                        e['detail'] = detail
                self.ecrire(evenements)
                code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
                self.assertEqual(code, 3, erreur)
                self.assertIn('sonde', erreur)
                self.assertEqual(self.attestations(), [])
                self.assertEqual(self.envois, [])

    def test_sonde_d_un_autre_commit_refusee(self):
        """B2 : la preuve porte le commit de l'execution."""
        evenements = journal_complet()
        evenements[3]['detail'] = detail_sonde('ameesh-agents-1', sha=AUTRE_SHA)
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('autre commit', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_evenements_d_un_autre_commit_refuses(self):
        """B2 : un journal complet au SHA B ne prouve pas la cible A."""
        evenements = journal_complet()
        for e in evenements:
            e['sha'] = AUTRE_SHA
        self.ecrire(evenements)
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('autre execution', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    def test_message_de_sonde_non_relu_n_emet_rien(self):
        """B2 : la remise est relue en base, pas seulement declaree."""
        self.ecrire(journal_complet())
        with mock.patch.object(self.actif, 'verifier_remises',
                               side_effect=self.actif.PreuveManquante('sonde 7 introuvable')):
            code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 3, erreur)
        self.assertIn('introuvable', erreur)
        self.assertEqual(self.attestations(), [])
        self.assertEqual(self.envois, [])

    # --- B3 : l'emission est idempotente, meme apres erreur d'ecriture ----

    def test_erreur_ecriture_apres_envoi_ne_renvoie_pas(self):
        self.ecrire(journal_complet())
        code, _, erreur = self.echec_ecriture_apres_envoi()
        self.assertEqual(code, 4, erreur)
        self.assertIn('disque plein', erreur)
        self.assertEqual(len(self.envois), 1)
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertIn('docs01', attestation['emissions'])
        self.assertNotIn('docs01', attestation['notifies'])
        # relance : le courrier deja depose (meme cle) est retrouve, pas renvoye
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual(len(self.envois), 1)
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertEqual(attestation['emissions'], {})
        self.assertEqual(attestation['notifies']['docs01']['message'], 4242)
        self.assertTrue(attestation['notifies']['docs01']['reconcilie'])

    def test_cle_non_ecrite_aucun_envoi(self):
        """B3 : sans cle durable, pas d'envoi (une relance reste sure)."""
        self.ecrire(journal_complet())
        with mock.patch.object(self.actif, 'ecrire_attestation',
                               side_effect=OSError('disque plein')):
            code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 4, erreur)
        self.assertEqual(self.envois, [])

    def test_envoi_echoue_puis_relance_une_seule_fois(self):
        """B3 : un envoi en echec ne depose rien ; la relance renvoie une fois."""
        self.ecrire(journal_complet())
        with mock.patch.object(self.actif, 'envoyer',
                               side_effect=RuntimeError('base absente')):
            code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 4, erreur)
        self.assertIn('base absente', erreur)
        self.assertEqual(self.envois, [])
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertIn('docs01', attestation['emissions'])
        code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual([e['dest'] for e in self.envois], ['docs01'])
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertEqual(attestation['emissions'], {})

    def test_relecture_impossible_ne_renvoie_pas(self):
        """B3 : sans relecture possible (base injoignable), l'absence n'est pas
        prouvee : la marque reste ambigue, rien n'est renvoye et le relais
        operateur prend le signal (code 5)."""
        self.ecrire(journal_complet())
        code, _, _ = self.echec_ecriture_apres_envoi()
        self.assertEqual(code, 4)
        self.assertEqual(len(self.envois), 1)
        avant = json.loads(self.attestations()[0].read_text())['emissions']['docs01']
        with mock.patch.object(self.actif, 'deja_emis',
                               side_effect=RuntimeError('base absente')):
            code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, self.actif.CODE_AMBIGU, erreur)
        self.assertIn('ambigu', erreur)
        self.assertIn('relais operateur', erreur)
        self.assertEqual(len(self.envois), 1)     # aucun renvoi automatique
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertEqual(attestation['emissions']['docs01'], avant)
        self.assertNotIn('docs01', attestation['notifies'])

    def test_courrier_sorti_des_20000_derniers_est_retrouve(self):
        """B3 / #4180 : 20001 courriers plus recents ne cachent pas un depot durable ;
        la relance reconcilie au lieu de renvoyer (temoin de l'avis R1)."""
        self.ecrire(journal_complet())
        code, _, erreur = self.echec_ecriture_apres_envoi()
        self.assertEqual(code, 4, erreur)
        self.assertEqual(len(self.envois), 1)
        cle = self.envois[0]['charge']['emission']
        expediteur = self.actif.work.SYSTEM_SENDER
        # Le depot vise est enfoui sous plus de 20000 courriers.
        courriers = [{'id': 4000 + i, 'sender': expediteur, 'recipient': 'docs01',
                      'payload': {'notify': True}} for i in range(20001)]
        courriers.append({'id': 4242, 'sender': expediteur, 'recipient': 'docs01',
                          'payload': {'staging_actif': {'emission': cle}}})

        def recherche(conn, recipient, sender, key):
            return next((row['id'] for row in courriers
                         if row['recipient'] == recipient and row['sender'] == sender
                         and row['payload'].get('staging_actif', {}).get('emission') == key),
                        None)

        with mock.patch.object(self.actif.config, 'load', return_value={}), \
             mock.patch.object(self.actif.db_mod, 'connect',
                               return_value=mock.MagicMock()), \
             mock.patch.object(self.actif.mail, 'staging_emission', side_effect=recherche), \
             mock.patch.object(self.actif, 'deja_emis', side_effect=self.deja_emis_reel):
            code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertEqual(len(self.envois), 1)     # aucun second envoi
        attestation = json.loads(self.attestations()[0].read_text())
        self.assertEqual(attestation['emissions'], {})
        self.assertEqual(attestation['notifies']['docs01']['message'], 4242)
        self.assertTrue(attestation['notifies']['docs01']['reconcilie'])

    def test_courrier_non_remis_attestation_conservee(self):
        self.ecrire(journal_complet())
        with mock.patch.object(self.actif, 'envoyer', side_effect=RuntimeError('base absente')):
            code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 4, erreur)
        self.assertIn('base absente', erreur)
        fichiers = self.attestations()
        self.assertEqual(len(fichiers), 1)
        attestation = json.loads(fichiers[0].read_text())
        self.assertEqual(attestation['notifies'], {})

    # --- N1 : le temporaire est prive des sa creation ---------------------

    def test_temporaire_0600_et_unique(self):
        self.ecrire(journal_complet())
        modes, noms = [], []
        reel = os.replace

        def remplacer(src, dst):
            modes.append(stat.S_IMODE(os.stat(src).st_mode))
            noms.append(str(src))
            return reel(src, dst)

        with mock.patch.object(self.actif.os, 'replace', side_effect=remplacer):
            code, _, erreur = self.lancer(*self.hotes(), '--rapporteur', 'docs01')
        self.assertEqual(code, 0, erreur)
        self.assertTrue(modes)
        self.assertEqual(set(modes), {0o600}, modes)
        self.assertEqual(len(set(noms)), len(noms), noms)
        self.assertEqual(stat.S_IMODE(os.stat(self.attestations()[0]).st_mode), 0o600)


class PreuveSondeTest(unittest.TestCase):
    """Contrats unitaires de la preuve de sonde (B2)."""

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.mod = charger_actif()

    def test_detail_valide(self):
        preuve = self.mod.preuve_sonde('ameesh-agents-1',
                                       detail_sonde('ameesh-agents-1'), SHA)
        self.assertEqual(preuve, {'message': MESSAGE, 'destinataire': DESTINATAIRE,
                                  'delai_s': 2.0,
                                  'hote_destinataire': 'ameesh-agents-1'})

    def test_details_refuses(self):
        cas = {
            'absent': (None, 'absente'),
            'vide': ('', 'absente'),
            'texte libre': ('courrier X : sonde remise (message 7)', 'illisible'),
            'json non objet': ('[1, 2]', 'forme inconnue'),
            'autre hote': (detail_sonde('ameesh-agents-2'), 'autre hote'),
            'aucun sha': (detail_sonde('ameesh-agents-1', sha=''), 'autre commit'),
            'sans-sonde': (detail_sonde('ameesh-agents-1', verdict='sans-sonde'),
                           'sonde non remise'),
            'ko': (detail_sonde('ameesh-agents-1', verdict='ko'), 'sonde non remise'),
            'sonde absente': (json.dumps({'hote': 'ameesh-agents-1', 'verdict': 'ok',
                                          'sha': SHA, 'sonde': None}),
                              'aucune sonde'),
            'message illisible': (detail_sonde('ameesh-agents-1', message=0),
                                  'message de sonde'),
            'message booleen': (detail_sonde('ameesh-agents-1', message=True),
                                'message de sonde'),
            'destinataire vide': (detail_sonde('ameesh-agents-1', destinataire=''),
                                  'destinataire'),
            'delai absent': (detail_sonde('ameesh-agents-1', delai=None), 'delai'),
            'delai negatif': (detail_sonde('ameesh-agents-1', delai=-1), 'delai'),
            'hote destinataire absent': (json.dumps({
                'hote': 'ameesh-agents-1', 'verdict': 'ok', 'personas': 2, 'sha': SHA,
                'sonde': {'message': MESSAGE, 'destinataire': DESTINATAIRE,
                          'delai_s': 2.0}}),
                'hote du destinataire'),
            'personas illisible': (json.dumps({
                'hote': 'ameesh-agents-1', 'verdict': 'ok', 'personas': 'deux',
                'sha': SHA, 'sonde': {'message': MESSAGE, 'destinataire': DESTINATAIRE,
                                      'delai_s': 2.0,
                                      'hote_destinataire': 'ameesh-agents-1'}}),
                'nombre de personas'),
        }
        for nom, (detail, attendu) in cas.items():
            with self.subTest(cas=nom):
                with self.assertRaises(self.mod.PreuveManquante) as ctx:
                    self.mod.preuve_sonde('ameesh-agents-1', detail, SHA)
                self.assertIn(attendu, str(ctx.exception))

    def test_detail_serveur_sans_persona(self):
        """0 persona + mode serveur : la sonde croisee est la seule preuve."""
        detail = detail_sonde('manaty-mesh-1', personas=0,
                              hote_destinataire='ameesh-agents-1')
        preuve = self.mod.preuve_sonde('manaty-mesh-1', detail, SHA, 'serveur')
        self.assertEqual(preuve['hote_destinataire'], 'ameesh-agents-1')

    def test_details_serveur_refuses(self):
        cas = {
            'sonde locale a 0 persona': (
                detail_sonde('manaty-mesh-1', personas=0), 'serveur', 'non croisee'),
            'destinataire hors canon': (
                detail_sonde('manaty-mesh-1', personas=0, hote_destinataire='ailleurs'),
                'serveur', 'hors staging'),
            'croisee avec executants': (
                detail_sonde('manaty-mesh-1', personas=2,
                             hote_destinataire='ameesh-agents-1'),
                'serveur', 'autre hote'),
            'croisee hors mode serveur': (
                detail_sonde('manaty-mesh-1', personas=0,
                             hote_destinataire='ameesh-agents-1'),
                'agents', 'autre hote'),
        }
        for nom, (detail, mode, attendu) in cas.items():
            with self.subTest(cas=nom):
                with self.assertRaises(self.mod.PreuveManquante) as ctx:
                    self.mod.preuve_sonde('manaty-mesh-1', detail, SHA, mode)
                self.assertIn(attendu, str(ctx.exception))

    def message(self, **kw):
        row = {'id': MESSAGE, 'sender': self.mod.work.SYSTEM_SENDER,
               'recipient': DESTINATAIRE, 'delivered_ts': 1.0,
               'payload': {'staging': {'hote': 'ameesh-agents-1', 'sha': SHA,
                                       'destinataire_hote': 'ameesh-agents-1'}}}
        row.update(kw)
        return row

    def sonde(self, hote_destinataire='ameesh-agents-1'):
        return {'message': MESSAGE, 'destinataire': DESTINATAIRE, 'delai_s': 2.0,
                'hote_destinataire': hote_destinataire}

    def message_croise(self, **kw):
        row = self.message(payload={'staging': {
            'hote': 'manaty-mesh-1', 'sha': SHA,
            'destinataire_hote': 'ameesh-agents-1'}})
        row.update(kw)
        return row

    def test_message_remis_valide(self):
        self.assertEqual(
            self.mod.controle_message(self.message(), 'ameesh-agents-1', self.sonde(), SHA),
            self.sonde())

    def test_message_croise_valide(self):
        """Une remise croisee posterieure au redemarrage est une preuve."""
        sonde = self.sonde()
        self.assertEqual(
            self.mod.controle_message(self.message_croise(delivered_ts=200.0),
                                      'manaty-mesh-1', sonde, SHA,
                                      apres_ts='1970-01-01T00:02:00Z'),
            sonde)

    def test_message_croise_refuse(self):
        sonde = self.sonde()
        cas = {
            'remise anterieure au redemarrage': (
                self.message_croise(delivered_ts=100.0), '1970-01-01T00:02:00Z',
                'anterieure au redemarrage'),
            'redemarrage non horodate': (
                self.message_croise(delivered_ts=200.0), 'zzz', 'non horodate'),
            'redemarrage absent': (
                self.message_croise(delivered_ts=200.0), None, 'non horodate'),
            'hote destinataire incoherent': (
                self.message_croise(delivered_ts=200.0,
                                    payload={'staging': {'hote': 'manaty-mesh-1',
                                                         'sha': SHA,
                                                         'destinataire_hote': 'autre'}}),
                '1970-01-01T00:02:00Z', 'hote destinataire inattendu'),
        }
        for nom, (row, apres, attendu) in cas.items():
            with self.subTest(cas=nom):
                with self.assertRaises(self.mod.PreuveManquante) as ctx:
                    self.mod.controle_message(row, 'manaty-mesh-1', sonde, SHA,
                                              apres_ts=apres)
                self.assertIn(attendu, str(ctx.exception))

    def test_messages_refuses(self):
        cas = {
            'introuvable': (None, 'introuvable'),
            'non remis': (self.message(delivered_ts=None), 'non remis'),
            'mauvais destinataire': (self.message(recipient='autre'), 'destine'),
            'mauvais expediteur': (self.message(sender='agent'), 'expediteur'),
            'autre hote': (self.message(payload={'staging': {'hote': 'autre', 'sha': SHA}}),
                           'autre hote'),
            'autre commit': (self.message(payload={'staging': {'hote': 'ameesh-agents-1',
                                                               'sha': AUTRE_SHA}}),
                             'autre commit'),
            'sans charge staging': (self.message(payload={}), 'autre hote'),
            'destinataire hote absent': (
                self.message(payload={'staging': {'hote': 'ameesh-agents-1', 'sha': SHA}}),
                'hote destinataire inattendu'),
            'destinataire hote different': (
                self.message(payload={'staging': {'hote': 'ameesh-agents-1', 'sha': SHA,
                                                  'destinataire_hote': 'ameesh-agents-2'}}),
                'hote destinataire inattendu'),
        }
        for nom, (row, attendu) in cas.items():
            with self.subTest(cas=nom):
                with self.assertRaises(self.mod.PreuveManquante) as ctx:
                    self.mod.controle_message(row, 'ameesh-agents-1', self.sonde(), SHA)
                self.assertIn(attendu, str(ctx.exception))


class ReconciliationExhaustiveTest(unittest.TestCase):
    """#4180 : recherche ciblée exhaustive ; une erreur interdit le renvoi."""

    def test_recherche_ciblee_et_fermeture_sur_absence_ou_depot(self):
        mod = charger_actif()
        for resultat in (None, 42):
            with self.subTest(resultat=resultat):
                conn = mock.MagicMock()
                with mock.patch.object(mod.config, 'load', return_value={}), \
                     mock.patch.object(mod.db_mod, 'connect', return_value=conn), \
                     mock.patch.object(mod.mail, 'staging_emission',
                                       return_value=resultat) as recherche:
                    self.assertEqual(mod.deja_emis('docs01', 'cle'), resultat)
                recherche.assert_called_once_with(conn, 'docs01',
                                                  mod.work.SYSTEM_SENDER, 'cle')
                conn.close.assert_called_once()

    def test_erreur_de_lecture_propagee_connexion_fermee(self):
        mod = charger_actif()
        conn = mock.MagicMock()
        with mock.patch.object(mod.config, 'load', return_value={}), \
             mock.patch.object(mod.db_mod, 'connect', return_value=conn), \
             mock.patch.object(mod.mail, 'staging_emission',
                               side_effect=RuntimeError('lecture impossible')):
            with self.assertRaisesRegex(RuntimeError, 'lecture impossible'):
                mod.deja_emis('docs01', 'cle')
        conn.close.assert_called_once()


class StagingSignalTest(unittest.TestCase):
    """staging.sh : le signal n'est demande qu'apres une sequence saine."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ameesh-1323-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'; self.state.mkdir()
        (self.state / 'calls.log').touch()
        (self.state / 'scenario').write_text('')
        self.home = self.root / 'home'; self.home.mkdir()
        self.repo = self.root / 'repo'
        self.script_dir = self.repo / 'deploy/mise-a-jour'
        self.script_dir.mkdir(parents=True)
        for name in ('staging.sh', 'sante-courrier.py', 'actif.py'):
            (self.script_dir / name).write_text((DEPLOY / name).read_text())
        for name, source in (('vm.sh', STUB), ('poste.sh', STUB)):
            path = self.script_dir / name
            path.write_text(source)
            path.chmod(0o755)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        (self.bin / 'ssh').write_text(SSH)
        (self.bin / 'ssh').chmod(0o755)
        # #3039 : le staging passe les gestes du poste par systemd-run ; le
        # banc le remplace par un lanceur direct (le vrai exigerait un bus
        # utilisateur et le cgroup du service de release).
        (self.bin / 'systemd-run').write_text(SYSTEMD_RUN)
        (self.bin / 'systemd-run').chmod(0o755)
        venv = self.home / '.local/share/ameesh/venv/bin'; venv.mkdir(parents=True)
        for name, source in (('python', VENV_PYTHON), ('ameesh', VENV_AMEESH)):
            path = venv / name
            path.write_text(source)
            path.chmod(0o755)
        self.config = self.root / 'bascule.env'
        self.config.write_text(
            'HOTES_STAGING="ameesh-agents-1=root@10.77.0.3 ameesh-agents-2=root@10.77.0.4 ameesh-agents-3=root@10.77.0.5 '
            'manaty-mesh-1=root@10.77.0.2 pc-smichea=poste"\n'
            'VM=10.77.0.2\nPERSONAS_VM=\n')
        run = ['git', '-C', str(self.repo)]
        env = self.git_env()
        subprocess.run(run + ['init', '-q'], check=True, capture_output=True)
        (self.repo / 'marque').write_text('x\n')
        subprocess.run(run + ['add', 'marque'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['-c', 'user.email=t@invalid', '-c', 'user.name=t',
                              'commit', '-qm', 'base'], check=True, capture_output=True, env=env)
        (self.repo / 'marque').write_text('x\ny\n')
        subprocess.run(run + ['add', 'marque'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['-c', 'user.email=t@invalid', '-c', 'user.name=t',
                              'commit', '-qm', 'tete'], check=True, capture_output=True, env=env)
        subprocess.run(run + ['update-ref', 'refs/remotes/origin/main', 'HEAD'],
                       check=True, capture_output=True, env=env)
        self.sha = subprocess.run(run + ['rev-parse', 'HEAD'], check=True,
                                  capture_output=True, text=True).stdout.strip()
        base = env_isole()
        bacs = {}
        for nom in ('etat', 'fils', 'v0', 'config', 'tmp'):
            bacs[nom] = self.root / nom
            bacs[nom].mkdir()
        self.env = dict(base, HOME=str(self.home), BASCULE_ENV=str(self.config),
                        FAKE_STATE=str(self.state),
                        PATH=str(self.bin) + os.pathsep + base.get('PATH', ''),
                        TMPDIR=str(bacs['tmp']),
                        # #3848 : le banc tourne sur une VM ; les sequences
                        # completes doivent rester celles des cinq hotes.
                        HOTE_LOCAL='pc-smichea',
                        AMEESH_CONFIG=str(self.root / 'config-absente.json'),
                        AMEESH_STATE=str(bacs['etat']),
                        AGENT_MESH_STATE=str(bacs['etat']),
                        AMEESH_THREADS=str(bacs['fils']),
                        AGENT_MAIL_STATE=str(bacs['v0']),
                        AGENT_MAIL_CONFIG=str(bacs['config']),
                        AMEESH_DSN=DSN_REFUSE, AGENT_MESH_DSN=DSN_REFUSE,
                        AMEESH_SCHEMA='refuse_1323', AGENT_MESH_SCHEMA='refuse_1323',
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def git_env(self):
        return dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@invalid',
                    GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@invalid')

    def run_staging(self, *args, env=None):
        env = env or self.env
        self.assertEqual(env.get('HOME'), str(self.home))
        self.assertFalse(Path(env.get('AMEESH_CONFIG', '')).exists())
        for nom in ('AMEESH_STATE', 'AGENT_MESH_STATE', 'AMEESH_THREADS',
                    'AGENT_MAIL_STATE', 'AGENT_MAIL_CONFIG'):
            self.assertTrue(str(env.get(nom, '')).startswith(str(self.root)), nom)
        self.assertEqual(env.get('AMEESH_DSN'), DSN_REFUSE)
        p = subprocess.run(['bash', str(self.script_dir / 'staging.sh'), *args],
                           env=env, capture_output=True, text=True, timeout=180)
        return p

    def calls(self):
        return (self.state / 'calls.log').read_text().splitlines()

    def actifs(self):
        return [l for l in self.calls() if l.startswith('actif ')]

    def notifications(self):
        return [l for l in self.calls() if l.startswith('venv-python notification')]

    def journal(self):
        dossier = self.home / '.local/state/ameesh/mise-a-jour'
        fichiers = sorted(dossier.glob('staging-*.jsonl'))
        self.assertTrue(fichiers, dossier)
        return [json.loads(l) for l in fichiers[0].read_text().splitlines()]

    def creer_poste_installe(self):
        """Le poste publie un nom court : la relecture doit le retrouver."""
        court = self.sha[:7]
        partage = self.home / '.local/share/ameesh'
        (partage / ('src-%s' % court)).mkdir(parents=True, exist_ok=True)
        (partage / 'src-current').symlink_to('src-%s' % court)

    def test_sequence_complete_appelle_le_signal_avec_les_preuves(self):
        self.creer_poste_installe()
        p = self.run_staging('--lots', '917 988', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        appels = self.actifs()
        self.assertEqual(len(appels), 1, self.calls())
        # #4880 : seule la publication de fin de suivi suit l'attestation ;
        # aucune nouvelle operation sur un hote ne doit suivre le signal.
        calls = self.calls()
        apres = calls[calls.index(appels[0]) + 1:]
        self.assertEqual(len(apres), 1, apres)
        self.assertIn(' -m ameesh.staging_watch ', apres[0])
        suivi = json.loads(apres[0].split(' -m ameesh.staging_watch ', 1)[1])
        self.assertEqual(suivi['state'], 'completed')
        self.assertEqual(suivi['sha'], self.sha)
        ligne = appels[0]
        self.assertIn('actif.py', ligne)
        self.assertIn('--sha %s' % self.sha, ligne)
        self.assertIn('--depuis ', ligne)
        self.assertIn('--hote pc-smichea=%s' % self.sha[:7], ligne)
        for hote in HOTES:
            self.assertIn('--hote %s=' % hote, ligne)
        self.assertIn('--lot 917', ligne)
        self.assertIn('--lot 988', ligne)
        self.assertIn('--rapporteur docs01', ligne)
        self.assertIn('--mode ameesh-agents-1=agents', ligne)
        self.assertIn('--mode manaty-mesh-1=serveur', ligne)
        self.assertIn('--mode pc-smichea=poste', ligne)
        self.assertNotIn('--sans-sonde', ligne)

    def test_journal_porte_la_preuve_de_sonde_en_json(self):
        """B2 : le detail de `sante` est le rapport structure, lie au commit."""
        self.creer_poste_installe()
        p = self.run_staging('--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        sante = [t for t in self.journal() if t['etape'] == 'sante']
        self.assertEqual(len(sante), len(HOTES), sante)
        for trace in sante:
            rapport = json.loads(trace['detail'])
            self.assertEqual(rapport['hote'], trace['hote'])
            self.assertEqual(rapport['sha'], self.sha)
            self.assertEqual(rapport['verdict'], 'ok')
            self.assertEqual(rapport['sonde']['message'], MESSAGE)
            self.assertEqual(rapport['sonde']['destinataire'], DESTINATAIRE)
            attendu = ('ameesh-agents-1' if trace['hote'] == 'manaty-mesh-1'
                       else trace['hote'])
            self.assertEqual(rapport['sonde']['hote_destinataire'], attendu)
        mesh = json.loads(next(t for t in sante
                               if t['hote'] == 'manaty-mesh-1')['detail'])
        self.assertEqual(mesh['personas'], 0)
        self.assertEqual(mesh['sonde']['hote_destinataire'], 'ameesh-agents-1')

    def test_sonde_appelee_en_json_avec_le_commit(self):
        self.creer_poste_installe()
        p = self.run_staging()
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        appels = [l for l in self.calls() if 'sante-courrier.py' in l]
        self.assertEqual(len(appels), len(HOTES), self.calls())
        for ligne in appels:
            self.assertIn('--json', ligne)
            self.assertIn('--sha %s' % self.sha, ligne)
            if '--hote manaty-mesh-1 ' in ligne:
                self.assertIn('--serveur', ligne)
            else:
                self.assertNotIn('--serveur', ligne)
        serveur = [l for l in appels if '--serveur' in l]
        self.assertEqual(len(serveur), 1, appels)
        self.assertIn('--hote manaty-mesh-1 ', serveur[0])

    def test_echec_et_retour_n_emettent_aucun_signal(self):
        (self.state / 'scenario').write_text('ko=manaty-mesh-1:controler\n')
        p = self.run_staging('--lots', '917', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.actifs(), [])
        self.assertEqual(len(self.notifications()), 1)
        self.assertIn('mesh-design', self.calls()[-1])

    def test_sous_ensemble_n_atteste_rien(self):
        p = self.run_staging('--hotes', 'ameesh-agents-1 ameesh-agents-2',
                             '--lots', '917', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(self.actifs(), [])
        self.assertIn('sequence partielle', p.stderr)

    def preuves_du_journal_reel(self):
        """Le vrai controle d'actif.py (preuves) sur le journal que staging.sh
        vient d'ecrire, avec les arguments exacts du signal : la couture que
        les bancs separes ne voient pas (avis cx2 h-34, #3853)."""
        argv = (self.state / 'actif.args').read_bytes().decode().split('\0')[1:-1]
        actif = charger_actif()
        args = actif.analyseur().parse_args(argv)
        couples = lambda liste: dict(e.partition('=')[::2] for e in liste)
        # le banc ssh ne relit pas le commit installe des VM (valeur vide) :
        # cette relecture n'est pas la couture visee, le journal l'est.
        actifs = {nom: valeur or args.sha for nom, valeur in couples(args.hote).items()}
        evenements = actif.lire_journal(Path(args.journal), args.depuis)
        return actif.preuves(evenements, args.sha, actifs,
                             couples(args.mode), couples(args.absent))

    def test_journal_reel_depuis_une_vm_est_attestable(self):
        """Depuis une VM, serveur present (dump serveur journalise) ou reporte
        (sans acces root, sauvegarde par defaut) : le journal complet passe
        le controle reel des preuves."""
        cas = {'serveur present': (),
               'serveur reporte': ('--absent', 'manaty-mesh-1=sans acces root')}
        for nom, options in cas.items():
            with self.subTest(nom):
                for f in (self.home / '.local/state/ameesh/mise-a-jour').glob('staging-*.jsonl'):
                    f.unlink()
                env = dict(self.env, HOTE_LOCAL='ameesh-agents-1')
                p = self.run_staging(*options, '--rapporteurs', 'docs01', env=env)
                self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
                preuve, reportes = self.preuves_du_journal_reel()
                attendus = {'pc-smichea'} | ({'manaty-mesh-1'} if options else set())
                self.assertEqual(set(reportes), attendus)
                self.assertNotIn('pc-smichea', preuve)

    def test_poste_absent_appelle_le_signal_avec_absent(self):
        """#3848 : depuis une VM, le signal porte --absent et les quatre VM."""
        env = dict(self.env, HOTE_LOCAL='ameesh-agents-1')
        p = self.run_staging('--lots', '917', '--rapporteurs', 'docs01', env=env)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        appels = self.actifs()
        self.assertEqual(len(appels), 1, self.calls())
        ligne = appels[0]
        self.assertIn('--absent pc-smichea=', ligne)
        self.assertNotIn('--hote pc-smichea=', ligne)
        for hote in HOTES_VMS:
            self.assertIn('--hote %s=' % hote, ligne)
        trace = next(t for t in self.journal()
                     if t['hote'] == 'pc-smichea' and t['etape'] == 'absent')
        self.assertEqual(trace['verdict'], 'reporte')
        self.assertEqual(trace['sha'], self.sha)
        self.assertIn('mise a jour a son retour', trace['detail'])
        self.assertIn('poste absent', p.stdout)

    def test_sans_sonde_transmet_le_refus(self):
        self.creer_poste_installe()
        p = self.run_staging('--sans-sonde', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        appels = self.actifs()
        self.assertEqual(len(appels), 1, self.calls())
        self.assertIn('--sans-sonde', appels[0])
        # la sonde de lecture seule est journalisee sans verdict de remise
        sante = [t for t in self.journal() if t['etape'] == 'sante']
        self.assertEqual(len(sante), len(HOTES), sante)
        for trace in sante:
            self.assertEqual(json.loads(trace['detail'])['verdict'], 'sans-sonde')

    def test_a_blanc_n_appelle_rien(self):
        p = self.run_staging('--a-blanc', '--lots', '917')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.actifs(), [])

    def test_rc_ambigu_est_relaye(self):
        """B3 : un code 5 (emission ambigue, aucun renvoi) est signale comme
        tel et n'echoue pas le deploiement."""
        self.creer_poste_installe()
        (self.state / 'scenario').write_text('ko=actif:5\n')
        p = self.run_staging('--lots', '917', '--rapporteurs', 'docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertEqual(len(self.actifs()), 1)
        self.assertIn('ambigu', p.stderr)
        self.assertIn('relayer a la main', p.stderr)

    def test_listes_normalisees(self):
        self.creer_poste_installe()
        p = self.run_staging('--lots', '917, 988 917', '--rapporteurs', 'docs01 docs01')
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        ligne = self.actifs()[0]
        self.assertEqual(ligne.count('--lot '), 2)
        self.assertEqual(ligne.count('--rapporteur '), 1)


#: stub vm.sh/poste.sh : journalise l'appel, injecte un KO par scenario
STUB = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
host=pc-smichea
if [ "$(basename "$0")" = vm.sh ]; then
  if [ "${1:-}" = agents ]; then
    host=$(sed -n 's/^HOTE_AGENTS=//p' "$BASCULE_ENV")
    shift
  else
    host=manaty-mesh-1
  fi
fi
action=${1:-}
printf 'call host=%s action=%s args=%s\n' "$host" "$action" "$*" >> "$state/calls.log"
if grep -qx "ko=$host:$action" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=$host:$action" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "echec injecte : $host $action" >&2
  exit 1
fi
exit 0
'''

#: venv du poste : distingue la notification, le signal actif et la sonde ;
#: la sonde rend le rapport JSON que staging.sh journalise (preuve #1323) et
#: reproduit la topologie reelle : manaty-mesh-1 (serveur) est a 0 persona et
#: sa sonde est croisee vers une VM, les autres hotes remettent localement.
VENV_PYTHON = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
case "${1:-}" in
  -) printf 'venv-python notification %s\n' "$*" >> "$state/calls.log"; exit 0 ;;
esac
if [ "$(basename "${1:-}")" = actif.py ]; then
  printf 'actif %s\n' "$*" >> "$state/calls.log"
  # arguments exacts (une raison contient des espaces) pour rejouer le vrai
  # controle des preuves sur le journal reel (#3853)
  printf '%s\0' "$@" > "$state/actif.args"
  if grep -qx "ko=actif:5" "$state/scenario" 2>/dev/null; then
    grep -vx "ko=actif:5" "$state/scenario" > "$state/scenario.tmp" || true
    mv "$state/scenario.tmp" "$state/scenario"
    exit 5
  fi
  exit 0
fi
hote=""; sha=""; cible=auto; precedent=""
for arg in "$@"; do
  [ "$precedent" = --hote ] && hote=$arg
  [ "$precedent" = --sha ] && sha=$arg
  [ "$precedent" = --sonde ] && cible=$arg
  precedent=$arg
done
if grep -qx "ko=sante:$hote" "$state/scenario" 2>/dev/null; then
  grep -vx "ko=sante:$hote" "$state/scenario" > "$state/scenario.tmp" || true
  mv "$state/scenario.tmp" "$state/scenario"
  echo "courrier $hote : KO (echec injecte)" >&2
  exit 1
fi
if [ "$cible" = jamais ]; then
  printf '{"hote": "%s", "verdict": "sans-sonde", "sha": "%s", "sonde": null, "pourquoi": "sonde desactivee"}\n' "$hote" "$sha"
  exit 0
fi
if [ "$hote" = manaty-mesh-1 ]; then
  printf '{"hote": "%s", "verdict": "ok", "sha": "%s", "personas": 0, "souffrance": [], "boites_mortes": [], "sonde": {"message": %s, "destinataire": "%s", "delai_s": 2.0, "hote_destinataire": "ameesh-agents-1"}, "pourquoi": null}\n' "$hote" "$sha" 7 quidu
  exit 0
fi
printf '{"hote": "%s", "verdict": "ok", "sha": "%s", "personas": 2, "souffrance": [], "boites_mortes": [], "sonde": {"message": %s, "destinataire": "%s", "delai_s": 2.0, "hote_destinataire": "%s"}, "pourquoi": null}\n' "$hote" "$sha" 7 quidu "$hote"
'''

VENV_AMEESH = r'''#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_STATE:?}
printf 'stub %s %s\n' "$0" "$*" >> "$state/calls.log"
printf 'venv-ameesh %s\n' "$*" >> "$state/calls.log"
exit 0
'''

SSH = r'''#!/usr/bin/env bash
printf 'stub %s %s\n' "$0" "$*" >> "${FAKE_STATE:?}/calls.log"
printf 'ssh %s\n' "$*" >> "${FAKE_STATE:?}/calls.log"
case "$*" in
  *"rev-parse --verify HEAD"*) echo "0123456789abcdef0123456789abcdef01234567"; exit 0 ;;  # clone migrations (#4050)
  *"bash -s -- "*) cat >/dev/null || true; exit 0 ;;   # migrations depuis une VM (#3848)
  *ameesh-sauvegarde.service*)   # dump serveur, poste absent (relecture dev1 #3848)
    cat >/dev/null || true
    printf '/home/ameesh/sauvegardes/ameesh-t.dump\n4096\n%064d\n' 0; exit 0 ;;
esac
exit 255
'''

#: #3039 : `staging.sh` enveloppe `redemarrer`/`retour` du poste dans
#: `systemd-run --user`. Le banc tient le meme contrat (options, cwd, setenv)
#: et lance la commande directement, sans bus systemd.
SYSTEMD_RUN = r'''#!/usr/bin/env python3
import os
import subprocess
import sys

args = list(sys.argv[1:])
env = dict(os.environ)
cwd = None
index = 0
while index < len(args):
    arg = args[index]
    if arg in ('--user', '--collect', '--wait', '--pipe'):
        index += 1
    elif arg == '--unit':
        index += 2
    elif arg == '--working-directory':
        cwd = args[index + 1]
        index += 2
    elif arg.startswith('--setenv='):
        name, value = arg[len('--setenv='):].split('=', 1)
        env[name] = value
        index += 1
    else:
        break
raise SystemExit(subprocess.run(args[index:], cwd=cwd, env=env).returncode)
'''


class HotesPartagesTest(unittest.TestCase):
    """#3965 : la coupure relit l'attestation avec les memes hotes qu'actif.py."""

    def test_release_cutoff_et_actif_listent_les_memes_hotes(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
        from ameesh import release_cutoff
        actif = charger_actif()
        self.assertEqual(release_cutoff.STAGING_AGENTS, actif.HOTES_AGENTS)
        self.assertEqual(release_cutoff.STAGING_VMS, actif.HOTES_VMS)
        self.assertEqual(release_cutoff.STAGING_HOSTS, actif.HOTES_CANON)


if __name__ == '__main__':
    unittest.main()
