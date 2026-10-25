#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""#3549 — migrate pc-smichea's runners to the Unix user `ameesh`, reversibly.

Décision owner #135872 a (signée le 25/10/2026, approbation #110) : les
exécuteurs d'agents de pc-smichea passent sous un utilisateur Unix distinct,
`ameesh`, comme sur les VM, avec une clé dédiée dans son home. Le `~/.ssh`
(0700) de l'owner leur devient illisible, et c'est vérifiable. Les séances
GUI et les appareils restent au bureau, sous le compte de l'owner. Le retour
arrière est une commande : `retour`.

Ce script est le **côté hôte** du runbook `migration-runners-ameesh.md`. Il
est idempotent, journalise chaque étape sous
`/var/lib/ameesh/migration-runners/` (0700) et n'écrit aucun secret dans son
journal. Il ne fait rien sans un sous-commande explicite :

    verifier     inventaire et gardes, lecture seule (sans root)
    preparer     utilisateur, home, linger, config/état/canon/repos, venv,
                 unités installées mais pas démarrées (root)
    identites    clé SSH dédiée, `gh`, auth Codex/DeepSeek par copie
                 contrôlée, et les connexions web Claude à faire par l'owner
                 (root)
    basculer     drain des exécuteurs, copie finale, cwd, démarrage sous
                 `ameesh`, preuve par le canari, retrait des pièces #3444/
                 #3540 devenues inutiles (root)
    preuve       matrice de preuves de la migration (root)
    retour       retour en une commande aux unités utilisateur de smichea
                 (root)
    etat         état et journal de la dernière exécution

Le runbook décrit l'ordre, les créneaux à éviter, les vagues de drain et les
limites connues. Aucun sous-commande n'appelle `sudo` : le bureau lance le
script en root (ou avec un `sudo` explicite).
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import grp
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# constantes du poste
# --------------------------------------------------------------------------

HOTE = "pc-smichea"
SOURCE = "smichea"
CIBLE = "ameesh"
SERVICE = "ameesh-runner-agent@.service"
PREFIXE_UNITE = "ameesh-runner-agent@"
SLICE = "ameesh-agents.slice"
#: #3444 (drop-in cles-owner.conf) et #3540 (git-agents.conf) : sans objet
#: après la séparation des comptes ; retirés par `basculer`, restaurés par
#: `retour`.
OBSOLETES = ("cles-owner.conf", "git-agents.conf")
ETATS_QUEUE_INACTIFS = {"landed", "ejected", "skipped", "cancelled"}
MARGE_AVANT_MIN = 60
MARGE_APRES_MIN = 30
FUSEAU = "Europe/Paris"
#: empreinte GitHub publiée (docs.github.com, clés SSH de github.com).
EMPREINTE_GITHUB = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
CLE_GITHUB = ("github.com ssh-ed25519 "
              "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl")
PATHS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
#: motifs de journalisation d'un secret : jamais dans le journal ni stdout.
SECRET = re.compile(r"(password|passwd|token|secret|api[_-]?key|BEGIN [A-Z ]*PRIVATE KEY)",
                    re.IGNORECASE)


class Refus(Exception):
    """Une garde refuse le geste ; rien de destructif n'a été fait."""


# --------------------------------------------------------------------------
# petit socle : comptes, hôte, exécution
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Compte:
    nom: str
    uid: int
    gid: int
    home: str
    shell: str
    groupes: tuple[str, ...] = ()


@dataclasses.dataclass
class Resultat:
    code: int
    sortie: str = ""
    erreur: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0


Executeur = Callable[[Sequence[str], Mapping[str, str] | None, float | None], Resultat]


@dataclasses.dataclass
class Hote:
    """Le poste, avec une racine injectable pour les tests.

    En production `racine = /`. Les tests construisent un poste factice avec
    une racine temporaire, des comptes injectés et un exécuteur qui enregistre
    les commandes : aucun test ne touche l'hôte réel.
    """

    racine: Path = Path("/")
    comptes: dict[str, Compte] | None = None
    executeur: Executeur | None = None
    appels: list[tuple[str, ...]] = dataclasses.field(default_factory=list)

    def chemin(self, chemin: str | Path) -> Path:
        chemin = str(chemin)
        return self.racine / chemin.lstrip("/") if str(self.racine) != "/" else Path(chemin)

    def est_racine(self) -> bool:
        return hasattr(os, "geteuid") and os.geteuid() == 0

    def compte(self, nom: str) -> Compte:
        if self.comptes is not None:
            try:
                return self.comptes[nom]
            except KeyError:
                raise Refus("compte Unix absent de l'inventaire : %s" % nom)
        try:
            ligne = pwd.getpwnam(nom)
        except KeyError:
            raise Refus("compte Unix absent : %s" % nom)
        groupes = []
        try:
            groupes = [grp.getgrgid(g).gr_name
                       for g in os.getgrouplist(nom, ligne.pw_gid)]
        except OSError:
            pass
        return Compte(nom, ligne.pw_uid, ligne.pw_gid, ligne.pw_dir,
                      ligne.pw_shell or "", tuple(groupes))

    def lancer(self, argv: Sequence[str], *, utilisateur: str | None = None,
               env: Mapping[str, str] | None = None, verifier: bool = True,
               delai: float | None = 600) -> Resultat:
        """Lance une commande, éventuellement sous un autre compte, sans son
        environnement de session (HOME/XDG/DBUS posés explicitement)."""
        argv = [str(x) for x in argv]
        environnement: dict[str, str] = {}
        if utilisateur is not None:
            compte = self.compte(utilisateur)
            environnement = {
                "HOME": compte.home,
                "XDG_RUNTIME_DIR": "/run/user/%d" % compte.uid,
                "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % compte.uid,
                "PATH": ":".join([compte.home + "/.local/bin", *PATHS]),
                "LANG": "C.UTF-8",
            }
            environnement.update(env or {})
            if os.geteuid() != compte.uid:
                argv = ["runuser", "-u", compte.nom, "--", "env", "-i",
                        *["%s=%s" % (k, v) for k, v in environnement.items()],
                        *argv]
            else:
                environnement = dict(environnement)
        elif env:
            environnement = dict(env)
        self.appels.append(tuple(argv))
        if self.executeur is not None:
            return self.executeur(argv, environnement or None, delai)
        try:
            fait = subprocess.run(argv, capture_output=True, text=True,
                                  env=environnement or None, timeout=delai)
        except subprocess.TimeoutExpired:
            return Resultat(124, "", "délai dépassé : %s" % " ".join(argv[:4]))
        except FileNotFoundError as exc:
            return Resultat(127, "", str(exc))
        return Resultat(fait.returncode, fait.stdout or "", fait.stderr or "")

    def lire(self, chemin: str | Path, *, defaut: str | None = None) -> str | None:
        try:
            return Path(chemin).read_text(encoding="utf-8")
        except OSError:
            return defaut


@dataclasses.dataclass
class Contexte:
    hote: Hote
    depot: Path
    etat_dir: Path
    source: Compte | None = None
    cible: Compte | None = None
    racine_surcharge: bool | None = None

    @property
    def source_home(self) -> Path:
        base = self.source.home if self.source is not None else "/home/%s" % SOURCE
        return self.hote.chemin(base)

    @property
    def cible_home(self) -> Path:
        base = self.cible.home if self.cible is not None else "/home/%s" % CIBLE
        return self.hote.chemin(base)

    def changer_source(self) -> Compte:
        if self.source is None:
            self.source = self.hote.compte(SOURCE)
        return self.source

    def changer_cible(self) -> Compte:
        if self.cible is None:
            self.cible = self.hote.compte(CIBLE)
        return self.cible

    def est_racine(self) -> bool:
        if self.racine_surcharge is not None:
            return self.racine_surcharge
        return self.hote.est_racine()


def dire(message: str = "") -> None:
    print(message, flush=True)


def refuser(message: str) -> None:
    raise Refus(message)


def sans_secret(texte: str) -> str:
    """Retire les lignes qui ressemblent à un secret avant journalisation."""
    sortie = []
    for ligne in texte.splitlines():
        sortie.append("[masqué]" if SECRET.search(ligne) else ligne)
    return "\n".join(sortie)


# --------------------------------------------------------------------------
# journal et état
# --------------------------------------------------------------------------


def horodatage() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def journal(ctx: Contexte, etape: str, message: str, **donnees) -> None:
    ctx.etat_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(ctx.etat_dir, 0o700)
    ligne = {"ts": horodatage(), "etape": etape, "message": sans_secret(message),
             "donnees": donnees}
    chemin_journal = ctx.etat_dir / "journal.jsonl"
    with open(chemin_journal, "a", encoding="utf-8") as flux:
        flux.write(json.dumps(ligne, ensure_ascii=False) + "\n")
    with contextlib.suppress(OSError):
        os.chmod(chemin_journal, 0o600)
    dire("  [%s] %s" % (etape, message))


def etat_vide() -> dict:
    return {"version": 1, "stage": "neuf", "commence": horodatage(),
            "source": SOURCE, "cible": CIBLE, "hote": HOTE,
            "agents": [], "actifs": [], "enabled": [], "pauses": {},
            "pauses_etrangeres": {}, "cles": {}, "etapes": []}


def lit_etat(ctx: Contexte) -> dict:
    chemin = ctx.etat_dir / "etat.json"
    try:
        with open(chemin, encoding="utf-8") as flux:
            etat = json.load(flux)
    except (OSError, ValueError):
        return etat_vide()
    if not isinstance(etat, dict) or etat.get("version") != 1:
        refuser("état de migration illisible : %s" % chemin)
    return etat


def ecrit_etat(ctx: Contexte, etat: dict) -> None:
    ctx.etat_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    chemin = ctx.etat_dir / "etat.json"
    tmp = chemin.with_suffix(".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as flux:
        json.dump(etat, flux, ensure_ascii=False, indent=2)
        flux.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, chemin)


@contextlib.contextmanager
def verrou(ctx: Contexte, action: str):
    """Un seul geste de migration à la fois, même depuis deux terminaux."""
    ctx.etat_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    chemin = ctx.etat_dir / "verrou"
    with open(chemin, "a+", encoding="utf-8") as flux:
        try:
            fcntl.flock(flux.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            refuser("une autre migration tient le verrou %s" % chemin)
        flux.seek(0)
        flux.truncate()
        flux.write("%s %s %d\n" % (action, horodatage(), os.getpid()))
        flux.flush()
        yield


# --------------------------------------------------------------------------
# inventaire et gardes
# --------------------------------------------------------------------------


def exiger_racine(ctx: Contexte) -> None:
    if not ctx.est_racine():
        refuser("ce geste modifie l'hôte : le lancer en root (sudo)")


def exiger_source(ctx: Contexte) -> Compte:
    return ctx.changer_source()


def exiger_cible(ctx: Contexte) -> Compte:
    return ctx.changer_cible()


def exiger_hote(ctx: Contexte) -> None:
    """Refuse tout autre poste que pc-smichea, sur la config vivante."""
    config = ctx.source_home / ".config/ameesh/config.json"
    try:
        donnees = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        refuser("configuration de la source illisible (%s) : %s" % (config, exc))
    hote = donnees.get("host")
    if hote != HOTE:
        refuser("hôte configuré %r, attendu %r : mauvais poste" % (hote, HOTE))


def lire_json(chemin: Path) -> dict:
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        refuser("JSON illisible (%s) : %s" % (chemin, exc))


def ameesh_source(ctx: Contexte, *args: str, delai: float = 120) -> Resultat:
    binaire = ctx.source_home / ".local/share/ameesh/venv/bin/ameesh"
    return ctx.hote.lancer([str(binaire), *args], utilisateur=SOURCE, delai=delai)


def ameesh_cible(ctx: Contexte, *args: str, delai: float = 120) -> Resultat:
    binaire = ctx.cible_home / ".local/share/ameesh/venv/bin/ameesh"
    return ctx.hote.lancer([str(binaire), *args], utilisateur=CIBLE, delai=delai)


def systemctl_source(ctx: Contexte, *args: str, delai: float = 120) -> Resultat:
    return ctx.hote.lancer(["systemctl", "--user", *args], utilisateur=SOURCE,
                           delai=delai)


def systemctl_cible(ctx: Contexte, *args: str, delai: float = 120) -> Resultat:
    return ctx.hote.lancer(["systemctl", "--user", *args], utilisateur=CIBLE,
                           delai=delai)


def unites_de(agents: Sequence[str]) -> list[str]:
    return ["%s%s.service" % (PREFIXE_UNITE, nom) for nom in agents]


def etat_enabled(ctx: Contexte, agents: Sequence[str], *,
                 cible: bool = False) -> list[str]:
    """Les agents dont l'unité est activée au démarrage."""
    actifs = []
    for nom in agents:
        if statut_unite(ctx, nom, cible=cible)["active_file"] == "enabled":
            actifs.append(nom)
    return actifs


def activer_unites(ctx: Contexte, agents: Sequence[str], *,
                   cible: bool = False) -> None:
    outil = systemctl_cible if cible else systemctl_source
    for nom in agents:
        unite = "%s%s.service" % (PREFIXE_UNITE, nom)
        fait = outil(ctx, "enable", unite, delai=60)
        if not fait.ok:
            refuser("enable %s a échoué : %s" % (unite, fait.erreur.strip()))
    fait = outil(ctx, "daemon-reload", delai=60)
    if not fait.ok:
        refuser("daemon-reload après enable a échoué : %s" % fait.erreur.strip())


def desactiver_unites(ctx: Contexte, agents: Sequence[str], *,
                      cible: bool = False) -> None:
    outil = systemctl_cible if cible else systemctl_source
    for nom in agents:
        unite = "%s%s.service" % (PREFIXE_UNITE, nom)
        fait = outil(ctx, "disable", unite, delai=60)
        if not fait.ok:
            refuser("disable %s a échoué : %s" % (unite, fait.erreur.strip()))
    fait = outil(ctx, "daemon-reload", delai=60)
    if not fait.ok:
        refuser("daemon-reload après disable a échoué : %s" % fait.erreur.strip())


def agents_du_poste(ctx: Contexte) -> list[dict]:
    """Les agents en mode execute sur pc-smichea, lus au registre."""
    fait = ameesh_source(ctx, "list", "--json")
    if not fait.ok:
        refuser("registre illisible (`ameesh list --json`) : %s" % fait.erreur.strip())
    try:
        lignes = json.loads(fait.sortie)
    except ValueError as exc:
        refuser("registre illisible (`ameesh list --json`) : %s" % exc)
    if not isinstance(lignes, list):
        refuser("registre illisible : liste attendue")
    agents = []
    for ligne in lignes:
        if not isinstance(ligne, dict):
            continue
        if ligne.get("host") != HOTE:
            continue
        if ligne.get("mode") != "execute":
            continue
        nom = str(ligne.get("name") or "")
        if not nom or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", nom):
            refuser("nom d'agent inattendu au registre : %r" % nom)
        agents.append({"name": nom, "harness": ligne.get("harness") or "",
                       "status": ligne.get("status") or "",
                       "operator_paused": bool(ligne.get("operator_paused"))})
    return sorted(agents, key=lambda a: a["name"])


def propiete_unite(sortie: str, cle: str) -> str:
    for ligne in sortie.splitlines():
        if ligne.startswith(cle + "="):
            return ligne.split("=", 1)[1]
    return ""


def statut_unite(ctx: Contexte, nom: str, *, cible: bool = False,
                 delai: float = 30) -> dict:
    fait = (systemctl_cible if cible else systemctl_source)(
        ctx, "show", "--property=LoadState,ActiveState,MainPID,UnitFileState",
        "%s%s.service" % (PREFIXE_UNITE, nom), delai=delai)
    return {"ok": fait.ok,
            "charge": propiete_unite(fait.sortie, "LoadState"),
            "actif": propiete_unite(fait.sortie, "ActiveState"),
            "pid": propiete_unite(fait.sortie, "MainPID"),
            "active_file": propiete_unite(fait.sortie, "UnitFileState")}


def unites_installees(ctx: Contexte, *, cible: bool = False) -> list[str]:
    """Les instances d'unité présentes, déduites du registre et de systemctl."""
    agents = [a["name"] for a in agents_du_poste(ctx)]
    trouves = set()
    fait = (systemctl_cible if cible else systemctl_source)(
        ctx, "list-units", "--all", "--plain", "--no-legend",
        PREFIXE_UNITE + "*.service", delai=60)
    if fait.ok:
        for ligne in fait.sortie.splitlines():
            champs = ligne.split()
            if champs and champs[0].startswith(PREFIXE_UNITE):
                nom = champs[0][len(PREFIXE_UNITE):]
                if nom.endswith(".service"):
                    nom = nom[:-len(".service")]
                trouves.add(nom)
    return sorted(set(agents) | trouves)


def creneau(ctx: Contexte) -> tuple[bool, str]:
    """Le geste doit rester hors des coupures (11:00 et 23:00 Europe/Paris)."""
    fait = ameesh_source(ctx, "release", "calendar", delai=60)
    if not fait.ok:
        return False, "calendrier de coupure illisible : %s" % fait.erreur.strip()
    correspondance = re.search(r"prochaine\s+(\d{1,2})/(\d{1,2})\s+(\d{2}):(\d{2})",
                               fait.sortie)
    if not correspondance:
        return False, "prochaine coupure introuvable dans %r" % fait.sortie.strip()
    jour, mois, heure, minute = (int(x) for x in correspondance.groups())
    try:
        from zoneinfo import ZoneInfo
        fuseau = ZoneInfo(FUSEAU)
    except Exception:  # tzdata absente : refus, jamais une heure locale devinée
        return False, "fuseau %s indisponible (tzdata)" % FUSEAU
    maintenant = dt.datetime.now(fuseau)
    prochaine = dt.datetime(maintenant.year, mois, jour, heure, minute, tzinfo=fuseau)
    while prochaine <= maintenant:
        prochaine += dt.timedelta(days=1)
    avant = (prochaine - maintenant).total_seconds() / 60.0
    precedente = prochaine - dt.timedelta(hours=12)
    apres = (maintenant - precedente).total_seconds() / 60.0
    if avant < MARGE_AVANT_MIN:
        return False, ("coupure dans %.0f min (< %d) : attendre %s"
                       % (avant, MARGE_AVANT_MIN, prochaine.strftime("%d/%m %H:%M")))
    if apres < MARGE_APRES_MIN:
        return False, ("coupure il y a %.0f min (< %d) : laisser passer le staging"
                       % (apres, MARGE_APRES_MIN))
    return True, ("hors créneau : prochaine coupure %s, dans %.0f min"
                  % (prochaine.strftime("%d/%m %H:%M"), avant))


def paquets_inactifs(ctx: Contexte) -> tuple[bool, str]:
    fait = ameesh_source(ctx, "work", "queue", "show", "--target", "main", "--json",
                         delai=60)
    if not fait.ok:
        return False, "file des paquets illisible : %s" % fait.erreur.strip()
    try:
        lignes = json.loads(fait.sortie)
    except ValueError as exc:
        return False, "file des paquets illisible : %s" % exc
    if not isinstance(lignes, list):
        return False, "file des paquets inattendue"
    actifs = [ligne for ligne in lignes
              if str(ligne.get("state") or "").lower() not in ETATS_QUEUE_INACTIFS]
    if actifs:
        details = ", ".join("#%s %s" % (l.get("work_item_id"), l.get("state"))
                            for l in actifs[:5])
        return False, "paquet(s) en cours sur main : %s" % details
    return True, "aucun paquet en cours sur main"


def drill_recent(ctx: Contexte) -> tuple[bool, str]:
    dossier = ctx.source_home / ".local/state/ameesh/drills"
    if not dossier.is_dir():
        return True, "aucun drill local"
    limite = time.time() - 2 * 3600
    recents = []
    for fichier in dossier.glob("*.jsonl"):
        with contextlib.suppress(OSError):
            if fichier.stat().st_mtime >= limite:
                recents.append(fichier.name)
    if recents:
        return False, "drill récent sur le poste : %s" % ", ".join(sorted(recents)[:3])
    return True, "aucun drill local récent"


def config_source(ctx: Contexte) -> dict:
    return lire_json(ctx.source_home / ".config/ameesh/config.json")


def comptes_declares(config: dict) -> dict[str, list[dict]]:
    comptes = config.get("accounts") or {}
    if not isinstance(comptes, dict):
        refuser("config accounts : objet attendu")
    sortie: dict[str, list[dict]] = {}
    for harnais, lignes in comptes.items():
        if not isinstance(lignes, list):
            refuser("config accounts.%s : liste attendue" % harnais)
        sortie[str(harnais)] = [dict(l) for l in lignes if isinstance(l, dict)]
    return sortie


def chemin_cible(ctx: Contexte, chemin: str | None, defaut: str) -> Path:
    """Traduit un chemin du home smichea vers le home ameesh."""
    if not chemin:
        return ctx.cible_home / defaut
    chemin = chemin.replace(str(ctx.source_home), str(ctx.cible_home))
    chemin = chemin.replace("/home/%s" % SOURCE, "/home/%s" % CIBLE)
    return Path(chemin)


def dossier_config_dir(ctx: Contexte, compte: dict, harnais: str) -> Path:
    """Dossier d'un compte `config_dir`, dans le home ameesh."""
    defaut = {"claude": ".claude", "codex": ".codex", "deepseek": ".dsh"}.get(
        harnais, ".config/ameesh")
    return chemin_cible(ctx, compte.get("path"), defaut)


def reecrire_home(texte: str, source: Path, cible: Path) -> str:
    """Réécrit les chemins absolus du home source, jamais `%h`."""
    return texte.replace(str(source), str(cible))


def ecrire_fichier(ctx: Contexte, chemin: Path, donnees: str | bytes,
                   mode: int = 0o600, owner: str = CIBLE) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(donnees, str):
        donnees = donnees.encode("utf-8")
    tmp = chemin.with_name(chemin.name + ".tmp.%d" % os.getpid())
    with open(tmp, "wb") as flux:
        flux.write(donnees)
    os.chmod(tmp, mode)
    os.replace(tmp, chemin)
    if ctx.hote.comptes is None:
        compte = ctx.hote.compte(owner)
        os.chown(chemin, compte.uid, compte.gid)
    dire("  écrit %s (%04o, %s)" % (chemin, mode, owner))


def chown_cible(ctx: Contexte, chemin: Path, *, recursif: bool = False) -> None:
    """Le compte cible doit pouvoir écrire son home : les répertoires créés
    par root (état, unités, config) lui appartiennent."""
    if ctx.hote.comptes is not None:
        return
    compte = ctx.changer_cible()
    if recursif:
        for dossier, sous, fichiers in os.walk(chemin):
            os.chown(dossier, compte.uid, compte.gid)
            for nom in fichiers:
                with contextlib.suppress(OSError):
                    os.chown(os.path.join(dossier, nom), compte.uid, compte.gid,
                             follow_symlinks=False)
    else:
        os.chown(chemin, compte.uid, compte.gid)


def copier_fichier(ctx: Contexte, source: Path, cible: Path, *,
                   mode: int | None = None, owner: str = CIBLE,
                   reecrire: bool = True) -> None:
    if not source.exists():
        return
    cible.parent.mkdir(parents=True, exist_ok=True)
    texte = None
    if reecrire:
        with contextlib.suppress(UnicodeDecodeError, OSError):
            texte = source.read_text(encoding="utf-8")
            texte = reecrire_home(texte, ctx.source_home, ctx.cible_home)
    if texte is not None:
        ecrire_fichier(ctx, cible, texte, mode=mode or (source.stat().st_mode & 0o777),
                       owner=owner)
    else:
        shutil.copy2(source, cible, follow_symlinks=False)
        if mode is not None:
            os.chmod(cible, mode)
        if ctx.hote.comptes is None:
            compte = ctx.hote.compte(owner)
            os.chown(cible, compte.uid, compte.gid, follow_symlinks=False)


# --------------------------------------------------------------------------
# verifier
# --------------------------------------------------------------------------


def inventaire(ctx: Contexte) -> dict:
    """Tout ce que le runbook exige avant de préparer, en lecture seule."""
    source = exiger_source(ctx)
    exiger_hote(ctx)
    config = config_source(ctx)
    agents = agents_du_poste(ctx)
    unites = unites_installees(ctx)
    etats = {nom: statut_unite(ctx, nom) for nom in unites}
    actifs = [nom for nom, etat in etats.items() if etat["actif"] == "active"]
    arrets = [nom for nom, etat in etats.items() if etat["actif"] in ("inactive", "failed")]
    ssh = ctx.source_home / ".ssh"
    mode_ssh = oct(ssh.stat().st_mode & 0o777) if ssh.exists() else "absent"
    share = ctx.source_home / ".local/share/ameesh"
    courant = share / "src-current"
    installe = os.path.basename(os.path.realpath(courant)) if courant.exists() else ""
    version = ""
    fait = ameesh_source(ctx, "--version", delai=30)
    if fait.ok:
        version = fait.sortie.strip().splitlines()[0] if fait.sortie.strip() else ""
    cible = None
    try:
        cible = ctx.hote.compte(CIBLE)
    except Refus:
        pass
    return {
        "hote": HOTE,
        "source": dataclasses.asdict(source),
        "etats": etats,
        "mode_ssh_source": mode_ssh,
        "config": str(ctx.source_home / ".config/ameesh/config.json"),
        "host_config": config.get("host"),
        "agents": agents,
        "unites": unites,
        "actifs": actifs,
        "arrets": arrets,
        "cible": dataclasses.asdict(cible) if cible else None,
        "installe": installe,
        "version": version,
        "canon": config.get("canons") or [],
        "creneau": creneau(ctx),
        "paquets": paquets_inactifs(ctx),
        "drill": drill_recent(ctx),
    }


def afficher_inventaire(ctx: Contexte, inv: dict,
                        sortie_json: bool = False) -> None:
    if sortie_json:
        dire(json.dumps(inv, ensure_ascii=False, indent=2))
        return
    source = inv["source"]
    dire("poste            : %s" % inv["hote"])
    dire("compte source    : %s (uid %s, home %s)" % (source["nom"], source["uid"],
                                                       source["home"]))
    dire("~/.ssh source    : %s" % inv["mode_ssh_source"])
    dire("config           : %s (host %r)" % (inv["config"], inv["host_config"]))
    dire("code installé    : %s %s" % (inv["installe"] or "?",
                                        "(%s)" % inv["version"] if inv["version"] else ""))
    dire("canon            : %s" % ", ".join(
        c.get("path", "?") for c in inv["canon"]) or "aucun")
    dire("agents execute   : %d" % len(inv["agents"]))
    for agent in inv["agents"]:
        etat = inv.get("etats", {}).get(agent["name"], {})
        dire("  %-20s %-9s %-8s %s" % (agent["name"], agent["harness"],
                                        agent["status"], etat.get("actif", "?")))
    dire("unités           : %d (%d actives, %d arrêtées)"
         % (len(inv["unites"]), len(inv["actifs"]), len(inv["arrets"])))
    dire("compte cible     : %s" % (
        "%s (uid %s, home %s)" % (inv["cible"]["nom"], inv["cible"]["uid"],
                                  inv["cible"]["home"]) if inv["cible"] else "absent"))
    dire("créneau          : %s — %s" % ("OK" if inv["creneau"][0] else "KO",
                                          inv["creneau"][1]))
    dire("paquets          : %s — %s" % ("OK" if inv["paquets"][0] else "KO",
                                          inv["paquets"][1]))
    dire("drill            : %s — %s" % ("OK" if inv["drill"][0] else "KO",
                                          inv["drill"][1]))


def commande_verifier(ctx: Contexte, args) -> int:
    exiger_source(ctx)
    inv = inventaire(ctx)
    afficher_inventaire(ctx, inv, sortie_json=args.json)
    if args.avant_bascule:
        manques = verifier_avant_bascule(ctx, inv)
        if manques:
            dire("")
            dire("refus avant bascule :")
            for manque in manques:
                dire("  - %s" % manque)
            return 1
    return 0


# --------------------------------------------------------------------------
# preparer
# --------------------------------------------------------------------------


def groupes_interdits() -> set[str]:
    """Un compte d'exécuteur ne doit pas pouvoir lire le home de l'owner par
    un chemin privilégié : `docker` (root de fait), `sudo`/`wheel`, `adm`."""
    interdits = {g for g in ("docker", "sudo", "wheel", "adm", "systemd-journal")
                 if _groupe_existe(g)}
    return interdits


def _groupe_existe(nom: str) -> bool:
    try:
        grp.getgrnam(nom)
        return True
    except KeyError:
        return False


def verifier_compte_cible(ctx: Contexte) -> Compte | None:
    try:
        compte = ctx.hote.compte(CIBLE)
    except Refus:
        return None
    interdits = groupes_interdits() & set(compte.groupes)
    if interdits:
        refuser("le compte %s appartient à %s : la séparation ne serait pas "
                "réelle (lecture de ~/%s/.ssh par docker, sudo ou wheel) ; "
                "retirer ces groupes d'abord (gpasswd -d %s GROUPE)"
                % (CIBLE, ", ".join(sorted(interdits)), SOURCE, CIBLE))
    if compte.home.rstrip("/") != "/home/%s" % CIBLE:
        refuser("home de %s inattendu : %s" % (CIBLE, compte.home))
    return compte


def creer_compte(ctx: Contexte) -> Compte:
    compte = verifier_compte_cible(ctx)
    if compte is None:
        dire("  création du compte %s" % CIBLE)
        fait = ctx.hote.lancer(["useradd", "--create-home", "--home-dir",
                                "/home/%s" % CIBLE, "--shell", "/bin/bash",
                                "--user-group", CIBLE], delai=120)
        if not fait.ok:
            refuser("useradd %s a échoué : %s" % (CIBLE, fait.erreur.strip()))
        ctx.cible = None
        compte = verifier_compte_cible(ctx)
        if compte is None:
            refuser("compte %s toujours absent après useradd" % CIBLE)
        with contextlib.suppress(Exception):
            ctx.hote.lancer(["passwd", "-l", CIBLE], delai=30)
    return compte


def preparer_dossiers(ctx: Contexte) -> None:
    compte = exiger_cible(ctx)
    for chemin, mode in (
            (ctx.cible_home, 0o750),
            (ctx.cible_home / ".config", 0o700),
            (ctx.cible_home / ".config/ameesh", 0o700),
            (ctx.cible_home / ".local", 0o755),
            (ctx.cible_home / ".local/bin", 0o755),
            (ctx.cible_home / ".local/share", 0o755),
            (ctx.cible_home / ".local/share/ameesh", 0o755),
            (ctx.cible_home / ".local/state", 0o755),
            (ctx.cible_home / ".local/state/ameesh", 0o755),
            (ctx.cible_home / ".ssh", 0o700),
            (ctx.cible_home / "development", 0o755),
            (ctx.cible_home / "development/manaty", 0o755),
            (ctx.cible_home / "canon", 0o755),
    ):
        chemin.mkdir(parents=True, exist_ok=True)
        os.chmod(chemin, mode)
        if ctx.hote.comptes is None:
            os.chown(chemin, compte.uid, compte.gid)


def preparer_linger(ctx: Contexte) -> None:
    fait = ctx.hote.lancer(["loginctl", "enable-linger", CIBLE], delai=60)
    if not fait.ok:
        refuser("loginctl enable-linger %s a échoué : %s" % (CIBLE, fait.erreur.strip()))
    montrer = ctx.hote.lancer(["loginctl", "show-user", CIBLE, "--property=Linger"],
                              delai=30)
    if not montrer.ok or "Linger=yes" not in montrer.sortie:
        refuser("linger non posé pour %s : %s" % (CIBLE, montrer.sortie.strip()))
    compte = exiger_cible(ctx)
    bus = Path("/run/user/%d/bus" % compte.uid)
    for _ in range(30):
        if bus.exists() or ctx.hote.comptes is not None:
            break
        time.sleep(1)
    else:
        refuser("gestionnaire systemd utilisateur absent : %s ; vérifier "
                "systemd-logind puis relancer" % bus)


def preparer_config(ctx: Contexte) -> None:
    """Copie config, env, profils, clés et le trousseau de clés CI. Les
    secrets root de `bascule.env` et `notify.env` restent hors du compte."""
    source = ctx.source_home / ".config/ameesh"
    cible = ctx.cible_home / ".config/ameesh"
    cible.mkdir(parents=True, exist_ok=True)
    os.chmod(cible, 0o700)
    chown_cible(ctx, cible)
    copier_config_json(ctx, source / "config.json", cible / "config.json")
    # un config.json valide, avec les chemins du nouveau home
    lire_json(cible / "config.json")
    copier_fichier(ctx, source / "env", cible / "env", mode=0o600)
    copier_fichier(ctx, ctx.source_home / ".pgpass",
                   ctx.cible_home / ".pgpass", mode=0o600)
    # préférences git (aliases, safe.directory) ; `gh auth setup-git`
    # réécrira l'assistant d'identification plus tard, avec le chemin cible.
    copier_fichier(ctx, ctx.source_home / ".gitconfig",
                   ctx.cible_home / ".gitconfig", mode=0o600)
    for dossier in ("runner-profiles", "keys", "cles-ci"):
        arbre = source / dossier
        if not arbre.is_dir():
            continue
        destination = cible / dossier
        destination.mkdir(parents=True, exist_ok=True)
        os.chmod(destination, 0o700)
        chown_cible(ctx, destination)
        for chemin in sorted(arbre.rglob("*")):
            if chemin.is_dir():
                continue
            relatif = chemin.relative_to(arbre)
            copier_fichier(ctx, chemin, destination / relatif, mode=0o600)
    chown_cible(ctx, cible, recursif=True)
    # le fichier de compatibilité #3540 ne sert plus ; ne pas le copier
    with contextlib.suppress(FileNotFoundError):
        (cible / "git-agents.conf").unlink()


def preparer_etat(ctx: Contexte) -> None:
    """État XDG utile : sessions, worktrees suivis, radar/trains, journaux de
    mise à jour. Les caches régénérables et les 13 Gio de `events.jsonl` ne
    traversent pas : ils ne servent pas à reprendre un tour."""
    source = ctx.source_home / ".local/state/ameesh"
    cible = ctx.cible_home / ".local/state/ameesh"
    cible.mkdir(parents=True, exist_ok=True)
    chown_cible(ctx, cible)
    rsync(ctx, source, cible, supprimer=True, chown=CIBLE, exclusions=[
        ".menage/", "github/bin/",
        "events.jsonl", "events.jsonl.*", "*/events.jsonl", "*/events.jsonl.*",
        "*.lock", "*/attach-request.lock", "*/session-use.lock",
        "__pycache__/",
    ])
    autre = ctx.source_home / ".local/state/ameesh-host"
    if autre.is_dir():
        destination = ctx.cible_home / ".local/state/ameesh-host"
        destination.mkdir(parents=True, exist_ok=True)
        chown_cible(ctx, destination)
        rsync(ctx, autre, destination, supprimer=True, chown=CIBLE)
    for petit in ("agent-mail", "claude", "gh"):
        chemin = ctx.source_home / ".local/state" / petit
        if chemin.is_dir():
            destination = ctx.cible_home / ".local/state" / petit
            destination.mkdir(parents=True, exist_ok=True)
            chown_cible(ctx, destination)
            rsync(ctx, chemin, destination, supprimer=True, chown=CIBLE)
    reecrire_marqueurs(ctx, cible)


def reecrire_marqueurs(ctx: Contexte, racine: Path) -> None:
    """Les marqueurs `worktree.json` portent le common dir absolu du home
    source ; on les traduit pour que l'adoption d'un dossier déplacé reste
    possible."""
    for marqueur in racine.glob("*/worktree.json"):
        try:
            donnees = json.loads(marqueur.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        change = _reecrire_valeurs(donnees, ctx.source_home, ctx.cible_home)
        with contextlib.suppress(OSError):
            chemin = marqueur
            tmp = chemin.with_suffix(".tmp.%d" % os.getpid())
            tmp.write_text(json.dumps(change, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.chmod(tmp, 0o600)
            if ctx.hote.comptes is None:
                compte = ctx.changer_cible()
                os.chown(tmp, compte.uid, compte.gid)
            os.replace(tmp, chemin)


def _reecrire_valeurs(valeur, source: Path, cible: Path):
    if isinstance(valeur, str):
        return valeur.replace(str(source), str(cible))
    if isinstance(valeur, list):
        return [_reecrire_valeurs(v, source, cible) for v in valeur]
    if isinstance(valeur, dict):
        return {k: _reecrire_valeurs(v, source, cible) for k, v in valeur.items()}
    return valeur


def _reecrire_chemins(valeur, source: Path, cible: Path):
    """Comme `_reecrire_valeurs`, mais seulement les chaînes qui SONT un
    chemin sous le home source : un mot de passe de DSN qui contiendrait la
    chaîne `/home/smichea` ne doit pas être réécrit."""
    if isinstance(valeur, str):
        return str(cible) + valeur[len(str(source)):] \
            if valeur.startswith(str(source)) else valeur
    if isinstance(valeur, list):
        return [_reecrire_chemins(v, source, cible) for v in valeur]
    if isinstance(valeur, dict):
        return {k: _reecrire_chemins(v, source, cible) for k, v in valeur.items()}
    return valeur


def copier_config_json(ctx: Contexte, source: Path, cible: Path) -> None:
    donnees = lire_json(source)
    donnees = _reecrire_chemins(donnees, ctx.source_home, ctx.cible_home)
    ecrire_fichier(ctx, cible, json.dumps(donnees, ensure_ascii=False,
                                          indent=2) + "\n", mode=0o600)


def rsync(ctx: Contexte, source: Path, cible: Path, *, supprimer: bool = False,
          chown: str | None = None, exclusions: Sequence[str] = (),
          delai: float = 7200) -> None:
    if not source.exists():
        return
    if shutil.which("rsync") is None:
        refuser("rsync absent : l'installer (pacman -S rsync) avant de préparer")
    cible.mkdir(parents=True, exist_ok=True)
    commande = ["rsync", "-aH", "--numeric-ids", "--info=progress2",
                "--exclude=.git/index.lock"]
    if supprimer:
        commande.append("--delete")
    if chown:
        commande.append("--chown=%s:%s" % (chown, chown))
    for motif in exclusions:
        commande.append("--exclude=%s" % motif)
    commande.extend([str(source).rstrip("/") + "/", str(cible).rstrip("/") + "/"])
    fait = ctx.hote.lancer(commande, delai=delai)
    if not fait.ok:
        refuser("rsync %s -> %s a échoué (%s) : %s"
                % (source, cible, fait.code, fait.erreur.strip()))


def preparer_canon(ctx: Contexte) -> None:
    source = ctx.source_home / "canon"
    if not source.is_dir():
        refuser("canon absent : %s" % source)
    cible = ctx.cible_home / "canon"
    rsync(ctx, source, cible, supprimer=True, chown=CIBLE,
          exclusions=["__pycache__/"])
    chown_cible(ctx, cible)
    tete = ctx.hote.lancer(["git", "-C", str(cible), "rev-parse", "HEAD"],
                           utilisateur=CIBLE, delai=60)
    if not tete.ok:
        refuser("canon copié illisible par %s : %s" % (CIBLE, tete.erreur.strip()))
    avant = ctx.hote.lancer(["git", "-C", str(source), "rev-parse", "HEAD"],
                            utilisateur=SOURCE, delai=60)
    if avant.ok and avant.sortie.strip() != tete.sortie.strip():
        refuser("canon copié sur %s, source sur %s"
                % (tete.sortie.strip(), avant.sortie.strip()))


def preparer_repos(ctx: Contexte, *, final: bool = False) -> None:
    source = ctx.source_home / "development/manaty"
    if not source.is_dir():
        refuser("clones absents : %s" % source)
    cible = ctx.cible_home / "development/manaty"
    # Ne jamais exclure un nom que ces dépôts suivent (Nexlink suit
    # `desktop/build/`, `mobile/.next/` et un `__pycache__/` de test) : le
    # garde `verifier_exclusions_trackees` refuse si un fichier suivi tombe
    # sous une exclusion. On ne retire donc que des sorties franchement
    # régénérables.
    exclusions = [
        "node_modules/", "*/node_modules/", "**/node_modules/",
        "target/", "dist/", ".venv/", "venv/",
        ".pytest_cache/", ".mypy_cache/", ".git/index.lock",
    ]
    rsync(ctx, source, cible, supprimer=True, chown=CIBLE, exclusions=exclusions)
    chown_cible(ctx, cible)
    verifier_exclusions_trackees(ctx, cible)
    reecrire_git(ctx, cible)
    reparer_worktrees(ctx, cible)
    for agent in agents_du_poste(ctx):
        dossier = cible / agent["name"]
        if dossier.is_dir():
            continue
        # un agent peut travailler dans un clone au nom du projet + agent
        candidat = cible / ("nexlink-" + agent["name"])
        if not candidat.is_dir() and not final:
            dire("  note : dossier de travail de %s introuvable pour l'instant"
                 % agent["name"])


def verifier_exclusions_trackees(ctx: Contexte, racine: Path) -> None:
    """Une exclusion ne doit jamais retirer un fichier **suivi** du dépôt.
    `build/`, `target/`… sont des sorties de build dans les dépôts du mesh ;
    si un dépôt les suit, la copie serait incomplète : on refuse."""
    composants = {"node_modules", "dist", "target", ".venv", "venv",
                  ".pytest_cache", ".mypy_cache"}
    for dossier, sous, _fichiers in os.walk(racine):
        sous[:] = [d for d in sous if d not in (
            "node_modules", "target", "dist", "build", ".next", "__pycache__")]
        if not Path(dossier, ".git").is_dir():
            continue
        fait = ctx.hote.lancer(["git", "-C", dossier, "ls-files", "-z"],
                               utilisateur=CIBLE, delai=300)
        if not fait.ok:
            refuser("git ls-files illisible dans %s : %s"
                    % (dossier, fait.erreur.strip()[-300:]))
        for chemin in fait.sortie.split("\0"):
            if chemin and composants & set(chemin.split("/")):
                refuser("fichier suivi exclu par la copie : %s/%s"
                        % (dossier, chemin))


def reecrire_git(ctx: Contexte, racine: Path) -> None:
    """Traduit le home source dans les fichiers `.git` des worktrees liés."""
    vieux = str(ctx.source_home).encode()
    neuf = str(ctx.cible_home).encode()
    for dossier, sous, fichiers in os.walk(racine):
        sous[:] = [d for d in sous if d not in ("node_modules", "target", "dist")]
        if ".git" in fichiers:
            chemin = Path(dossier) / ".git"
            with contextlib.suppress(OSError):
                if b"gitdir:" in chemin.read_bytes():
                    donnees = chemin.read_bytes().replace(vieux, neuf)
                    chemin.write_bytes(donnees)
        if dossier.endswith("/.git/worktrees") or "/.git/worktrees/" in dossier:
            for nom in fichiers:
                if nom == "gitdir":
                    chemin = Path(dossier) / nom
                    with contextlib.suppress(OSError):
                        chemin.write_bytes(chemin.read_bytes().replace(vieux, neuf))


def reparer_worktrees(ctx: Contexte, racine: Path) -> None:
    """Répare les liens des worktrees copiés, sans jamais forcer."""
    depots = []
    for dossier, sous, _fichiers in os.walk(racine):
        sous[:] = [d for d in sous if d not in ("node_modules", "target", "dist")]
        if Path(dossier, ".git").is_dir():
            depots.append(Path(dossier))
    for depot in depots:
        fait = ctx.hote.lancer(["git", "-C", str(depot), "worktree", "repair"],
                               utilisateur=CIBLE, delai=120)
        if not fait.ok:
            journal(ctx, "preparer", "worktree repair non concluant : %s" % depot,
                    code=fait.code, erreur=sans_secret(fait.erreur.strip())[-300:])


def preparer_venv(ctx: Contexte) -> None:
    """Reconstruit le venv pour le nouveau home : jamais copier un venv (ses
    shebangs portent le chemin du home source). Le snapshot installé est
    copié, puis `bascule.py` prépare le venv versionné et l'active."""
    share_source = ctx.source_home / ".local/share/ameesh"
    courant = share_source / "src-current"
    if not courant.exists():
        refuser("instantané installé introuvable : %s" % courant)
    nom = os.path.basename(os.path.realpath(courant))
    if not re.fullmatch(r"src-[0-9a-f]{7,40}", nom):
        refuser("nom d'instantané inattendu : %s" % nom)
    source = share_source / nom
    if not source.is_dir():
        refuser("instantané installé absent : %s" % source)
    cible = ctx.cible_home / ".local/share/ameesh"
    destination = cible / nom
    rsync(ctx, source, destination, supprimer=True, chown=CIBLE,
          exclusions=["__pycache__/", "*.egg-info/", ".pytest_cache/"])
    chown_cible(ctx, cible)
    chown_cible(ctx, destination)
    venv = cible / "venv"
    if not (venv / "bin/python").exists():
        dire("  création du venv stable %s" % venv)
        fait = ctx.hote.lancer(["python3", "-m", "venv", str(venv)],
                               utilisateur=CIBLE, delai=300)
        if not fait.ok:
            refuser("python3 -m venv a échoué (%s) : installer python avant "
                    "(pacman -S python)" % fait.erreur.strip())
    bascule = destination / "deploy/mise-a-jour/bascule.py"
    if not bascule.is_file():
        refuser("bascule.py absent de l'instantané : %s" % bascule)
    version = nom[len("src-"):]
    for action in ("preparer", "activer"):
        commande = ["python3", str(bascule), action, "--venv", str(venv),
                    "--src", str(destination), "--version", version]
        journal(ctx, "preparer", "bascule.py %s du venv %s" % (action, version))
        fait = ctx.hote.lancer(commande, utilisateur=CIBLE, delai=1800)
        if not fait.ok:
            refuser("bascule.py %s a échoué : %s"
                    % (action, sans_secret((fait.sortie + fait.erreur).strip())[-1200:]))
    lien = cible / "src-current"
    with contextlib.suppress(FileNotFoundError):
        lien.unlink()
    os.symlink(nom, lien)
    if ctx.hote.comptes is None:
        compte = exiger_cible(ctx)
        os.chown(lien, compte.uid, compte.gid, follow_symlinks=False)
    for outil in ("ameesh", "agent-mail", "agent-runner"):
        script = venv / "bin" / outil
        if not script.exists():
            continue
        raccourci = ctx.cible_home / ".local/bin" / outil
        raccourci.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            raccourci.unlink()
        os.symlink(str(script), raccourci)
        if ctx.hote.comptes is None:
            compte = exiger_cible(ctx)
            os.chown(raccourci, compte.uid, compte.gid, follow_symlinks=False)
    verifier = ctx.hote.lancer([str(venv / "bin/ameesh"), "--version"],
                               utilisateur=CIBLE, delai=60)
    if not verifier.ok:
        refuser("ameesh du nouveau venv ne répond pas : %s" % verifier.erreur.strip())
    dire("  venv prêt : %s" % verifier.sortie.strip())


def preparer_unites(ctx: Contexte) -> None:
    source = ctx.source_home / ".config/systemd/user"
    cible = ctx.cible_home / ".config/systemd/user"
    cible.mkdir(parents=True, exist_ok=True)
    os.chmod(cible, 0o755)
    chown_cible(ctx, cible)
    fichiers = []
    for nom in (SERVICE, SLICE):
        if not (source / nom).is_file():
            refuser("unité source absente : %s" % (source / nom))
        fichiers.append((source / nom, cible / nom))
    for chemin in sorted(source.glob(PREFIXE_UNITE + "*.service")):
        if chemin.name == SERVICE:
            continue
        fichiers.append((chemin, cible / chemin.name))
    for dropin in sorted(source.glob(PREFIXE_UNITE + "*.service.d/*.conf")):
        if dropin.name in OBSOLETES:
            continue
        fichiers.append((dropin, cible / dropin.parent.name / dropin.name))
    for dropin in sorted(source.glob(PREFIXE_UNITE + ".service.d/*.conf")):
        if dropin.name in OBSOLETES:
            continue
        fichiers.append((dropin, cible / dropin.parent.name / dropin.name))
    for ancien, nouveau in fichiers:
        copier_fichier(ctx, ancien, nouveau, mode=0o644)
    chown_cible(ctx, cible, recursif=True)
    modele = cible / SERVICE
    if modele.is_file():
        texte = modele.read_text(encoding="utf-8", errors="replace")
        if "UnsetEnvironment=SSH_AUTH_SOCK" not in texte:
            refuser("le gabarit d'unité cible ne retire pas SSH_AUTH_SOCK : "
                    "réinstaller le gabarit #3444 avant la bascule")
    # jamais les pièces #3444 / #3540 dans le home cible
    for interdit in (cible / (PREFIXE_UNITE + ".service.d/cles-owner.conf"),
                     cible / "git-agents.conf",
                     ctx.cible_home / ".config/ameesh/git-agents.conf"):
        with contextlib.suppress(FileNotFoundError):
            interdit.unlink()
            dire("  retiré du home cible : %s" % interdit)
    fait = systemctl_cible(ctx, "daemon-reload", delai=60)
    if not fait.ok:
        refuser("daemon-reload (cible) a échoué : %s" % fait.erreur.strip())
    # Ne PAS activer au boot ici : tant que la source est active, une
    # activation cible ferait deux runners après un redémarrage. L'activation
    # cible et la désactivation source sont dans la même section critique de
    # `basculer`, une fois la source arrêtée.
    dire("  unités installées (%d fichiers), aucune activée : la bascule fera "
         "le basculement enabled" % len(fichiers))


def preparer(ctx: Contexte, etat: dict) -> None:
    creer_compte(ctx)
    preparer_dossiers(ctx)
    preparer_linger(ctx)
    preparer_config(ctx)
    preparer_etat(ctx)
    preparer_canon(ctx)
    preparer_repos(ctx)
    preparer_venv(ctx)
    preparer_unites(ctx)


def commande_preparer(ctx: Contexte, args) -> int:
    exiger_racine(ctx)
    with verrou(ctx, "preparer"):
        exiger_source(ctx)
        exiger_hote(ctx)
        etat = lit_etat(ctx)
        if etat.get("stage") in ("bascule", "preuve"):
            refuser("migration déjà basculée : « retour » d'abord, ou --reprendre "
                    "explicitement")
        etat["stage"] = "prepare"
        etat["agents"] = agents_du_poste(ctx)
        journal(ctx, "preparer", "préparation du compte %s et de son home" % CIBLE)
        preparer(ctx, etat)
        etat["stage"] = "prepare"
        ecrit_etat(ctx, etat)
        dire("")
        dire("préparation terminée. Étape suivante : identites (connexions web "
             "Claude de l'owner regroupées en un seul geste).")
    return 0


# --------------------------------------------------------------------------
# identites
# --------------------------------------------------------------------------


def executer_sous_cible(ctx: Contexte, argv: Sequence[str], *,
                        delai: float = 120) -> Resultat:
    return ctx.hote.lancer(argv, utilisateur=CIBLE, delai=delai)


def executer_sous_source(ctx: Contexte, argv: Sequence[str], *,
                         delai: float = 120) -> Resultat:
    return ctx.hote.lancer(argv, utilisateur=SOURCE, delai=delai)


def _version_outil(ctx: Contexte, outil: str, *, cible: bool) -> str:
    fait = (executer_sous_cible if cible else executer_sous_source)(
        ctx, [outil, "--version"], delai=60)
    if not fait.ok:
        return ""
    lignes = (fait.sortie or fait.erreur).strip().splitlines()
    return lignes[0].strip() if lignes else ""


def cle_ssh_source(ctx: Contexte) -> tuple[bool, str]:
    """La clé dédiée de ameesh, générée une fois, jamais copiée de l'owner."""
    dossier = ctx.cible_home / ".ssh"
    cle = dossier / "id_ed25519"
    if not cle.exists():
        fait = executer_sous_cible(ctx, ["ssh-keygen", "-q", "-t", "ed25519",
                                         "-N", "", "-C",
                                         "ameesh@pc-smichea runners (#3549)",
                                         "-f", str(cle)], delai=60)
        if not fait.ok:
            return False, "ssh-keygen a échoué : %s" % fait.erreur.strip()
    os.chmod(cle, 0o600)
    pub = cle.with_suffix(".pub")
    if not pub.exists():
        fait = executer_sous_cible(ctx, ["ssh-keygen", "-y", "-f", str(cle)],
                                   delai=30)
        if not fait.ok:
            return False, "lecture de la clé publique impossible"
        ecrire_fichier(ctx, pub, fait.sortie, mode=0o644)
    empreinte = executer_sous_cible(ctx, ["ssh-keygen", "-lf", str(pub)], delai=30)
    return True, empreinte.sortie.strip() if empreinte.ok else "empreinte illisible"


def config_ssh_cible(ctx: Contexte) -> None:
    dossier = ctx.cible_home / ".ssh"
    dossier.mkdir(parents=True, exist_ok=True)
    os.chmod(dossier, 0o700)
    config = dossier / "config"
    if not config.exists():
        ecrire_fichier(ctx, config, (
            "Host github.com\n"
            "  HostName github.com\n"
            "  User git\n"
            "  IdentityFile ~/.ssh/id_ed25519\n"
            "  IdentitiesOnly yes\n"), mode=0o600)
    known = dossier / "known_hosts"
    source_known = ctx.source_home / ".ssh/known_hosts"
    if not known.exists() and source_known.is_file():
        copier_fichier(ctx, source_known, known, mode=0o644, reecrire=False)
    # GitHub : clé publiée et épinglée, jamais un keyscan cru. On retire une
    # éventuelle ligne github.com copiée de la source, puis on la remplace par
    # la clé publiée ; l'empreinte est revérifiée par ssh-keygen.
    contenu = ctx.hote.lire(known, defaut="") or ""
    lignes = [l for l in contenu.splitlines()
              if not l.startswith("github.com ")]
    lignes.append(CLE_GITHUB)
    nouveau = "\n".join(lignes) + "\n"
    ecrire_fichier(ctx, known, nouveau, mode=0o644)
    tmp = ctx.etat_dir / "github.hostkey"
    ecrire_fichier(ctx, tmp, CLE_GITHUB + "\n", mode=0o600)
    empreinte = ctx.hote.lancer(["ssh-keygen", "-lf", str(tmp)], delai=30)
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    if not empreinte.ok or EMPREINTE_GITHUB not in empreinte.sortie:
        refuser("empreinte GitHub inattendue : %s" % empreinte.sortie.strip())


def verifier_ssh_github(ctx: Contexte) -> tuple[bool, str]:
    fait = executer_sous_cible(ctx, [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-T", "git@github.com"], delai=60)
    texte = fait.sortie + fait.erreur
    if "successfully authenticated" in texte:
        return True, texte.strip().splitlines()[0]
    return False, "ssh GitHub : %s" % texte.strip()[-300:]


def preparer_gh(ctx: Contexte, config: dict, *, creer: bool = True) -> list[str]:
    manques = []
    dossier = ctx.cible_home / ".config/gh"
    if creer:
        dossier.mkdir(parents=True, exist_ok=True)
        os.chmod(dossier, 0o700)
    source = ctx.source_home / ".config/gh/hosts.yml"
    cible = dossier / "hosts.yml"
    if creer and not cible.exists() and source.is_file():
        copier_fichier(ctx, source, cible, mode=0o600, reecrire=False)
    if not cible.exists():
        manques.append("gh : aucun hosts.yml — faire `gh auth login` sous %s" % CIBLE)
        return manques
    status = executer_sous_cible(ctx, ["gh", "auth", "status"], delai=60)
    if not status.ok:
        manques.append("gh auth status : %s" % (status.sortie + status.erreur).strip()[-300:])
        return manques
    # l'assistant d'identification, avec le chemin réel de gh du compte
    if creer:
        fait = executer_sous_cible(ctx, ["gh", "auth", "setup-git"], delai=60)
        if not fait.ok:
            manques.append("gh auth setup-git : %s" % fait.erreur.strip()[-300:])
    return manques


def preparer_codex(ctx: Contexte, config: dict, *, creer: bool = True) -> list[str]:
    manques = []
    comptes = comptes_declares(config).get("codex", [])
    if not comptes and any(a["harness"] == "codex" for a in agents_du_poste(ctx)):
        comptes = [{"name": "primaire"}]
    for compte in comptes:
        nom = compte.get("name") or "?"
        dossier = dossier_config_dir(ctx, compte, "codex")
        if creer:
            dossier.mkdir(parents=True, exist_ok=True)
            os.chmod(dossier, 0o700)
        source_dossier = chemin_cible(ctx, compte.get("path"), ".codex")
        source_dossier = Path(str(source_dossier).replace(str(ctx.cible_home),
                                                          str(ctx.source_home)))
        for fichier in ("auth.json", "config.toml"):
            source = source_dossier / fichier
            if creer and source.is_file() and not (dossier / fichier).exists():
                copier_fichier(ctx, source, dossier / fichier, mode=0o600,
                               reecrire=fichier == "config.toml")
        if not (dossier / "auth.json").is_file():
            manques.append("codex %s : auth.json absent — copie contrôlée ou "
                           "`CODEX_HOME=%s codex login`" % (nom, dossier))
    return manques


def preparer_deepseek(ctx: Contexte, config: dict) -> list[str]:
    manques = []
    comptes = comptes_declares(config).get("deepseek", [])
    if not comptes and any(a["harness"] == "deepseek" for a in agents_du_poste(ctx)):
        comptes = [{"name": "primaire", "type": "api_key_env",
                    "key_env": "DEEPSEEK_API_KEY"}]
    for compte in comptes:
        nom = compte.get("name") or "?"
        type_compte = compte.get("type") or "api_key_env"
        if type_compte == "api_key_env":
            variable = compte.get("key_env") or "DEEPSEEK_API_KEY"
            if not _variable_presente(ctx, variable):
                manques.append("deepseek %s : variable %s absente du nouvel "
                               "environnement" % (nom, variable))
        fichier = compte.get("key_file")
        if fichier:
            cible = chemin_cible(ctx, fichier, ".config/ameesh/keys/deepseek")
            if not cible.is_file():
                manques.append("deepseek %s : fichier de clé absent (%s)" % (nom, cible))
    return manques


def _variable_presente(ctx: Contexte, variable: str) -> bool:
    env = ctx.hote.lire(ctx.cible_home / ".config/ameesh/env", defaut="") or ""
    return any(ligne.startswith(variable + "=") and ligne.split("=", 1)[1].strip()
               for ligne in env.splitlines())


def commandes_claude(ctx: Contexte, config: dict, *,
                     creer: bool = True) -> tuple[list[str], list[str]]:
    """Les logins web Claude à faire par l'owner, un par compte déclaré."""
    commandes, manques = [], []
    comptes = comptes_declares(config).get("claude", [])
    if not comptes:
        comptes = [{"name": "primaire"}]
    for compte in comptes:
        nom = compte.get("name") or "?"
        dossier = dossier_config_dir(ctx, compte, "claude")
        if creer:
            dossier.mkdir(parents=True, exist_ok=True)
            os.chmod(dossier, 0o700)
        # `claude auth status` lit CLAUDE_CONFIG_DIR : le passer explicitement
        fait = executer_sous_cible(ctx, [
            "env", "CLAUDE_CONFIG_DIR=%s" % dossier, "claude", "auth", "status"],
            delai=60) if _binaire_cible(ctx, "claude") else None
        connecte = False
        if fait is not None and fait.ok:
            try:
                connecte = json.loads(fait.sortie).get("loggedIn") is True
            except (ValueError, AttributeError):
                connecte = '"loggedIn": true' in (fait.sortie + fait.erreur)
        if connecte:
            continue
        commandes.append("sudo -u %s -H env CLAUDE_CONFIG_DIR=%s claude auth login"
                         % (CIBLE, dossier))
        manques.append("claude %s : connexion web de l'owner requise (%s)" % (nom, dossier))
    return commandes, manques


def _binaire_cible(ctx: Contexte, nom: str) -> bool:
    fait = executer_sous_cible(ctx, ["sh", "-c", "command -v %s" % nom], delai=30)
    return fait.ok and fait.sortie.strip() != ""


def verifier_outils(ctx: Contexte) -> list[str]:
    manques = []
    for nom in ("claude", "codex", "gh", "git", "ssh", "python3"):
        if not _binaire_cible(ctx, nom):
            manques.append("%s introuvable dans le PATH de %s (poser le même "
                           "binaire que sur %s)" % (nom, CIBLE, SOURCE))
    env = ctx.hote.lire(ctx.cible_home / ".config/ameesh/env", defaut="") or ""
    if not any(l.startswith("AMEESH_DSH_BIN=") for l in env.splitlines()):
        if not _binaire_cible(ctx, "dsh"):
            manques.append("dsh introuvable et AMEESH_DSH_BIN absent du nouvel env")
    return manques


def commande_identites(ctx: Contexte, args) -> int:
    exiger_racine(ctx)
    with verrou(ctx, "identites"):
        exiger_source(ctx)
        exiger_hote(ctx)
        exiger_cible(ctx)
        config = lire_json(ctx.cible_home / ".config/ameesh/config.json")
        journal(ctx, "identites", "clé SSH dédiée et vérification des harnais")
        ok, detail = cle_ssh_source(ctx)
        dire("  clé SSH ameesh : %s" % detail)
        if not ok:
            return 1
        config_ssh_cible(ctx)
        manques = []
        manques += verifier_outils(ctx)
        manques += preparer_gh(ctx, config)
        manques += preparer_codex(ctx, config)
        manques += preparer_deepseek(ctx, config)
        commandes, claude_manques = commandes_claude(ctx, config)
        manques += claude_manques
        if commandes:
            dire("")
            dire("geste owner, en une seule séance (connexions web Claude) :")
            for commande in commandes:
                dire("  %s" % commande)
            dire("puis relancer : %s identites" % sys.argv[0])
        if manques:
            dire("")
            dire("identités incomplètes :")
            for manque in manques:
                dire("  - %s" % manque)
            return 1
        ok, detail = verifier_ssh_github(ctx)
        dire("  ssh GitHub : %s" % detail)
        if not ok:
            dire("  poser la clé publique sur GitHub (une seule fois) :")
            pub = ctx.hote.lire(ctx.cible_home / ".ssh/id_ed25519.pub", defaut="")
            dire("    gh ssh-key add %s --title 'pc-smichea ameesh runners'"
                 % (pub or "<clé publique>").strip())
            return 1
        dire("identités prêtes.")
    return 0


# --------------------------------------------------------------------------
# basculer
# --------------------------------------------------------------------------


def attendre_unites(ctx: Contexte, noms: Sequence[str], *, cible: bool,
                    delai: float, arret: bool = False) -> dict[str, dict]:
    """Attend l'état voulu, sans jamais décider sur une seule lecture."""
    limite = time.time() + delai
    restants = set(noms)
    etats: dict[str, dict] = {}
    while restants and time.time() < limite:
        for nom in sorted(restants):
            etat = statut_unite(ctx, nom, cible=cible)
            etats[nom] = etat
            bon = (etat["actif"] == "inactive" and etat["pid"] == "0") if arret \
                else (etat["actif"] == "active" and etat["pid"] not in ("", "0"))
            if bon:
                restants.discard(nom)
        if restants:
            time.sleep(5)
    for nom in restants:
        etats[nom] = statut_unite(ctx, nom, cible=cible)
    return etats


def poser_pauses(ctx: Contexte, agents: Sequence[dict], motif: str) -> dict[str, str]:
    posees = {}
    for agent in agents:
        nom = agent["name"]
        fait = ameesh_source(ctx, "pause", nom, "--reason", motif,
                             "--human", "human:smichea",
                             "--not-after", str(int(time.time()) + 7200), "--json")
        if not fait.ok:
            refuser("pause de %s refusée : %s" % (nom, (fait.sortie + fait.erreur).strip()))
        try:
            reponse = json.loads(fait.sortie)
        except ValueError:
            refuser("réponse de pause illisible pour %s" % nom)
        if reponse.get("changed"):
            posees[nom] = motif
            dire("  %s : admission fermée (pause opérateur)" % nom)
        else:
            raison = reponse.get("reason") or reponse.get("operator_pause_reason") or ""
            if raison == motif:
                posees[nom] = motif
            else:
                dire("  %s : pause étrangère conservée (%s)" % (nom, raison or "?"))
    return posees


def lever_pauses(ctx: Contexte, pauses: Mapping[str, str]) -> list[str]:
    non_levees = []
    for nom, motif in sorted(pauses.items()):
        fait = ameesh_cible(ctx, "resume", nom, "--operator",
                            "--reason", "migration #3549 terminée",
                            "--expect-reason", motif, "--human", "human:smichea")
        if not fait.ok and not any(x in (fait.sortie + fait.erreur) for x in
                                   ("no operator pause", "aucune pause")):
            # le CLI cible peut être en panne (unité arrêtée, venv en cours de
            # réparation) : la pause vit en base, le CLI source la lève aussi.
            secours = ameesh_source(ctx, "resume", nom, "--operator",
                                    "--reason", "migration #3549 terminée",
                                    "--expect-reason", motif,
                                    "--human", "human:smichea")
            if not secours.ok and not any(
                    x in (secours.sortie + secours.erreur) for x in
                    ("no operator pause", "aucune pause")):
                non_levees.append(nom)
                dire("  %s : pause non levée — %s"
                     % (nom, (fait.sortie + fait.erreur + secours.sortie
                              + secours.erreur).strip()[-300:]))
    return non_levees


def attendre_bail(ctx: Contexte, nom: str, *, delai: float = 180) -> tuple[bool, dict]:
    limite = time.time() + delai
    dernier: dict = {}
    while time.time() < limite:
        fait = ameesh_cible(ctx, "show", nom, "--json", delai=60)
        if fait.ok:
            try:
                dernier = json.loads(fait.sortie)
            except ValueError:
                dernier = {}
            if (dernier.get("lease_owner") and
                    float(dernier.get("lease_expires_ts") or 0) > time.time()):
                return True, dernier
        time.sleep(5)
    return False, dernier


def demarrer_canari(ctx: Contexte, nom: str) -> tuple[bool, str]:
    unite = "%s%s.service" % (PREFIXE_UNITE, nom)
    fait = systemctl_cible(ctx, "start", unite, delai=120)
    if not fait.ok:
        return False, "start %s : %s" % (unite, fait.erreur.strip())
    etats = attendre_unites(ctx, [nom], cible=True, delai=120)
    if etats.get(nom, {}).get("actif") != "active":
        return False, "unité %s non active après start" % unite
    ok, agent = attendre_bail(ctx, nom)
    if not ok:
        return False, "aucun bail vivant pour %s après démarrage (%s)" % (
            nom, agent.get("status") or "?")
    return True, "canari %s actif, bail %s" % (nom, agent.get("lease_owner"))


def test_courrier(ctx: Contexte, nom: str, marqueur: str,
                  delai: float = 420) -> tuple[bool, str]:
    avant = None
    fait = ameesh_cible(ctx, "show", nom, "--json", delai=60)
    if fait.ok:
        with contextlib.suppress(ValueError):
            avant = json.loads(fait.sortie).get("last_turn_ts")
    envoi = ameesh_cible(ctx, "mail", "send", nom, marqueur, "--from", "bureau",
                         "--urgent", delai=60)
    if not envoi.ok:
        return False, "envoi du courrier test refusé : %s" % (
            envoi.sortie + envoi.erreur).strip()[-300:]
    limite = time.time() + delai
    while time.time() < limite:
        fait = ameesh_cible(ctx, "show", nom, "--json", delai=60)
        if fait.ok:
            with contextlib.suppress(ValueError):
                donnees = json.loads(fait.sortie)
                if (float(donnees.get("last_turn_ts") or 0) > float(avant or 0)
                        and donnees.get("status") != "running"):
                    return True, "courrier test livré et tour terminé (%s)" % nom
        time.sleep(10)
    return False, "courrier test non consommé en %.0f s" % delai


def canon_sync(ctx: Contexte, *, cible: bool) -> tuple[bool, str]:
    outil = ameesh_cible if cible else ameesh_source
    fait = outil(ctx, "canon", "sync", "--host", HOTE, "--canon",
                 str(ctx.cible_home / "canon" if cible else ctx.source_home / "canon"),
                 delai=300)
    return fait.ok, (fait.sortie + fait.erreur).strip()[-500:]


def verifier_cwd(ctx: Contexte, agents: Sequence[dict], *, cible: bool) -> list[str]:
    outil = ameesh_cible if cible else ameesh_source
    home = str(ctx.cible_home if cible else ctx.source_home)
    manques = []
    for agent in agents:
        if agent.get("status") == "stopped":
            continue
        fait = outil(ctx, "show", agent["name"], "--json", delai=60)
        if not fait.ok:
            manques.append("%s : show illisible" % agent["name"])
            continue
        try:
            donnees = json.loads(fait.sortie)
        except ValueError:
            manques.append("%s : JSON illisible" % agent["name"])
            continue
        cwd = str(donnees.get("cwd") or "")
        if not cwd.startswith(home + "/"):
            manques.append("%s : cwd %s hors du home %s" % (agent["name"], cwd or "?",
                                                             home))
        elif not Path(cwd).is_dir():
            manques.append("%s : cwd absent %s" % (agent["name"], cwd))
    return manques


def retirer_pieces_obsoletes(ctx: Contexte, etat: dict) -> None:
    """#3444 et #3540 deviennent sans objet : sauvegarde puis retrait."""
    cibles = [ctx.source_home / ".config/systemd/user" /
              (PREFIXE_UNITE + ".service.d/cles-owner.conf"),
              ctx.source_home / ".config/ameesh/git-agents.conf"]
    sauvegarde = ctx.etat_dir / "sauvegarde"
    for chemin in cibles:
        if not chemin.exists():
            continue
        relatif = chemin.relative_to(ctx.source_home)
        destination = sauvegarde / relatif
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chemin, destination, follow_symlinks=False)
        os.chmod(destination, 0o600)
        etat.setdefault("pieces_retirees", []).append(str(relatif))
        chemin.unlink()
        dire("  retiré (sauvegardé) : %s" % relatif)
    systemctl_source(ctx, "daemon-reload", delai=60)


def annuler_avant_canari(ctx: Contexte, actifs: Sequence[str],
                         pauses: Mapping[str, str], etat: dict) -> None:
    """Restaure la source si l'échec précède tout démarrage cible."""
    dire("")
    dire("échec avant tout démarrage sous %s : remise en service de la source" % CIBLE)
    # remettre l'ensemble enabled d'avant : la source doit redémarrer au
    # boot, la cible non.
    enabled_source = etat.get("enabled_source") or list(actifs)
    if enabled_source:
        with contextlib.suppress(Refus):
            activer_unites(ctx, enabled_source)
    if actifs:
        with contextlib.suppress(Refus):
            desactiver_unites(ctx, actifs, cible=True)
    if actifs:
        systemctl_source(ctx, "start", *["%s%s.service" % (PREFIXE_UNITE, n)
                                        for n in actifs], delai=300)
        attendre_unites(ctx, list(actifs), cible=False, delai=120)
    non_levees = lever_pauses(ctx, pauses)
    if non_levees:
        dire("  pauses NON levées : %s" % ", ".join(non_levees))
    etat["stage"] = "prepare"
    etat["pauses"] = {}
    ecrit_etat(ctx, etat)


def basculer(ctx: Contexte, args) -> int:
    etat = lit_etat(ctx)
    if etat.get("stage") not in ("prepare", "identites"):
        refuser("état %r : lancer « preparer » puis « identites » d'abord"
                % etat.get("stage"))
    manques = verifier_avant_bascule(ctx, inventaire(ctx))
    if manques:
        dire("refus avant bascule :")
        for manque in manques:
            dire("  - %s" % manque)
        return 1
    agents = agents_du_poste(ctx)
    etat["agents"] = agents
    actifs = [a["name"] for a in agents
              if statut_unite(ctx, a["name"])["actif"] == "active"]
    if not actifs:
        refuser("aucune unité active à drainer : état déjà migré ?")
    # tous les agents, pas seulement les actifs : une unité arrêtée mais
    # activée au boot relancerait un runner smichea après le redémarrage.
    enabled_source = etat_enabled(ctx, [a["name"] for a in agents])
    etat["enabled_source"] = enabled_source
    etat["enabled_cible"] = []
    motif = "migration #3549 pc-smichea vers %s (%s)" % (CIBLE, horodatage())
    journal(ctx, "basculer", "drain de %d exécuteurs, motif %r" % (len(actifs), motif),
            agents=actifs)
    pauses = poser_pauses(ctx, [a for a in agents if a["name"] in actifs], motif)
    etat["pauses"] = pauses
    ecrit_etat(ctx, etat)
    try:
        fait = systemctl_source(ctx, "stop",
                                *["%s%s.service" % (PREFIXE_UNITE, n) for n in actifs],
                                delai=300)
        if not fait.ok:
            refuser("arrêt refusé : %s" % fait.erreur.strip())
        etats = attendre_unites(ctx, actifs, cible=False, delai=args.attente_arret,
                                arret=True)
        non_arretes = [n for n, e in etats.items()
                       if not (e["actif"] == "inactive" and e["pid"] == "0")]
        if non_arretes:
            refuser("arrêt non prouvé pour %s (ActiveState/MainPID) : aucun "
                    "démarrage cible" % ", ".join(non_arretes))
        journal(ctx, "basculer", "arrêt prouvé des %d exécuteurs" % len(actifs))
        # la source ne doit plus démarrer au boot : sinon un redémarrage
        # lancerait à la fois les unités de smichea et celles d'ameesh.
        if enabled_source:
            desactiver_unites(ctx, enabled_source)
            journal(ctx, "basculer", "unités source désactivées au boot",
                    agents=enabled_source)
        # copie finale, source quiescente
        preparer_etat(ctx)
        preparer_repos(ctx, final=True)
        ok, detail = canon_sync(ctx, cible=True)
        if not ok:
            refuser("canon sync (cwd) a échoué : %s" % detail)
        manques = verifier_cwd(ctx, [a for a in agents if a["name"] in actifs],
                               cible=True)
        if manques:
            refuser("cwd non migrés : %s" % "; ".join(manques))
        canari = args.canari or ("codex2" if any(a["name"] == "codex2" for a in agents)
                                 else actifs[0])
        if canari not in actifs:
            refuser("canari %s absent des exécuteurs actifs" % canari)
        # à partir d'ici un démarrage cible peut exister : l'état autorise
        # « retour » même si la suite échoue.
        etat["stage"] = "bascule"
        etat["migre_le"] = horodatage()
        etat["actifs"] = actifs
        etat["canari"] = canari
        ecrit_etat(ctx, etat)
        ok, detail = demarrer_canari(ctx, canari)
        if not ok:
            refuser("canari en échec : %s" % detail)
        journal(ctx, "basculer", detail)
        if canari in pauses:
            non_levees = lever_pauses(ctx, {canari: pauses[canari]})
            if non_levees:
                refuser("pause du canari non levée")
            pauses.pop(canari, None)
        marqueur = ("migration #3549 : test de courrier sur %s, aucune action "
                    "attendue ; répondre « reçu » si le tour aboutit." % HOTE)
        ok, detail_courrier = test_courrier(ctx, canari, marqueur)
        if not ok:
            refuser("preuve de courrier du canari en échec : %s" % detail_courrier)
        journal(ctx, "basculer", detail_courrier)
        for nom in actifs:
            if nom == canari:
                continue
            ok, detail = demarrer_canari(ctx, nom)
            if not ok:
                refuser("démarrage de %s en échec : %s" % (nom, detail))
            journal(ctx, "basculer", detail)
        non_levees = lever_pauses(ctx, pauses)
        if non_levees:
            refuser("pauses non levées : %s" % ", ".join(non_levees))
        activer_unites(ctx, actifs, cible=True)
        etat["enabled_cible"] = list(actifs)
        journal(ctx, "basculer", "unités cible activées au boot", agents=actifs)
        retirer_pieces_obsoletes(ctx, etat)
        etat["stage"] = "bascule"
        etat["preuve_courrier"] = detail_courrier
        ecrit_etat(ctx, etat)
    except Refus:
        etats_cible = {n: statut_unite(ctx, n, cible=True)["actif"] for n in actifs}
        if not any(e in ("active", "activating", "reloading")
                   for e in etats_cible.values()):
            annuler_avant_canari(ctx, actifs, pauses, etat)
        else:
            dire("échec après démarrage sous %s : les unités cible restent "
                 "en place et les pauses non levées gardent l'admission fermée ; "
                 "« retour » pour revenir aux unités de %s"
                 % (CIBLE, SOURCE))
        raise
    dire("")
    dire("bascule faite : %d exécuteurs actifs sous %s ; lancer « preuve »."
         % (len(actifs), CIBLE))
    return 0


def verifier_avant_bascule(ctx: Contexte, inv: dict) -> list[str]:
    """Les gardes strictes, toutes lues avant le premier arrêt."""
    manques = []
    source = ctx.source_home
    if inv["host_config"] != HOTE:
        manques.append("hôte configuré %r" % inv["host_config"])
    if not inv["agents"]:
        manques.append("aucun agent execute sur le poste")
    if "active" not in [statut_unite(ctx, a["name"])["actif"] for a in inv["agents"]]:
        manques.append("aucune unité source active")
    if not inv["creneau"][0]:
        manques.append(inv["creneau"][1])
    if not inv["paquets"][0]:
        manques.append(inv["paquets"][1])
    if not inv["drill"][0]:
        manques.append(inv["drill"][1])
    # ~/.ssh de l'owner doit rester privé
    ssh = source / ".ssh"
    if ssh.exists() and (ssh.stat().st_mode & 0o077):
        manques.append("~/.ssh source trop ouvert : %04o" % (ssh.stat().st_mode & 0o777))
    # cible : compte sans groupe privilégié, linger, venv, unités, config
    try:
        compte = ctx.hote.compte(CIBLE)
    except Refus:
        compte = None
    if compte is None:
        manques.append("compte %s absent : preparer d'abord" % CIBLE)
    else:
        interdits = groupes_interdits() & set(compte.groupes)
        if interdits:
            manques.append("%s dans %s : séparation non réelle"
                           % (CIBLE, ", ".join(sorted(interdits))))
        linger = ctx.hote.lancer(["loginctl", "show-user", CIBLE,
                                  "--property=Linger"], delai=30)
        if "Linger=yes" not in linger.sortie:
            manques.append("linger non posé pour %s" % CIBLE)
    # modes : ~/.ssh 0700, clé 0600, config et env 0600
    ssh_cible = ctx.cible_home / ".ssh"
    cle_cible = ssh_cible / "id_ed25519"
    if ssh_cible.is_dir() and (ssh_cible.stat().st_mode & 0o077):
        manques.append("%s trop ouvert : %04o" % (ssh_cible,
                                                  ssh_cible.stat().st_mode & 0o777))
    if cle_cible.is_file() and (cle_cible.stat().st_mode & 0o077):
        manques.append("clé SSH cible trop ouverte : %04o"
                       % (cle_cible.stat().st_mode & 0o777))
    for fichier in (ctx.cible_home / ".config/ameesh/config.json",
                    ctx.cible_home / ".config/ameesh/env"):
        if fichier.is_file() and (fichier.stat().st_mode & 0o077):
            manques.append("%s trop ouvert : %04o"
                           % (fichier, fichier.stat().st_mode & 0o777))
    venv = ctx.cible_home / ".local/share/ameesh/venv/bin/ameesh"
    if not venv.exists():
        manques.append("venv cible absent : %s" % venv)
    else:
        fait = ctx.hote.lancer([str(venv), "--version"], utilisateur=CIBLE, delai=60)
        if not fait.ok:
            manques.append("ameesh cible ne répond pas")
    for agent in inv["agents"]:
        nom = agent["name"]
        unite = "%s%s.service" % (PREFIXE_UNITE, nom)
        etat = statut_unite(ctx, nom, cible=True)
        avant = statut_unite(ctx, nom)
        if etat["charge"] in ("", "not-found"):
            manques.append("unité cible absente : %s" % unite)
        if etat["actif"] in ("active", "activating", "reloading"):
            manques.append("unité cible déjà active : %s (retour d'abord)" % unite)
        if avant["actif"] == "active" and avant["active_file"] != "enabled":
            manques.append("unité source active mais non activée au boot : %s" % unite)
    # identités et outils, sans rien écrire
    manques += verifier_outils(ctx)
    config = lire_json(ctx.cible_home / ".config/ameesh/config.json")
    _, claude_manques = commandes_claude(ctx, config, creer=False)
    manques += claude_manques
    manques += preparer_gh(ctx, config, creer=False)
    manques += preparer_codex(ctx, config, creer=False)
    manques += preparer_deepseek(ctx, config)
    ok, detail = verifier_ssh_github(ctx)
    if not ok:
        manques.append(detail)
    # même version de harnais de part et d'autre : une migration ne change pas
    # le moteur en même temps que le compte.
    for outil in ("claude", "codex"):
        avant = _version_outil(ctx, outil, cible=False)
        apres = _version_outil(ctx, outil, cible=True)
        if avant and apres and avant != apres:
            manques.append("%s : version cible %r != source %r"
                           % (outil, apres, avant))
    # capacité : `development` peut être un montage dédié (bind mount du
    # runbook § 4.10), le reste vit sous le home. Deux mesures distinctes.
    besoin = {"development": 0, "home": 0}
    for chemin, cle in ((ctx.source_home / "development/manaty", "development"),
                        (ctx.source_home / ".local/state/ameesh", "home"),
                        (ctx.source_home / ".local/share/ameesh", "home")):
        if chemin.exists():
            taille = ctx.hote.lancer(["du", "-sb", str(chemin)], delai=600)
            with contextlib.suppress(ValueError, IndexError):
                besoin[cle] += int(taille.sortie.split()[0])
    for cle in ("development", "home"):
        if not besoin[cle]:
            continue
        racine = ctx.cible_home / cle if cle == "development" else ctx.cible_home
        try:
            libre = shutil.disk_usage(racine).free
        except OSError:
            libre = 0
        if libre < besoin[cle] * 1.2:
            manques.append(
                "espace libre insuffisant pour %s : %.1f Gio libres pour ~%.1f "
                "Gio à copier — voir le runbook § 4.10 (recréer les clones ou "
                "monter le volume de développement sur /home/ameesh/development)"
                % (cle, libre / 2**30, besoin[cle] / 2**30))
    return manques


def commande_basculer(ctx: Contexte, args) -> int:
    exiger_racine(ctx)
    with verrou(ctx, "basculer"):
        exiger_source(ctx)
        exiger_hote(ctx)
        return basculer(ctx, args)


# --------------------------------------------------------------------------
# preuve
# --------------------------------------------------------------------------


def verifier_ssh_isole(ctx: Contexte) -> tuple[bool, str]:
    """`~/.ssh` de l'owner doit être EACCES sous ameesh, directement."""
    cible = ctx.source_home / ".ssh"
    if not cible.exists():
        return False, "~/.ssh source absent : preuve impossible"
    script = ('import os,sys\n'
              'try:\n'
              '    os.listdir(sys.argv[1])\n'
              'except PermissionError:\n'
              '    print("EACCES")\n'
              'except OSError as e:\n'
              '    print("OSError:%s" % e.strerror)\n'
              '    sys.exit(1)\n'
              'else:\n'
              '    print("LISIBLE")\n'
              '    sys.exit(1)\n')
    fait = executer_sous_cible(ctx, ["python3", "-c", script, str(cible)], delai=30)
    if fait.ok and fait.sortie.strip() == "EACCES":
        return True, "%s : EACCES" % cible
    return False, "%s : %s" % (cible, (fait.sortie + fait.erreur).strip()[-200:])


def verifier_docker(ctx: Contexte) -> tuple[bool, str]:
    compte = ctx.hote.compte(CIBLE)
    if "docker" in compte.groupes:
        return False, ("%s est dans le groupe docker : la clé de l'owner reste "
                       "lisible (docker run -v /home/%s/.ssh) — retirer le groupe "
                       "ou installer un Docker rootless" % (CIBLE, SOURCE))
    return True, "aucun groupe docker (pas de root de fait)"


def git_ssh_fetch(ctx: Contexte) -> tuple[bool, str]:
    """Une vraie lecture de dépôt par SSH, avec la clé dédiée, sous ameesh."""
    depot = ctx.cible_home / "development/manaty" / (args_canari(ctx) or "ameesh")
    if not depot.is_dir():
        # premier dépôt copié
        candidats = sorted((ctx.cible_home / "development/manaty").iterdir()) \
            if (ctx.cible_home / "development/manaty").is_dir() else []
        for candidat in candidats:
            if (candidat / ".git").exists():
                depot = candidat
                break
    if not (depot / ".git").exists():
        return False, "aucun dépôt copié pour la preuve git SSH"
    fait = executer_sous_cible(ctx, ["git", "-C", str(depot), "fetch", "--no-tags",
                                     "git@github.com:manaty/ameesh.git",
                                     "refs/heads/main"], delai=180)
    if not fait.ok:
        return False, "git fetch SSH : %s" % (fait.sortie + fait.erreur).strip()[-300:]
    return True, "git fetch SSH vert dans %s" % depot


def args_canari(ctx: Contexte) -> str:
    return str(lit_etat(ctx).get("canari") or "")


def verifier_gh(ctx: Contexte) -> tuple[bool, str]:
    fait = executer_sous_cible(ctx, ["gh", "auth", "status"], delai=60)
    if not fait.ok:
        return False, "gh auth status : %s" % (fait.sortie + fait.erreur).strip()[-300:]
    return True, "gh authentifié"


def verifier_leases(ctx: Contexte, agents: Sequence[dict]) -> list[str]:
    manques = []
    for agent in agents:
        fait = ameesh_cible(ctx, "show", agent["name"], "--json", delai=60)
        if not fait.ok:
            manques.append("%s : show illisible" % agent["name"])
            continue
        try:
            donnees = json.loads(fait.sortie)
        except ValueError:
            manques.append("%s : JSON illisible" % agent["name"])
            continue
        if not donnees.get("lease_owner"):
            manques.append("%s : aucun bail" % agent["name"])
        elif float(donnees.get("lease_expires_ts") or 0) <= time.time():
            manques.append("%s : bail expiré" % agent["name"])
    return manques


def verifier_doctor(ctx: Contexte) -> tuple[bool, str]:
    fait = ameesh_cible(ctx, "doctor", delai=300)
    texte = fait.sortie + fait.erreur
    if not fait.ok or "verdict    : OK" not in texte:
        return False, "doctor cible non vert : %s" % texte.strip()[-600:]
    return True, "doctor cible vert"


def verifier_harness(ctx: Contexte) -> tuple[bool, str]:
    fait = ameesh_cible(ctx, "doctor", "--harness", delai=120)
    if not fait.ok:
        return False, "doctor --harness : %s" % (fait.sortie + fait.erreur).strip()[-400:]
    return True, "harnais résolus"


def verifier_comptes(ctx: Contexte) -> list[str]:
    fait = ameesh_cible(ctx, "accounts", "list", "--json", delai=120)
    if not fait.ok:
        return ["accounts list illisible"]
    try:
        donnees = json.loads(fait.sortie)
    except ValueError:
        return ["accounts list : JSON illisible"]
    manques = []
    for compte in donnees.get("accounts") or []:
        problemes = compte.get("problems") or []
        textes = [str(p) for p in problemes if "credentials missing" in str(p).lower()
                  or "identifiant" in str(p).lower()]
        if textes:
            manques.append("%s/%s : %s" % (compte.get("harness"), compte.get("account"),
                                           "; ".join(textes)))
    return manques


def preuve(ctx: Contexte) -> dict:
    etat = lit_etat(ctx)
    agents = agents_du_poste(ctx)
    actifs = [a for a in agents if statut_unite(ctx, a["name"], cible=True)["actif"]
              == "active"]
    resultats: dict[str, dict] = {}

    def noter(nom: str, ok: bool, detail: str) -> None:
        resultats[nom] = {"ok": bool(ok), "detail": detail}
        dire("  %-22s %s — %s" % (nom, "OK" if ok else "KO", detail))

    journal(ctx, "preuve", "matrice de preuves de la migration")
    noter("ssh_owner_eacces", *verifier_ssh_isole(ctx))
    noter("docker", *verifier_docker(ctx))
    noter("git_ssh_fetch", *git_ssh_fetch(ctx))
    noter("gh", *verifier_gh(ctx))
    noter("doctor", *verifier_doctor(ctx))
    noter("harness", *verifier_harness(ctx))
    manques = verifier_cwd(ctx, actifs, cible=True)
    noter("cwd", not manques, "; ".join(manques) or "tous sous le home ameesh")
    manques = verifier_leases(ctx, actifs)
    noter("baux", not manques, "; ".join(manques) or "%d baux vivants" % len(actifs))
    manques = verifier_comptes(ctx)
    noter("comptes", not manques, "; ".join(manques) or "identifiants présents")
    # unités : aucune en échec, actifs = état d'avant
    source_actifs = set(etat.get("actifs") or [])
    cibles_actifs = {a["name"] for a in actifs}
    enabled_cible = set(etat_enabled(ctx, sorted(cibles_actifs), cible=True))
    u = systemctl_cible(ctx, "list-units", "--all", "--state=failed",
                        "--plain", "--no-legend", PREFIXE_UNITE + "*.service",
                        delai=60)
    noter("unites", source_actifs == cibles_actifs
          and enabled_cible == cibles_actifs and not (u.sortie or "").strip(),
          "%d actives (attendu %d), %d activées (attendu %d), échecs : %s"
          % (len(cibles_actifs), len(source_actifs), len(enabled_cible),
             len(cibles_actifs), (u.sortie or "").strip() or "aucun"))
    # courrier : le journal de bascule doit porter la remise du canari
    courrier = etat.get("preuve_courrier") or ""
    noter("courrier", "courrier test livré" in courrier, courrier or "aucune preuve")
    # clé dédiée : jamais celle de l'owner
    pub_source = ctx.source_home / ".ssh/id_ed25519.pub"
    pub_cible = ctx.cible_home / ".ssh/id_ed25519.pub"
    cles_distinctes = True
    if pub_source.is_file() and pub_cible.is_file():
        cles_distinctes = (pub_source.read_text(encoding="utf-8").strip()
                           != pub_cible.read_text(encoding="utf-8").strip())
    noter("cle_dediee", cles_distinctes,
          "clé ameesh distincte du home de l'owner" if cles_distinctes
          else "la clé cible est celle de l'owner")
    resultats["verdict"] = {"ok": all(r["ok"] for k, r in resultats.items()
                                      if k != "verdict")}
    return resultats


def commande_preuve(ctx: Contexte, args) -> int:
    exiger_racine(ctx)
    with verrou(ctx, "preuve"):
        exiger_source(ctx)
        exiger_hote(ctx)
        resultats = preuve(ctx)
        chemin = ctx.etat_dir / "preuve.json"
        ecrit_etat_fichier(chemin, resultats)
        dire("")
        if resultats["verdict"]["ok"]:
            etat = lit_etat(ctx)
            etat["stage"] = "preuve"
            ecrit_etat(ctx, etat)
            dire("preuve : VERT — migration #3549 établie.")
            return 0
        dire("preuve : ROUGE — voir les lignes KO ci-dessus.")
        return 1


def ecrit_etat_fichier(chemin: Path, donnees: dict) -> None:
    tmp = chemin.with_suffix(".tmp.%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8") as flux:
        json.dump(donnees, flux, ensure_ascii=False, indent=2)
        flux.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, chemin)


# --------------------------------------------------------------------------
# retour
# --------------------------------------------------------------------------


def restaurer_pieces(ctx: Contexte) -> list[str]:
    sauvegarde = ctx.etat_dir / "sauvegarde"
    restaures = []
    for chemin in (ctx.source_home / ".config/systemd/user" /
                   (PREFIXE_UNITE + ".service.d/cles-owner.conf"),
                   ctx.source_home / ".config/ameesh/git-agents.conf"):
        relatif = chemin.relative_to(ctx.source_home)
        source = sauvegarde / relatif
        if source.is_file():
            chemin.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, chemin, follow_symlinks=False)
            os.chmod(chemin, 0o600)
            if ctx.hote.comptes is None:
                compte = exiger_source(ctx)
                os.chown(chemin, compte.uid, compte.gid)
            restaures.append(str(relatif))
    return restaures


def retour(ctx: Contexte, args) -> int:
    etat = lit_etat(ctx)
    if etat.get("stage") not in ("bascule", "preuve", "retour") and not args.force:
        refuser("état %r : aucun basculement à défaire (--force pour forcer)"
                % etat.get("stage"))
    # Des tours sous la cible après la bascule ? Le retour les perdrait.
    if etat.get("stage") in ("bascule", "preuve") and not args.force:
        from datetime import datetime as _dt
        try:
            migre = _dt.strptime(etat.get("migre_le", ""), "%Y-%m-%dT%H:%M:%SZ")
            migre_ts = migre.replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            migre_ts = 0
        agents = agents_du_poste(ctx)
        for agent in agents:
            fait = ameesh_cible(ctx, "show", agent["name"], "--json", delai=60)
            if not fait.ok:
                continue
            with contextlib.suppress(ValueError):
                donnees = json.loads(fait.sortie)
                if float(donnees.get("last_turn_ts") or 0) > migre_ts \
                        and agent["name"] != etat.get("canari"):
                    refuser("%s a travaillé sous %s depuis la bascule : un retour "
                            "perdrait ce travail (--force si c'est le tour de test)"
                            % (agent["name"], CIBLE))
    motif = "retour #3549 vers les unités de %s (%s)" % (SOURCE, horodatage())
    agents = agents_du_poste(ctx)
    actifs = [a["name"] for a in agents
              if statut_unite(ctx, a["name"], cible=True)["actif"]
              in ("active", "activating", "reloading")]
    journal(ctx, "retour", "arrêt des %d exécuteurs cible, retour à %s"
            % (len(actifs), SOURCE))
    pauses = poser_pauses(ctx, [a for a in agents if a["name"] in actifs], motif) \
        if actifs else {}
    if actifs:
        fait = systemctl_cible(ctx, "stop",
                               *["%s%s.service" % (PREFIXE_UNITE, n) for n in actifs],
                               delai=300)
        if not fait.ok:
            refuser("arrêt de la cible refusé : %s" % fait.erreur.strip())
        etats = attendre_unites(ctx, actifs, cible=True, delai=args.attente_arret,
                                arret=True)
        non_arretes = [n for n, e in etats.items()
                       if not (e["actif"] == "inactive" and e["pid"] == "0")]
        if non_arretes:
            refuser("arrêt non prouvé sous %s : %s" % (CIBLE, ", ".join(non_arretes)))
    # remettre les pièces #3444/#3540, recharger, échanger les unités
    # activées : la source redémarre au boot, la cible non.
    restaures = restaurer_pieces(ctx)
    if restaures:
        dire("  pièces restaurées : %s" % ", ".join(restaures))
    systemctl_source(ctx, "daemon-reload", delai=60)
    enabled_cible = etat.get("enabled_cible") or actifs
    if enabled_cible:
        with contextlib.suppress(Refus):
            desactiver_unites(ctx, enabled_cible, cible=True)
    enabled_source = (etat.get("enabled_source") or etat.get("actifs")
                      or list(actifs))
    if enabled_source:
        activer_unites(ctx, enabled_source)
    a_demarrer = list(etat.get("actifs") or actifs)
    if a_demarrer:
        ok, detail = canon_sync(ctx, cible=False)
        if not ok:
            refuser("canon sync de retour a échoué : %s" % detail)
        fait = systemctl_source(ctx, "start",
                                *["%s%s.service" % (PREFIXE_UNITE, n)
                                  for n in a_demarrer], delai=300)
        if not fait.ok:
            refuser("redémarrage source refusé : %s" % fait.erreur.strip())
        etats = attendre_unites(ctx, a_demarrer, cible=False, delai=180)
        non_actifs = [n for n, e in etats.items() if e["actif"] != "active"]
        if non_actifs:
            refuser("unités source non actives : %s" % ", ".join(non_actifs))
    non_levees = lever_pauses(ctx, pauses)
    if non_levees:
        refuser("pauses de retour non levées : %s" % ", ".join(non_levees))
    etat["stage"] = "retour"
    etat["retour_le"] = horodatage()
    ecrit_etat(ctx, etat)
    dire("")
    dire("retour fait : les unités actives de %s servent de nouveau ; "
         "le compte %s et ses données restent en place." % (SOURCE, CIBLE))
    return 0


def commande_retour(ctx: Contexte, args) -> int:
    exiger_racine(ctx)
    with verrou(ctx, "retour"):
        exiger_source(ctx)
        exiger_hote(ctx)
        return retour(ctx, args)


# --------------------------------------------------------------------------
# etat
# --------------------------------------------------------------------------


def commande_etat(ctx: Contexte, args) -> int:
    etat = lit_etat(ctx)
    if args.json:
        dire(json.dumps(etat, ensure_ascii=False, indent=2))
    else:
        dire("état       : %s" % etat.get("stage"))
        dire("hôte       : %s" % etat.get("hote"))
        dire("agents     : %s" % ", ".join(a.get("name", "?") for a in etat.get("agents") or []))
        dire("actifs     : %s" % ", ".join(etat.get("actifs") or []))
        dire("basculé le : %s" % (etat.get("migre_le") or "—"))
        dire("journal    : %s" % (ctx.etat_dir / "journal.jsonl"))
        for ligne in reversed((ctx.hote.lire(ctx.etat_dir / "journal.jsonl", defaut="")
                               or "").splitlines()[-10:]):
            with contextlib.suppress(ValueError):
                entree = json.loads(ligne)
                dire("  %s %-10s %s" % (entree.get("ts"), entree.get("etape"),
                                        entree.get("message")))
    return 0


# --------------------------------------------------------------------------
# point d'entrée
# --------------------------------------------------------------------------


def construire_contexte(depot: Path | None = None) -> Contexte:
    depot = depot or Path(__file__).resolve().parents[2]
    racine = Path("/var/lib/ameesh/migration-runners")
    hote = Hote()
    ctx = Contexte(hote=hote, depot=depot, etat_dir=hote.chemin(racine))
    with contextlib.suppress(Refus):
        ctx.source = hote.compte(SOURCE)
    if ctx.source is None:
        refuser("compte source %s absent : mauvais poste" % SOURCE)
    ctx.cible = None
    return ctx


def analyse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="migration-runners-ameesh.py",
        description="Migration #3549 : exécuteurs de pc-smichea sous "
                    "l'utilisateur Unix ameesh, réversible.")
    sous = parser.add_subparsers(dest="commande", required=True)

    p = sous.add_parser("verifier", help="inventaire et gardes, lecture seule")
    p.add_argument("--json", action="store_true", help="sortie JSON")
    p.add_argument("--avant-bascule", action="store_true",
                   help="exiger aussi la matrice de gardes de la bascule")
    p.set_defaults(fonction=commande_verifier)

    p = sous.add_parser("preparer", help="compte, home, état, venv, unités (sans démarrer)")
    p.add_argument("--reprendre", action="store_true")
    p.set_defaults(fonction=commande_preparer)

    p = sous.add_parser("identites", help="clé SSH, gh, auth Codex/DeepSeek, logins Claude")
    p.set_defaults(fonction=commande_identites)

    p = sous.add_parser("basculer", help="drain, copie finale, démarrage sous ameesh")
    p.add_argument("--canari", default=None, help="agent canari (défaut : codex2)")
    p.add_argument("--attente-arret", type=float, default=2200,
                   help="borne du drainage, en secondes (défaut 2200)")
    p.set_defaults(fonction=commande_basculer)

    p = sous.add_parser("preuve", help="matrice de preuves")
    p.set_defaults(fonction=commande_preuve)

    p = sous.add_parser("retour", help="une commande : revenir aux unités de smichea")
    p.add_argument("--force", action="store_true",
                   help="même si des tours ont eu lieu sous ameesh")
    p.add_argument("--attente-arret", type=float, default=2200)
    p.set_defaults(fonction=commande_retour)

    p = sous.add_parser("etat", help="état et journal")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fonction=commande_etat)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = analyse_arguments(argv)
    try:
        ctx = construire_contexte()
        return args.fonction(ctx, args)
    except Refus as exc:
        print("migration-runners-ameesh: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("migration-runners-ameesh: interrompu ; état conservé sous "
              "/var/lib/ameesh/migration-runners", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
