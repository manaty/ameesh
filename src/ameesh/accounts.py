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
   seuil) : parmi les comptes **utilisables** (profil valide, sous le seuil
   dans toutes leurs fenêtres), celui qui a **le plus de retard sur son
   rythme** (`lag` : le plus petit rapport utilisé / autorisé à cet instant,
   `used_at / pace_cap`, pris sur sa fenêtre la plus contraignante — L117,
   amendement de 0034 du 2026-10-10) ; un compte sans fenêtre ouverte ou sans
   relevé compte pour 0 % utilisé, donc passe en premier ; à égalité, la
   capacité inutilisée qui **expire le plus tôt** (`expiry`, la règle 0034 du
   matin), puis l'ordre déclaré ;
4. sinon pause : tous les comptes sont au seuil.

**Failover order and caps** (L226b, owner decision 2026-10-07, "primaries
last, capped"): step 3 ranks by the account's `priority` first (the harness's
default account last), the pace only breaking ties. Each window is held to
the lowest of its pace cap (or `short_window_cap` on the short window), the
account's fixed `cap` (50 % for the default account) and `overage_cap`
(80 %) when the account may be billed beyond its plan; an account in overage
is never chosen. These caps apply to continuity too (`limits`, `evaluate`).

**Authentication failure** (L226b, table `credential_health`): an account
reported `failing` (by the L226a sentinel or by a turn that failed on
authentication) is never chosen, neither by continuity nor by rank; a
harness with a plan account never falls back to a pay-per-token API key. If
no healthy account is left under its cap, `Choice.auth_blocked`: the failing
account is returned, and the runner's guard keeps the agent `auth_blocked`
until a healthy account is back.

Le seuil est celui de la garde (0019, inchangé) : `min(90 %, part écoulée +
10 points)` sur les vraies jauges **du compte**, relues à chaque choix —
Claude : les `rate_limit_event` du flux des agents, attribués par le marqueur
de compte qui précède chaque tour ; Codex : les journaux de session du
`CODEX_HOME` du compte. Une fenêtre dont `resets_at` est passé compte pour
0 % (`Gauge.used_at`, L71) : un compte sans relevé récent est essayé, pas
écarté — son premier tour rapporte sa jauge.

Chaque choix porte sa raison (`Choice.why`, « sans relevé : 0 % utilisé, le
plus en retard sur son rythme ; avant : primaire seven_day 35 % utilisé
(rythme 55 %) »), journalisée par l'exécuteur (`account_turn.choose`).

La ligne `account_active` garde le **dernier compte choisi** pour une
nouvelle session (et le forçage) ; elle change en base par
comparer-et-changer (`storage.accounts`) : deux workers qui font le même
choix n'écrivent qu'une bascule. Les retenues (`account_holds`) de 0027 §3 ne
sont plus posées : le choix par retard sur le rythme les remplace.
"""
from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from typing import Callable

from . import auth_failure, harnesses, platform, storage
from .config import NAME_RE

TYPES = ("config_dir", "api_key_env")
#: L226b (owner decision 2026-10-07, "primaries last, capped"): defaults of the
#: per-account settings `cap`, `overage_cap` and `priority`
PRIMARY_CAP = 0.50          #: the harness's default account leaves half of each window to the owner
OVERAGE_CAP = 0.80          #: an account that may be billed beyond its plan stops early
PRIMARY_PRIORITY = 100      #: the default account comes last
OTHER_PRIORITY = 50
#: `overage` setting: read from the harness's journals, or declared
OVERAGE_MODES = ("auto", "allowed", "none")
#: windows up to this length are "short" (`short_window_cap`): 5 h for both plans
SHORT_WINDOW_S = 6 * 3600.0


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
    #: L226b settings (see `limits`): rank in the failover order (lower first),
    #: fixed cap on every window, overage capability and its cap, fixed cap
    #: replacing the pace cap on the short window
    priority: int | None = None
    cap: float | None = None
    overage: str = "auto"
    overage_cap: float = OVERAGE_CAP
    short_window_cap: float | None = None

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
            source = ("file %s" % self.key_file) if self.key_file else (
                "variable %s" % self.key_env if self.key_env else "inherited variable")
            return "API key → %s (%s)" % (
                self.env or default_key_env_of(self.harness) or "?", source)
        return "dir %s" % (self.path or "default")


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
        raise AccountError("`accounts` must be an object {harness: [profiles]}")
    out: dict[str, list[Profile]] = {}
    for harness, items in raw.items():
        if harness not in harnesses.known_ids():
            raise AccountError("unknown harness in `accounts`: %r (known descriptors: %s)"
                               % (harness, ", ".join(harnesses.known_ids()) or "none"))
        if not isinstance(items, list) or not items:
            raise AccountError("`accounts.%s` must be a non-empty list" % harness)
        seen: set[str] = set()
        profiles: list[Profile] = []
        for item in items:
            if not isinstance(item, dict):
                raise AccountError("`accounts.%s`: profile is not an object" % harness)
            name = str(item.get("name") or "")
            if not NAME_RE.match(name):
                raise AccountError("`accounts.%s`: invalid account name %r" % (harness, name))
            if name in seen:
                raise AccountError("`accounts.%s`: duplicate account %r" % (harness, name))
            seen.add(name)
            kind = str(item.get("type") or "config_dir")
            if kind not in TYPES:
                raise AccountError("account %s/%s: unknown type %r (%s)"
                                   % (harness, name, kind, ", ".join(TYPES)))
            try:
                hourly = float(item.get("hourly_usd") or 0.0)
            except (TypeError, ValueError):
                raise AccountError("account %s/%s: unreadable hourly_usd" % (harness, name))
            if hourly < 0 or hourly != hourly:
                raise AccountError("account %s/%s: negative hourly_usd" % (harness, name))
            min_balance = item.get("min_balance")
            if min_balance is not None:
                try:
                    min_balance = float(min_balance)
                except (TypeError, ValueError):
                    raise AccountError("account %s/%s: unreadable min_balance" % (harness, name))
            caps = {}
            for key in ("cap", "overage_cap", "short_window_cap"):
                value = item.get(key)
                if value is None:
                    continue
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise AccountError("account %s/%s: unreadable %s" % (harness, name, key))
                if not 0 < value <= 1:
                    raise AccountError("account %s/%s: %s must be in ]0, 1]"
                                       % (harness, name, key))
                caps[key] = value
            priority = item.get("priority")
            if priority is not None and (isinstance(priority, bool)
                                         or not isinstance(priority, int)):
                raise AccountError("account %s/%s: priority must be an integer"
                                   % (harness, name))
            overage = str(item.get("overage") or "auto")
            if overage not in OVERAGE_MODES:
                raise AccountError("account %s/%s: overage must be one of %s"
                                   % (harness, name, ", ".join(OVERAGE_MODES)))
            path = str(item.get("path") or "")
            key_env = str(item.get("key_env") or "")
            key_file = str(item.get("key_file") or "")
            env = str(item.get("env") or "")
            if kind == "config_dir":
                if key_env or key_file:
                    raise AccountError("account %s/%s: key_env/key_file are for api_key_env only"
                                       % (harness, name))
                if path and not os.path.isabs(os.path.expanduser(path)):
                    raise AccountError("account %s/%s: absolute path expected" % (harness, name))
            else:
                if path:
                    raise AccountError("account %s/%s: path is for config_dir only"
                                       % (harness, name))
                if bool(key_env) == bool(key_file):
                    raise AccountError("account %s/%s: exactly one of key_env or key_file"
                                       % (harness, name))
                if not (env or default_key_env_of(harness)):
                    raise AccountError("account %s/%s: `env` variable expected" % (harness, name))
                if key_file and not os.path.isabs(os.path.expanduser(key_file)):
                    raise AccountError("account %s/%s: absolute path expected" % (harness, name))
            profiles.append(Profile(
                harness=harness, name=name, kind=kind,
                path=_expand(path) if path else "",
                env=env or (default_key_env_of(harness) if kind == "api_key_env" else ""),
                key_env=key_env, key_file=_expand(key_file) if key_file else "",
                hourly_usd=hourly, min_balance=min_balance,
                check_credentials=bool(item.get("check_credentials", True)),
                priority=priority, overage=overage, **caps,
            ))
        out[harness] = profiles
    return out


def profiles(cfg, harness: str) -> list[Profile]:
    """Les comptes déclarés pour ce harnais (liste vide : aucun, comportement d'avant L30)."""
    return parse(getattr(cfg, "accounts", None) or {}).get(harness, [])


def homes(cfg, harness: str) -> list[str]:
    """L95 : les dossiers de configuration des comptes déclarés du harnais
    (où vivent leurs journaux de session) ; vide sans comptes ou si la
    configuration est invalide — jamais une erreur pour un lecteur."""
    try:
        return [p.home() for p in profiles(cfg, harness) if p.kind == "config_dir"]
    except (AccountError, OSError):
        return []


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
            return ["missing directory: %s" % profile.path]
        if not stat.S_ISDIR(info.st_mode):
            return ["not a directory: %s" % profile.path]
        if uid is not None and info.st_uid != uid:
            problems.append("directory owned by another user")
        if info.st_mode & 0o077:
            problems.append("directory not private (%o, expected 700)" % (info.st_mode & 0o777))
        cred = credentials_of(profile.harness)
        if cred and profile.check_credentials and not platform.is_macos():
            if not os.path.isfile(os.path.join(profile.path, cred)):
                problems.append("credentials missing (%s): a human must log in" % cred)
        return problems
    if profile.key_file:
        try:
            info = os.lstat(profile.key_file)
        except OSError:
            return ["missing key file: %s" % profile.key_file]
        if not stat.S_ISREG(info.st_mode):
            return ["key file is not a regular file: %s" % profile.key_file]
        if uid is not None and info.st_uid != uid:
            problems.append("key file owned by another user")
        if info.st_mode & 0o177:
            problems.append("key file too permissive (%o, expected 600)"
                            % (info.st_mode & 0o777))
        return problems
    if not environ.get(profile.key_env):
        problems.append("missing key variable: %s" % profile.key_env)
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
        raise AccountError("account %s/%s unusable: %s"
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
            raise AccountError("account %s/%s: unreadable key file (%s)"
                               % (profile.harness, profile.name, exc.strerror))
    else:
        key = str(environ.get(profile.key_env) or "").strip()
    if not key:
        raise AccountError("account %s/%s: empty key" % (profile.harness, profile.name))
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
    #: L226b: {gauge key: (cap, label)}, the cap each window is held to
    limits: dict = field(default_factory=dict)
    #: L226b: held back by an absolute rule (in overage, past a fixed cap),
    #: not only by its pace: no budget setting lifts it
    hard: bool = False

    @property
    def ok(self) -> bool:
        return not self.reason

    @property
    def unpaced_ok(self) -> bool:
        """Usable when the pace does not hold (budget guard off, L30): only
        the absolute rules of L226b keep the account out."""
        return not self.hard


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


def account_cap(profile: Profile) -> float | None:
    """The fixed cap on every window of the account (L226b): its `cap`
    setting, or `PRIMARY_CAP` for the harness's default account (~/.claude,
    ~/.codex: the owner's own), none otherwise."""
    if profile.cap is not None:
        return profile.cap
    return PRIMARY_CAP if profile.default else None


def priority(profile: Profile) -> int:
    """The account's rank in the failover order (L226b), lower first: its
    `priority` setting, or the default account last."""
    if profile.priority is not None:
        return profile.priority
    return PRIMARY_PRIORITY if profile.default else OTHER_PRIORITY


def overage_capable(profile: Profile, overage) -> bool:
    """May the account be billed beyond its plan? Its `overage` setting, or
    (`auto`) what its harness last reported; unknown counts as no."""
    if profile.overage != "auto":
        return profile.overage == "allowed"
    return bool(overage is not None and overage.capable)


def fixed_caps(profile: Profile, gauges, *, critical: bool = False) -> list[tuple[float, str]]:
    """[(cap, label)]: the caps that hold on every window of the account
    whatever its pace (L226b): its fixed cap (`account_cap`), and
    `overage_cap` when it may be billed beyond its plan.

    `critical` (L242b, owner decision 2026-10-12 10:00): the implicit 50 %
    cap of the default account does not hold for a critical agent; a `cap`
    set in the configuration and `overage_cap` still do."""
    fixed = []
    cap = account_cap(profile)
    if critical and profile.cap is None:
        cap = None
    if cap is not None:
        fixed.append((cap, "primary cap" if profile.cap is None else "account cap"))
    if overage_capable(profile, getattr(gauges, "overage", None)):
        fixed.append((profile.overage_cap, "overage cap"))
    return fixed


def limits(profile: Profile, gauges, now: float, *, critical: bool = False) -> dict:
    """{gauge key: (cap, label)}: the usage each window of the account is held
    to (L226b), the lowest of
    * the pace cap of the guard (0019), or on the short window the account's
      `short_window_cap` when set (it replaces the pace cap there);
    * the account's fixed cap (`account_cap`: 50 % for the primary);
    * `overage_cap` (80 %) when the account may be billed beyond its plan.

    `critical` (L242b): see `fixed_caps`.
    """
    fixed = fixed_caps(profile, gauges, critical=critical)
    out = {}
    for gauge in gauges:
        if profile.short_window_cap is not None and gauge.window_s <= SHORT_WINDOW_S:
            base = (profile.short_window_cap, "short window cap")
        else:
            base = (gauge.pace_cap(now), "pace cap")
        out[gauge.key] = min([base, *fixed], key=lambda pair: pair[0])
    return out


def evaluate(book, profile: Profile, items: list[Profile], now: float,
             environ=None, *, record: bool = True, critical: bool = False) -> Evaluation:
    """Le compte est-il sous le seuil de la garde (0019) ? Ne lit aucun secret.

    `record=False` (simulation, L39) : l'historique des jauges n'est pas écrit.
    `critical` (L242b) : pour un agent critique, sans le plafond implicite de
    50 % du compte par défaut (`fixed_caps`)."""
    result = Evaluation(profile)
    result.problems = check(profile, environ)
    if result.problems:
        result.reason = "unusable profile: %s" % "; ".join(result.problems)
        return result
    result.gauges = gauges_of(book, profile, items)
    if result.gauges and record:
        # historique des jauges de L26, attribué au compte (migration 0028)
        book.record_gauges(result.gauges, account=profile.name)
    if in_overage(result.gauges, now):
        # L226b: an account in overage is never chosen (no spending beyond the plan)
        result.reason = "%s plan: in overage (billed beyond the plan)" % profile.harness
        result.hard = True
        # eligible again once the windows that ran out have reset
        spent = result.gauges.spent()
        resets = [g.resets_at for g in spent if g.resets_at]
        # (none dated, or a reading without windows: no date, never a guess)
        result.until = max(resets) if resets and len(resets) == len(spent) else None
        return result
    result.limits = limits(profile, result.gauges, now, critical=critical)
    depasses = []
    for gauge in result.gauges:
        # fenêtre remise à zéro depuis le relevé : vierge (L71 : `used_at`
        # compte alors 0 %)
        if gauge.used_at(now) >= result.limits[gauge.key][0]:
            depasses.append(gauge)
    if depasses:
        fixed = fixed_caps(profile, result.gauges, critical=critical)
        hard = [(g, pair) for g in depasses for pair in fixed if g.used_at(now) >= pair[0]]
        result.hard = bool(hard)
        # an absolute cap first in the reason: the pace may be lower, it is
        # not what holds the account
        gauge, (cap, label) = hard[0] if hard else (depasses[0],
                                                    result.limits[depasses[0].key])
        if label == "pace cap":
            result.reason = ("plan %s %s: %.0f%% used with %.0f%% of the window elapsed "
                             "(pace cap %.0f%%)" % (
                                 profile.harness, gauge.key, gauge.used * 100,
                                 gauge.elapsed(now) * 100, cap * 100))
        else:
            result.reason = "%s plan %s: %.0f%% used (%s %.0f%%)" % (
                profile.harness, gauge.key, gauge.used * 100, label, cap * 100)
        resets = [g.resets_at for g in depasses if g.resets_at]
        result.until = max(resets) if len(resets) == len(depasses) else None
        return result
    if profile.hourly_usd > 0 and book.db is not None:
        spent = book.spent("all", 3600, account=profile.name, harnesses=[profile.harness])
        if spent >= profile.hourly_usd:
            result.reason = ("account hourly cap: %.2f $ over 60 min (cap %.2f $)"
                             % (spent, profile.hourly_usd))
            result.until = now + 3600.0
            return result
    if profile.min_balance is not None and book.db is not None:
        solde = latest_balance(book.db, profile)
        if solde is not None and float(solde["total"]) <= profile.min_balance:
            result.reason = ("account balance: %.4f %s (threshold %.4f)"
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
    #: L226b : aucun compte sain sous son seuil — `profile` est un compte en
    #: échec d'authentification, que la garde de l'exécuteur ne lance pas
    #: (l'agent reste `auth_blocked`)
    auth_blocked: bool = False
    #: L242b : choix propre à un agent critique — le dernier choix partagé
    #: (`account_active`) n'a pas bougé, aucun forçage n'a été levé
    per_agent: bool = False


def _label(evaluations: dict) -> str:
    return "; ".join("%s: %s" % (name, ev.reason) for name, ev in evaluations.items())


def duration(seconds: float) -> str:
    """Une durée lisible : « 52 min », « 3 h 10 », « 2 j 4 h »."""
    minutes = max(0, int(round(seconds / 60.0)))
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%d h %02d" % (hours, minutes) if minutes else "%d h" % hours
    days, hours = divmod(hours, 24)
    return "%d d %d h" % (days, hours) if hours else "%d d" % days


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


def lag(gauges, now: float, caps: dict | None = None) -> tuple[float, object | None]:
    """Le retard du compte sur son rythme (L117, amendement 0034 du 2026-10-10) :
    `(rapport, jauge)` de sa fenêtre **la plus contraignante**, où le rapport
    est l'utilisé valable à `now` sur l'autorisé à cet instant de la fenêtre
    (`used_at / pace_cap`, le même plafond que la garde 0019). Plus il est
    petit, plus le compte est en retard. Un compte sans jauge, ou dont toutes
    les fenêtres sont échues, vaut 0 : il passe en premier.

    La fenêtre la plus contraignante plutôt que la plus longue : c'est la
    distance réelle du compte à la garde (qui l'écarte dès qu'UNE fenêtre
    atteint 1), comme 0034 §1 le retenait déjà ; quand la fenêtre de 7 jours
    est la plus en avance — le cas où un forfait hebdomadaire se perd — c'est
    elle qui décide, et une session neuve n'est pas envoyée sur un compte dont
    la fenêtre de 5 h est presque au seuil.

    L226b: `caps` ({gauge key: (cap, label)}, `Evaluation.limits`) replaces
    the pace cap by the cap the window is actually held to.
    """
    best: tuple[float, object | None] = (0.0, None)
    for gauge in gauges:
        cap = caps[gauge.key][0] if caps and gauge.key in caps else gauge.pace_cap(now)
        ratio = gauge.used_at(now) / cap if cap > 0 else 1.0
        if best[1] is None or ratio > best[0]:
            best = (ratio, gauge)
    return best


def explain(result: Evaluation, now: float) -> str:
    """Le retard d'un compte sur son rythme, lisible : « seven_day 35 %
    utilisé (rythme 55 %) », « sans relevé : 0 % utilisé »."""
    _ratio, gauge = lag(result.gauges, now, result.limits)
    if gauge is None:
        return "no reading: 0% used"
    if all(g.reset_passed(now) for g in result.gauges):
        return "stale reading (window reset): 0% used"
    cap, label = result.limits.get(gauge.key) or (gauge.pace_cap(now), "pace cap")
    bound = ("pace %.0f%%" % (cap * 100)) if label == "pace cap" \
        else "%s %.0f%%" % (label, cap * 100)
    if not gauge.resets_at:
        return "%s %.0f%% used (undated gauge, %s)" % (
            gauge.key, gauge.used_at(now) * 100, bound)
    return "%s %.0f%% used (%s)" % (gauge.key, gauge.used_at(now) * 100, bound)


def rank(items: list[Profile], evaluations: dict, now: float, *,
         paced: bool = True) -> list[Profile]:
    """Les comptes utilisables, du premier à consommer au dernier (0034 amendée).

    Clé : L226b, the failover order first (`priority`: the default account
    last, owner decision 2026-10-07), then le retard sur le rythme (`lag`, le
    plus petit rapport d'abord), puis l'échéance de la capacité inutilisée (la
    plus proche d'abord ; sans fenêtre en cours, après), puis l'ordre déclaré.

    `paced=False` (garde de budget désactivée) : un compte au seuil de son
    rythme reste utilisable, jamais un compte écarté par une règle absolue
    (`Evaluation.hard`)."""
    def key(pair):
        index, profile = pair
        result = evaluations[profile.name]
        gauges = result.gauges
        found = expiry(gauges, now)
        return (priority(profile), round(lag(gauges, now, result.limits)[0], 6),
                found[0] if found is not None else float("inf"), index)
    usable = [(i, p) for i, p in enumerate(items)
              if (evaluations[p.name].ok if paced else evaluations[p.name].unpaced_ok)]
    return [p for _i, p in sorted(usable, key=key)]


def reason_of(ranked: list[Profile], evaluations: dict, now: float) -> str:
    """La raison du choix d'une nouvelle session (journal, `accounts list`) :
    « sans relevé : 0 % utilisé, le plus en retard sur son rythme ; avant :
    primaire seven_day 35 % utilisé (rythme 55 %) »."""
    first = ranked[0]
    if len(ranked) > 1 and priority(first) < priority(ranked[1]):
        # L226b: the failover order decided, not the pace
        why = "%s, first in the failover order (priority %d)" % (
            explain(evaluations[first.name], now), priority(first))
    else:
        why = "%s, furthest behind its pace" % explain(evaluations[first.name], now)
    if len(ranked) > 1:
        why += "; ahead of: %s" % ", ".join(
            "%s %s" % (p.name, explain(evaluations[p.name], now)) for p in ranked[1:])
    return why


def choose(db, host: str, harness: str, items: list[Profile], book, *,
           now: float | None = None, agent: str | None = None, environ=None,
           simulate: bool = False, session_account: str | None = None,
           paced: bool = True, critical: bool = False) -> Choice:
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

    `paced=False` (garde de budget désactivée, L30) : quand aucun compte
    n'est sous son seuil, le rythme est levé — mais jamais les règles
    absolues de L226b (overage, plafonds fixes : décision du propriétaire du
    2026-10-07). Aucun compte hors de ces règles : pause, comme avec la garde.

    `critical` (L242b, agent critique : orchestrateur, conception, auditeur) :
    le choix est propre à l'agent — il n'écrit jamais le dernier choix
    partagé et ne lève aucun forçage (un forçage au seuil ne le retient pas
    s'il reste un compte) ; sans autre compte, le compte par défaut sert
    au-delà de son plafond implicite de 50 % (jamais en overage, rythme et
    `overage_cap` tenus), en dernier.
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
                     reason="account %r removed from the configuration" % active_name,
                     agent=agent)
        row = store.active(host, harness) or {"account": items[0].name, "forced": False}
        active_name = row.get("account") if row.get("account") in names else items[0].name
    active = by_name(items, active_name) or items[0]
    # L226b : un compte en échec d'authentification n'est jamais choisi
    sick = auth_failure.failing(db, host, harness)
    candidates = healthy(items, sick)

    # 1. forçage manuel : il prime, même sur la continuité (un compte forcé en
    # échec d'authentification est rendu tel quel : la garde de l'exécuteur
    # bloque l'agent, elle ne bascule pas contre un geste humain)
    def forced(profile: Profile) -> Choice:
        current = ev(profile)
        if current.ok or (not paced and current.unpaced_ok):
            return Choice(profile, profile, forced=True,
                          why="forced (ameesh accounts use)")
        return Choice(None, profile, reason="account %s forced (ameesh accounts use): %s"
                      % (profile.name, current.reason), forced=True)

    if row.get("forced"):
        held = forced(active)
        if held.profile is not None:
            return held
        # L226b (bureau, 2026-10-12): a forced account at its threshold does
        # not hold the agent while another healthy account is under its own —
        # the force is lifted (journaled `auto`) and the automatic choice runs
        free = [p for p in candidates if p.name != active.name
                and (ev(p).ok if paced else ev(p).unpaced_ok)]
        if critical:
            pass        # L242b : choix propre à l'agent, le forçage reste aux autres
        elif not free:
            return held
        elif not simulate and not store.set_auto(
                host, harness, expected=active.name,
                reason="forced account %s at threshold (%s); %s free"
                % (active.name, cache[active.name].reason, free[0].name)):
            # #295: the row changed since it was read — a human forced another
            # account meanwhile: that force holds (never lifted in the same turn)
            row = store.active(host, harness) or {}
            now_forced = by_name(items, row.get("account"))
            if row.get("forced") and now_forced is not None:
                return forced(now_forced)

    # 2. continuité : la session garde son compte tant qu'il est sous son seuil
    # (les autres comptes ne sont alors pas relus : rien à choisir)
    own = by_name(items, session_account) if session_account else None
    if own is not None and own in candidates and ev(own).ok:
        return Choice(own, active, kept=True,
                      why="continuity: the session stays on %s, under its threshold" % own.name)
    for profile in items:
        ev(profile)
    # 3. le compte le plus en retard sur son rythme (amendement 0034, L117)
    ranked = rank(candidates, cache, now)
    if not ranked and not paced:
        # L226b: past its pace a healthy account stays usable, never past an
        # absolute rule
        ranked = rank(candidates, cache, now, paced=False)
    beyond = False
    if not ranked and critical:
        # L242b : le compte par défaut au-delà de son plafond de 50 %, pour un
        # agent critique seulement, en dernier (jamais en overage)
        relaxed = dict(cache)
        for profile in candidates:
            if profile.default and profile.cap is None:
                relaxed[profile.name] = evaluate(book, profile, items, now, environ,
                                                 record=False, critical=True)
        ranked = rank(candidates, relaxed, now, paced=paced) or (
            [] if paced else rank(candidates, relaxed, now, paced=False))
        if ranked:
            beyond = True
            cache = relaxed
    if not ranked:
        blocked = [p for p in items if p.name in sick
                   and (cache[p.name].ok if paced else cache[p.name].unpaced_ok)]
        if blocked:
            # L226b : des comptes sous leur seuil, mais aucun sain — jamais de
            # repli payé en plus : le compte en panne est rendu, la garde de
            # l'exécuteur tient l'agent en `auth_blocked` et l'alerte part
            stuck = active if active in blocked else blocked[0]
            return Choice(stuck, active, auth_blocked=True,
                          why="no healthy %s account: %s" % (
                              harness, "; ".join("%s: %s" % (p.name, sick[p.name].get(
                                  "reason") or "authentication failing")
                                  for p in items if p.name in sick)))
        return Choice(None, active, reason="all %s accounts at threshold — %s"
                      % (harness, _label(cache)))
    target = ranked[0]
    why = reason_of(ranked, cache, now)
    if beyond:
        why = ("critical agent: %s beyond its %.0f%% cap, every other account at "
               "threshold (owner decision 2026-10-12); %s" % (
                   target.name, PRIMARY_CAP * 100, why))
    left = own if own is not None else (active if target.name != active.name else None)
    if left is not None and left.name in sick:
        why = "%s authentication failing (%s); %s" % (
            left.name, sick[left.name].get("reason") or "?", why)
    elif left is not None and not cache[left.name].ok:
        why = "%s at threshold (%s); %s" % (left.name, cache[left.name].reason, why)
    if critical:
        # L242b : jamais le dernier choix partagé ; la bascule est celle de
        # l'agent (depuis le compte de sa session, sinon le choix partagé)
        origin = own if own is not None else active
        moved = None
        if target.name != origin.name:
            moved = {"from": origin.name, "to": target.name, "kind": "agent", "reason": why}
        return Choice(target, active, switched=moved, why=why, per_agent=True)
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
    # forçage posé entre-temps prime, évalué comme en 1 (L226b : un primaire
    # au-dessus de son plafond ou un compte en overage n'est pas lancé)
    fresh = store.active(host, harness) or {}
    if fresh.get("forced"):
        return forced(by_name(items, fresh.get("account")) or active)
    return Choice(target, by_name(items, fresh.get("account")) or target, why=why)


def healthy(items: list[Profile], sick: dict) -> list[Profile]:
    """The candidate accounts of an automatic choice (L226b): never one in
    authentication failure, and never a pay-per-token API key for a harness
    that has a plan account (owner decision 2026-10-07: no spending beyond
    the plans; an API key stays usable by `ameesh accounts use`)."""
    sains = [p for p in items if p.name not in sick]
    if any(p.kind != "api_key_env" for p in items):
        sains = [p for p in sains if p.kind != "api_key_env"]
    return sains


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
        raise AccountError("unknown account for %s: %r (known: %s)"
                           % (harness, name, ", ".join(p.name for p in items)))
    problems = check(profile)
    if problems:
        raise AccountError("account %s/%s unusable: %s"
                           % (harness, name, "; ".join(problems)))
    store = storage.of(db).accounts
    store.init_active(host, harness, items[0].name)
    changed = store.switch(host, harness, expected=None, to=name, kind="manuel",
                           reason="manual override by %s" % by, agent=None, forced=True)
    if changed:
        store.release(host, harness, name)
    return changed


def automatic(db, host: str, harness: str, *, by: str) -> bool:
    """Rend la main à l'automate (journalisé `auto`)."""
    return storage.of(db).accounts.set_auto(host, harness,
                                            reason="back to automatic by %s" % by)


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
                # L226b: failover order and overage flags
                "priority": priority(profile),
                "overage": _overage_row(profile, result.gauges, now),
                "gauges": [
                    # L71 : une fenêtre échue compte pour 0 % ; le dernier
                    # relevé reste lisible (`last_used`)
                    {"key": g.key, "used": g.used_at(now), "last_used": g.used,
                     "cap": (result.limits.get(g.key) or (g.pace_cap(now),))[0],
                     "elapsed": g.elapsed(now), "resets_at": g.resets_at,
                     "reset_passed": g.reset_passed(now)}
                    for g in result.gauges
                ],
                "_gauges": list(result.gauges),
            })
        _forecast(rows[len(rows) - len(items):], items, now)
    return rows


def in_overage(gauges, now: float) -> bool:
    """Is the account in overage at `now` (a plain list of gauges: no)?"""
    return bool(getattr(gauges, "in_overage", None) and gauges.in_overage(now))


def _overage_row(profile: Profile, gauges, now: float) -> dict:
    """The overage flags of an account, for `ameesh accounts list --json`."""
    read = getattr(gauges, "overage", None)
    return {"setting": profile.overage, "capable": overage_capable(profile, read),
            "in_use": in_overage(gauges, now)}


def _forecast(rows: list[dict], items: list[Profile], now: float) -> None:
    """L74 (0034 §5) : pour chaque compte, la capacité **perdue** à la prochaine
    remise à zéro de chaque fenêtre en cours si rien ne change (`losses`), son
    échéance (`expires_in_s`), et le compte que prendrait une nouvelle session
    (`next`, avec sa raison `why`, la même que celle du choix — L117)."""
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
        row["why"] = ""
    ranked = rank(items, evaluations, now)
    forced = [row for row in rows if row["forced"]]
    target = forced[0]["account"] if forced else (ranked[0].name if ranked else None)
    for row in rows:
        row["next"] = row["account"] == target and row["ok"]
        if row["next"]:
            row["why"] = ("forced (ameesh accounts use)" if forced
                          else reason_of(ranked, evaluations, now))


def format_rows(rows: list[dict]) -> str:
    """Le tableau des comptes (texte)."""
    if not rows:
        return "no account declared (`accounts` key of the host configuration)"
    lines = ["%-9s %-14s %-7s %-8s %s" % ("harness", "account", "active", "state", "gauges")]
    for row in rows:
        actif = ("forced" if row["forced"] else "yes") if row["active"] else ""
        etat = "ok" if row["ok"] else "limit" if not row["problems"] else "invalid"
        gauges = ", ".join(
            "%s %.0f%% (pace %.0f%%%s)" % (
                g["key"], g["used"] * 100, g["cap"] * 100,
                ", reset passed, last reading %.0f%%"
                % (g.get("last_used", g["used"]) * 100) if g.get("reset_passed") else "")
            for g in row["gauges"]) or "—"
        lines.append("%-9s %-14s %-7s %-8s %s" % (
            row["harness"], row["account"], actif, etat, gauges))
        if row["reason"]:
            lines.append("%-9s %-14s %-7s %-8s ↳ %s" % ("", "", "", "", row["reason"]))
        if row.get("losses"):
            lines.append("%-9s %-14s %-7s %-8s ↳ lost at reset if nothing changes: %s"
                         % ("", "", "", "", ", ".join(
                             "%s %.0f%% in %s" % (l["key"], l["lost"] * 100,
                                                     duration(l["in_s"]))
                             for l in row["losses"])))
        if row.get("next"):
            lines.append("%-9s %-14s %-7s %-8s ↳ next choice for a new session: %s"
                         % ("", "", "", "", row.get("why") or "—"))
        if row.get("hold_until") and float(row["hold_until"]) > time.time():
            lines.append("%-9s %-14s %-7s %-8s ↳ held until %s" % (
                "", "", "", "", time.strftime("%Y-%m-%d %H:%M",
                                              time.localtime(float(row["hold_until"])))))
    return "\n".join(lines)
