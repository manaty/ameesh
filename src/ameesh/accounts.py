# SPDX-License-Identifier: AGPL-3.0-only
"""Comptes multiples par fournisseur (lot L30, décision 0027).

Pour chaque harnais, l'hôte déclare une **liste ordonnée de comptes** dans sa
configuration (`~/.config/ameesh/config.json`, clé `accounts`) — jamais dans
le canon : ce sont des secrets d'hôte.

    "accounts": {
      "claude": [
        {"name": "primaire"},
        {"name": "secondaire", "type": "config_dir", "path": "/…/comptes/claude-2"}
      ],
      "codex": [
        {"name": "primaire"},
        {"name": "secondaire", "type": "config_dir", "path": "/…/comptes/codex-2"}
      ],
      "deepseek": [
        {"name": "cle-a", "type": "api_key_env", "key_env": "DEEPSEEK_API_KEY_A",
         "hourly_usd": 5},
        {"name": "cle-b", "type": "api_key_env", "key_file": "/…/comptes/deepseek-b.key"}
      ]
    }

Types de profil :

* `config_dir` : un dossier de configuration du harnais, passé par
  `CLAUDE_CONFIG_DIR` (Claude Code), `CODEX_HOME` (Codex) ou `DSH_HOME`
  (DeepSeek Harness). Sans `path`, c'est le dossier que le harnais prendrait
  de lui-même dans l'environnement de l'exécuteur (variable héritée, sinon
  son dossier par défaut) ; ses jauges y sont lues. Un dossier
  déclaré doit appartenir à l'utilisateur et être privé (0700) ; ses
  identifiants doivent être présents (présence seulement : ameesh ne lit
  jamais leur contenu).
* `api_key_env` : une clé d'API passée au harnais par une variable
  d'environnement (`env`, par défaut `DEEPSEEK_API_KEY` pour DeepSeek). La clé
  vient d'une variable de l'environnement de l'exécuteur (`key_env`) ou d'un
  fichier privé (`key_file`, 0600). Elle n'est lue qu'au lancement du tour,
  pour l'environnement du harnais, et n'est jamais journalisée.

`hourly_usd` (facultatif, tout type) : plafond horaire propre au compte, sur
le grand livre (`turn_costs.account`) — le « plafond » d'un compte payé au
token (0027 §2). `min_balance` (facultatif, clé d'API) : le compte est au seuil
quand son dernier solde relevé (`provider_balances.account`, relevé par
l'exécuteur ou `ameesh cost balance --record`) est au plus ce montant ; il
redevient éligible au premier relevé au-dessus (solde rechargé).

**Choix du compte, avant chaque tour** (`choose`, décision 0034 qui amende
0027 §2–3) — les comptes d'un fournisseur forment un **réservoir** :

1. forçage manuel (`ameesh accounts use`) : ce compte, ou pause s'il est au
   seuil ; `ameesh accounts auto` rend la main à la règle ;
2. **continuité** : la session en cours de l'agent reste sur son compte tant
   que celui-ci est sous son seuil — pas de changement en cours de session
   pour un gain marginal ;
3. sinon (ouverture ou rotation de session, ou compte de la session au
   seuil) : parmi les comptes **utilisables** (profil valide, sous le seuil),
   celui dont la **capacité inutilisée expire le plus tôt** (`expiry` : la
   remise à zéro la plus proche parmi ses fenêtres en cours) ; un compte sans
   fenêtre en cours (relevé échu, non daté ou absent) passe après ; à
   égalité, l'ordre déclaré ;
4. sinon pause : tous les comptes sont au seuil.

Le seuil est celui de la garde (0019, inchangé) : `min(90 %, part écoulée +
10 points)` sur les vraies jauges **du compte**, relues à chaque choix —
Claude : les `rate_limit_event` du flux des agents, attribués par le marqueur
de compte qui précède chaque tour ; Codex : les journaux de session du
`CODEX_HOME` du compte. Une fenêtre dont `resets_at` est passé compte pour
0 % (`Gauge.used_at`, L71) : un compte sans relevé récent est essayé, pas
écarté — son premier tour rapporte sa jauge.

Chaque choix porte sa raison (`Choice.why`, « five_hour expire dans 52 min,
0 % utilisé »), journalisée par l'exécuteur (`account_turn.choose`).

La ligne `account_active` garde le **dernier compte choisi** pour une
nouvelle session (et le forçage) ; elle change en base par
comparer-et-changer (`storage.accounts`) : deux workers qui font le même
choix n'écrivent qu'une bascule. Les retenues (`account_holds`) de 0027 §3 ne
sont plus posées : le choix par échéance les remplace.
"""
from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from . import harnesses, storage
from .config import NAME_RE

TYPES = ("config_dir", "api_key_env")


def _account_key(harness: str, key: str, default: str = "") -> str:
    """Une clé de compte déclarée par le descripteur du harnais (L16, R22).

    Plus de table `harnais → variable/dossier` dans le code : un harnais
    inconnu n'a simplement pas de valeur, et un harnais nouveau se décrit dans
    son descripteur (`ameesh.accounts`).
    """
    descriptor = harnesses.get(harness)
    if descriptor is None:
        return default
    value = descriptor.accounts.get(key)
    return str(value) if isinstance(value, str) and value else default


def config_env_of(harness: str) -> str:
    """Variable qui désigne le dossier de configuration du harnais."""
    return _account_key(harness, "config_env")


def default_home_of(harness: str) -> str:
    """Dossier de configuration par défaut quand la variable est absente."""
    return _account_key(harness, "default_home", "~")


def credentials_of(harness: str) -> str:
    """Fichier d'identifiants dont on vérifie la PRÉSENCE (jamais le contenu)."""
    return _account_key(harness, "credentials")


def session_store_of(harness: str) -> str:
    """Sous-dossier où le harnais range ses sessions."""
    return _account_key(harness, "session_store")


def default_key_env_of(harness: str) -> str:
    """Variable de clé d'API lue par le harnais, par défaut."""
    return _account_key(harness, "key_env")


def _all_account_keys(key: str) -> set[str]:
    return {value for value in (
        _account_key(ident, key) for ident in harnesses.known_ids()) if value}



class AccountError(ValueError):
    """Configuration des comptes invalide, ou profil inutilisable."""


@dataclass(frozen=True)
class Profile:
    """Un compte déclaré par l'hôte. Ne porte AUCUN secret : un chemin ou un nom."""

    harness: str
    name: str
    kind: str = "config_dir"
    path: str = ""            #: dossier de configuration ; vide = défaut du harnais
    env: str = ""             #: variable de clé passée au harnais (api_key_env)
    key_env: str = ""         #: variable de l'exécuteur qui porte la clé
    key_file: str = ""        #: fichier privé (0600) qui porte la clé
    hourly_usd: float = 0.0
    min_balance: float | None = None
    check_credentials: bool = True

    @property
    def default(self) -> bool:
        """Le dossier par défaut du harnais (aucune variable posée)."""
        return self.kind == "config_dir" and not self.path

    def home(self, environ=None) -> str:
        """Le dossier de configuration effectif du harnais pour ce compte.

        Sans `path` (compte par défaut, ou clé d'API) : celui que le harnais
        prendrait de lui-même dans l'environnement de l'exécuteur — sa variable
        (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `DSH_HOME`) si elle est posée,
        sinon son dossier par défaut. C'est aussi là que `launch_env` le lance
        et que ses jauges sont lues (un seul mécanisme).
        """
        if self.kind == "config_dir" and self.path:
            return self.path
        environ = os.environ if environ is None else environ
        inherited = environ.get(config_env_of(self.harness)) or ""
        return os.path.abspath(os.path.expanduser(
            inherited or default_home_of(self.harness)))

    def session_store(self) -> str:
        """Le dossier des sessions du harnais pour ce compte (chemin résolu)."""
        return os.path.realpath(os.path.join(self.home(), session_store_of(self.harness)))

    def describe(self) -> str:
        """Ce qu'on peut afficher : le type et l'emplacement, jamais la clé."""
        if self.kind == "api_key_env":
            source = ("fichier %s" % self.key_file) if self.key_file else (
                "variable %s" % self.key_env if self.key_env else "variable héritée")
            return "clé d'API → %s (%s)" % (
                self.env or default_key_env_of(self.harness) or "?", source)
        return "dossier %s" % (self.path or "par défaut")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(str(path)))


def parse(raw) -> dict[str, list[Profile]]:
    """`{harnais: [Profile, …]}` depuis la clé `accounts` de la configuration.

    Toute erreur de forme lève `AccountError` : une configuration de comptes
    illisible suspend les tours du harnais (fail-closed), elle ne retombe pas
    en silence sur un compte au hasard.
    """
    if raw in (None, "", {}):
        return {}
    if not isinstance(raw, dict):
        raise AccountError("`accounts` doit être un objet {harnais: [profils]}")
    out: dict[str, list[Profile]] = {}
    for harness, items in raw.items():
        if harness not in harnesses.known_ids():
            raise AccountError("harnais inconnu dans `accounts` : %r (descripteurs connus : %s)"
                               % (harness, ", ".join(harnesses.known_ids()) or "aucun"))
        if not isinstance(items, list) or not items:
            raise AccountError("`accounts.%s` doit être une liste non vide" % harness)
        seen: set[str] = set()
        profiles: list[Profile] = []
        for item in items:
            if not isinstance(item, dict):
                raise AccountError("`accounts.%s` : profil non objet" % harness)
            name = str(item.get("name") or "")
            if not NAME_RE.match(name):
                raise AccountError("`accounts.%s` : nom de compte invalide %r" % (harness, name))
            if name in seen:
                raise AccountError("`accounts.%s` : compte en double %r" % (harness, name))
            seen.add(name)
            kind = str(item.get("type") or "config_dir")
            if kind not in TYPES:
                raise AccountError("compte %s/%s : type %r inconnu (%s)"
                                   % (harness, name, kind, ", ".join(TYPES)))
            try:
                hourly = float(item.get("hourly_usd") or 0.0)
            except (TypeError, ValueError):
                raise AccountError("compte %s/%s : hourly_usd illisible" % (harness, name))
            if hourly < 0 or hourly != hourly:
                raise AccountError("compte %s/%s : hourly_usd négatif" % (harness, name))
            min_balance = item.get("min_balance")
            if min_balance is not None:
                try:
                    min_balance = float(min_balance)
                except (TypeError, ValueError):
                    raise AccountError("compte %s/%s : min_balance illisible" % (harness, name))
            path = str(item.get("path") or "")
            key_env = str(item.get("key_env") or "")
            key_file = str(item.get("key_file") or "")
            env = str(item.get("env") or "")
            if kind == "config_dir":
                if key_env or key_file:
                    raise AccountError("compte %s/%s : key_env/key_file réservés à api_key_env"
                                       % (harness, name))
                if path and not os.path.isabs(os.path.expanduser(path)):
                    raise AccountError("compte %s/%s : chemin absolu attendu" % (harness, name))
            else:
                if path:
                    raise AccountError("compte %s/%s : path réservé à config_dir"
                                       % (harness, name))
                if bool(key_env) == bool(key_file):
                    raise AccountError("compte %s/%s : un seul de key_env ou key_file"
                                       % (harness, name))
                if not (env or default_key_env_of(harness)):
                    raise AccountError("compte %s/%s : variable `env` attendue" % (harness, name))
                if key_file and not os.path.isabs(os.path.expanduser(key_file)):
                    raise AccountError("compte %s/%s : chemin absolu attendu" % (harness, name))
            profiles.append(Profile(
                harness=harness, name=name, kind=kind,
                path=_expand(path) if path else "",
                env=env or (default_key_env_of(harness) if kind == "api_key_env" else ""),
                key_env=key_env, key_file=_expand(key_file) if key_file else "",
                hourly_usd=hourly, min_balance=min_balance,
                check_credentials=bool(item.get("check_credentials", True)),
            ))
        out[harness] = profiles
    return out


def profiles(cfg, harness: str) -> list[Profile]:
    """Les comptes déclarés pour ce harnais (liste vide : aucun, comportement d'avant L30)."""
    return parse(getattr(cfg, "accounts", None) or {}).get(harness, [])


def by_name(items: list[Profile], name: str | None) -> Profile | None:
    for profile in items:
        if profile.name == name:
            return profile
    return None


# --------------------------------------------------------------------------
# validation (dossiers 0700, fichiers de clé 0600, présence des identifiants)
# --------------------------------------------------------------------------

def check(profile: Profile, environ=None) -> list[str]:
    """Les problèmes d'un profil (liste vide : utilisable). Ne lit aucun secret.

    Un dossier déclaré : existe, appartient à l'utilisateur, privé (aucun droit
    pour le groupe ni les autres), identifiants présents. Un fichier de clé :
    fichier ordinaire, à l'utilisateur, 0600 au plus. Une variable de clé :
    présente et non vide dans l'environnement de l'exécuteur.
    """
    environ = os.environ if environ is None else environ
    problems: list[str] = []
    uid = os.getuid() if hasattr(os, "getuid") else None
    if profile.kind == "config_dir":
        if profile.default:
            return problems  # dossier par défaut du harnais : celui de l'utilisateur
        try:
            info = os.stat(profile.path)
        except OSError:
            return ["dossier absent : %s" % profile.path]
        if not stat.S_ISDIR(info.st_mode):
            return ["pas un dossier : %s" % profile.path]
        if uid is not None and info.st_uid != uid:
            problems.append("dossier d'un autre utilisateur")
        if info.st_mode & 0o077:
            problems.append("dossier non privé (%o, attendu 700)" % (info.st_mode & 0o777))
        cred = credentials_of(profile.harness)
        if cred and profile.check_credentials and sys.platform != "darwin":
            if not os.path.isfile(os.path.join(profile.path, cred)):
                problems.append("identifiants absents (%s) : connexion humaine à faire" % cred)
        return problems
    if profile.key_file:
        try:
            info = os.lstat(profile.key_file)
        except OSError:
            return ["fichier de clé absent : %s" % profile.key_file]
        if not stat.S_ISREG(info.st_mode):
            return ["fichier de clé non ordinaire : %s" % profile.key_file]
        if uid is not None and info.st_uid != uid:
            problems.append("fichier de clé d'un autre utilisateur")
        if info.st_mode & 0o177:
            problems.append("fichier de clé trop ouvert (%o, attendu 600)"
                            % (info.st_mode & 0o777))
        return problems
    if not environ.get(profile.key_env):
        problems.append("variable de clé absente : %s" % profile.key_env)
    return problems


# --------------------------------------------------------------------------
# environnement du tour
# --------------------------------------------------------------------------

def auth_variables(declared: dict[str, list[Profile]], *, selected: bool) -> set[str]:
    """Les variables d'identifiants à retirer de l'environnement d'un harnais.

    Toujours : les variables SOURCES de clé de tous les profils déclarés (tous
    harnais) — la clé d'un compte non choisi n'atteint jamais un harnais.
    Quand un compte est choisi (`selected`) : aussi les destinations de clé,
    les variables de dossier de configuration et les clés d'API connues des
    harnais (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
    `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `DSH_HOME`) — une identité héritée ne
    doit pas prendre le pas sur le compte choisi.
    """
    out: set[str] = set()
    for items in declared.values():
        for profile in items:
            if profile.key_env:
                out.add(profile.key_env)
            if selected and profile.env:
                out.add(profile.env)
    if selected:
        out.update(_all_account_keys("config_env"))
        out.update(_all_account_keys("key_env"))
    return out


def launch_env(env: dict, declared: dict[str, list[Profile]], profile: Profile | None,
               environ=None) -> None:
    """L'environnement d'un harnais lancé (tour ou attach), sur un hôte à comptes.

    Part de `env` (copie de l'environnement de l'exécuteur), RETIRE les
    variables d'identifiants (`auth_variables`), puis applique SEULEMENT le
    profil choisi, validé au lancement (`apply_env`). `profile` None : harnais
    sans comptes déclarés — seules les sources de clé des autres comptes sont
    retirées, le reste est inchangé. Les clés sont lues dans `environ`
    (l'environnement de l'exécuteur), jamais dans la copie nettoyée.
    """
    environ = os.environ if environ is None else environ
    for name in auth_variables(declared, selected=profile is not None):
        env.pop(name, None)
    if profile is not None:
        variable = config_env_of(profile.harness)
        if variable and profile.path == "" and environ.get(variable):
            # compte sans dossier déclaré : le dossier hérité de l'exécuteur
            # pour CE harnais (pas celui d'un autre harnais, ni d'un autre compte)
            env[variable] = environ[variable]
        apply_env(env, profile, environ)


def apply_env(env: dict, profile: Profile, environ=None) -> None:
    """Pose dans `env` (environnement du harnais) ce qui sélectionne le compte.

    Le profil est **validé ici, au lancement** (dossier 0700 et identifiants
    présents, fichier de clé 0600, variable présente), quel que soit l'état de
    la garde de budget : un profil devenu invalide depuis le choix refuse le
    tour. `config_dir` : la variable du dossier ; pour le compte par
    défaut (sans chemin), le dossier hérité de l'exécuteur est conservé par
    `launch_env`. `api_key_env` : la clé, lue dans la variable de l'exécuteur ou le
    fichier privé. La clé ne quitte cette fonction que dans `env` ; un échec
    lève `AccountError` sans jamais citer la clé.
    """
    environ = os.environ if environ is None else environ
    problems = check(profile, environ)
    if problems:
        raise AccountError("compte %s/%s inutilisable : %s"
                           % (profile.harness, profile.name, "; ".join(problems)))
    variable = config_env_of(profile.harness)
    if profile.kind == "config_dir":
        if not profile.default and variable:
            env[variable] = profile.path
        # compte par défaut : `env` garde le dossier hérité (voir `launch_env`)
        return
    if profile.key_file:
        try:
            with open(profile.key_file, encoding="utf-8") as fh:
                key = fh.read().strip()
        except OSError as exc:
            raise AccountError("compte %s/%s : fichier de clé illisible (%s)"
                               % (profile.harness, profile.name, exc.strerror))
    else:
        key = str(environ.get(profile.key_env) or "").strip()
    if not key:
        raise AccountError("compte %s/%s : clé vide" % (profile.harness, profile.name))
    env[profile.env] = key


def session_portable(harness: str, old: Profile | None, new: Profile) -> bool:
    """Une session ouverte sous `old` peut-elle être reprise sous `new` ?

    Voir l'étude `docs/design/etudes/comptes-multiples.md` :

    * Claude Code : oui si les deux dossiers de configuration partagent leur
      stockage de sessions (`projects/`, même chemin résolu, p. ex. un lien
      symbolique) — la session est un fichier, les identifiants sont à part ;
    * Codex : non — la session est rattachée au compte qui l'a créée et ses
      relevés de jauge vivent dans les sessions du `CODEX_HOME` ; on tourne avec
      résumé ;
    * DeepSeek Harness : oui si le dossier du harnais (`DSH_HOME`) est le même
      — seule la clé d'API change.

    `old` None : compte inconnu (retiré de la configuration) → non.
    """
    if old is None:
        return False
    if old.name == new.name and old.home() == new.home():
        return True
    if harness == "codex":
        return False
    if harness == "claude":
        return old.session_store() == new.session_store()
    if harness == "deepseek":
        return os.path.realpath(old.home()) == os.path.realpath(new.home())
    return False


# --------------------------------------------------------------------------
# jauges et seuil par compte
# --------------------------------------------------------------------------

@dataclass
class Evaluation:
    """L'état d'un compte au moment du choix."""

    profile: Profile
    reason: str = ""                     #: '' = sous le seuil
    gauges: list = field(default_factory=list)
    until: float | None = None           #: remise à zéro attendue si au seuil
    problems: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reason


def gauges_of(book, profile: Profile, items: list[Profile]) -> list:
    """Les jauges de forfait du compte, lues dans SES journaux."""
    if profile.harness == "claude":
        return book.claude_gauges(account=profile.name, primary=items[0].name)
    if profile.harness == "codex":
        # Un seul mécanisme (cost.CostBook) : argument > AMEESH_CODEX_SESSIONS >
        # $CODEX_HOME/sessions > ~/.codex/sessions. Un compte déclaré a son
        # CODEX_HOME : ses sessions sont passées en argument. Le compte par
        # défaut est celui de l'environnement : le mécanisme sans argument.
        if profile.default:
            return book.codex_gauges()
        return book.codex_gauges(sessions=os.path.join(profile.home(), "sessions"))
    return []


def evaluate(book, profile: Profile, items: list[Profile], now: float,
             environ=None, *, record: bool = True) -> Evaluation:
    """Le compte est-il sous le seuil de la garde (0019) ? Ne lit aucun secret.

    `record=False` (simulation, L39) : l'historique des jauges n'est pas écrit."""
    result = Evaluation(profile)
    result.problems = check(profile, environ)
    if result.problems:
        result.reason = "profil inutilisable : %s" % "; ".join(result.problems)
        return result
    result.gauges = gauges_of(book, profile, items)
    if result.gauges and record:
        # historique des jauges de L26, attribué au compte (migration 0028)
        book.record_gauges(result.gauges, account=profile.name)
    depasses = []
    for gauge in result.gauges:
        # fenêtre remise à zéro depuis le relevé : vierge (L71 : `exceeded`
        # compte alors 0 %, voir `Gauge.used_at`)
        if gauge.exceeded(now):
            depasses.append(gauge)
    if depasses:
        gauge = depasses[0]
        result.reason = ("forfait %s %s : %.0f%% utilisé pour %.0f%% de la fenêtre écoulée "
                         "(plafond de rythme %.0f%%)" % (
                             profile.harness, gauge.key, gauge.used * 100,
                             gauge.elapsed(now) * 100, gauge.pace_cap(now) * 100))
        resets = [g.resets_at for g in depasses if g.resets_at]
        result.until = max(resets) if len(resets) == len(depasses) else None
        return result
    if profile.hourly_usd > 0 and book.db is not None:
        spent = book.spent("all", 3600, account=profile.name, harnesses=[profile.harness])
        if spent >= profile.hourly_usd:
            result.reason = ("plafond horaire du compte : %.2f $ sur 60 min (plafond %.2f $)"
                             % (spent, profile.hourly_usd))
            result.until = now + 3600.0
            return result
    if profile.min_balance is not None and book.db is not None:
        solde = latest_balance(book.db, profile)
        if solde is not None and float(solde["total"]) <= profile.min_balance:
            result.reason = ("solde du compte : %.4f %s (seuil %.4f)"
                             % (float(solde["total"]), solde["currency"], profile.min_balance))
            result.until = None  # éligible dès qu'un relevé montre le solde rechargé
    return result


def latest_balance(db, profile: Profile) -> dict | None:
    """Le dernier solde relevé du compte (toutes devises : le plus bas)."""
    rows = storage.of(db).operations.balances(provider=profile.harness, since_s=0.0,
                                              account=profile.name)
    latest: dict = {}
    for row in rows:
        latest[row["currency"]] = row  # ordre chronologique : le dernier gagne
    if not latest:
        return None
    return min(latest.values(), key=lambda r: float(r["total"]))


@dataclass
class Choice:
    """Le compte retenu pour le prochain tour, et ce qui a changé."""

    profile: Profile | None              #: None = pause, tous les comptes au seuil
    active: Profile | None               #: dernier compte choisi (même au seuil)
    reason: str = ""                     #: raison de la pause
    switched: dict | None = None         #: {from, to, kind, reason} si CE choix a basculé
    forced: bool = False
    why: str = ""                        #: raison du choix (journal, 0034 §5)
    kept: bool = False                   #: continuité : la session garde son compte


def _label(evaluations: dict) -> str:
    return " ; ".join("%s : %s" % (name, ev.reason) for name, ev in evaluations.items())


def duration(seconds: float) -> str:
    """Une durée lisible : « 52 min », « 3 h 10 », « 2 j 4 h »."""
    minutes = max(0, int(round(seconds / 60.0)))
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%d h %02d" % (hours, minutes) if minutes else "%d h" % hours
    days, hours = divmod(hours, 24)
    return "%d j %d h" % (days, hours) if hours else "%d j" % days


def expiry(gauges, now: float) -> tuple | None:
    """L'échéance du compte (0034 §1) : `(resets_at, inutilisé, jauge)` de sa
    fenêtre en cours qui se remet à zéro le plus tôt, ou None (aucune fenêtre
    en cours : relevé échu, non daté ou absent — rien n'expire)."""
    best = None
    for gauge in gauges:
        found = gauge.expiring(now)
        if found is not None and (best is None or found[0] < best[0]):
            best = (found[0], found[1], gauge)
    return best


def explain(result: Evaluation, now: float) -> str:
    """La raison lisible d'un choix par échéance : « five_hour expire dans
    52 min, 0 % utilisé »."""
    found = expiry(result.gauges, now)
    if found is not None:
        resets_at, _unused, gauge = found
        return "%s expire dans %s, %.0f %% utilisé" % (
            gauge.key, duration(resets_at - now), gauge.used_at(now) * 100)
    if any(g.reset_passed(now) for g in result.gauges):
        return "relevé échu (fenêtre remise à zéro : 0 %), essayé"
    if result.gauges:
        return "jauges non datées, sous le seuil"
    return "sans jauge de forfait, sous le seuil"


def rank(items: list[Profile], evaluations: dict, now: float) -> list[Profile]:
    """Les comptes utilisables, du premier à consommer au dernier (0034 §1).

    Clé : l'échéance de la capacité inutilisée (la plus proche d'abord ; sans
    fenêtre en cours, après), puis l'ordre déclaré."""
    def key(pair):
        index, profile = pair
        found = expiry(evaluations[profile.name].gauges, now)
        return (found[0] if found is not None else float("inf"), index)
    usable = [(i, p) for i, p in enumerate(items) if evaluations[p.name].ok]
    return [p for _i, p in sorted(usable, key=key)]


def choose(db, host: str, harness: str, items: list[Profile], book, *,
           now: float | None = None, agent: str | None = None, environ=None,
           simulate: bool = False, session_account: str | None = None) -> Choice:
    """Choisit le compte du prochain tour (voir l'en-tête du module).

    `session_account` : le compte sous lequel tourne la session en cours de
    l'agent (None : pas de session, ou session neuve) — elle le garde tant
    qu'il est sous son seuil (0034 §4).

    Seule écriture : le dernier compte choisi (`account_active`,
    comparer-et-changer, journalisé `bascule`) et l'historique des jauges.
    Une erreur de base remonte : l'appelant suspend (fail-closed).

    `simulate=True` (L39, `ameesh resume`) : le compte que choisirait le
    prochain tour, SANS AUCUNE écriture (ni bascule, ni historique des
    jauges) ; `switched` dit la bascule qui aurait lieu.
    """
    now = time.time() if now is None else now
    store = storage.of(db).accounts
    row = store.active(host, harness) or (
        {"account": items[0].name, "forced": False} if simulate
        else store.init_active(host, harness, items[0].name))
    names = [p.name for p in items]
    cache: dict[str, Evaluation] = {}

    def ev(profile: Profile) -> Evaluation:
        if profile.name not in cache:
            cache[profile.name] = evaluate(book, profile, items, now, environ,
                                           record=not simulate)
        return cache[profile.name]

    active_name = row.get("account")
    if active_name not in names and simulate:
        row = {"account": items[0].name, "forced": False}
        active_name = items[0].name
    if active_name not in names:
        # compte retiré de la configuration : retour au primaire, journalisé
        store.switch(host, harness, expected=active_name, to=items[0].name, kind="config",
                     reason="compte %r retiré de la configuration" % active_name,
                     agent=agent)
        row = store.active(host, harness) or {"account": items[0].name, "forced": False}
        active_name = row.get("account") if row.get("account") in names else items[0].name
    active = by_name(items, active_name) or items[0]

    # 1. forçage manuel : il prime, même sur la continuité
    if row.get("forced"):
        current = ev(active)
        if current.ok:
            return Choice(active, active, forced=True,
                          why="forcé (ameesh accounts use)")
        return Choice(None, active, reason="compte %s forcé (ameesh accounts use) : %s"
                      % (active.name, current.reason), forced=True)

    # 2. continuité : la session garde son compte tant qu'il est sous son seuil
    # (les autres comptes ne sont alors pas relus : rien à choisir)
    own = by_name(items, session_account) if session_account else None
    if own is not None and ev(own).ok:
        return Choice(own, active, kept=True,
                      why="continuité : la session reste sur %s, sous son seuil" % own.name)
    for profile in items:
        ev(profile)
    # 3. le compte dont la capacité inutilisée expire le plus tôt
    ranked = rank(items, cache, now)
    if not ranked:
        return Choice(None, active, reason="tous les comptes %s au seuil — %s"
                      % (harness, _label(cache)))
    target = ranked[0]
    why = explain(cache[target.name], now)
    if len(ranked) > 1:
        why += " (avant %s)" % ", ".join(
            "%s : %s" % (p.name, explain(cache[p.name], now)) for p in ranked[1:])
    left = own if own is not None else (active if target.name != active.name else None)
    if left is not None and not cache[left.name].ok:
        why = "%s au seuil (%s) ; %s" % (left.name, cache[left.name].reason, why)
    if target.name == active.name:
        return Choice(target, active, why=why)
    switched = {"from": active.name, "to": target.name, "kind": "bascule", "reason": why}
    if simulate:
        return Choice(target, target, switched=switched, why=why)
    if store.switch(host, harness, expected=active.name, to=target.name, kind="bascule",
                    reason=why, agent=agent):
        store.release(host, harness, target.name)  # retenue d'avant 0034, s'il en reste
        return Choice(target, target, switched=switched, why=why)
    # course : un autre worker a changé le dernier choix entre-temps ; un
    # forçage posé entre-temps prime, sinon notre choix tient
    fresh = store.active(host, harness) or {}
    if fresh.get("forced"):
        return _reread(store, host, harness, items, active)
    return Choice(target, by_name(items, fresh.get("account")) or target, why=why)


def _reread(store, host: str, harness: str, items: list[Profile], fallback: Profile) -> Choice:
    """Course perdue contre un forçage : on prend le compte en place."""
    row = store.active(host, harness) or {}
    profile = by_name(items, row.get("account")) or fallback
    return Choice(profile, profile, forced=bool(row.get("forced")),
                  why="forcé (ameesh accounts use)" if row.get("forced") else "")


def current(db, host: str, harness: str, items: list[Profile]) -> Profile:
    """Le compte actif, sans évaluer les jauges (garde de budget désactivée)."""
    row = storage.of(db).accounts.active(host, harness)
    return by_name(items, (row or {}).get("account")) or items[0]


# --------------------------------------------------------------------------
# forçage manuel (`ameesh accounts use|auto`)
# --------------------------------------------------------------------------

def force(db, host: str, harness: str, items: list[Profile], name: str, *, by: str) -> bool:
    """Force le compte `name` (forçage manuel, journalisé `manuel`)."""
    profile = by_name(items, name)
    if profile is None:
        raise AccountError("compte inconnu pour %s : %r (connus : %s)"
                           % (harness, name, ", ".join(p.name for p in items)))
    problems = check(profile)
    if problems:
        raise AccountError("compte %s/%s inutilisable : %s"
                           % (harness, name, "; ".join(problems)))
    store = storage.of(db).accounts
    store.init_active(host, harness, items[0].name)
    changed = store.switch(host, harness, expected=None, to=name, kind="manuel",
                           reason="forçage manuel par %s" % by, agent=None, forced=True)
    if changed:
        store.release(host, harness, name)
    return changed


def automatic(db, host: str, harness: str, *, by: str) -> bool:
    """Rend la main à l'automate (journalisé `auto`)."""
    return storage.of(db).accounts.set_auto(host, harness,
                                            reason="retour en automatique par %s" % by)


# --------------------------------------------------------------------------
# rapport (`ameesh accounts list`, `ameesh cost`)
# --------------------------------------------------------------------------

def report(cfg, db, book, *, now: float | None = None,
           harnesses: list[str] | None = None,
           evaluate_fn: Callable | None = None,
           record: bool = True) -> list[dict]:
    """Une ligne par compte déclaré : actif, forcé, état, jauges, retenue.

    Lecture seule : aucune bascule n'est faite ici. `record=False` (L71) :
    l'historique des jauges n'est pas écrit non plus — c'est le cas des
    commandes d'affichage (`ameesh accounts list`, `ameesh cost report`) ;
    le relevé revient aux exécuteurs (avant chaque tour) ou à une demande
    explicite.
    """
    now = time.time() if now is None else now
    declared = parse(getattr(cfg, "accounts", None) or {})
    store = storage.of(db).accounts if db is not None else None
    rows: list[dict] = []
    for harness, items in declared.items():
        if harnesses and harness not in harnesses:
            continue
        active = store.active(cfg.host, harness) if store else None
        holds = store.holds(cfg.host, harness) if store else {}
        active_name = (active or {}).get("account") or items[0].name
        for profile in items:
            result = (evaluate_fn or evaluate)(book, profile, items, now, record=record)
            hold = holds.get(profile.name) or {}
            rows.append({
                "harness": harness,
                "account": profile.name,
                "type": profile.kind,
                "where": profile.describe(),
                "active": profile.name == active_name,
                "forced": bool((active or {}).get("forced")) and profile.name == active_name,
                "ok": result.ok,
                "reason": result.reason,
                "problems": list(result.problems),
                "hold_until": hold.get("until_ts"),
                "hourly_usd": profile.hourly_usd,
                "gauges": [
                    # L71 : une fenêtre échue compte pour 0 % ; le dernier
                    # relevé reste lisible (`last_used`)
                    {"key": g.key, "used": g.used_at(now), "last_used": g.used,
                     "cap": g.pace_cap(now),
                     "elapsed": g.elapsed(now), "resets_at": g.resets_at,
                     "reset_passed": g.reset_passed(now)}
                    for g in result.gauges
                ],
                "_gauges": list(result.gauges),
            })
        _forecast(rows[len(rows) - len(items):], items, now)
    return rows


def _forecast(rows: list[dict], items: list[Profile], now: float) -> None:
    """L74 (0034 §5) : pour chaque compte, la capacité **perdue** à la prochaine
    remise à zéro de chaque fenêtre en cours si rien ne change (`losses`), son
    échéance (`expires_in_s`), et le compte que prendrait une nouvelle session
    (`next`, avec sa raison `why`)."""
    evaluations = {}
    for row, profile in zip(rows, items):
        result = Evaluation(profile, reason=row["reason"], gauges=row["_gauges"])
        evaluations[profile.name] = result
        row["losses"] = [
            {"key": g.key, "resets_at": found[0], "in_s": found[0] - now, "lost": found[1]}
            for g in row["_gauges"] for found in [g.expiring(now)] if found is not None]
        found = expiry(row["_gauges"], now)
        row["expires_in_s"] = (found[0] - now) if found is not None else None
        row["next"] = False
        row["why"] = explain(result, now) if row["ok"] else ""
    ranked = rank(items, evaluations, now)
    forced = [row for row in rows if row["forced"]]
    target = forced[0]["account"] if forced else (ranked[0].name if ranked else None)
    for row in rows:
        row["next"] = row["account"] == target and row["ok"]


def format_rows(rows: list[dict]) -> str:
    """Le tableau des comptes (texte)."""
    if not rows:
        return "aucun compte déclaré (clé `accounts` de la configuration de l'hôte)"
    lines = ["%-9s %-14s %-7s %-8s %s" % ("harnais", "compte", "actif", "état", "jauges")]
    for row in rows:
        actif = ("forcé" if row["forced"] else "oui") if row["active"] else ""
        etat = "ok" if row["ok"] else "seuil" if not row["problems"] else "invalide"
        gauges = ", ".join(
            "%s %.0f%% (rythme %.0f%%%s)" % (
                g["key"], g["used"] * 100, g["cap"] * 100,
                ", remise à zéro passée, dernier relevé %.0f%%"
                % (g.get("last_used", g["used"]) * 100) if g.get("reset_passed") else "")
            for g in row["gauges"]) or "—"
        lines.append("%-9s %-14s %-7s %-8s %s" % (
            row["harness"], row["account"], actif, etat, gauges))
        if row["reason"]:
            lines.append("%-9s %-14s %-7s %-8s ↳ %s" % ("", "", "", "", row["reason"]))
        if row.get("losses"):
            lines.append("%-9s %-14s %-7s %-8s ↳ perdu à la remise à zéro si rien ne change : %s"
                         % ("", "", "", "", ", ".join(
                             "%s %.0f %% dans %s" % (l["key"], l["lost"] * 100,
                                                     duration(l["in_s"]))
                             for l in row["losses"])))
        if row.get("next"):
            lines.append("%-9s %-14s %-7s %-8s ↳ prochain choix pour une nouvelle session : %s"
                         % ("", "", "", "", row.get("why") or "—"))
        if row.get("hold_until") and float(row["hold_until"]) > time.time():
            lines.append("%-9s %-14s %-7s %-8s ↳ retenu jusqu'à %s" % (
                "", "", "", "", time.strftime("%Y-%m-%d %H:%M",
                                              time.localtime(float(row["hold_until"])))))
    return "\n".join(lines)
