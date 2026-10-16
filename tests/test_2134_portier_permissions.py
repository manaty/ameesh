# SPDX-License-Identifier: AGPL-3.0-only
"""#2134 — un appelant accorde ce que le reusable qu'il appelle demande.

GitHub refuse le fichier de l'appelant quand le workflow appelé demande une
permission que l'appelant n'accorde pas, et le run ne démarre pas. Bannière du
run 38964102149 (lue sur la page du run : elle n'est ni dans l'API, ni dans un
log) :

    Invalid workflow file: .github/workflows/paquet.yml#L79
    The workflow is not valid. .github/workflows/paquet.yml (Line: 79, Col: 3):
    Error calling workflow
    'manaty/ameesh/.github/workflows/tests.yml@2efd2816f7338528b3994e4aa4d3420ab504eb94'.
    The nested job 'tests' is requesting 'actions: read', but is only allowed
    'actions: none'.

Ce n'est pas un run en échec : c'est un `startup_failure`, zéro job, aucun
log — le message n'existe que dans la bannière HTML de la page du run. Un
appelant qui déclare `permissions:` reçoit `none` pour toute portée qu'il
n'énumère pas : `paquet.yml` accordait contents, pull-requests et checks, et
taisait `actions`, que le job `tests` de `tests.yml` demande depuis #856
(relire l'état terminal des jobs d'une exécution).

Le défaut ne se voyait que quand le job `suite` devait démarrer : tant qu'une
PR ouverte couvrait la tête poussée, il était `skipped` et le portier restait
vert (relevé du 16/10 : tous les succès du portier ont `suite` sauté, tous ses
échecs sont des `startup_failure` sans même le job `couverture`). Le repli du
portier — « la suite tourne » — était donc mort pile dans le cas qu'il existe
pour servir : hors PR ouverte, aucune suite ne part, le check requis n'existe
pas et la fusion est bloquée.

L'invariant est une RELATION entre deux fichiers. Ni un YAML lu seul, ni
actionlint (qui ne résout pas les `uses:` locaux) ne le voient ; d'où ce
témoin, qui tient les deux documents et refuse de conclure quand il ne peut
pas décider (aucun droit déclaré, forme inconnue, appelé introuvable) : le
silence d'un témoin qui ne vérifie rien est le défaut qu'il surveille.

#2134 — a caller grants what the reusable it calls asks for. GitHub rejects
the caller's whole file (startup_failure, zero jobs, no log) when the called
workflow requests a permission the caller does not grant; a caller that
declares `permissions:` gets `none` for every scope it omits. The invariant is
a relation between two files, so the witness holds both and fails closed.
"""
from __future__ import annotations

import os
import unittest

from ameesh import canon

from .support import REPO

WORKFLOWS = os.path.join(REPO, ".github", "workflows")
#: un `uses:` local : le fichier est résolu au même commit que l'appelant
LOCAL = "./.github/workflows/"

#: l'ordre de GitHub : `read` ne satisfait pas `write`
RANG = {"none": 0, "read": 1, "write": 2}
#: les `permissions:` en chaîne couvrent d'un coup toutes les portées
TOUT = {"none": 0, "read-all": 1, "write-all": 2}
NOMS = {0: "none", 1: "read", 2: "write"}


def droit(valeur):
    """(portées déclarées, rang par défaut) d'une valeur `permissions:`.

    Un dictionnaire est pris tel quel (portée absente : `none`) ; `none`,
    `read-all` et `write-all` couvrent tout (`portées` vide, défaut non nul).
    Toute autre forme lève ValueError : le témoin refuse de conclure, il ne
    suppose jamais un droit."""
    if valeur is None:
        return {}, 0
    if isinstance(valeur, str):
        if valeur not in TOUT:
            raise ValueError("permissions: %r inconnu" % valeur)
        return {}, TOUT[valeur]
    if isinstance(valeur, dict):
        portees = {}
        for portee, niveau in valeur.items():
            if niveau not in RANG:
                raise ValueError("permissions: %s: %r inconnu" % (portee, niveau))
            portees[str(portee)] = RANG[niveau]
        return portees, 0
    raise ValueError("permissions: forme inconnue %r" % (valeur,))


def droit_du_job(appelant, job):
    """Le droit effectif du job appelant : son bloc `permissions:` s'il en
    déclare un (il remplace celui du workflow), sinon celui du workflow ;
    `None` si ni l'un ni l'autre n'en déclare — le droit vient alors du défaut
    du dépôt, réglage mutable que le fichier ne fige pas."""
    if "permissions" in job:
        return job["permissions"]
    return appelant.get("permissions")


def couches_demandees(appele):
    """[(où, portées, défaut)] : le bloc de permissions du workflow appelé,
    puis celui de chaque job qui en déclare un. Un job sans bloc hérite du
    workflow : il n'ajoute rien à vérifier."""
    portees, defaut = droit(appele.get("permissions"))
    couches = [("workflow", portees, defaut)]
    for job_nom, job in (appele.get("jobs") or {}).items():
        if isinstance(job, dict) and "permissions" in job:
            portees, defaut = droit(job["permissions"])
            couches.append((str(job_nom), portees, defaut))
    return couches


def _manques_dune_couche(accordees, defaut, ou, portees, demande_defaut):
    """Les portées qu'une couche de demande réclame au-delà du droit accordé.

    Un défaut de demande non nul (`read-all`, `write-all`) réclame TOUTES les
    portées à ce rang : le pire droit accordé (le minimum) décide, et la ligne
    nomme la portée qui l'atteint."""
    lignes = []
    for portee in sorted(portees):
        rang = portees[portee]
        accorde = accordees.get(portee, defaut)
        if rang > accorde:
            lignes.append("%s demande %s: %s — accordé %s: %s"
                          % (ou, portee, NOMS[rang], portee, NOMS[accorde]))
    if demande_defaut > 0:
        pire = min([defaut] + list(accordees.values()))
        if demande_defaut > pire:
            ou_portee = "toutes les portées"
            if pire != defaut:
                ou_portee = sorted(p for p, r in accordees.items() if r == pire)[0]
            lignes.append("%s demande %s: %s — accordé %s: %s"
                          % (ou, ou_portee, NOMS[demande_defaut], ou_portee,
                             NOMS[pire]))
    return lignes


def manquantes(appelant_nom, appelant, job_nom, appele_nom, appele):
    """Les portées que l'appelé demande et que le job appelant n'accorde pas,
    en lignes lisibles. Toute indécision est rendue comme un manque."""
    if appele is None:
        return ["%s/%s : appelé introuvable, contrat non vérifiable"
                % (appelant_nom, job_nom)]
    job = (appelant.get("jobs") or {}).get(job_nom) or {}
    try:
        accordees, defaut = droit(droit_du_job(appelant, job))
    except ValueError as exc:
        return ["%s/%s : %s ; contrat non vérifiable" % (appelant_nom, job_nom, exc)]
    if droit_du_job(appelant, job) is None:
        return ["%s/%s : aucune permission déclarée (job ni workflow) ; le droit "
                "vient du défaut du dépôt, que le fichier ne fige pas — déclarer "
                "les portées explicitement" % (appelant_nom, job_nom)]
    try:
        couches = couches_demandees(appele)
    except ValueError as exc:
        return ["%s : %s ; contrat non vérifiable" % (appele_nom, exc)]
    lignes = []
    for ou, portees, demande_defaut in couches:
        lignes += _manques_dune_couche(
            accordees, defaut, "%s (%s)" % (appele_nom, ou), portees, demande_defaut)
    if lignes:
        return ["%s/%s appelle %s : %s"
                % (appelant_nom, job_nom, appele_nom, "; ".join(lignes))]
    return []


def appels_locaux():
    """[(appelant, job, appelé, chemin)] de chaque `uses:` local du dépôt.

    `appelé` vaut None quand le fichier visé n'existe pas : un appel local
    cassé est signalé, jamais sauté en silence."""
    appels = []
    for nom in sorted(os.listdir(WORKFLOWS)):
        if not nom.endswith((".yml", ".yaml")):
            continue
        doc = _document(os.path.join(WORKFLOWS, nom))
        for job_nom, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            uses = str(job.get("uses") or "")
            if not uses.startswith(LOCAL):
                continue
            chemin = os.path.normpath(os.path.join(REPO, uses.removeprefix("./")))
            appele = os.path.basename(chemin) if os.path.exists(chemin) else None
            appels.append((nom, str(job_nom), appele, chemin))
    return appels


def manques_du_depot():
    """Les manques de tous les appels locaux du dépôt."""
    lignes = []
    for appelant_nom, job_nom, appele_nom, chemin in appels_locaux():
        appelant = _document(os.path.join(WORKFLOWS, appelant_nom))
        appele = _document(chemin) if appele_nom else None
        lignes += manquantes(appelant_nom, appelant, job_nom, appele_nom, appele)
    return lignes


def _document(chemin):
    with open(chemin, encoding="utf-8") as fh:
        return canon.load_yaml(fh.read()) or {}


class ContratPermissionsTest(unittest.TestCase):
    """La relation appelant/appelé, tenue sur les fichiers du dépôt."""

    def test_le_scan_trouve_au_moins_un_appel_local(self):
        # garde de repli : si la découverte casse, le témoin ne doit pas
        # passer parce qu'il ne regarde plus rien.
        appels = appels_locaux()
        self.assertTrue(appels, "aucun `uses:` local trouvé : le scan ne "
                                "vérifie rien, il ne peut pas être vert")

    def test_le_portier_paquet_appelle_la_suite_par_le_reusable_tests_yml(self):
        # l'invariant du lot porte sur cette paire : si elle disparaît, le
        # témoin générique deviendrait vide sans le dire.
        self.assertIn(("paquet.yml", "suite", "tests.yml",
                       os.path.join(WORKFLOWS, "tests.yml")),
                      appels_locaux())

    def test_chaque_appelant_accorde_ce_que_l_appele_demande(self):
        manques = manques_du_depot()
        self.assertEqual(manques, [], (
            "GitHub refuse le fichier de l'appelant pour un droit manquant : le "
            "run ne démarre pas (startup_failure, zéro job, aucun log). Un "
            "appelant qui déclare `permissions:` reçoit `none` pour toute "
            "portée qu'il n'énumère pas.\n" + "\n".join(manques)))

    def test_le_portier_accorde_actions_read(self):
        # la régression #2134, en clair : le job `tests` de tests.yml relit
        # l'état terminal de ses jobs (#856), il lui faut `actions: read`.
        appelant = _document(os.path.join(WORKFLOWS, "paquet.yml"))
        job = (appelant.get("jobs") or {}).get("suite") or {}
        accordees, defaut = droit(droit_du_job(appelant, job))
        self.assertGreaterEqual(accordees.get("actions", defaut), RANG["read"],
                                "le job `suite` doit accorder `actions: read` au "
                                "reusable tests.yml (#856, #2134)")

    def test_le_temoin_voit_une_permission_manquante(self):
        # rouge/vert du vérificateur lui-même, sur des documents synthétiques :
        # il signale l'absence, puis se tait quand elle est comblée.
        appele = {"permissions": {"contents": "read"},
                  "jobs": {"tests": {"permissions": {"contents": "read",
                                                     "actions": "read"}}}}
        rouge = {"permissions": {"contents": "read", "checks": "write"},
                 "jobs": {"suite": {"uses": LOCAL + "tests.yml"}}}
        lignes = manquantes("paquet.yml", rouge, "suite", "tests.yml", appele)
        self.assertTrue(lignes, "l'absence d'`actions: read` n'est pas vue")
        self.assertIn("actions", "\n".join(lignes))
        vert = {"permissions": {"contents": "read", "checks": "write",
                                "actions": "read"},
                "jobs": {"suite": {"uses": LOCAL + "tests.yml"}}}
        self.assertEqual(manquantes("paquet.yml", vert, "suite", "tests.yml",
                                    appele), [])

    def test_un_read_all_de_l_appelant_couvre_la_demande(self):
        # `permissions: read-all` est un droit, pas une forme inconnue : il
        # couvre toute demande de rang `read`.
        appelant = {"permissions": "read-all",
                    "jobs": {"suite": {"uses": LOCAL + "tests.yml"}}}
        appele = {"jobs": {"tests": {"permissions": {"contents": "read",
                                                     "actions": "read"}}}}
        self.assertEqual(manquantes("paquet.yml", appelant, "suite", "tests.yml",
                                    appele), [])

    def test_un_droit_non_declare_refuse_de_conclure(self):
        # pas de `permissions:` du tout : le défaut du dépôt est un réglage
        # mutable, le témoin ne le suppose pas — il refuse de conclure.
        appelant = {"jobs": {"suite": {"uses": LOCAL + "tests.yml"}}}
        appele = {"jobs": {"tests": {"permissions": {"contents": "read"}}}}
        lignes = manquantes("paquet.yml", appelant, "suite", "tests.yml", appele)
        self.assertTrue(lignes, "un droit non déclaré doit refuser de conclure")
        self.assertIn("aucune permission déclarée", "\n".join(lignes))

    def test_une_forme_de_permissions_inconnue_refuse_de_conclure(self):
        appelant = {"permissions": {"contents": "parfois"},
                    "jobs": {"suite": {"uses": LOCAL + "tests.yml"}}}
        appele = {"jobs": {"tests": {"permissions": {"contents": "read"}}}}
        lignes = manquantes("paquet.yml", appelant, "suite", "tests.yml", appele)
        self.assertTrue(lignes, "une forme inconnue doit refuser de conclure")
        self.assertIn("non vérifiable", "\n".join(lignes))

    def test_un_appele_introuvable_refuse_de_conclure(self):
        appelant = {"permissions": {"contents": "read"},
                    "jobs": {"suite": {"uses": LOCAL + "absent.yml"}}}
        lignes = manquantes("paquet.yml", appelant, "suite", None, None)
        self.assertTrue(lignes, "un appelé introuvable doit refuser de conclure")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
