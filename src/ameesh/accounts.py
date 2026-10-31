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
  (DeepSeek Harness). Sans `path`, c'est le **dossier par défaut du harnais**
  (`~/.claude`, `~/.codex`, `~/.dsh`) — jamais celui que l'environnement de
  l'exécuteur désigne : un compte déclaré n'hérite pas du compte ambiant
  (L1078), sans quoi deux profils pouvaient se disputer le même dossier et
  les mêmes jauges. Ses jauges y sont lues. Un dossier
  déclaré doit appartenir à l'utilisateur et être privé (0700) ; ses
  identifiants doivent être présents (présence seulement : ameesh ne lit
  jamais leur contenu). Un profil synthétique (hors configuration, nom
  `(default)` : session d'avant L30, hôte sans comptes déclarés) garde, lui,
  le dossier de l'environnement de l'exécuteur.
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
4. sinon pause : tous les comptes sont au seuil, ou écartés faute d'un relevé
   assez récent pour placer (voir « Fraîcheur du relevé »).

**Fraîcheur du relevé** (#1561) : un relevé **périmé** — une fenêtre datée
s'est remise à zéro depuis la lecture (typiquement la fenêtre de 5 h) alors
qu'une autre court encore (les 7 jours) — n'est plus un instantané courant de
l'état du compte : la fenêtre encore ouverte n'en est qu'une borne
inférieure, le compte a pu servir depuis. Un tel compte est **non vérifié** :
il ne passe jamais avant un compte à relevé courant, et sa raison le dit
(`explain`) au lieu d'annoncer un « 30 % utilisé » trompeur ; il reste
**essayé** faute de mieux (L71), son premier tour rapportant sa jauge.
#1672 **borne cet essai** : un relevé plus vieux que ce qui reste de
**chaque** fenêtre encore ouverte est une information trop ancienne pour
engager un tour — prudence, pas preuve d'un quota atteint —, le compte est
écarté du placement (`rank`) et, sans compte plaçable, le choix **pause** ; la
remise à zéro qui motive la pause le rouvre (L71, 0 %). Le 15/10, le choix
automatique a pris un compte Claude à sa limite hebdomadaire sur la foi d'un
relevé de 30 % vieux de quatre jours. La **continuité** garde son chemin —
une session déjà sur le compte y reste, c'est son tour qui rapporte la jauge —,
et un forçage manuel reste souverain. Un relevé **absent** garde la règle
L117 : un compte jamais utilisé compte pour 0 % et passe en premier, pour ne
pas laisser un forfait inutilisé.
Le relevé porte son horodatage (`Gauge.read_at`) : marqueur de compte Claude,
date du journal de session Codex.

**Failover order and caps** (L226b, owner decision 2026-10-07, "primaries
last, capped"): step 3 ranks by the account's `priority` first (the harness's
default account last), the pace only breaking ties. Each window is held to
the lowest of its pace cap (or `short_window_cap` on the short window), the
account's fixed `cap` (50 % for the default account) and `overage_cap`
(80 %) when the account may be billed beyond its plan; an account in overage
is never chosen. These caps apply to continuity too (`limits`, `evaluate`).

**Plafond de rythme par compte et fin de fenêtre** (#1176, décision de
l'owner du 2026-10-14) : `pace_ceiling` remplace les 90 % en dur, et
`pace_ceiling_end` relève le plafond dans les `pace_ceiling_end_hours`
dernières heures avant la remise à zéro d'une fenêtre — **seulement** quand
l'overage y est impossible, prouvé par un relevé frais (`overage_impossible`) :
un compte dont l'overage est possible, inconnu ou dont la preuve est périmée
garde son plafond de base (et `overage_cap` s'il peut être facturé). Le refus
d'overage et la coupure d'un tour sur `isUsingOverage` sont inchangés.

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
0 % (`Gauge.used_at`, L71) ; une fois **toutes** les fenêtres datées remises à
zéro — et aucune fenêtre non datée encore ouverte, qui ne prouve pas sa remise
à zéro (#1566 N2) — le relevé redevient courant (0 %) et le compte replaçable.
Un relevé seulement **partiel** ne place plus de session quand il est plus
vieux que toute fenêtre encore ouverte (#1672, « Fraîcheur du relevé ») ; en
deçà, s'il reste **informateur** (`_informative_partial`, #1566 N1), il
concourt avec les relevés courants et peut passer devant eux.

Chaque choix porte sa raison (`Choice.why`, « no reading: unverified, le plus
en retard sur son rythme ; avant : primaire seven_day 35 % utilisé
(rythme 55 %) »), journalisée par l'exécuteur (`account_turn.choose`).

La ligne `account_active` garde le **dernier compte choisi** pour une
nouvelle session (et le forçage) ; elle change en base par
comparer-et-changer (`storage.accounts`) : deux workers qui font le même
choix n'écrivent qu'une bascule. Les retenues de rythme (`account_holds`) de
0027 §3 ne sont plus posées : le choix par retard sur le rythme les remplace ;
la table sert désormais à publier pour tout l'hôte l'**overage** d'un compte
qu'une bascule vient de quitter (#1883, `OVERAGE_HOLD_S`) — une règle absolue
que les autres runners ne peuvent pas déduire de leur propre lecture quand
deux configurations lisent deux dossiers sous le même nom de compte.
"""
from __future__ import annotations

import getpass
import hashlib
import math
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field, replace
from typing import Callable, NamedTuple

from . import auth_failure, harnesses, platform, storage
from .config import NAME_RE
from .cost import PACE_CEILING, reading_outdated, stale_reading

#: #1469: a billing account id (`openai-manaty`), the same on every host
BILLING_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

TYPES = ("config_dir", "api_key_env")
#: L226b (owner decision 2026-10-07, "primaries last, capped"): defaults of the
#: per-account settings `cap`, `overage_cap` and `priority`
PRIMARY_CAP = 0.50          #: the harness's default account leaves half of each window to the owner
#: L242b (2): a non-critical agent stops this much below the pace cap, so that
#: the critical agents keep the margin up to it
RESERVE = 0.10
OVERAGE_CAP = 0.80          #: an account that may be billed beyond its plan stops early
PRIMARY_PRIORITY = 100      #: the default account comes last
OTHER_PRIORITY = 50
#: #1883 : durée d'une retenue d'overage publiée pour l'hôte. Quand un runner
#: QUITTE un compte en dépassement, il publie ce fait en base
#: (`account_holds`) : les autres processus du même hôte écartent le compte —
#: leur configuration peut lire un autre dossier sous le même nom (le 16/10,
#: deux définitions de « secondaire » lisaient, l'une son dépassement,
#: l'autre un compte à 34 %, et le choix partagé basculait en boucle). La
#: retenue est datée : une lecture isolée n'immobilise pas le compte au-delà
#: du délai, et une lecture locale saine n'est jamais bloquée par une retenue
#: qu'aucune bascule n'a posée.
OVERAGE_HOLD_S = 600.0
#: #3755 : durée par défaut d'un forçage manuel (`ameesh accounts use --for`).
#: Un forçage retire le choix automatique : sans échéance il reste en place
#: jusqu'à ce qu'on pense à le lever (le Codex secondaire de pc-smichea,
#: forcé le 26/10/2026, est resté en dépassement ~2 h). Court par défaut : le
#: geste « une fois » rend la main à l'automate au terme.
DEFAULT_FORCE_S = 3600.0
#: Raison journalisée quand un forçage arrive à son échéance (`kind` `auto`).
FORCE_EXPIRED_REASON = "forcing expired (--for): back to the automatic choice"
#: `overage` setting: read from the harness's journals, or declared
OVERAGE_MODES = ("auto", "allowed", "none")
#: windows up to this length are "short" (`short_window_cap`): 5 h for both plans
SHORT_WINDOW_S = 6 * 3600.0
#: #1176 : relèvement de fin de fenêtre. Dans les `pace_ceiling_end_hours`
#: dernières heures avant `resets_at`, le plafond de rythme d'un compte dont
#: l'overage est **impossible** (prouvé par un relevé frais, voir
#: `overage_impossible`) monte jusqu'à `pace_ceiling_end`. Sans
#: `pace_ceiling_end` déclaré, aucun relèvement : le plafond de base tient.
PACE_CEILING_END_HOURS = 6.0
#: Raison affichée du plafond effectif (`accounts list`) quand il est relevé.
CEILING_REASON_END = "end of window, overage impossible"


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


def keychain_service_of(harness: str) -> str:
    """Service du trousseau macOS qui porte les identifiants du harnais (L65).

    Vide : le harnais range ses identifiants dans son dossier (Codex), et le
    fichier de `credentials_of` reste la référence.
    """
    return _account_key(harness, "keychain_service")


def session_store_of(harness: str) -> str:
    """Sous-dossier où le harnais range ses sessions."""
    return _account_key(harness, "session_store")


def default_key_env_of(harness: str) -> str:
    """Variable de clé d'API lue par le harnais, par défaut."""
    return _account_key(harness, "key_env")


def _all_account_keys(key: str) -> set[str]:
    return {value for value in (
        _account_key(ident, key) for ident in harnesses.known_ids()) if value}


def _expand_home(path: str, environ=None) -> str:
    """Résout `~` avec le HOME de `environ` (l'environnement de l'exécuteur),
    sinon celui du processus : un dossier de compte se lit dans *cet*
    environnement, et un `~` n'y désigne pas le même utilisateur."""
    environ = os.environ if environ is None else environ
    home = environ.get("HOME") or ""
    if home and (path == "~" or path.startswith("~/")):
        if path == "~":
            resolved = home
        elif home == "/":
            resolved = "/" + path[2:]
        else:
            resolved = home.rstrip("/") + path[1:]
        return os.path.abspath(resolved)
    return os.path.abspath(os.path.expanduser(str(path)))


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
    #: #1075: multiplies the elapsed share of the pace cap (1.0 = nominal
    #: pace). #3647: compiled from the canon (`accounts.pace_multipliers` of
    #: `processus/mecanique.yaml`, `with_canon_pace`), identical on every host;
    #: the host's `accounts` key only applies, signalled, while the canon
    #: declares no such block or cannot be read.
    pace_multiplier: float = 1.0
    #: #3647: the multiplier declared by the host's configuration (None = not
    #: declared), kept to signal a local setting that differs from the canon
    pace_local: float | None = None
    #: #3647: where `pace_multiplier` comes from: `canon`, or `local` (canon
    #: block absent or unreadable: legacy setting, signalled)
    pace_source: str = "local"
    #: #3647 (owner decision #141793 a): the forecast loss at reset above which
    #: a window's pace is raised (`underused_floor`); None = no raise
    underused_loss: float | None = None
    #: #1469: the provider billing account this profile spends from, shared by
    #: every key, profile name, agent and host that bills it (`account_budget`);
    #: empty = no daily account envelope applies
    billing_account: str = ""
    #: #1469: dollars reserved before each turn on that billing account (the
    #: bound of one turn), settled at the real cost when the turn ends
    reserve_usd: float = 2.0
    #: #3779: provider short-window admission of this account. `None` = the
    #: harness default (`account_window`); an explicit integer caps the
    #: account's concurrent turns (0 disables the cap), an explicit number of
    #: seconds spaces two turn starts (0 disables the gap). The start gap is
    #: what the observed bursts needed: five turn starts in 90 s produced 429s
    #: even with one active turn.
    max_concurrent: int | None = None
    min_start_gap_s: float | None = None
    #: #1176: the account's own pace ceiling (default `cost.PACE_CEILING`),
    #: and its end-of-window raise. Within `pace_ceiling_end_hours` of a
    #: window's `resets_at`, the ceiling rises to `pace_ceiling_end` when the
    #: account's overage is impossible (fresh proof, `overage_impossible`);
    #: `None` = no raise, the base ceiling always holds.
    pace_ceiling: float = PACE_CEILING
    pace_ceiling_end: float | None = None
    pace_ceiling_end_hours: float = PACE_CEILING_END_HOURS
    #: Vrai pour un profil issu de la configuration de l'hôte (`parse`) : un
    #: profil déclaré sans `path` désigne le dossier par défaut du harnais, pas
    #: celui de l'environnement de l'exécuteur (L1078). Faux pour un profil
    #: synthétique (« (default) » : hôte sans comptes, session d'avant L30).
    declared: bool = False
    #: #4944: explicitly assign historical unnamed balance readings to this profile.
    adopts_unnamed: bool = False
    #: #313/#2580 : raison du refus du compte par CE mesh (`accounts_shared:
    #: refuse` ou `account_sharing: strict`, identifiant déclaré aussi par un
    #: autre mesh de l'hôte, posée par `account_claims.mark_refused`) ; vide =
    #: pas de refus
    refused: str = ""

    @property
    def default(self) -> bool:
        """Le dossier par défaut du harnais (aucune variable posée)."""
        return self.kind == "config_dir" and not self.path

    def home(self, environ=None) -> str:
        """Le dossier de configuration effectif du harnais pour ce compte.

        * dossier déclaré (`path`) : ce dossier ;
        * **compte déclaré sans dossier** : le dossier par défaut du harnais
          (`~/.claude`, `~/.codex`, `~/.dsh`), jamais la variable de
          l'environnement de l'exécuteur — un compte déclaré n'hérite pas du
          compte ambiant (L1078) ;
        * profil synthétique (hors configuration, `(default)`) ou clé d'API :
          celui que le harnais prendrait de lui-même dans l'environnement de
          l'exécuteur — sa variable (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
          `DSH_HOME`) si elle est posée, sinon son dossier par défaut. C'est
          aussi là que `launch_env` le lance et que ses jauges sont lues
          (un seul mécanisme).
        """
        if self.kind == "config_dir" and self.path:
            return self.path
        environ = os.environ if environ is None else environ
        if self.kind == "config_dir" and self.declared:
            return _expand_home(default_home_of(self.harness), environ)
        inherited = environ.get(config_env_of(self.harness)) or ""
        return _expand_home(inherited or default_home_of(self.harness), environ)

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


def _pace_multiplier(harness: str, name: str, value) -> float:
    """`pace_multiplier` d'un profil compte (#1075), validé strictement.

    Absent : 1.0 (rythme nominal) — aucun défaut implicite selon le harnais,
    le nom ou le dossier. Présent : un nombre fini strictement positif.
    """
    if value is None:
        return 1.0
    if isinstance(value, bool):
        raise AccountError("account %s/%s: pace_multiplier must be a finite number > 0"
                           % (harness, name))
    try:
        multiplier = float(value)
    except (TypeError, ValueError):
        raise AccountError("account %s/%s: unreadable pace_multiplier" % (harness, name))
    if not math.isfinite(multiplier) or multiplier <= 0:
        raise AccountError("account %s/%s: pace_multiplier must be a finite number > 0"
                           % (harness, name))
    return multiplier


def _ratio(harness: str, name: str, key: str, value) -> float:
    """Un plafond de compte dans ]0, 1] (#1176), validé strictement."""
    if isinstance(value, bool):
        raise AccountError("account %s/%s: %s must be in ]0, 1]" % (harness, name, key))
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AccountError("account %s/%s: unreadable %s" % (harness, name, key))
    if not math.isfinite(number) or not 0 < number <= 1:
        raise AccountError("account %s/%s: %s must be in ]0, 1]" % (harness, name, key))
    return number


def _pace_ceiling(harness: str, name: str, item: dict) -> tuple[float, float | None, float]:
    """(`pace_ceiling`, `pace_ceiling_end`, `pace_ceiling_end_hours`) d'un
    profil compte (#1176), validés strictement.

    Absent : 0.90 (`cost.PACE_CEILING`), aucun défaut implicite selon le
    harnais, le nom ou le dossier. `pace_ceiling_end` absent : aucun
    relèvement. `pace_ceiling_end_hours` absent avec un `pace_ceiling_end`
    déclaré : 6 h ; déclaré seul (sans `pace_ceiling_end`), c'est une erreur —
    un réglage sans effet serait un piège.
    """
    ceiling = (PACE_CEILING if item.get("pace_ceiling") is None
               else _ratio(harness, name, "pace_ceiling", item.get("pace_ceiling")))
    end, hours = item.get("pace_ceiling_end"), item.get("pace_ceiling_end_hours")
    if end is None:
        if hours is not None:
            raise AccountError("account %s/%s: pace_ceiling_end_hours without "
                               "pace_ceiling_end" % (harness, name))
        return ceiling, None, PACE_CEILING_END_HOURS
    end = _ratio(harness, name, "pace_ceiling_end", end)
    if end < ceiling:
        raise AccountError("account %s/%s: pace_ceiling_end must be >= pace_ceiling"
                           % (harness, name))
    if hours is None:
        return ceiling, end, PACE_CEILING_END_HOURS
    if isinstance(hours, bool):
        raise AccountError("account %s/%s: pace_ceiling_end_hours must be a finite "
                           "number > 0" % (harness, name))
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise AccountError("account %s/%s: unreadable pace_ceiling_end_hours"
                           % (harness, name))
    if not math.isfinite(hours) or hours <= 0:
        raise AccountError("account %s/%s: pace_ceiling_end_hours must be a finite "
                           "number > 0" % (harness, name))
    return ceiling, end, hours


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
            pace_multiplier = _pace_multiplier(harness, name, item.get("pace_multiplier"))
            pace_local = (None if item.get("pace_multiplier") is None
                          else pace_multiplier)
            billing = str(item.get("billing_account") or "")
            if billing and not BILLING_RE.match(billing):
                raise AccountError("account %s/%s: invalid billing_account %r"
                                   % (harness, name, billing))
            try:
                reserve = float(item.get("reserve_usd", 2.0))
            except (TypeError, ValueError):
                raise AccountError("account %s/%s: unreadable reserve_usd" % (harness, name))
            if not reserve > 0 or reserve == float("inf"):
                raise AccountError("account %s/%s: reserve_usd must be positive"
                                   % (harness, name))
            # #3779 : admission de la fenêtre courte du fournisseur. Absent =
            # défaut du harnais (`account_window`) ; 0 explicite désactive.
            max_concurrent = item.get("max_concurrent")
            if max_concurrent is not None and (
                    isinstance(max_concurrent, bool)
                    or not isinstance(max_concurrent, int) or max_concurrent < 0):
                raise AccountError("account %s/%s: max_concurrent must be an "
                                   "integer >= 0" % (harness, name))
            min_start_gap_s = item.get("min_start_gap_s")
            if min_start_gap_s is not None:
                if isinstance(min_start_gap_s, bool):
                    raise AccountError("account %s/%s: min_start_gap_s must be a "
                                       "finite number >= 0" % (harness, name))
                try:
                    min_start_gap_s = float(min_start_gap_s)
                except (TypeError, ValueError):
                    raise AccountError("account %s/%s: unreadable min_start_gap_s"
                                       % (harness, name))
                if not math.isfinite(min_start_gap_s) or min_start_gap_s < 0:
                    raise AccountError("account %s/%s: min_start_gap_s must be a "
                                       "finite number >= 0" % (harness, name))
            pace_ceiling, pace_ceiling_end, pace_ceiling_end_hours = _pace_ceiling(
                harness, name, item)
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
            adopts = item.get("adopts_unnamed", False)
            if not isinstance(adopts, bool) or (adopts and kind != "api_key_env"):
                raise AccountError("account %s/%s: adopts_unnamed must be a boolean "
                                   "on an api_key_env profile" % (harness, name))
            if adopts and any(p.adopts_unnamed for p in profiles):
                raise AccountError("accounts.%s: only one profile may adopt unnamed "
                                   "balance readings" % harness)
            profiles.append(Profile(
                harness=harness, name=name, kind=kind,
                path=_expand(path) if path else "",
                env=env or (default_key_env_of(harness) if kind == "api_key_env" else ""),
                key_env=key_env, key_file=_expand(key_file) if key_file else "",
                hourly_usd=hourly, min_balance=min_balance, adopts_unnamed=adopts,
                check_credentials=bool(item.get("check_credentials", True)),
                priority=priority, overage=overage, **caps,
                pace_multiplier=pace_multiplier, pace_local=pace_local, declared=True,
                billing_account=billing, reserve_usd=reserve,
                max_concurrent=max_concurrent, min_start_gap_s=min_start_gap_s,
                pace_ceiling=pace_ceiling,
                pace_ceiling_end=pace_ceiling_end,
                pace_ceiling_end_hours=pace_ceiling_end_hours,
            ))
        _refuse_shared_homes(harness, profiles)
        out[harness] = profiles
    return out


def _refuse_shared_homes(harness: str, profiles: list[Profile]) -> None:
    """#1883 : deux comptes ne peuvent pas DÉCLARER le même dossier.

    Un compte `config_dir` qui porte un `path` désigne ce dossier ; un compte
    sans `path` suit le dossier par défaut du harnais. Deux comptes dont l'un
    au moins **déclare** un dossier ne doivent pas résoudre vers le même — le
    16/10, le choix a vu « secondaire » tantôt dans son dossier (5 h à 100 %,
    en dépassement), tantôt dans celui du primaire (34 %), et a basculé en
    boucle en lançant des tours facturés au-delà du forfait. La comparaison est
    faite sur le chemin **résolu** (`realpath`) : deux liens vers le même
    dossier sont le même compte. Refus à la lecture de la configuration, jamais
    un choix silencieux.

    Deux comptes sans `path` ne déclarent rien : ils lisent le même dossier par
    défaut, et c'est la spécification de L94 (`pace_multiplier` d'un compte
    déclaré se lit sans exiger un dossier à chacun). Les refuser rendait
    `accounts.parse` silencieusement vide pour les lecteurs qui l'attrapent
    (`sous_utilisation._declared_profiles`) : le rythme retombait à ×1 sans
    alerte, et la sous-utilisation disparaissait — la CI du 16/10 a rougi là
    (`test_le_rythme_percu_suit_le_multiplicateur_du_profil`)."""
    seen: dict[str, tuple[str, bool]] = {}
    for profile in profiles:
        if profile.kind != "config_dir":
            continue
        home = os.path.realpath(profile.home())
        declare = bool(profile.path)
        if home in seen:
            other, other_declares = seen[home]
            if declare or other_declares:
                raise AccountError(
                    "`accounts.%s`: accounts %r and %r resolve to the same "
                    "configuration directory (%s) — one reading cannot stand for two "
                    "accounts" % (harness, other, profile.name, home))
        else:
            seen[home] = (profile.name, declare)


# --------------------------------------------------------------------------
# #3647 : rythme compilé du canon (§11, décision owner #141793 a)
# --------------------------------------------------------------------------

#: durée de vie de la règle de rythme lue au canon, par configuration : la
#: compilation coûte une lecture Git, et `profiles` est lu à chaque tour
CANON_PACE_TTL_S = 60.0
_CANON_PACE: dict = {}


def canon_pace(cfg, *, now: float | None = None):
    """`(AccountsPace | None, erreur)` : le rythme des comptes du canon appliqué.

    `(règle, "")` : le bloc `accounts` de `processus/mecanique.yaml` au commit
    exact du canon ; `(None, "")` : le canon ne déclare pas ce bloc ;
    `(None, erreur)` : canon illisible. Dans les deux derniers cas, le réglage
    local de l'hôte reste appliqué et l'alerte `account_pace_local` le dit
    (`pace_drift`) — jamais un rythme local en silence.
    """
    from . import mechanics
    now = time.monotonic() if now is None else now
    key = id(cfg)
    cached = _CANON_PACE.get(key)
    if cached is not None and cached[0] is cfg and now - cached[1] < CANON_PACE_TTL_S:
        return cached[2]
    try:
        value = (mechanics.configured(cfg).accounts, "")
    except mechanics.MechanicsError as exc:
        value = (None, str(exc))
    except Exception as exc:  # noqa: BLE001 — lecture Git : le réglage local tient, signalé
        value = (None, "canon unreadable: %s" % exc)
    if len(_CANON_PACE) >= 16:
        _CANON_PACE.clear()
    _CANON_PACE[key] = (cfg, now, value)
    return value


def with_canon_pace(cfg, declared: dict[str, list[Profile]]) -> dict[str, list[Profile]]:
    """#3647 : les profils déclarés, au rythme compilé du canon.

    Avec un bloc `accounts` au canon, le multiplicateur de chaque compte est
    celui du canon (×1 s'il n'y figure pas) et le seuil de relèvement
    (`underused_loss`) y est posé : deux hôtes aux configurations locales
    différentes calculent le même rythme pour le même compte. Sans ce bloc, ou
    canon illisible, les profils sont rendus tels quels (réglage local)."""
    rule, _error = canon_pace(cfg) if cfg is not None else (None, "")
    if rule is None:
        return declared
    return {harness: [replace(profile,
                              pace_multiplier=rule.multiplier(harness, profile.name),
                              pace_source="canon",
                              underused_loss=rule.underused_loss)
                      for profile in items]
            for harness, items in declared.items()}


def pace_drift(cfg) -> list[dict]:
    """#3647 : les comptes dont le rythme local n'est pas celui du canon.

    Une ligne par compte : `pace_multiplier` déclaré par l'hôte et différent du
    canon (ignoré), ou réglage local appliqué faute de bloc `accounts` au canon
    ou de canon lisible. Configuration de comptes illisible : rien d'ici, les
    alertes de compte le disent déjà."""
    try:
        declared = parse(getattr(cfg, "accounts", None) or {})
    except AccountError:
        return []
    rule, error = canon_pace(cfg)
    out = []
    for harness, items in declared.items():
        for profile in items:
            local = profile.pace_local
            if rule is not None:
                canon_value = rule.multiplier(harness, profile.name)
                if local is None or local == canon_value:
                    continue
                out.append({"harness": harness, "account": profile.name,
                            "local": local, "canon": canon_value, "applied": canon_value,
                            "detail": "local pace_multiplier ×%g ignored: the canon "
                                      "sets ×%g" % (local, canon_value)})
            elif local is not None and local != 1.0:
                out.append({"harness": harness, "account": profile.name,
                            "local": local, "canon": None, "applied": local,
                            "detail": "local pace_multiplier ×%g applied: %s" % (
                                local, error or "the canon declares no accounts pace")})
    return out


def profiles(cfg, harness: str) -> list[Profile]:
    """Les comptes déclarés pour ce harnais (liste vide : aucun, comportement d'avant L30).

    #313/#2580 : avec `accounts_shared: refuse` ou `account_sharing: strict`,
    un compte dont l'identifiant est déclaré aussi par un autre mesh de
    l'hôte porte son refus (`refused`). #3647 : au rythme compilé du canon
    (`with_canon_pace`)."""
    from . import account_claims
    declared = parse(getattr(cfg, "accounts", None) or {})
    if harness not in declared:
        return []
    return account_claims.mark_refused(
        cfg, with_canon_pace(cfg, {harness: declared[harness]})[harness])


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
# trousseau macOS (L65) : Claude Code y range ses identifiants OAuth
# --------------------------------------------------------------------------

#: Claude Code nomme son service Keychain `Claude Code-credentials` ; un
#: `CLAUDE_CONFIG_DIR` déclaré ajoute `-<8 hex>` (sha256 du chemin) depuis que
#: les profils multiples ont leur propre entrée (2.1.56).
KEYCHAIN_DIGEST_HEX = 8
#: variables qui portent le nom de compte du trousseau (le compte macOS)
KEYCHAIN_ACCOUNT_ENV = ("USER", "LOGNAME")
#: `security` ne lit que les attributs de l'élément : jamais `-w` (le secret)
SECURITY_TIMEOUT_S = 5.0


def _config_dir_digest(path: str) -> str:
    """Le suffixe du service Keychain d'un dossier de configuration : les
    huit premiers caractères hexadécimaux du sha256 du chemin."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:KEYCHAIN_DIGEST_HEX]


def keychain_services(profile: Profile, environ=None) -> list[str]:
    """Les services Keychain où CE profil peut avoir ses identifiants.

    * dossier déclaré (`path`) : l'entrée namespacée du dossier (le harnais
      reçoit `CLAUDE_CONFIG_DIR`), plus le fichier vérifié par l'appelant ;
    * compte déclaré sans dossier : l'entrée globale (la variable de dossier
      reste absente, `apply_env`) ;
    * profil synthétique (hôte sans comptes) : l'entrée globale, et celle du
      dossier hérité si l'exécuteur porte `CLAUDE_CONFIG_DIR`.
    """
    service = keychain_service_of(profile.harness)
    if not service:
        return []
    if profile.path:
        return ["%s-%s" % (service, _config_dir_digest(profile.path))]
    if profile.declared:
        return [service]
    environ = os.environ if environ is None else environ
    variable = config_env_of(profile.harness)
    if variable and environ.get(variable):
        return [service, "%s-%s" % (service, _config_dir_digest(profile.home(environ)))]
    return [service]


def _keychain_available(*, which=shutil.which) -> bool:
    """Le binaire `security` est-il là (macOS) ?"""
    return bool(which("security") or os.path.isfile("/usr/bin/security"))


def _keychain_account(environ=None) -> str:
    environ = os.environ if environ is None else environ
    for name in KEYCHAIN_ACCOUNT_ENV:
        if environ.get(name):
            return str(environ[name])
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return ""


def _keychain_item(service: str, account: str = "", *, run=subprocess.run,
                   which=shutil.which) -> bool:
    """L'élément `service`/`account` existe-t-il dans le trousseau ?

    `security find-generic-password` sans `-w` : ameesh lit la présence de
    l'élément, jamais le jeton. L'absence de `security` ou un échec de la
    commande vaut « absent » ; l'appelant dit alors qu'il n'a pas pu vérifier
    (`_keychain_available`)."""
    binary = which("security") or "/usr/bin/security"
    argv = [binary, "find-generic-password", "-s", service]
    if account:
        argv.extend(["-a", account])
    try:
        proc = run(argv, capture_output=True, text=True,
                   timeout=SECURITY_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _credentials_in_keychain(profile: Profile, environ=None) -> bool:
    account = _keychain_account(environ)
    return any(_keychain_item(service, account)
               for service in keychain_services(profile, environ))


def _append_keychain_problem(profile: Profile, problems: list[str], environ) -> None:
    if not _keychain_available():
        problems.append("cannot check Keychain credentials (security not found): "
                        "a human must log in")
        return
    services = keychain_services(profile, environ)
    if not _credentials_in_keychain(profile, environ):
        problems.append("credentials missing in the Keychain (%s): a human must log in"
                        % ", ".join(services))


# --------------------------------------------------------------------------
# validation (dossiers 0700, fichiers de clé 0600, présence des identifiants)
# --------------------------------------------------------------------------

def check(profile: Profile, environ=None) -> list[str]:
    """Les problèmes d'un profil (liste vide : utilisable). Ne lit aucun secret.

    Un dossier déclaré : existe, appartient à l'utilisateur, privé (aucun droit
    pour le groupe ni les autres), identifiants présents. Sur macOS, Claude
    Code les range dans le trousseau (L65) : la présence y est vérifiée par
    `security find-generic-password` (jamais le secret), avec le fichier
    `.credentials.json` comme autre preuve. Un fichier de clé : fichier
    ordinaire, à l'utilisateur, 0600 au plus. Une variable de clé : présente
    et non vide dans l'environnement de l'exécuteur. #313 : un compte refusé
    par ce mesh (`refused`) a ce problème en premier.
    """
    refused = getattr(profile, "refused", "")
    return ([refused] if refused else []) + _check(profile, environ)


def _check(profile: Profile, environ=None) -> list[str]:
    environ = os.environ if environ is None else environ
    from . import setup_token
    if setup_token.present(profile, environ):
        try:
            _, expires = setup_token.read(profile, environ)
            if expires is not None and expires <= time.time():
                return ["setup-token expired: import a new human-issued token"]
            return []
        except setup_token.TokenError as exc:
            return [str(exc)]
    problems: list[str] = []
    uid = os.getuid() if hasattr(os, "getuid") else None
    if profile.kind == "config_dir":
        cred = credentials_of(profile.harness)
        if profile.default:
            # dossier par défaut du harnais : celui de l'utilisateur. Sur
            # macOS seulement (L65), un compte DÉCLARÉ sans dossier est
            # vérifié dans le trousseau ; un profil synthétique (hôte sans
            # comptes) garde son comportement d'avant L30 : il peut
            # s'authentifier autrement qu'avec un compte de forfait.
            if (profile.declared and platform.is_macos() and cred
                    and keychain_service_of(profile.harness)
                    and profile.check_credentials
                    and not os.path.isfile(os.path.join(profile.home(environ), cred))):
                _append_keychain_problem(profile, problems, environ)
            return problems
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
        if cred and profile.check_credentials:
            if not os.path.isfile(os.path.join(profile.path, cred)):
                if platform.is_macos() and keychain_service_of(profile.harness):
                    _append_keychain_problem(profile, problems, environ)
                else:
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
        out.update({"ANTHROPIC_AUTH_TOKEN", "CODEX_API_KEY"})
        out.add("CLAUDE_CODE_OAUTH_TOKEN")
        out.add("CLAUDE_CODE_OAUTH_REFRESH_TOKEN")
        out.add("CLAUDE_CODE_OAUTH_SCOPES")
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
        if (variable and profile.path == "" and environ.get(variable)
                and not (profile.declared and profile.kind == "config_dir")):
            # profil synthétique ou clé d'API : le dossier hérité de
            # l'exécuteur pour CE harnais (pas celui d'un autre harnais, ni
            # d'un autre compte). Un compte déclaré sans dossier, lui, désigne
            # le dossier par défaut du harnais : la variable reste retirée
            # (`auth_variables`), donc le harnais prend son dossier par défaut
            # — jamais celui du compte ambiant (L1078).
            env[variable] = environ[variable]
        apply_env(env, profile, environ)


def apply_env(env: dict, profile: Profile, environ=None) -> None:
    """Pose dans `env` (environnement du harnais) ce qui sélectionne le compte.

    Le profil est **validé ici, au lancement** (dossier 0700 et identifiants
    présents, fichier de clé 0600, variable présente), quel que soit l'état de
    la garde de budget : un profil devenu invalide depuis le choix refuse le
    tour. `config_dir` : la variable du dossier pour un dossier déclaré ;
    un compte déclaré sans dossier garde la variable absente (le harnais prend
    son dossier par défaut, L1078), un profil synthétique garde le dossier
    hérité de l'exécuteur (`launch_env`). `api_key_env` : la clé, lue dans la
    variable de l'exécuteur ou le fichier privé. La clé ne quitte cette
    fonction que dans `env` ; un échec lève `AccountError` sans jamais citer
    la clé.
    """
    environ = os.environ if environ is None else environ
    problems = check(profile, environ)
    if problems:
        raise AccountError("account %s/%s unusable: %s"
                           % (profile.harness, profile.name, "; ".join(problems)))
    from . import setup_token
    if setup_token.present(profile, environ):
        token, _ = setup_token.read(profile, environ)
        # Authentication must preserve the selected session directory (#4546).
        env[setup_token.VARIABLE] = token
    variable = config_env_of(profile.harness)
    if profile.kind == "config_dir":
        if not profile.default and variable:
            env[variable] = profile.path
        # compte par défaut : la variable du dossier reste absente (profil
        # déclaré : dossier par défaut du harnais) ou héritée (profil
        # synthétique, voir `launch_env`)
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
    until: float | None = None           #: heure de libération si au seuil
                                         #: (#777) : croisement du rythme ou
                                         #: remise à zéro (#898)
    problems: list = field(default_factory=list)
    #: L242b (4) : la fenêtre qui retient le compte et son usage, (clé, part)
    held: tuple | None = None
    #: L226b: {gauge key: (cap, label)}, the cap each window is held to
    limits: dict = field(default_factory=dict)
    #: #1176 : {clé de jauge: Ceiling}, le plafond de rythme effectif du compte
    #: (de base ou relevé en fin de fenêtre), avec sa raison
    ceilings: dict = field(default_factory=dict)
    #: L226b: held back by an absolute rule (in overage, past a fixed cap),
    #: not only by its pace: no budget setting lifts it
    hard: bool = False
    #: #3647 : {clé de jauge: perte prévue au reset} des fenêtres dont le
    #: rythme est relevé (`underused_floor`)
    raises: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.reason

    @property
    def unpaced_ok(self) -> bool:
        """Usable when the pace does not hold (budget guard off, L30): only
        the absolute rules of L226b keep the account out."""
        return not self.hard


def default_home(harness: str, environ=None) -> str:
    """Le dossier de configuration par défaut du harnais (`~/.claude`,
    `~/.codex`, `~/.dsh`), `~` résolu dans `environ` s'il est fourni."""
    return _expand_home(default_home_of(harness), environ)


def primary_of(harness: str, items: list[Profile], environ=None) -> Profile | None:
    """Le compte « par défaut » du harnais parmi les profils déclarés (L1078).

    C'est celui dont le dossier de configuration est le dossier par défaut du
    harnais — jamais le premier de la liste, jamais celui que l'environnement
    de l'exécuteur désigne. Les relevés Claude sans marqueur de compte
    (sessions d'avant L30) ne sont attribués qu'à lui : une liste réduite au
    seul secondaire ne les lui prend pas.
    """
    cible = os.path.realpath(default_home(harness, environ))
    for profile in items:
        if profile.kind != "config_dir":
            continue
        if os.path.realpath(profile.home(environ)) == cible:
            return profile
    return None


def model_scoped(gauges, model):
    """Les jauges qui comptent pour une session de modèle `model`.

    #1580 (tranche 3) : un seau propre à un modèle (`Gauge.scope`, le `title`
    du widget omarchy) ne compte que pour une session de ce modèle. `model`
    `None` : pas de session (affichage) — tous les seaux sont rendus ;
    `''` : session de modèle inconnu — les seaux de modèle sont **écartés**,
    jamais attribués au hasard. Les fenêtres générales passent toujours.
    """
    if model is None or not any(getattr(g, "scope", "") for g in gauges):
        return gauges
    session = (model or "").strip().lower()
    kept = [g for g in gauges
            if not getattr(g, "scope", "") or (session and g.scope in session)]
    if len(kept) == len(gauges):
        return gauges
    from .cost import Gauges  # import local : accounts n'importe pas cost
    try:
        return Gauges(kept, getattr(gauges, "overage", None))
    except TypeError:           # un lecteur qui rend une simple liste
        return kept


def _widget_key(profile: Profile, items: list) -> str:
    """#1789/N3 : la clé du fichier du widget pour ce compte.

    Le widget nomme ses fichiers d'après le dossier de configuration
    (`~/.claude` → `claude.json`, `~/.claude-secondary` →
    `claude-secondary.json`) : `snapshot_key`. Deux dossiers déclarés de même
    nom ne s'y distinguent pas ; la clé passe alors au **chemin complet**
    (empreinte courte), que le widget n'écrit pas — le lecteur reste inerte
    plutôt que d'afficher les jauges d'un autre compte (une clé fondée sur le
    nom du compte lirait, elle, le fichier d'un autre dossier)."""
    from . import widget_usage
    key = widget_usage.snapshot_key(profile)
    if sum(widget_usage.snapshot_key(p) == key for p in items
           if p.harness == "claude" and p.kind == "config_dir") > 1:
        chemin = os.path.realpath(profile.home()).encode("utf-8")
        return "path-%s" % hashlib.sha1(chemin).hexdigest()[:12]
    return key


def _read_at_of(gauges) -> float | None:
    """L'horodatage du relevé (#1561) : le plus récent des seaux datés, None
    si aucun ne porte de date. Sert à comparer deux sources (#1648)."""
    stamps = [stamp for stamp in
              (getattr(g, "read_at", None) for g in gauges or ()) if stamp]
    return max(stamps) if stamps else None


def gauges_of(book, profile: Profile, items: list[Profile], environ=None, *,
              model=None, now: float | None = None) -> list:
    """Les jauges de forfait du compte.

    L1078 : le primaire d'un relevé Claude est le compte **par défaut du
    harnais** (`primary_of`), pas le premier profil de la liste ; un compte
    Codex est lu dans son propre dossier de sessions, jamais le `CODEX_HOME`
    ambiant de l'exécuteur, qui peut être celui d'un autre compte.

    #1580 (tranche 3) : le fichier du widget omarchy d'abord quand il est
    frais (il porte les seaux propres à un modèle, invisibles des journaux),
    ses drapeaux d'overage repris du journal ; sinon les journaux
    `rate_limit_event.unifiedWindows`, comme avant. #1648 : entre deux relevés
    datés, le plus récent gagne — un widget arrêté mais encore dans sa fenêtre
    ne masque plus un journal plus frais. `model` filtre les seaux de modèle
    (`model_scoped`).

    #1789/N3 : le widget ne se lit que pour un compte à dossier de
    configuration. Une clé API n'en a pas — `home()` retombe sur le dossier
    par défaut, et `accounts list` afficherait les jauges du primaire sur sa
    ligne ; les journaux, eux, sont nommés par compte (`primary_of`)."""
    if profile.harness == "claude" and profile.kind == "config_dir":
        from . import widget_usage
        primary = primary_of(profile.harness, items, environ)
        primary_name = primary.name if primary is not None else None
        journal = book.claude_gauges(account=profile.name,
                                     primary=primary_name)
        gauges = widget_usage.read(profile, now=now,
                                   key=_widget_key(profile, items))
        widget_at, journal_at = _read_at_of(gauges), _read_at_of(journal)
        if gauges is not None and widget_at is not None and journal_at is not None \
                and widget_at < journal_at:
            gauges = None      # #1648 : le journal est plus récent
        if gauges is not None:
            overage = getattr(journal, "overage", None)
            if overage is not None:
                gauges.overage = overage
        else:
            gauges = journal
        return model_scoped(gauges, model)
    if profile.harness == "claude":
        primary = primary_of(profile.harness, items, environ)
        return model_scoped(
            book.claude_gauges(
                account=profile.name,
                primary=primary.name if primary is not None else None),
            model)
    if profile.harness == "codex":
        # Un seul mécanisme (cost.CostBook) : argument > AMEESH_CODEX_SESSIONS >
        # $CODEX_HOME/sessions > ~/.codex/sessions. Un compte qui désigne un
        # dossier (déclaré, ou synthétique avec `path`) lit SES sessions ; le
        # profil synthétique sans dossier garde le mécanisme de l'exécuteur.
        # #3034 : son `auth.json` nomme le forfait vivant, pour ignorer les
        # relevés d'un ancien forfait restés dans le même dossier de sessions.
        if profile.declared or profile.path:
            from .codex_gauges import live_plan
            home = profile.home(environ)
            return book.codex_gauges(
                sessions=os.path.join(home, "sessions"), plan=live_plan(home))
        return book.codex_gauges()
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


# --------------------------------------------------------------------------
# #1176 : plafond de rythme par compte et relèvement de fin de fenêtre
# --------------------------------------------------------------------------

class Ceiling(NamedTuple):
    """Le plafond de rythme effectif d'une fenêtre (#1176).

    * `value` : le plafond à appliquer à `now` (`pace_ceiling`, ou
      `pace_ceiling_end` quand le relèvement s'applique) ;
    * `reason` : '' pour le plafond de base, `CEILING_REASON_END` quand il est
      relevé (affiché par `accounts list`) ;
    * `from_ts` : l'heure à partir de laquelle le relèvement s'applique
      (`resets_at` moins `pace_ceiling_end_hours`) quand il est configuré ET
      prouvé ; None sinon. Sert à annoncer la fin de pause ;
    * `end` : le plafond relevé, None sans relèvement.
    """

    value: float
    reason: str = ""
    from_ts: float | None = None
    end: float | None = None


def overage_impossible(gauges, now: float) -> bool:
    """L'overage est-il impossible pour ce compte, prouvé par un relevé frais ?

    Une seule source, jamais une déclaration de configuration : le relevé de
    jauges, `overage.capable` **explicitement faux** (Claude `overageStatus`,
    Codex `credits.has_credits`) et frais — au moins une fenêtre encore en
    cours (un relevé dont toutes les fenêtres sont remises à zéro est périmé,
    L71), et pas en overage.

    #1176 (B2) : le `.claude.json` du compte a été retiré de cette preuve.
    Son `cachedExtraUsageDisabledReason` peut dater d'une autre fenêtre, et
    le mtime du **contenant** ne date pas la valeur : Claude Code réécrit ce
    fichier pour d'autres raisons (une clé étrangère ajoutée suffisait à
    faire relever le plafond alors que la capacité d'overage restait
    inconnue). Aucune preuve fraîche rattachée à la capacité n'étant
    démontrable, la suppression conservatrice s'impose : information
    inconnue, absente ou périmée ⇒ pas de relèvement.

    Toute autre situation — information absente, inconnue, périmée, ou
    overage possiblement facturé — vaut « non » : le plafond de base tient.
    """
    read = getattr(gauges, "overage", None)
    if read is None or read.capable is not False:
        return False
    if in_overage(gauges, now):
        return False
    return any(not g.reset_passed(now) for g in gauges)


def pace_ceiling(profile: Profile, gauges, gauge, now: float) -> Ceiling:
    """Le plafond de rythme effectif de cette fenêtre, et sa raison (#1176).

    Le plafond de base est `profile.pace_ceiling` (défaut `PACE_CEILING`).
    Dans les `pace_ceiling_end_hours` dernières heures avant la remise à zéro
    de la fenêtre (`resets_at`), il monte à `pace_ceiling_end` si — et
    seulement si — `overage_impossible` le prouve pour ce compte. Une fenêtre
    sans date, un relèvement non déclaré, une preuve absente ou périmée
    laissent le plafond de base : jamais de relèvement dans le doute.
    """
    base = profile.pace_ceiling
    end = profile.pace_ceiling_end
    if end is None or end <= base or not gauge.resets_at:
        return Ceiling(base)
    if in_overage(gauges, now):
        # un compte en overage n'est jamais choisi : son plafond ne se relève pas
        return Ceiling(base)
    from_ts = float(gauge.resets_at) - profile.pace_ceiling_end_hours * 3600.0
    left = float(gauge.resets_at) - now
    if left < 0.0:
        return Ceiling(base)
    if not overage_impossible(gauges, now):
        return Ceiling(base)
    if now >= from_ts:
        return Ceiling(end, CEILING_REASON_END, from_ts, end)
    return Ceiling(base, from_ts=from_ts, end=end)


def underused_floor(profile: Profile, gauges, gauge, now: float) -> float | None:
    """#3647 (décision owner #141793 a, canon §11) : le rythme relevé d'une
    fenêtre sous-utilisée, ou None.

    Quand la perte prévue au reset de la fenêtre — sa part de quota inutilisée
    si rien ne change (`Gauge.expiring`, la « lost at reset » d'`accounts
    list`) — dépasse le seuil du canon (`underused_loss`), le plafond de
    rythme ne descend pas sous `1 − underused_loss` : c'est le relèvement
    minimal qui ramène la perte prévue au seuil. Il ne dépasse jamais le
    plafond de rythme du compte (`Gauge.pace_cap`), et les plafonds fixes
    s'appliquent ensuite (`limits`) : jamais d'overage.

    Aucun relèvement sans seuil déclaré, sur un relevé périmé (#1561 : la
    perte n'y est pas annoncée), sur une fenêtre non datée ou échue, ni sur un
    compte en overage. Calcul déterministe, sans état : le même relevé donne
    le même rythme sur chaque hôte."""
    threshold = profile.underused_loss
    if threshold is None or in_overage(gauges, now):
        return None
    found = gauge.expiring(now)
    if found is None or found[1] <= threshold or stale_reading(gauges, now):
        return None
    return 1.0 - threshold


def underused_floors(profile: Profile, gauges, now: float) -> dict:
    """{clé de jauge: plancher} des fenêtres au rythme relevé (#3647)."""
    out = {}
    for gauge in gauges:
        floor = underused_floor(profile, gauges, gauge, now)
        if floor is not None:
            out[gauge.key] = floor
    return out


def raise_reason(profile: Profile, lost: float) -> str:
    """#3647 : la raison d'un rythme relevé, affichée et journalisée."""
    return "pace raised: forecast loss at reset %.0f%% > %.0f%%" % (
        lost * 100, (profile.underused_loss or 0.0) * 100)


#: #3647 : journal des relèvements de rythme, dans le dossier d'état de l'hôte
RAISES_JOURNAL = "pace_raises.jsonl"
#: la fin du journal relue pour ne journaliser qu'une fois chaque relèvement
_RAISES_TAIL = 65536


def journal_raises(book, profile: Profile, gauges, raises: dict, now: float) -> int:
    """#3647 : journalise chaque relèvement de rythme avec la lecture des jauges.

    Une ligne JSON par fenêtre et par remise à zéro (`resets_at`) : le premier
    tour qui relève le rythme l'écrit, les suivants la retrouvent à la fin du
    journal et n'écrivent rien. Verrou exclusif de l'hôte pendant la relecture
    et l'écriture. **Ne lève jamais** : le journal ne doit pas casser la garde
    de budget. Rend le nombre de lignes écrites."""
    import fcntl
    import json
    state_dir = getattr(book, "state_dir", None)
    if not state_dir or not raises:
        return 0
    path = os.path.join(state_dir, RAISES_JOURNAL)
    by_key = {g.key: g for g in gauges}
    try:
        with open(path, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _RAISES_TAIL))
            seen = set()
            for line in fh.read().splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    seen.add((item.get("harness"), item.get("account"),
                              item.get("key"), item.get("resets_at")))
            written = 0
            for key, lost in sorted(raises.items()):
                gauge = by_key.get(key)
                if gauge is None:
                    continue
                ident = (profile.harness, profile.name, key, gauge.resets_at)
                if ident in seen:
                    continue
                fh.write(json.dumps({
                    "ts": round(now, 3), "harness": profile.harness,
                    "account": profile.name, "key": key,
                    "resets_at": gauge.resets_at, "lost": round(lost, 4),
                    "threshold": profile.underused_loss,
                    "floor": round(1.0 - profile.underused_loss, 4),
                    "pace_multiplier": profile.pace_multiplier,
                    "pace_source": profile.pace_source,
                    "reason": raise_reason(profile, lost),
                    "gauges": [{"key": g.key, "used": g.used, "elapsed": round(g.elapsed(now), 4),
                                "resets_at": g.resets_at, "window_s": g.window_s,
                                "read_at": getattr(g, "read_at", None),
                                "source": getattr(g, "source", "")} for g in gauges],
                }, sort_keys=True) + "\n")
                written += 1
            fh.flush()
            return written
    except Exception:  # noqa: BLE001 — journal facultatif : jamais fatal
        return 0


def pace_ceilings(profile: Profile, gauges, now: float) -> dict:
    """{clé de jauge: Ceiling} pour toutes les fenêtres du compte (#1176)."""
    return {gauge.key: pace_ceiling(profile, gauges, gauge, now)
            for gauge in gauges}


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


def limits(profile: Profile, gauges, now: float, *, critical: bool = False,
           reserve: bool = False, ceilings: dict | None = None,
           floors: dict | None = None) -> dict:
    """{gauge key: (cap, label)}: the usage each window of the account is held
    to (L226b), the lowest of
    * the pace cap of the guard (0019), or on the short window the account's
      `short_window_cap` when set (it replaces the pace cap there);
    * the account's fixed cap (`account_cap`: 50 % for the primary);
    * `overage_cap` (80 %) when the account may be billed beyond its plan.

    `critical` (L242b): see `fixed_caps`. `reserve` (L242b (2), a
    non-critical agent): the pace cap, or the short window cap that replaces
    it, is lowered by `RESERVE` —
    `max(min(PACE_CEILING, multiplier × elapsed + PACE_MARGIN) − RESERVE,
    PACE_MARGIN)` — and the fixed caps are not lowered. #777 : tant que
    `multiplier × elapsed ≤ RESERVE` (soit `≤ PACE_MARGIN` quand la réserve
    vaut la marge) la réserve ne s'applique pas, le plafond de rythme y vaut
    la marge (10 points) et non `multiplier × écoulé + marge − RESERVE` — à
    8 % d'écoulé avec `multiplier = 2` il vaut 16 % (#1028 P3) ; l'énoncé de
    la décision du propriétaire (« plafond = max(écoulé, marge) ») décrit ce
    début de fenêtre. En fin de fenêtre le seuil vaut
    `PACE_CEILING − RESERVE` (80 %), il ne suit plus `max(écoulé, marge)`
    (#1028 N1).

    `ceilings` (#1176, `pace_ceilings`) : les plafonds effectifs déjà
    calculés pour ce compte ; sans eux, ils le sont ici. Le plafond de rythme
    est celui du compte (`pace_ceiling`), ou son plafond de fin de fenêtre
    quand `overage_impossible` le prouve.

    `floors` (#3647, `underused_floors`) : le rythme relevé des fenêtres
    sous-utilisées ; sans eux, il est calculé ici. La réserve des
    non-critiques et les plafonds fixes s'appliquent par-dessus.
    """
    fixed = fixed_caps(profile, gauges, critical=critical)
    cut = RESERVE if reserve else 0.0
    if ceilings is None:
        ceilings = pace_ceilings(profile, gauges, now)
    if floors is None:
        floors = underused_floors(profile, gauges, now)
    out = {}
    for gauge in gauges:
        ceiling = ceilings.get(gauge.key)
        pace = (gauge.held_cap(now, cut, profile.pace_multiplier,
                               ceiling.value if ceiling is not None
                               else profile.pace_ceiling,
                               floors.get(gauge.key, 0.0)), "pace cap")
        if profile.short_window_cap is not None and gauge.window_s <= SHORT_WINDOW_S:
            base = (max(0.0, profile.short_window_cap - cut), "short window cap")
            if reserve:
                # #373 r1 : jamais au-dessus du rythme moins la réserve,
                # min(80 %, écoulé), même quand le plafond court le remplace
                base = min(base, pace, key=lambda pair: pair[0])
        else:
            base = pace
        out[gauge.key] = min([base, *fixed], key=lambda pair: pair[0])
    return out


def _release(profile: Profile, depasses, hard, now: float,
             cut: float, ceilings: dict | None = None) -> tuple[float | None, object | None]:
    """La fenêtre qui bloque le plus longtemps, et l'heure où elle libère.

    Ne se rattrapent pas par l'écoulé, et attendent donc leur remise à zéro :
    un plafond fixe (`hard`), et le plafond court du compte
    (`short_window_cap`, abaissé de `cut` comme dans `limits`). Une fenêtre
    tenue par le rythme seul se libère quand l'écoulé rejoint l'usage
    (`Gauge.free_at`, #777). `(None, None)` si l'une des fenêtres qui bloquent
    n'est pas datée : aucune heure annoncée (#373 r2).

    Le plafond court se teste sur l'usage, pas sur le libellé de `limits` :
    sous réserve, le plafond de rythme est souvent le plus bas à l'instant du
    relevé (il porte alors le libellé « pace cap »), mais il ne rattrape
    jamais un usage au-dessus du plafond court — voir #777 r2, un compte
    `short_window_cap` 0,75 à 70 % d'usage avec 30 % d'écoulé.

    `ceilings` (#1176) : le plafond effectif de chaque fenêtre. Un relèvement
    de fin de fenêtre déjà prouvé peut libérer la fenêtre avant sa remise à
    zéro : c'est cette heure-là qu'on annonce, pas la remise à zéro — et pas
    avant que le rythme ait rattrapé l'usage sous le plafond relevé
    (#1506, qui reprend le contrat de #1227 N1).
    """
    dated = []
    for gauge in depasses:
        if not gauge.resets_at:
            return None, None
        ceiling = (ceilings or {}).get(gauge.key)
        short = (profile.short_window_cap is not None
                 and gauge.window_s <= SHORT_WINDOW_S)
        if any(gauge is held for held, _ in hard) or (
                short and gauge.used_at(now) >= max(0.0, profile.short_window_cap - cut)):
            free = float(gauge.resets_at)
        else:
            free = gauge.free_at(now, cut, profile.pace_multiplier,
                                 ceiling.value if ceiling is not None
                                 else profile.pace_ceiling)
            if free is None:
                return None, None
            if (ceiling is not None and ceiling.from_ts is not None
                    and ceiling.end is not None and ceiling.from_ts > now
                    and gauge.used_at(now) < ceiling.end - cut):
                # #1506 (reprend #1227 N1) : le relèvement ne libère qu'une
                # fois le rythme rattrapé sous le plafond relevé. Annoncer
                # `from_ts` seul promettait une pause que l'usage ne tenait pas
                # encore à cette heure-là ; l'heure juste est la plus tardive
                # des deux, sans jamais dépasser la libération calculée au
                # plafond courant.
                raised = gauge.free_at(now, cut, profile.pace_multiplier,
                                       ceiling.end)
                if raised is not None:
                    free = min(free, max(float(ceiling.from_ts), raised))
        dated.append((free, gauge))
    return max(dated, key=lambda pair: pair[0])


def evaluate(book, profile: Profile, items: list[Profile], now: float,
             environ=None, *, record: bool = True, critical: bool = False,
             reserve: bool = False, model=None) -> Evaluation:
    """Le compte est-il sous le seuil de la garde (0019) ? Ne lit aucun secret.

    `record=False` (simulation, L39) : l'historique des jauges n'est pas écrit.
    `critical` (L242b) : pour un agent critique, sans le plafond implicite de
    50 % du compte par défaut (`fixed_caps`). `reserve` : pour un agent non
    critique, le rythme moins `RESERVE` (`limits`). `model` (#1580 tranche 3) :
    le modèle de la session ; `None` = pas de session (affichage), tous les
    seaux comptent, `''` = modèle inconnu, les seaux de modèle sont écartés
    (`gauges_of`).

    #1176 : le plafond de rythme est celui du compte (`pace_ceiling`), relevé
    en fin de fenêtre (`pace_ceiling_end`) quand `overage_impossible` le
    prouve ; le refus d'overage (`in_overage`) et ses règles sont inchangés.
    """
    result = Evaluation(profile)
    result.problems = check(profile, environ)
    if result.problems:
        result.reason = "unusable profile: %s" % "; ".join(result.problems)
        # #313 : le refus d'un compte partagé avec un autre mesh est une règle
        # absolue — la garde de budget désactivée ne le lève pas
        result.hard = bool(getattr(profile, "refused", ""))
        return result
    from .codex_gauges import AmbiguousCodexGauges
    try:
        result.gauges = gauges_of(book, profile, items, environ, model=model, now=now)
    except AmbiguousCodexGauges as exc:
        result.reason = str(exc)
        result.hard = True
        return result
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
    result.ceilings = pace_ceilings(profile, result.gauges, now)
    floors = underused_floors(profile, result.gauges, now)
    result.raises = {g.key: g.expiring(now)[1] for g in result.gauges if g.key in floors}
    if result.raises and record:
        journal_raises(book, profile, result.gauges, result.raises, now)
    result.limits = limits(profile, result.gauges, now, critical=critical, reserve=reserve,
                           ceilings=result.ceilings, floors=floors)
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
        result.held = (gauge.key, gauge.used)
        ceiling = result.ceilings.get(gauge.key)
        raised = ", %s" % ceiling.reason if ceiling is not None and ceiling.reason else ""
        if gauge.key in result.raises:
            raised += ", %s" % raise_reason(profile, result.raises[gauge.key])
        if label == "pace cap":
            result.reason = ("plan %s %s: %.0f%% used with %.0f%% of the window elapsed "
                             "(pace cap %.0f%%%s)" % (
                                 profile.harness, gauge.key, gauge.used * 100,
                                 gauge.elapsed(now) * 100, cap * 100, raised))
        else:
            result.reason = "%s plan %s: %.0f%% used (%s %.0f%%)" % (
                profile.harness, gauge.key, gauge.used * 100, label, cap * 100)
        if reserve and not hard:
            result.reason += ", %.0f points kept for critical agents" % (RESERVE * 100)
        # #373 r2 : la pause affichée est celle de la fenêtre qui bloque le
        # plus longtemps ; #777, #898 : à l'heure où elle libère vraiment —
        # croisement du rythme, ou remise à zéro quand il ne rattrape plus ;
        # #1176 : le relèvement de fin de fenêtre peut la libérer avant la
        # remise à zéro.
        result.until, late = _release(profile, depasses, hard, now,
                                      RESERVE if reserve else 0.0, result.ceilings)
        if late is not None:
            result.held = (late.key, late.used)
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


#: #3969 : la raison d'un choix sans compte plaçable parce qu'un compte
#: déclaré est INVALIDE (dossier non privé, profil absent, identifiants
#: manquants) — une configuration de l'hôte, pas un budget : elle ne porte
#: jamais l'échéance d'une pause (`pause_label`). L'exécuteur la pose sous
#: `CONFIG_BLOCK_PREFIX`, d'où l'alerte `account_config_invalid`.
NO_VALID_PREFIX = "no valid "
#: #3969 : le `status_text` d'un agent bloqué par une configuration de comptes
CONFIG_BLOCK_PREFIX = "accounts : "


def remedy(profile: Profile, problem: str) -> str:
    """Le geste qui répare un problème de `check` (#3969), ou ''."""
    if problem.startswith("directory not private"):
        return "chmod 700 %s" % profile.path
    if problem.startswith("key file too permissive"):
        return "chmod 600 %s" % profile.key_file
    if problem.startswith("missing key file"):
        return "write the key to %s (chmod 600)" % profile.key_file
    if problem.startswith("missing directory"):
        return "install -d -m 700 %s, then log in" % profile.path
    if problem.startswith("credentials missing"):
        return "log in with %s=%s" % (config_env_of(profile.harness) or "the harness",
                                      profile.path)
    return ""


def invalid(profile: Profile, problems: list[str]) -> list[str]:
    """Les problèmes de configuration d'un compte, remède compris (#3969).
    Un compte refusé par ce mesh (#313, `refused`) n'en est pas un : c'est
    une règle, pas une erreur d'hôte."""
    if getattr(profile, "refused", ""):
        return []
    out = []
    for problem in problems:
        geste = remedy(profile, problem)
        out.append("%s (fix: %s)" % (problem, geste) if geste else problem)
    return out


def no_valid_reason(harness: str, evaluations: dict) -> str:
    """#3969 : « no valid <harness> account: … » quand au moins un compte
    déclaré est invalide et qu'aucun n'est plaçable, sinon ''. Les autres
    comptes suivent, avec leur raison (seuil, relevé) : réparer la
    configuration suffit à rouvrir le placement, l'échéance d'un compte au
    seuil n'est pas la cause."""
    bad = {name: invalid(ev.profile, ev.problems) for name, ev in evaluations.items()}
    bad = {name: problems for name, problems in bad.items() if problems}
    if not bad:
        return ""
    why = "%s%s account: %s" % (NO_VALID_PREFIX, harness, "; ".join(
        "%s: %s" % (name, ", ".join(problems)) for name, problems in bad.items()))
    rest = {name: ev for name, ev in evaluations.items() if name not in bad}
    if rest:
        why += " — other accounts: %s" % _label(rest)
    return why


def doctor_lines(cfg, environ=None) -> tuple[bool, list[str]]:
    """#3969 : `(ko, lignes)` de `ameesh doctor` — chaque compte déclaré sur
    l'hôte que la sélection jugerait invalide (`check`), avec son remède. Le
    27/10, à la mise en service d'agents-3, des profils laissés en 755 ont
    tenu des agents en « pause budget » sous un doctor vert.

    Lu par l'utilisateur qui lance doctor : un dossier ou un fichier de clé
    à un autre utilisateur (doctor lancé hors du compte de l'exécuteur) n'est
    pas jugé, ni une variable de clé, que porte l'environnement de
    l'exécuteur ; un fichier de clé absent ou trop ouvert l'est."""
    environ = os.environ if environ is None else environ
    try:
        declared = parse(getattr(cfg, "accounts", None) or {})
    except AccountError as exc:
        return True, ["KO — invalid account configuration: %s" % exc]
    uid = os.getuid() if hasattr(os, "getuid") else None
    ko, lines = False, []
    for harness, items in declared.items():
        for profile in items:
            label = "%s/%s" % (harness, profile.name)
            if profile.kind == "api_key_env" and not profile.key_file:
                continue
            owned = profile.key_file or (
                profile.path if profile.kind == "config_dir" and not profile.default else "")
            if owned and uid is not None:
                try:
                    owner = os.lstat(owned).st_uid
                except OSError:
                    owner = uid
                if owner != uid:
                    lines.append("%s not checked (%s belongs to uid %d: run doctor as "
                                 "the runner's user)" % (label, owned, owner))
                    continue
            problems = invalid(profile, _check(profile, environ))
            if problems:
                ko = True
                lines.append("KO — %s invalid: %s" % (label, "; ".join(problems)))
    return ko, lines


def pause_label(evaluations: dict) -> str:
    """L242b (4) : la pause en court, devant sa raison détaillée — le compte
    qui se libère le premier : « pause (account secondaire, five_hour 96 %)
    until 14:20; ». Vide si aucun compte n'a de remise à zéro datée."""
    dated = [ev for ev in evaluations.values() if ev.until and ev.held]
    if not dated:
        return ""
    first = min(dated, key=lambda ev: ev.until)
    from .cost import until_label
    return "pause (account %s, %s %.0f %%) until %s; " % (
        first.profile.name, first.held[0], first.held[1] * 100, until_label(first.until))


#: #2934 : le préambule d'une pause budget — le compte, la fenêtre qui le
#: retient et l'échéance annoncée. Le pourcentage qui suit la fenêtre est une
#: mesure continue : il reste dans `status_text`, pas dans la nature.
_PAUSE_NATURE = re.compile(
    r"^pause \(account (?P<compte>\(default\)|[^,)]+)"
    r"(?:, (?P<fenetre>[^\s,]+) [\d.,]+\s*%)?\)"
    r"(?: until (?P<echeance>[^;]+))?")

#: #2934 : les mesures continues d'une raison sans ce préambule (plafond du
#: mesh ou du compte payé au token, solde d'API). Elles changent à chaque
#: contrôle sans que la pause change de nature.
_PAUSE_MEASURE = re.compile(r"\d+(?:[.,]\d+)?\s*(?:%|\$|€|USD\b|EUR\b)")


def pause_nature(reason: str) -> str:
    """La nature d'une pause budget : ce dont le changement mérite une note.

    « pause (account tertiaire, seven_day 83 %) until 14:20; … » donne le
    compte, la fenêtre et l'échéance (``tertiaire``, ``seven_day``,
    ``14:20``) ; les pourcentages, qui bougent à chaque relevé, n'en font pas
    partie (#2934). Une raison sans ce préambule se compare sans ses mesures
    continues (montants, pourcentages) : deux contrôles qui ne diffèrent que
    par la dépense ou la jauge ne réécrivent pas la note du fil, le détail
    restant dans `status_text`."""
    found = _PAUSE_NATURE.match(reason or "")
    if found:
        return "pause|%s|%s|%s" % (
            found.group("compte"), found.group("fenetre") or "",
            found.group("echeance") or "")
    return _PAUSE_MEASURE.sub("#", reason or "")


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


def lost_at_reset(gauges, now: float) -> float:
    """#1580 (propriétaire, 2026-10-15) : le budget qui sera **perdu à la
    remise à zéro** si rien ne le consomme d'ici là — la capacité inutilisée
    (`1 − utilisé`, 0034 §5/L74) de la fenêtre qui se remet à zéro le plus
    tôt (`expiry`). 0.0 : rien n'expire (aucune fenêtre en cours : relevé
    échu, non daté ou absent — une lecture périmée n'annonce aucune perte).

    C'est la part en jeu ; `lost_rate` en fait le score du choix en la
    rapportant au temps qui reste."""
    found = expiry(gauges, now)
    return found[1] if found is not None else 0.0


def lost_rate(gauges, now: float) -> float:
    """#1789/N1 : la part perdue **par seconde** d'ici la remise à zéro — la
    capacité inutilisée de la fenêtre qui se remet à zéro le plus tôt
    (`lost_at_reset`), rapportée au temps qui reste.

    C'est le score principal du choix : entre deux comptes utilisables,
    consommer d'abord celui dont le budget s'évapore le plus vite, et non le
    plus en retard sur son rythme — le retard ne dit pas ce qui sera perdu.
    L'ordre déclaré, la fraîcheur et la garde de budget ne bougent pas. 0.0 :
    rien n'expire (relevé échu, non daté ou absent, remise à zéro passée)."""
    found = expiry(gauges, now)
    if found is None:
        return 0.0
    remaining = float(found[0]) - now
    return found[1] / remaining if remaining > 0 else 0.0


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
    utilisé (rythme 55 %) », « sans relevé : non vérifié ».

    #1561 : un relevé périmé (une fenêtre remise à zéro depuis, l'autre
    encore ouverte) ne se lit plus comme une mesure courante — la raison dit
    « non vérifié » et rappelle la dernière valeur connue. Un relevé absent
    est « non vérifié » aussi : sa valeur n'est pas un 0 % mesuré (L117 le
    fait passer premier, mais la raison ne ment pas)."""
    gauges = result.gauges
    if not gauges:
        return "no reading: unverified"
    if _empty_after_reset(gauges, now):
        # #3586 : la fenêtre remise à zéro est vide ; ce n'est pas une mesure
        # courante (le relevé est ancien), mais c'est un 0 % sûr à essayer.
        return "stale reading (window reset since the reading): 0% used, tried first"
    if stale_reading(gauges, now):
        last = lag(gauges, now, result.limits)[1]
        suffix = (", last %s %.0f%%" % (last.key, last.used * 100)
                  if last is not None else "")
        return "stale reading (window reset since the reading): unverified" + suffix
    _ratio, gauge = lag(gauges, now, result.limits)
    if gauge is None:                       # jamais avec des jauges : ceinture
        return "no reading: 0% used"
    if all(g.reset_passed(now) for g in gauges):
        return "stale reading (window reset): 0% used"
    cap, label = result.limits.get(gauge.key) or (
        gauge.pace_cap(now, result.profile.pace_multiplier, result.profile.pace_ceiling),
        "pace cap")
    bound = ("pace %.0f%%" % (cap * 100)) if label == "pace cap" \
        else "%s %.0f%%" % (label, cap * 100)
    if not gauge.resets_at:
        return "%s %.0f%% used (undated gauge, %s)" % (
            gauge.key, gauge.used_at(now) * 100, bound)
    return "%s %.0f%% used (%s)" % (gauge.key, gauge.used_at(now) * 100, bound)


def placeable(result: Evaluation, now: float) -> bool:
    """#1672 : ce relevé autorise-t-il le **placement** d'une session ?

    Un relevé **partiel** (une fenêtre datée remise à zéro, une autre encore
    ouverte) reste plaçable tant que son âge ne dépasse pas ce qui reste de
    **chaque** fenêtre encore ouverte (`cost.reading_outdated`) : au-delà,
    c'est une information trop ancienne pour engager un tour — le 15/10, un
    relevé de 30 % vieux de quatre jours a envoyé un tour sur un compte Claude
    à sa limite hebdomadaire. C'est une **prudence**, pas la preuve d'un quota
    atteint : le relevé n'est qu'une borne inférieure, et la remise à zéro qui
    rend la pause nécessaire la rouvre (L71, 0 %).

    Oui pour un relevé **absent** (L117 : un compte jamais utilisé passe en
    premier), pour un relevé **courant** et pour un relevé dont toutes les
    fenêtres datées sont échues (L71, 0 %). La continuité et le forçage ne
    passent pas par ici."""
    return not result.gauges or not reading_outdated(result.gauges, now)



def _informative_partial(gauges, result: Evaluation, now: float) -> bool:
    """#1566 N1 — un relevé partiel porte-t-il encore une information utilisable ?

    Un relevé partiel (une fenêtre datée remise à zéro, une autre encore
    ouverte, #1561) reste **non vérifié** : #1561 le classait après tout état
    vérifié. Le pilote en a borné le classement (GO du 15/10, lot #1566) : il
    garde le rang d'un relevé **courant** (fraîcheur 1, jamais 0) quand

    * l'âge d'**au moins une** fenêtre encore ouverte est connu, fini et non
      futur, et ne dépasse pas ce qui lui reste (`now - read_at <=
      resets_at - now`) — même frontière que `cost.reading_outdated` (#1672),
      et lue par fenêtre : une fenêtre courte échue conservée par
      `codex_gauges.reconcile` ne vieillit pas la semaine récemment observée
      (#2843) ; et
    * **chaque** fenêtre encore ouverte (non datée comprise : elle ne prouve
      pas sa remise à zéro) reste sous son plafond effectif moins `RESERVE`
      (`Evaluation.limits` : rythme, fenêtre courte, plafond fixe ou d'overage
      — jamais `held_cap` seul, qui ignore les plafonds fixes).

    Sans preuve (âge inconnu, aucune fenêtre datée ouverte, plafond absent), le
    relevé reste périmé : fraîcheur 2. L'incident #1672 (relevé de quatre
    jours, fenêtre 7 j à 9 h de sa remise à zéro) échoue la borne d'âge et
    reste écarté du placement par `placeable`.
    """
    gauges = list(gauges)
    running = [g for g in gauges if not g.resets_at or not g.reset_passed(now)]
    ages = []
    for gauge in running:
        stamp = getattr(gauge, "read_at", None)
        if not stamp:
            continue
        age = now - float(stamp)
        if not math.isfinite(age) or age < 0:
            continue
        ages.append((age, gauge))
    if not ages:
        return False
    if not any(g.resets_at and age <= g.resets_at - now for age, g in ages):
        return False
    for gauge in running:
        found = result.limits.get(gauge.key)
        if found is None:
            return False
        if gauge.used_at(now) >= max(0.0, found[0] - RESERVE):
            return False
    return True


def _empty_after_reset(gauges, now: float) -> bool:
    """#3586 : un relevé antérieur au reset ne pèse plus quand tout vaut 0 %.

    Une fenêtre remise à zéro depuis la lecture est vide (`used_at` rend 0,
    L71) ; si **toutes** les fenêtres valent 0 % à `now`, le relevé ancien ne
    prouve aucun budget entamé. Il n'est plus classé « périmé derrière tout
    état vérifié » (#1561) : il s'essaie en premier, comme un relevé absent
    (L117), et le premier tour relit la jauge (`evaluate`/`record_gauges`) —
    c'est la sonde qui confirme le 0 %. Un compte réellement au seuil reste
    écarté par `Evaluation.ok`/`placeable` ; `auth_blocked` et les plafonds
    durs restent intacts, jamais d'overage.

    Sans gauge (relevé absent), la règle L117 existante s'applique : ce helper
    ne la double pas.
    """
    gauges = list(gauges)
    return bool(gauges) and stale_reading(gauges, now) \
        and all(g.used_at(now) <= 0.0 for g in gauges)


def _rank_key(profile: Profile, evaluations: dict, now: float) -> tuple:
    """La clé de classement d'un compte (0034 amendée, #1561, #1580, #1566).

    * la fraîcheur du relevé (#1561, #1566 N1) : sans relevé d'abord (L117,
      amendement 0034 du 2026-10-10 — un compte jamais utilisé compte pour
      0 % et passe en premier, pour ne pas laisser un forfait inutilisé), puis
      un relevé dont la fenêtre remise à zéro est vide (0 % partout, #3586 —
      essayé comme L117, la sonde du premier tour confirme), puis un relevé
      courant — ou un relevé partiel encore informatif (`_informative_partial`,
      GO pilote du 15/10) —, puis un relevé périmé, qui ne passe jamais avant
      un état vérifié ;
    * la perte par seconde d'ici la remise à zéro (`lost_rate`, la plus
      rapide d'abord, #1789/N1) ;
    * le retard sur le rythme (`lag`, le plus petit rapport d'abord) ;
    * l'échéance de la capacité inutilisée (la plus proche d'abord, 0034 §1) ;
    * la part de fenêtre perdue à cette remise à zéro (`lost_at_reset`, la
      plus grosse d'abord) ;
    * l'ordre déclaré (`priority`, L226b « primaries last »).

    Le budget qui s'évapore passe avant le retard et l'ordre déclaré : demande
    du propriétaire du 2026-10-15 (#1580) — le choix automatique ne suit plus
    un ordre fixe de priorités, il consomme d'abord ce qui sera perdu à la
    remise à zéro. Le retard reste le critère suivant : à perte égale, il
    équilibre les comptes et porte les plafonds fixes (L226b)."""
    result = evaluations[profile.name]
    gauges = result.gauges
    if _empty_after_reset(gauges, now):
        # #3586 : 0 % partout, aucun budget entamé à départager — l'ordre
        # déclaré décide, comme entre deux relevés absents (L117) ; la sonde
        # du premier tour relit la jauge et confirme le 0 %.
        return (0, 0.0, 0.0, float("inf"), 0.0, priority(profile))
    found = expiry(gauges, now)
    # #1561 : absent (0, L117), courant (1), périmé (2) — le périmé après tout
    # état vérifié ; #1580/#1789 : le budget perdu avant le retard, lui-même
    # avant l'ordre déclaré ; #1566 N1 : un partiel encore informatif garde le
    # rang d'un courant ; #3586 : un relevé dont la fenêtre remise à zéro est
    # vide (0 % partout) s'essaie en premier, comme L117.
    if not gauges:
        freshness = 0
    elif not stale_reading(gauges, now) or _informative_partial(gauges, result, now):
        freshness = 1
    else:
        freshness = 2
    return (freshness,
            -round(lost_rate(gauges, now), 9),
            round(lag(gauges, now, result.limits)[0], 6),
            found[0] if found is not None else float("inf"),
            -round(lost_at_reset(gauges, now), 6),
            priority(profile))


def rank(items: list[Profile], evaluations: dict, now: float, *,
         paced: bool = True) -> list[Profile]:
    """Les comptes plaçables, du premier à consommer au dernier (0034 amendée).

    Clé (#1561, #1580, #1789, #1566 N1) : d'abord un relevé **courant** — un
    compte dont le relevé est périmé (une fenêtre remise à zéro depuis la
    lecture, une autre encore ouverte) ne passe jamais avant un compte dont
    l'état est vérifié, quel que soit l'ordre de bascule, sauf s'il reste
    informateur (plus bas) ; puis le budget **qui s'évapore** — la perte par
    seconde d'ici la remise à zéro (`lost_rate`, la plus rapide d'abord,
    #1789/N1), le retard sur le rythme (`lag`, le plus petit rapport d'abord),
    l'échéance de la capacité inutilisée (la plus proche d'abord ; sans
    fenêtre en cours, après), la part perdue à cette remise à zéro
    (`lost_at_reset`, la plus grosse d'abord, #1580) ; puis l'ordre déclaré
    (`priority`, L226b « le compte par défaut en dernier », décision owner du
    2026-10-07).

    #1672 : ce dernier recours est **borné**. Un relevé plus vieux que ce qui
    reste de chaque fenêtre encore ouverte (`cost.reading_outdated`) est une
    information trop ancienne pour engager un tour : il **écarte** le compte du
    placement — le 15/10, le choix automatique a pris un compte Claude à sa
    limite hebdomadaire sur la foi d'un relevé de 30 % vieux de quatre jours,
    et le tour a été refusé ; sans compte plaçable, le choix pause. La
    continuité (`choose`, étape 2) garde son chemin : une session déjà sur le
    compte y reste et son tour rapporte la jauge ; un forçage manuel reste
    souverain.

    #1566 N1 : un relevé partiel qui reste **informateur** — âge sous ce qui
    reste d'une fenêtre datée ouverte, et chaque fenêtre ouverte sous son
    plafond effectif moins `RESERVE` (`_informative_partial`, GO pilote du
    15/10) — concourt avec les relevés courants : c'est la fin de l'inversion
    de l'ordre de bascule quand seule une fenêtre courte s'est remise à zéro.
    Sans preuve, il reste périmé (fraîcheur 2) ; un relevé écarté par
    `placeable` ne revient pas par cette règle.

    #3586 : un relevé périmé dont la fenêtre remise à zéro est **vide** (0 %
    partout à `now`, `_empty_after_reset`) ne porte aucun budget entamé à
    départager : il s'essaie en premier, comme un relevé absent (L117), et
    l'ordre déclaré décide ; la sonde du premier tour relit la jauge et
    confirme le 0 %. La borne #1672 (`placeable`) reste : un relevé plus vieux
    que la fenêtre ouverte n'entre pas.

    Un relevé **absent** garde la règle L117 (amendement 0034 du 2026-10-10) :
    un compte jamais utilisé compte pour 0 % et passe en premier, pour ne pas
    laisser un forfait inutilisé. Un relevé dont **toutes** les fenêtres
    datées sont remises à zéro — et aucune fenêtre non datée encore ouverte
    (#1566 N2) — est courant (L71, 0 %) : le compte est replaçable.

    `paced=False` (garde de budget désactivée) : un compte au seuil de son
    rythme reste utilisable, jamais un compte écarté par une règle absolue
    (`Evaluation.hard`) — ni un compte à relevé trop ancien, dont l'état réel
    est inconnu."""
    usable = [(i, p) for i, p in enumerate(items)
              if placeable(evaluations[p.name], now)
              and (evaluations[p.name].ok if paced else evaluations[p.name].unpaced_ok)]
    return [p for _i, p in sorted(
        usable, key=lambda pair: _rank_key(pair[1], evaluations, now) + (pair[0],))]


def reason_of(ranked: list[Profile], evaluations: dict, now: float) -> str:
    """La raison du choix d'une nouvelle session (journal, `accounts list`) :
    « no reading: unverified, tried first (L117) ; avant : primaire seven_day
    35 % utilisé (rythme 55 %) ». Un relevé périmé (#1561) n'apparaît que
    faute de relevé courant, et le dit.

    L'ordre déclaré ne s'annonce que s'il a réellement décidé, les critères de
    budget étant à égalité (`_rank_key`, #1580) ; la perte par seconde
    s'annonce de même quand c'est elle qui a décidé (#1789/N1)."""
    first = ranked[0]
    gauges = evaluations[first.name].gauges
    if not gauges:
        # L117 (amendement 0034 du 2026-10-10) : un compte jamais utilisé
        # compte pour 0 % et passe en premier — la raison le dit, sans
        # prétendre à un 0 % mesuré (#1561).
        why = "no reading: unverified, tried first (L117)"
    elif _empty_after_reset(gauges, now):
        # #3586 : la fenêtre remise à zéro est vide ; le relevé s'essaie en
        # premier comme L117, la sonde du premier tour confirme le 0 %.
        why = explain(evaluations[first.name], now)
        if len(ranked) > 1 and priority(first) != priority(ranked[1]):
            why += ", first in the failover order (priority %d)" % priority(first)
    elif stale_reading(gauges, now):
        # #1561 : un relevé périmé est essayé faute de mieux (L71), pas choisi
        # sur un rythme qui n'est plus mesuré. #1672 ne l'écarte que lorsqu'il
        # est plus vieux que toute fenêtre encore ouverte (`placeable`) ; ici
        # il reste dans le rang. #1566 N1 : encore informatif (âge sous le
        # reste d'une fenêtre ouverte, plafonds moins la réserve), il peut
        # passer avant un relevé courant — la raison le dit « partiel », sans
        # prétendre à une mesure courante.
        still = _informative_partial(gauges, evaluations[first.name], now)
        current_behind = any(
            evaluations[p.name].gauges
            and not stale_reading(evaluations[p.name].gauges, now)
            for p in ranked[1:])
        why = "%s, %s" % (
            explain(evaluations[first.name], now),
            "partial reading still informative (age within an open window, "
            "below cap - reserve)" if still and current_behind
            else "tried (no current reading)")
    else:
        why = None
        if len(ranked) > 1:
            mine = _rank_key(first, evaluations, now)
            other = _rank_key(ranked[1], evaluations, now)
            if mine[0] == other[0] and mine[1] != other[1]:
                # #1789/N1: the budget that evaporates fastest decided (the
                # freshness of the two readings is equal, so it really is the
                # first criterion that differs)
                why = "%s, losing its budget fastest" % explain(
                    evaluations[first.name], now)
            elif mine[:-1] == other[:-1] and mine[-1] != other[-1]:
                # L226b: the failover order decided, the budget criteria tied
                why = "%s, first in the failover order (priority %d)" % (
                    explain(evaluations[first.name], now), priority(first))
        if why is None:
            why = "%s, furthest behind its pace" % explain(evaluations[first.name], now)
    if len(ranked) > 1:
        why += "; ahead of: %s" % ", ".join(
            "%s %s" % (p.name, explain(evaluations[p.name], now)) for p in ranked[1:])
    return why


def _overage_reason(harness: str, agent: str | None) -> str:
    """#1883 : le motif publié dans `account_holds` pour un compte quitté en
    dépassement (`account_holds` portait les retenues de rythme de 0027 §3,
    remplacées par le classement 0034 ; elle sert ici à une **règle absolue** :
    un compte facturé au-delà de son plan n'est pris par aucun runner de
    l'hôte, même quand la lecture locale d'un autre processus ne voit pas le
    dépassement. La retenue n'est posée QUE par une bascule réelle qui quitte
    ce compte, dans la transaction de la bascule : une pause, ou une lecture
    locale saine, ne bloque personne."""
    return ("%s plan: in overage (billed beyond the plan)%s"
            % (harness, ", seen by %s" % agent if agent else ""))


def _held_for_overage(store, host: str, harness: str, now: float) -> set[str]:
    """#1883 : les comptes retenus pour overage sur cet hôte, encore valides.

    Lecture d'un seul jeu (`holds`), pour les chemins qui n'ont pas besoin de
    la cohérence retenues/identités (`_etat_placement` la donne)."""
    return _holds_valid(store.holds(host, harness), now)


#: #1883 : vivacité d'une empreinte de dossier publiée pour l'hôte — une
#: configuration divergente qui cesse de tourner cesse d'être un conflit après
#: cette fenêtre, sinon un profil retiré bloquerait le compte pour toujours.
IDENTITY_WINDOW_S = 3600.0


def identity_of(profile: Profile) -> str | None:
    """#1883 : l'empreinte du dossier résolu d'un compte `config_dir`.

    Jamais le chemin : un dossier de configuration est un secret d'hôte, seuls
    des noms et des empreintes passent par la base (`storage.accounts`). None
    pour un compte sans dossier (clé d'API), qui n'a pas d'identité à défendre.
    """
    if profile.kind != "config_dir":
        return None
    return hashlib.sha256(os.path.realpath(profile.home()).encode("utf-8")).hexdigest()


def _publish_identities(store, host: str, harness: str, items: list[Profile],
                        *, cfg=None, environ=None) -> None:
    """#1883 (R3 auditeur, B2) : publie l'empreinte du dossier résolu de chaque
    compte `config_dir` de ce processus.

    Écriture NARROW (`account_identities` seulement), faite par TOUT processus
    qui peut placer un tour, choix critique compris : sans elle, deux
    configurations exclusivement critiques ne se voient jamais et le conflit
    d'identité n'existe pour personne. La simulation (L39) ne publie jamais.

    #2580 : le même point publie aussi la revendication locale de l'hôte
    (`account_claims`) quand l'appelant fournit `cfg` — identité du mesh lue
    en base, jamais le DSN ; le registre de l'hôte voit alors un compte
    déclaré par deux meshes dont les bases sont distinctes. Un registre local
    illisible ne défait pas la publication en base déjà faite : le placement
    ne s'arrête pas pour lui."""
    for profile in items:
        empreinte = identity_of(profile)
        if empreinte is not None:
            store.publish_identity(host, harness, profile.name, empreinte)
    if cfg is None:
        return
    from . import account_claims
    try:
        account_claims.claim(cfg, store, host, harness, items, environ=environ)
    except (OSError, ValueError):
        pass


def _conflicts_of(vues: dict, items: list[Profile]) -> set[str]:
    """#1883 : les comptes dont une empreinte vivante contredit la leur."""
    conflits: set[str] = set()
    for profile in items:
        empreinte = identity_of(profile)
        if empreinte is None:
            continue
        if any(vue["identity"] != empreinte for vue in vues.get(profile.name, [])):
            conflits.add(profile.name)
    return conflits


def _holds_valid(holds: dict, now: float) -> set[str]:
    """#1883 : les comptes dont la retenue court encore à `now`."""
    return {name for name, hold in holds.items()
            if hold.get("until_ts") is not None and float(hold["until_ts"]) > now}


def _etat_placement(store, host: str, harness: str, items: list[Profile],
                    now: float) -> tuple[set[str], set[str]]:
    """#1883 (R3 auditeur, B1) : retenues valides et identités contredites,
    lues ENSEMBLE en une transaction sous le verrou de placement.

    Rend `(retenus, conflits)`. C'est le seul état sur lequel une décision de
    placement est validée : une retenue publiée pendant la lecture des
    identités ne peut plus être manquée (les deux jeux viennent de
    `store.placement_state`, pas de deux lectures séparées)."""
    etat = store.placement_state(host, harness, now - IDENTITY_WINDOW_S)
    return _holds_valid(etat["holds"], now), _conflicts_of(etat["identities"], items)


#: #1883 (R3 auditeur, B1) : nombre de reprises de la validation finale de
#: `choose`. Chaque reprise rejoue `decide` sur un état relu sous le verrou de
#: placement ; si l'état bouge encore à la borne, le tour est refusé — aucun
#: compte peut-être retenu n'est rendu.
VALIDATION_ESSAIS = 3


def identity_conflicts(store, host: str, harness: str, items: list[Profile],
                       now: float, *, publish: bool = True,
                       cfg=None) -> set[str]:
    """#1883 (arbitrage pilote, option a) : les comptes dont l'identité est
    contredite par une autre configuration de l'hôte.

    `publish=True` publie d'abord les empreintes de CE processus, puis relit
    celles des autres (`placement_state`, sous le verrou de placement) : deux
    empreintes **vivantes** (vues depuis `IDENTITY_WINDOW_S`) sous le même
    (hôte, harnais, nom) sont deux dossiers différents derrière le même nom.
    Le compte est alors refusé au placement — prendre la lecture de l'un pour
    une preuve saine de l'autre est exactement l'incident de #1883 (le
    « secondaire » de deux profils de pc-smichea, l'un à 47 %, l'autre à
    34 %). `publish=False` (alertes, simulation : aucune écriture partagée) se
    contente de relire. `cfg` (#2580) fait publier aussi la revendication
    locale de l'hôte."""
    if publish:
        _publish_identities(store, host, harness, items, cfg=cfg)
    etat = store.placement_state(host, harness, now - IDENTITY_WINDOW_S)
    return _conflicts_of(etat["identities"], items)


def choose(db, host: str, harness: str, items: list[Profile], book, *,
           now: float | None = None, agent: str | None = None, environ=None,
           simulate: bool = False, session_account: str | None = None,
           paced: bool = True, critical: bool = False, margin: bool = False,
           model: str = "", cfg=None) -> Choice:
    """Choisit le compte du prochain tour (voir l'en-tête du module).

    `session_account` : le compte sous lequel tourne la session en cours de
    l'agent (None : pas de session, ou session neuve) — elle le garde tant
    qu'il est sous son seuil (0034 §4).

    Écritures : le dernier compte choisi (`account_active`,
    comparer-et-changer, journalisé `bascule`), l'historique des jauges, et
    l'empreinte du dossier résolu des comptes `config_dir` (`account_identities`,
    pour que les autres processus de l'hôte voient un même nom déclaré vers
    deux dossiers ; jamais en simulation). Une erreur de base remonte :
    l'appelant suspend (fail-closed).

    `simulate=True` (L39, `ameesh resume`) : le compte que choisirait le
    prochain tour, SANS AUCUNE écriture (ni bascule, ni historique des
    jauges, ni empreinte d'identité, ni revendication locale #2580) ;
    `switched` dit la bascule qui aurait lieu.

    `paced=False` (garde de budget désactivée, L30) : quand aucun compte
    n'est sous son seuil, le rythme est levé — mais jamais les règles
    absolues de L226b (overage, plafonds fixes : décision du propriétaire du
    2026-10-07). Aucun compte hors de ces règles : pause, comme avec la garde.

    `critical` (L242b, agent critique : orchestrateur, conception, auditeur) :
    le choix est propre à l'agent — il n'écrit jamais le dernier choix
    partagé (ni ligne initiale, ni bascule `config`) et ne lève aucun
    forçage (un forçage au seuil ne le retient pas s'il reste un compte) ;
    sans autre compte, le compte par défaut sert au-delà de son plafond
    implicite de 50 % (jamais en overage ; rythme et `overage_cap` tenus,
    même avec `paced=False`), en dernier. #1883 (R3 auditeur, B2) : il publie
    en revanche l'empreinte de son dossier (écriture narrow,
    `account_identities`) — sans quoi deux configurations exclusivement
    critiques ne se verraient jamais.

    `margin` (L242b (3), #389 r1) : un agent critique qui tourne sous un
    harnais emprunté ne revient au sien qu'avec la réserve des non-critiques
    (`RESERVE`) sous le rythme — sans cette marge, chaque franchissement du
    plafond coûterait une rotation avec résumé.

    `model` (#1580 tranche 3) : le modèle de la session qui va tourner ; ses
    seaux propres (widget omarchy) comptent pour lui seul. Défaut `''` :
    modèle inconnu, les seaux de modèle sont écartés — jamais attribués au
    hasard ; l'appelant qui connaît le modèle de l'agent le passe.
    """
    now = time.time() if now is None else now
    # #2580 : le refus d'un compte partagé avec un autre mesh est appliqué à
    # TOUT le choix (continuité et forçage compris), avant de lire le compte
    # actif ; lecture seule. `_publish_identities` publiera ensuite les
    # revendications de l'hôte, jamais en simulation.
    if cfg is not None:
        from . import account_claims
        items = account_claims.mark_refused(cfg, items, db=db, now=now)
    store = storage.of(db).accounts
    # L242b : un choix critique n'écrit jamais l'état partagé (#324 r1)
    readonly = simulate or critical
    row = store.active(host, harness) or (
        {"account": items[0].name, "forced": False} if readonly
        else store.init_active(host, harness, items[0].name))
    # #3755 : un forçage arrivé à échéance rend la main à l'automate, même si
    # personne n'a lancé `ameesh accounts auto` (journalisé `auto`).
    row = _expire_force(store, host, harness, row, now, readonly=readonly)
    names = [p.name for p in items]
    cache: dict[str, Evaluation] = {}

    def ev(profile: Profile) -> Evaluation:
        if profile.name not in cache:
            cache[profile.name] = evaluate(book, profile, items, now, environ,
                                           record=not simulate,
                                           reserve=margin or not critical,
                                           model=model)
        return cache[profile.name]

    active_name = row.get("account")
    if active_name not in names and readonly:
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
    # #4166 : un compte fournisseur épuisé n'est jamais choisi non plus. Un
    # autre compte déclaré du harnais le remplace (le repli, sans geste) ; s'il
    # n'en reste aucun, le compte épuisé est rendu tel quel pour que la garde
    # `provider_credits_exhausted` de l'exécuteur s'applique (sonde bornée
    # comprise), comme un compte en échec d'authentification est rendu.
    from . import provider_credits
    try:
        epuises = provider_credits.exhausted_names(db, harness)
    except db_mod.DbError:
        epuises = set()
    # L226b/#1883 (R1 auditeur, B1) : les retenues d'overage publiées par les
    # AUTRES processus de l'hôte priment sur tout placement — forçage,
    # continuité comme classement. Un compte facturé au-delà du plan ne reçoit
    # aucun tour, même si la lecture locale d'ici le dit sain (deux
    # configurations peuvent lire deux dossiers sous le même nom) : c'est le
    # contrat « jamais de dépassement ».
    # #1883 (arbitrage pilote, option a) : deux configurations de l'hôte qui
    # déclarent le même nom vers deux dossiers résolus rendent toute lecture
    # ambiguë — le compte est refusé au placement (et signalé à l'owner), pas
    # pris pour une preuve saine.
    # #1883 (R3 auditeur, B1/B2) : les empreintes de CE processus sont publiées
    # (sauf en simulation, L39 ; y compris pour un choix critique, sans quoi
    # deux configurations exclusivement critiques ne se voient jamais), PUIS
    # les retenues et les identités sont lues ENSEMBLE, en une transaction sous
    # le verrou de placement : aucune retenue publiée pendant la lecture des
    # identités n'est manquée. Le point de validation final relit ce même état.
    if not simulate:
        _publish_identities(store, host, harness, items, cfg=cfg, environ=environ)
    retenus, conflits = _etat_placement(store, host, harness, items, now)

    # #1883 (R2 auditeur, B1) : la décision est une fonction du jeu d'écartés
    # (retenues d'overage, identités en conflit) ; `choose` la rejoue sur un
    # jeu FRAIS avant de rendre un compte (voir la reprise après l'appel).
    def decide(retenus: set[str], conflits: set[str], sick: dict) -> Choice:
        """La décision de placement pour un jeu d'écartés donné.

        Un compte retenu ou d'identité contredite ne reçoit aucun tour : ces
        écartés priment sur le forçage, la continuité et le classement.
        Les jeux sont relus à l'instant du placement (R2 auditeur, B1), comme
        les échecs d'authentification `sick` (#4489)."""
        nonlocal row, cache
        candidates = [p for p in healthy(items, sick) if p.name not in epuises]
        ecartes = retenus | conflits

        def stuck_exhausted(own: Profile | None) -> Profile | None:
            """Le compte épuisé à rendre quand aucun autre n'est plaçable : la
            garde de l'exécuteur le retient (et sa sonde bornée passe)."""
            for profil in (own, active, *items):
                if (profil is not None and profil.name in epuises
                        and profil.name not in ecartes):
                    return profil
            return None

        # 1. A forced authentication failure yields to another healthy account;
        # without a replacement the runner's auth guard holds the agent.
        def forced(profile: Profile) -> Choice:
            current = ev(profile)
            if profile.name in sick:
                return Choice(None, profile, forced=True, auth_blocked=True,
                              reason="forced account %s: authentication failing on this host" % profile.name)
            if profile.name in conflits:
                return Choice(None, profile, reason=(
                    "account %s forced (ameesh accounts use): declared with another "
                    "folder by a second configuration of this host" % profile.name),
                    forced=True)
            if profile.name in retenus:
                return Choice(None, profile, reason=(
                    "account %s forced (ameesh accounts use): in overage for another "
                    "runner of this host" % profile.name), forced=True)
            if profile.name in epuises:
                return Choice(None, profile, reason=(
                    "account %s forced (ameesh accounts use): provider credits "
                    "exhausted" % profile.name), forced=True)
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
            # the force is lifted (journaled `auto`) and the automatic choice runs.
            # #1672 : le remplaçant doit être réellement plaçable — un compte à
            # relevé périmé ne lève pas le forçage pour une pause.
            free = [p for p in candidates if p.name != active.name
                    and p.name not in ecartes
                    and placeable(ev(p), now)
                    and (ev(p).ok if paced else ev(p).unpaced_ok)]
            # #4166 : le motif de la levée dit l'épuisement, pas un seuil
            raison_forcee = (
                "forced account %s: authentication failing; %s free"
                % (active.name, free[0].name) if free and active.name in sick
                else "forced account %s: provider credits exhausted; %s free"
                % (active.name, free[0].name) if free and active.name in epuises
                else "forced account %s at threshold (%s); %s free"
                % (active.name, cache[active.name].reason,
                   free[0].name if free else "no account"))
            if critical:
                pass        # L242b : choix propre à l'agent, le forçage reste aux autres
            elif not free:
                # #4166 : forcer un compte épuisé sans autre compte plaçable ne
                # donne pas de tour ; la garde des crédits le retient et le dit
                if active.name in epuises:
                    return Choice(active, active, forced=True,
                                  why="forced account %s: provider credits exhausted"
                                      % active.name)
                if active.name in sick:
                    return Choice(active, active, forced=True, auth_blocked=True,
                                  why="forced account %s: authentication failing on this host" % active.name)
                return held
            elif not simulate and not store.set_auto(
                    host, harness, expected=active.name, reason=raison_forcee):
                # #295: the row changed since it was read — a human forced another
                # account meanwhile: that force holds (never lifted in the same turn)
                row = store.active(host, harness) or {}
                now_forced = by_name(items, row.get("account"))
                if row.get("forced") and now_forced is not None:
                    return forced(now_forced)

        # 2. continuité : la session garde son compte tant qu'il est sous son seuil
        # (les autres comptes ne sont alors pas relus : rien à choisir). Un compte
        # retenu ne l'est pas : c'est le PROCHAIN tour que ce choix décide, il ne
        # part pas sur un compte facturé au-delà du plan (R1 auditeur, B1).
        own = by_name(items, session_account) if session_account else None
        if (own is not None and own in candidates and own.name not in ecartes
                and own.name not in epuises and ev(own).ok):
            return Choice(own, active, kept=True,
                          why="continuity: the session stays on %s, under its "
                              "threshold" % own.name)
        for profile in items:
            ev(profile)
        # #1883 : jamais de placement sur un compte qu'un autre runner a fui en
        # dépassement, ni sur un nom que deux configurations déclarent vers
        # deux dossiers
        if ecartes:
            candidates = [p for p in candidates if p.name not in ecartes]
        # 3. le compte le plus en retard sur son rythme (amendement 0034, L117)
        ranked = rank(candidates, cache, now)
        if not ranked and not paced:
            # L226b: past its pace a healthy account stays usable, never past an
            # absolute rule
            ranked = rank(candidates, cache, now, paced=False)
        beyond = False
        if not ranked and critical:
            # L242b : le compte par défaut au-delà de son plafond de 50 %, pour un
            # agent critique seulement, en dernier (jamais en overage) ; au retour
            # d'un emprunt, avec la même marge (#389 r2)
            relaxed = dict(cache)
            for profile in candidates:
                if profile.default and profile.cap is None:
                    relaxed[profile.name] = evaluate(book, profile, items, now, environ,
                                                     record=False, critical=True,
                                                     reserve=margin)
            # le rythme tient ici même garde désactivée (#324 r1)
            ranked = rank(candidates, relaxed, now)
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
            # #4166 : plus aucun compte plaçable, mais un compte épuisé existe —
            # le rendre plutôt que pauser : la garde des crédits le retient
            # (aucun tour) et sa sonde bornée peut le rouvrir ; un autre compte
            # déclaré aurait été choisi ci-dessus (le repli).
            stuck = stuck_exhausted(own)
            if stuck is not None:
                return Choice(stuck, active, why=(
                    "no %s account to place: provider credits exhausted on %s "
                    "(bounded probe only)" % (harness, stuck.name)))
            # #1672 : un relevé plus vieux que toute fenêtre encore ouverte est
            # écarté du placement — le dire, plutôt que de le compter comme « au
            # seuil » (le relevé ne mesure plus l'état du compte). C'est une
            # prudence sur une information ancienne, pas la preuve d'un quota.
            # la remise à zéro la plus proche d'une fenêtre encore ouverte rouvre
            # le compte (L71) : l'annoncer, comme pause_label le fait au seuil
            from .cost import until_label
            withheld = ["%s until %s" % (p.name, until_label(min(
                            float(g.resets_at) for g in cache[p.name].gauges
                            if g.resets_at and not g.reset_passed(now)), now))
                        for p in items
                        if p.name not in sick and cache[p.name].gauges
                        and reading_outdated(cache[p.name].gauges, now)]
            # #3969 : un compte invalide (profil en 755 à la mise en service
            # d'agents-3) n'est pas une pause budget jusqu'au reset du compte
            # au seuil : la configuration de l'hôte est la cause, dite telle
            invalides = no_valid_reason(harness, cache)
            why = "%sall %s accounts at threshold — %s" % (
                pause_label(cache), harness, _label(cache))
            ecartes_why = []
            if conflits:
                ecartes_why.append(
                    "%s declared with different folders by two configurations of "
                    "this host" % ", ".join(sorted(conflits)))
            if retenus:
                ecartes_why.append("%s held for overage seen on this host"
                                   % ", ".join(sorted(retenus)))
            if invalides:
                why = invalides + "".join("; %s" % e for e in ecartes_why)
            elif ecartes_why:
                why = ("%sno %s account to place: %s — %s"
                       % (pause_label(cache), harness, "; ".join(ecartes_why),
                          _label(cache)))
            elif withheld:
                why = ("%sno %s account to place: reading older than every open "
                       "window (%s) — %s" % (pause_label(cache), harness,
                                             ", ".join(withheld), _label(cache)))
            return Choice(None, active, reason=why)
        target = ranked[0]
        why = reason_of(ranked, cache, now)
        if sick:
            why = "skipped authentication failing accounts on %s: %s; %s" % (
                host, ", ".join(sorted(sick)), why)
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
            # #1883 (R2 auditeur, B1) : rien n'est écrit ici — c'est le point de
            # validation de `choose` qui relit la retenue de ce retour, comme de
            # tous les autres.
            return Choice(target, active, why=why)
        switched = {"from": active.name, "to": target.name, "kind": "bascule", "reason": why}
        if simulate:
            return Choice(target, target, switched=switched, why=why)
        # L226b/#1883 (R1 auditeur, B2) : la retenue valide sur la CIBLE est
        # revérifiée DANS la transaction de bascule, sous le même verrou que les
        # autres workers — un classement sur un instantané périmé ne fait jamais
        # partir un tour sur un compte retenu. La retenue du compte quitté en
        # dépassement est posée dans la MÊME transaction.
        left_ev = cache.get(active.name)
        quitte_en_overage = left_ev is not None and in_overage(left_ev.gauges, now)
        if store.switch_guarded(
                host, harness, expected=active.name, to=target.name, kind="bascule",
                reason=why, agent=agent, now=now,
                hold_reason=_overage_reason(harness, agent) if quitte_en_overage else None,
                hold_until=(now + OVERAGE_HOLD_S) if quitte_en_overage else None):
            # #1883 (R2 auditeur, B2) : les retenues échues de la cible sont
            # nettoyées DANS la transaction de bascule, sous le verrou ; un
            # `release` ici effacerait une retenue publiée après la validation.
            return Choice(target, target, switched=switched, why=why)
        # refusée : un autre worker a changé le compte, ou une retenue est apparue
        # sur la cible entre le classement et la bascule. Ne JAMAIS rendre une
        # cible retenue : le tour partirait facturé au-delà du forfait.
        fresh = store.active(host, harness) or {}
        if target.name in _held_for_overage(store, host, harness, now):
            return Choice(None, by_name(items, fresh.get("account")) or active,
                          reason="no %s account to place: %s held for overage seen on "
                                 "this host" % (harness, target.name))
        # course : un forçage posé entre-temps prime, évalué comme en 1 (L226b :
        # un primaire au-dessus de son plafond ou un compte en overage n'est pas lancé)
        if fresh.get("forced"):
            return forced(by_name(items, fresh.get("account")) or active)
        return Choice(target, by_name(items, fresh.get("account")) or target, why=why)

    # #1883 (R2 auditeur, B1) : point unique de validation de TOUT retour qui
    # lance un tour (hors `auth_blocked`, que la garde de l'exécuteur ne lance
    # pas). Les retenues d'overage ET les identités contredites sont relues
    # ENSEMBLE (`_etat_placement`, une transaction sous le verrou de
    # placement), après la lecture des jauges comme après la bascule : un
    # compte qu'un autre runner de l'hôte vient de fuir en dépassement, ou
    # qu'une seconde configuration déclare vers un autre dossier, ne reçoit pas
    # de tour — même si le forçage, la continuité ou le classement l'ont retenu
    # sur un instantané périmé.
    # #1883 (R3 auditeur, B1) : la reprise est validée à son tour. La décision
    # rejouée sur l'état frais peut choisir un compte dont une retenue est
    # publiée pendant son classement ; chaque reprise repasse donc par la même
    # relecture, et une borne franchie refuse le tour (`VALIDATION_ESSAIS`) —
    # jamais un compte peut-être retenu rendu.
    # #4489 : l'échec d'authentification est relu avec elles — un échec
    # publié par un autre runner de l'hôte entre la lecture de `sick` et la
    # bascule ne fait pas partir un tour sur ce compte (la reprise saute au
    # suivant sain, ou rend `auth_blocked`).
    choice = decide(retenus, conflits, sick)
    for _ in range(VALIDATION_ESSAIS):
        if choice.profile is None or choice.auth_blocked:
            return choice
        frais, conflits_frais = _etat_placement(store, host, harness, items, now)
        sick = auth_failure.failing(db, host, harness)
        if (choice.profile.name not in frais
                and choice.profile.name not in conflits_frais
                and choice.profile.name not in sick):
            return choice
        # reprise : la décision est rejouée sur l'état frais — forçage levé
        # pour un compte libre, continuité rompue, compte écarté du classement.
        # Le compte actif partagé est relu : une bascule de la décision
        # précédente a pu le déplacer.
        row = store.active(host, harness) or row
        active_name = row.get("account")
        active = by_name(items, active_name) or active
        choice = decide(frais, conflits_frais, sick)
    return Choice(None, active, reason=(
        "no %s account to place: the placement state changed on every re-read "
        "(a hold, an identity conflict or an authentication failure was "
        "published while deciding); "
        "retrying next turn" % harness))


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
    row = _effective_active(row, time.time())
    return by_name(items, (row or {}).get("account")) or items[0]


# --------------------------------------------------------------------------
# forçage manuel (`ameesh accounts use|auto`)
# --------------------------------------------------------------------------

def force_expired(row: dict | None, now: float) -> bool:
    """#3755 : le forçage de `row` a-t-il atteint son échéance ?"""
    if not row or not row.get("forced"):
        return False
    until = row.get("forced_until")
    try:
        return until is not None and float(until) <= float(now)
    except (TypeError, ValueError):
        return False


def _effective_active(row: dict | None, now: float) -> dict | None:
    """La ligne active, forçage échu lu comme automatique (sans écriture)."""
    if force_expired(row, now):
        return dict(row, forced=False, forced_until=None)
    return row


def _expire_force(store, host: str, harness: str, row: dict | None, now: float,
                  *, readonly: bool) -> dict | None:
    """#3755 : lève un forçage échu (`auto`, journalisé) et relit la ligne.

    Un choix en simulation ou critique ne doit aucune écriture partagée
    (L39, #324 r1) : il lit le forçage échu comme automatique, sans le lever
    en base — le prochain choix d'un exécuteur le fera."""
    if not force_expired(row, now):
        return row
    if readonly:
        return _effective_active(row, now)
    store.set_auto(host, harness, reason=FORCE_EXPIRED_REASON,
                   expected=(row or {}).get("account"),
                   expected_until=(row or {}).get("forced_until"))
    return store.active(host, harness) or _effective_active(row, now)


def force(db, host: str, harness: str, items: list[Profile], name: str, *, by: str,
          book=None, now: float | None = None, until: float | None = None,
          allow: bool = False, reason: str = "") -> bool:
    """Force le compte `name` (forçage manuel, journalisé `manuel`).

    #3755 — un forçage ne contourne pas la règle du propriétaire (« jamais de
    dépassement ») : avec `book`, le compte est évalué ; une retenue
    d'overage publiée pour l'hôte (même quand sa lecture locale est saine) est
    relue SOUS le verrou de placement, dans la transaction de la bascule, et
    le forçage est **refusé** s'il est en overage ou au-delà de son rythme.
    `allow` (`--force "raison"`) passe outre, à condition de donner une
    `reason`, journalisée dans `account_switches` ; la retenue est alors levée
    dans la même transaction.

    `until` (epoch) est l'échéance du forçage ; par défaut
    `DEFAULT_FORCE_S` après `now` (#3755). Au terme, le choix et l'affichage
    le lisent comme automatique et le choix le lève (journalisé `auto`)."""
    profile = by_name(items, name)
    if profile is None:
        raise AccountError("unknown account for %s: %r (known: %s)"
                           % (harness, name, ", ".join(p.name for p in items)))
    problems = check(profile)
    if problems:
        raise AccountError("account %s/%s unusable: %s"
                           % (harness, name, "; ".join(problems)))
    now = time.time() if now is None else float(now)
    if allow and not (reason or "").strip():
        raise AccountError(
            "account %s/%s: --force needs a reason (journalized in "
            "account_switches)" % (harness, name))
    store = storage.of(db).accounts
    if book is not None:
        try:
            state = evaluate(book, profile, items, now, record=True)
        except Exception as exc:  # noqa: BLE001 - garde fail-closed
            raise AccountError(
                "account %s/%s cannot be evaluated (%s): refusing to force it; "
                "repair the reading first (no force overrides an unreadable gauge)"
                % (harness, name, exc))
        if state.problems:
            raise AccountError("account %s/%s unusable: %s"
                               % (harness, name, "; ".join(state.problems)))
        if not state.ok and not allow:
            raise AccountError(
                "account %s/%s is %s — refusing to force it: the owner's rule "
                "is never above the plan; use --force \"reason\" to force it "
                "anyway (reason and expiry are journalized)"
                % (harness, name, state.reason))
    if until is None:
        until = now + DEFAULT_FORCE_S
    try:
        until = float(until)
    except (TypeError, ValueError):
        raise AccountError("unreadable forcing expiry: %r" % (until,))
    if until <= now:
        raise AccountError("forcing expiry must be in the future")
    texte = "manual override by %s" % by
    if (reason or "").strip():
        texte += ": %s" % " ".join(str(reason).split())
    # #1883/#3755 : la retenue de la cible est relue SOUS le verrou de
    # placement, dans la même transaction que la bascule et sa levée : une
    # retenue publiée pendant l'évaluation des jauges (ou après une première
    # lecture) est vue ici — sans `allow`, elle refuse sans rien écrire.
    result = store.force_switch(host, harness, initial=items[0].name, to=name,
                                reason=texte, agent=None, forced_until=until,
                                now=now, allow=allow)
    if result.get("held"):
        raise AccountError(
            "account %s/%s is held for overage by another runner of this host "
            "(%s) — refusing to force it; use --force \"reason\" to force it "
            "anyway" % (harness, name, result["held"]))
    return bool(result.get("changed"))


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

    Chaque jauge porte son plafond effectif (`pace_ceiling`, #1176) et sa
    raison (`ceiling_reason`, vide pour le plafond de base) : le relèvement de
    fin de fenêtre s'y lit, comme dans la raison du compte.

    #2648 : l'évaluation applique la réserve des agents non critiques
    (`reserve=True`, L242b), comme `choose` — l'état, les plafonds affichés et
    le prochain choix sont ceux de la pause : un compte dans la bande de
    réserve n'est ni « ok » ni proposé.
    """
    now = time.time() if now is None else now
    from . import account_claims
    # #2580 : un compte déclaré aussi par un autre mesh de l'hôte. Lecture
    # unique, partagée par le refus des profils et la ligne d'affichage.
    shares = account_claims.shared(cfg, db=db, now=now)
    by_share = {(item["harness"], item["account"]): item for item in shares}
    # #3647 : au rythme compilé du canon, comme `profiles` (le choix)
    declared = {harness: account_claims.mark_refused(cfg, items, now=now,
                                                     shares=shares)
                for harness, items in with_canon_pace(
                    cfg, parse(getattr(cfg, "accounts", None) or {})).items()}
    store = storage.of(db).accounts if db is not None else None
    rows: list[dict] = []
    for harness, items in declared.items():
        if harnesses and harness not in harnesses:
            continue
        active = store.active(cfg.host, harness) if store else None
        # #3755 : un forçage échu s'affiche comme automatique (lecture seule).
        active = _effective_active(active, now)
        holds = store.holds(cfg.host, harness) if store else {}
        active_name = (active or {}).get("account") or items[0].name
        evaluations = {}
        for profile in items:
            try:
                # #2648 : la lecture qui désigne le compte d'une nouvelle
                # session applique l'éligible de la pause d'un non-critique
                # (réserve des critiques comprise). Sinon `accounts list`
                # proposait un compte que la garde mettait en pause sur une
                # autre fenêtre, et l'alerte `plan_underused` promettait la
                # même capacité.
                result = (evaluate_fn or evaluate)(
                    book, profile, items, now, record=record, reserve=True)
            except Exception as exc:  # noqa: BLE001 — #1600 : jauge illisible
                # #1600 : une lecture en erreur ne fait pas tomber l'affichage
                # (`ameesh accounts list`) : le compte reste listé, sans marge
                # promise, avec sa raison — même motif que `rebalance._offer`
                # et `live_fleet`.
                result = Evaluation(profile)
                result.reason = "gauge readings unreadable: %s" % exc
            evaluations[profile.name] = result
            hold = holds.get(profile.name) or {}
            rows.append({
                "harness": harness,
                "account": profile.name,
                "type": profile.kind,
                "where": profile.describe(),
                "active": profile.name == active_name,
                "forced": bool((active or {}).get("forced")) and profile.name == active_name,
                # #3755 : l'échéance du forçage (epoch), None sans forçage
                "forced_until": ((active or {}).get("forced_until")
                                 if profile.name == active_name else None),
                "ok": result.ok,
                "reason": result.reason,
                "problems": list(result.problems),
                "hold_until": hold.get("until_ts"),
                # #2580 : revendiqué vivant par un autre mesh de l'hôte
                # ({harness, account, others}) ; None = pas de partage
                "shared": by_share.get((harness, profile.name)),
                "hourly_usd": profile.hourly_usd,
                # #1075/#1028 : le rythme du compte, pour les affichages qui
                # lisent ses jauges hors d'`evaluate` (cost.report, progress) ;
                # #1227 N3 : ces lecteurs recalculent le plafond de rythme du
                # compte, pas seulement sa valeur
                "pace_multiplier": profile.pace_multiplier,
                # #3647 : sa source (canon, ou réglage local signalé) et les
                # fenêtres au rythme relevé ({clé: perte prévue})
                "pace_source": profile.pace_source,
                "pace_raises": dict(result.raises),
                # L226b: failover order and overage flags
                "priority": priority(profile),
                "overage": _overage_row(profile, result.gauges, now),
                "gauges": [
                    # L71 : une fenêtre échue compte pour 0 % ; le dernier
                    # relevé reste lisible (`last_used`) ; #1176 : le plafond
                    # effectif de la fenêtre et sa raison ; #1561 : `read_at`
                    # date le relevé, `stale` (posé par `_forecast`) dit qu'il
                    # n'est plus un instantané courant.
                    # #1702 (N3) : un relevé repris de l'historique partagé
                    # (`from_history`) expose son âge, pour qu'un hôte sans
                    # lecture locale n'affiche pas une mesure ancienne comme
                    # un instantané du jour. #1272 : la provenance voyage avec
                    # la jauge jusqu'à l'affichage — `produced_at` None =
                    # relevé non daté (rejeu du harnais ou flux sans marqueur),
                    # jamais présenté comme une mesure fraîche.
                    {"key": g.key, "used": g.used_at(now), "last_used": g.used,
                     "cap": (result.limits.get(g.key)
                             or (g.pace_cap(now, result.profile.pace_multiplier,
                                            result.profile.pace_ceiling),))[0],
                     "pace_ceiling": (result.ceilings.get(g.key)
                                      or Ceiling(result.profile.pace_ceiling)).value,
                     "ceiling_reason": (result.ceilings.get(g.key)
                                        or Ceiling(result.profile.pace_ceiling)).reason,
                     "elapsed": g.elapsed(now), "resets_at": g.resets_at,
                     "reset_passed": g.reset_passed(now),
                     "read_at": getattr(g, "read_at", None),
                     "from_history": bool(getattr(g, "from_history", False)),
                     "read_age_s": (now - float(g.read_at)
                                    if getattr(g, "read_at", None) is not None else None),
                     "produced_at": getattr(g, "produced_at", None),
                     "source": getattr(g, "source", "")}
                    for g in result.gauges
                ],
                "_gauges": list(result.gauges),
                "_limits": result.limits,
                "_ceilings": result.ceilings,
            })
        _forecast(rows[len(rows) - len(items):], items, now, evaluations)
    return rows


def in_overage(gauges, now: float) -> bool:
    """Is the account in overage at `now` (a plain list of gauges: no)?"""
    return bool(getattr(gauges, "in_overage", None) and gauges.in_overage(now))


def _overage_row(profile: Profile, gauges, now: float) -> dict:
    """The overage flags of an account, for `ameesh accounts list --json`."""
    read = getattr(gauges, "overage", None)
    return {"setting": profile.overage, "capable": overage_capable(profile, read),
            "in_use": in_overage(gauges, now)}


def _forecast(rows: list[dict], items: list[Profile], now: float,
              evaluations: dict) -> None:
    """L74 (0034 §5) : pour chaque compte, la capacité **perdue** à la prochaine
    remise à zéro de chaque fenêtre en cours si rien ne change (`losses`), son
    échéance (`expires_in_s`), et le compte que prendrait une nouvelle session
    (`next`, avec sa raison `why`, la même que celle du choix — L117).

    #1561 : un relevé périmé n'annonce ni perte ni échéance — la capacité
    inutilisée qu'il décrit (30 % d'une fenêtre de 7 j encore ouverte) n'est
    qu'une borne inférieure, et l'annonce « 70 % perdus à la remise à zéro »
    était fausse. Le compte porte `stale` pour les affichages.

    #1580 : les `Evaluation` réellement calculées par `report` sont reprises
    telles quelles — reconstruire ici des évaluations sans plafonds changeait
    les rapports de rythme (plafonds fixes, plafond court, `pace_multiplier`)."""
    for row, profile in zip(rows, items):
        stale = bool(row["_gauges"]) and stale_reading(row["_gauges"], now)
        row["stale"] = stale
        row["losses"] = [] if stale else [
            {"key": g.key, "resets_at": found[0], "in_s": found[0] - now, "lost": found[1]}
            for g in row["_gauges"] for found in [g.expiring(now)] if found is not None]
        found = None if stale else expiry(row["_gauges"], now)
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


def _mention_non_datee(row: dict, gauge: dict) -> str:
    """#1272 : une jauge Claude sans instant de production se dit NON DATÉE.

    La provenance d'un relevé Claude est l'instant de production publié par le
    marqueur du tour (#1132). Sans lui — rejeu seul du harnais, ou flux sans
    marqueur — l'affichage ne peut pas la présenter comme une mesure fraîche :
    la valeur reste lisible, mais porte « non daté : provenance inconnue ».
    La mention ne conclut pas « périmé » : aucune preuve de péremption n'est
    inventée (ni âge maximal, ni mtime, ni plafond). Codex ne publie pas
    d'instant de production ; ses jauges ne portent pas la mention. Le widget
    omarchy (#1580) non plus, mais il date lui-même son relevé (`updatedAt`,
    fraîcheur vérifiée par `widget_usage.read`) : ses jauges ne la portent pas.
    """
    from . import widget_usage
    if row.get("harness") != "claude" or gauge.get("produced_at") is not None \
            or gauge.get("source") == widget_usage.SOURCE:
        return ""
    return " [non daté : provenance inconnue]"


def format_rows(rows: list[dict]) -> str:
    """Le tableau des comptes (texte)."""
    if not rows:
        return "no account declared (`accounts` key of the host configuration)"
    lines = ["%-9s %-14s %-7s %-8s %s" % ("harness", "account", "active", "state", "gauges")]
    for row in rows:
        actif = ("forced" if row["forced"] else "yes") if row["active"] else ""
        etat = "ok" if row["ok"] else "limit" if not row["problems"] else "invalid"
        gauges = ", ".join(
            "%s %.0f%% (pace %.0f%%%s%s%s)%s" % (
                g["key"], g["used"] * 100, g["cap"] * 100,
                # #1176 : le plafond effectif et sa raison, quand il est relevé
                ", ceiling %.0f%%, %s" % (g["pace_ceiling"] * 100, g["ceiling_reason"])
                if g.get("ceiling_reason") else "",
                ", reset passed, last reading %.0f%%"
                % (g.get("last_used", g["used"]) * 100) if g.get("reset_passed") else "",
                # #1702 (N3) : un relevé repris de l'historique partagé dit son
                # âge — sans lecture locale, la valeur n'est pas un instantané.
                ", read %s ago" % duration(g["read_age_s"])
                if g.get("from_history") and g.get("read_age_s") is not None else "",
                _mention_non_datee(row, g))
            for g in row["gauges"]) or "—"
        lines.append("%-9s %-14s %-7s %-8s %s" % (
            row["harness"], row["account"], actif, etat, gauges))
        if row.get("stale"):
            # #1561 : le relevé n'est plus un instantané courant (une fenêtre
            # remise à zéro, une autre encore ouverte) — la valeur lue n'est
            # qu'une borne inférieure, pas un « 30 % utilisé » d'aujourd'hui.
            lines.append("%-9s %-14s %-7s %-8s ↳ reading not current: "
                         "window reset since the reading (unverified)"
                         % ("", "", "", ""))
        if row["reason"]:
            lines.append("%-9s %-14s %-7s %-8s ↳ %s" % ("", "", "", "", row["reason"]))
        if row.get("pace_raises"):
            # #3647 : fenêtres au rythme relevé (perte prévue au-delà du canon)
            lines.append("%-9s %-14s %-7s %-8s ↳ pace raised (forecast loss at reset "
                         "above the canon threshold): %s" % ("", "", "", "", ", ".join(
                             "%s %.0f%%" % (key, lost * 100)
                             for key, lost in sorted(row["pace_raises"].items()))))
        if row.get("losses"):
            lines.append("%-9s %-14s %-7s %-8s ↳ lost at reset if nothing changes: %s"
                         % ("", "", "", "", ", ".join(
                             "%s %.0f%% in %s" % (l["key"], l["lost"] * 100,
                                                     duration(l["in_s"]))
                             for l in row["losses"])))
        if row.get("next"):
            lines.append("%-9s %-14s %-7s %-8s ↳ next choice for a new session: %s"
                         % ("", "", "", "", row.get("why") or "—"))
        if row.get("forced") and row.get("forced_until"):
            # #3755 : le forçage a une échéance ; elle est dite en clair.
            lines.append("%-9s %-14s %-7s %-8s ↳ forcing expires in %s (%s), "
                         "then the automatic choice returns" % (
                             "", "", "", "",
                             duration(float(row["forced_until"]) - time.time()),
                             time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(float(row["forced_until"])))))
        if row.get("hold_until") and float(row["hold_until"]) > time.time():
            lines.append("%-9s %-14s %-7s %-8s ↳ held until %s" % (
                "", "", "", "", time.strftime("%Y-%m-%d %H:%M",
                                              time.localtime(float(row["hold_until"])))))
        if row.get("shared"):
            # #2580 : le compte n'est pas seulement à ce mesh ; le refus
            # éventuel est déjà dans `reason` (profil refusé).
            from . import account_claims
            lines.append("%-9s %-14s %-7s %-8s ↳ shared with another mesh of the "
                         "host: %s" % ("", "", "", "",
                                      account_claims.describe(row["shared"])))
    return "\n".join(lines)
