# Comptes multiples par fournisseur (L30, L74, L117)

Les comptes au forfait d'un fournisseur forment un **réservoir**, pas une
liste de secours (décision
[0034](design/decisions/0034-consommer-d-abord-ce-qui-expire.md), qui amende
[0027](design/decisions/0027-bascule-automatique-entre-comptes.md)) : un
forfait non consommé avant sa remise à zéro est perdu. Avant chaque tour, le
compte est choisi ainsi :

1. **Forçage** (`ameesh accounts use`) : ce compte, ou pause s'il est au
   seuil. `ameesh accounts auto` rend la main à la règle.
2. **Continuité** : une session en cours reste sur son compte tant que
   celui-ci est sous son seuil — pas de changement en cours de session pour un
   gain marginal.
3. Sinon (ouverture de session, rotation, ou compte de la session au seuil) :
   parmi les comptes **utilisables** (profil valide, sous le seuil de la garde
   `min(90 %, part écoulée + 10 points)` dans **toutes** leurs fenêtres,
   décision 0019), celui qui a **le plus de retard sur son rythme** : le plus
   petit rapport `utilisé / rythme` (le rythme est le plafond de la garde à cet
   instant de la fenêtre, colonne « rythme » de `ameesh accounts list`), pris
   sur sa fenêtre la plus contraignante (5 h ou 7 jours). Un compte sans
   fenêtre ouverte ou sans relevé compte pour 0 % utilisé : il passe **en
   premier**. À égalité, celui dont la capacité inutilisée expire le plus tôt,
   puis l'ordre déclaré (amendement du 2026-10-10 à 0034, lot L117 : un compte
   jamais utilisé ne doit pas rester inutilisé ; avant, le primaire, toujours
   en fenêtre ouverte, gagnait toujours).
4. Tous les comptes au seuil : pause.

Un relevé dont la fenêtre est échue (`resets_at` passé) compte pour **0 %**
(même règle que L71) : un compte resté à 93 % sur une fenêtre close n'est plus
écarté, il est essayé, et son premier tour rapporte sa jauge. Les jauges sont
relues à chaque choix. Jamais au milieu d'un tour. Étude :
[docs/design/etudes/comptes-multiples.md](design/etudes/comptes-multiples.md).

Le respect des conditions d'utilisation de chaque fournisseur pour l'usage de
plusieurs comptes reste de la responsabilité de l'équipe.

## 1. Préparer un compte secondaire (gestes humains)

ameesh ne se connecte jamais à un compte et ne lit jamais un identifiant :
la connexion est faite une fois, par un humain, dans un dossier dédié.

**Claude Code**

```sh
install -d -m 700 <dossier-claude-2>
# continuité des sessions : partager le stockage des sessions du primaire
ln -s <dossier-du-primaire>/projects <dossier-claude-2>/projects
# mêmes hooks et réglages que le primaire (agent-mail)
ln -s <dossier-du-primaire>/settings.json <dossier-claude-2>/settings.json
CLAUDE_CONFIG_DIR=<dossier-claude-2> claude     # puis /login, avec le compte secondaire
```

Le dossier du primaire est `~/.claude` s'il n'a pas de dossier dédié. Sans le
lien `projects`, chaque bascule fait une rotation avec résumé.

**Codex**

```sh
install -d -m 700 <dossier-codex-2>
cp <CODEX_HOME du primaire>/config.toml <dossier-codex-2>/   # et hooks.json si utilisé
CODEX_HOME=<dossier-codex-2> codex login
```

Ne partagez pas `sessions/` entre deux comptes Codex : les sessions sont
rattachées au compte qui les a créées et portent ses jauges. La bascule se
fait par rotation avec résumé.

**DeepSeek Harness (clé d'API)** : une clé par compte, soit dans une
variable de l'environnement de l'exécuteur (unité systemd, `EnvironmentFile`
0600), soit dans un fichier :

```sh
install -m 600 /dev/null <dossier-des-cles>/deepseek-b.key   # puis y coller la clé
```

## 2. Déclarer les comptes de l'hôte

Dans la configuration de l'**hôte** (`~/.config/ameesh/config.json`), jamais
dans le canon :

```json
{
  "accounts": {
    "claude": [
      {"name": "primaire"},
      {"name": "secondaire", "type": "config_dir", "path": "<dossier-claude-2>"}
    ],
    "codex": [
      {"name": "primaire"},
      {"name": "secondaire", "type": "config_dir", "path": "<dossier-codex-2>"}
    ],
    "deepseek": [
      {"name": "cle-a", "type": "api_key_env", "key_env": "DEEPSEEK_API_KEY_A",
       "hourly_usd": 5, "min_balance": 2},
      {"name": "cle-b", "type": "api_key_env", "key_file": "<dossier-des-cles>/deepseek-b.key"}
    ]
  }
}
```

* L'ordre départage les égalités de retard et d'échéance (par exemple deux
  comptes jamais utilisés, ou les clés d'API sans jauge) : le premier est le
  primaire.
* `config_dir` sans `path` : le dossier que le harnais prendrait de lui-même
  dans l'environnement de l'exécuteur (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`
  hérités s'ils sont posés, sinon `~/.claude` / `~/.codex`). Ses jauges Codex
  sont lues au même endroit, par le mécanisme commun : argument >
  `AMEESH_CODEX_SESSIONS` > `$CODEX_HOME/sessions` > `~/.codex/sessions` ; un
  compte déclaré lit `<son CODEX_HOME>/sessions`.
* Contrôles : dossier à l'utilisateur, **0700**, identifiants présents
  (`.credentials.json` pour Claude, `auth.json` pour Codex : présence
  seulement) ; fichier de clé **0600** ; variable de clé présente. Un compte
  qui échoue n'est jamais choisi, et `ameesh accounts list` dit pourquoi.
* `hourly_usd` : plafond horaire du compte (dépense des tours attribués au
  compte). Il est propre à l'hôte et s'ajoute aux plafonds du mesh, qui se
  règlent en base pour toutes les machines par `ameesh budget set` (L70,
  voir [EXPLOITATION.md](EXPLOITATION.md#plafonds-de-budget-du-mesh--ameesh-budget-l70)) ;
  un compte au seuil bascule, un plafond du mesh atteint met en pause. `min_balance` : le compte est au seuil quand son dernier solde
  relevé est au plus ce montant (relevé par l'exécuteur, ou
  `ameesh cost balance --record`).
* Isolement : sur un hôte à comptes, chaque harnais lancé (tour ou `ameesh
  attach`) part de l'environnement de l'exécuteur **sans** les variables
  d'identifiants — sources de clé de tous les comptes déclarés, et, quand un
  compte est choisi, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `DSH_HOME`,
  `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` — puis reçoit
  seulement celles du compte choisi. Le profil est revalidé à chaque
  lancement, garde de budget active ou non : un profil invalide refuse le
  tour (consigne remise en attente, diagnostic dans `ameesh show`).
* La bascule demande `ameesh migrate` (migration 0028) et un redémarrage de
  l'exécuteur pour relire la configuration.

## 3. Suivre et forcer

```sh
ameesh accounts list [--json]          # comptes, dernier choix, état, jauges, pertes, bascules
ameesh accounts use claude secondaire  # forçage manuel (plus de choix automatique)
ameesh accounts auto [claude]          # retour au choix automatique (0034)
ameesh accounts healthy claude secondaire  # compte reconnecté : déclaré sain (L226b)
ameesh cost report                     # dépense par agent, compte actif, jauges par compte
ameesh cost gauges                     # historique des jauges, par compte
ameesh cost balance --record           # soldes, par compte de clé d'API
ameesh budget                          # plafonds du mesh en vigueur, source, pauses (L70)
```

`ameesh accounts list` montre, pour chaque compte, la capacité **perdue à la
prochaine remise à zéro** de chaque fenêtre en cours si rien ne change, et le
compte que prendrait une nouvelle session, avec sa raison (la même que celle
du choix) :

```
harnais   compte         actif   état     jauges
claude    primaire       oui     ok       five_hour 37% (rythme 83%), seven_day 35% (rythme 55%)
                                          ↳ perdu à la remise à zéro si rien ne change : five_hour 63 % dans 1 h 21, seven_day 65 % dans 3 j 20 h
claude    secondaire             ok       —
                                          ↳ prochain choix pour une nouvelle session : sans relevé : 0 % utilisé, le plus en retard sur son rythme ; avant : tertiaire sans relevé : 0 % utilisé, primaire seven_day 35 % utilisé (rythme 55 %)
claude    tertiaire              ok       —
```

La colonne « actif » est le **dernier compte choisi** pour une nouvelle
session (ligne `account_active`) : des agents dont la session est ouverte sur
un autre compte y restent tant qu'il est sous son seuil.

`accounts list` et `cost report` sont des lectures : ils n'écrivent aucun
relevé de jauge (L71) ; le relevé revient aux exécuteurs, avant chaque tour,
ou à `ameesh cost gauges`. Un relevé dont la fenêtre est échue (`resets_at`
passé) compte pour **0 %** : un compte inutilisé dont le dernier relevé date
d'une fenêtre close n'est plus jugé au seuil (il ne servait pas, donc son
relevé n'était jamais rafraîchi). L'affichage garde le dernier relevé :
« codex-300min 0% (rythme 90%, remise à zéro passée, dernier relevé 93%) ».

Chaque choix de compte est journalisé avec sa raison dans le journal de
l'exécuteur (`[agent] compte claude : secondaire — sans relevé : 0 % utilisé,
le plus en retard sur son rythme ; avant : tertiaire sans relevé : 0 %
utilisé, primaire seven_day 35 % utilisé (rythme 55 %)`, ou `— continuité :
la session reste sur primaire, sous son seuil`), une fois par changement de choix et non à
chaque sondage. Chaque changement du dernier choix est journalisé en base
(`account_switches`, type `bascule`, avec la même raison) et dans le fil de
l'équipe de l'agent qui l'a déclenché. Les retenues jusqu'à la remise à zéro
(`account_holds`) et les « retours au primaire » de 0027 §3 ne sont plus
posés : le choix par retard sur le rythme les remplace. Une
session reprise sous l'autre compte, ou tournée avec résumé, est dite dans le
fil. Codex n'est jamais portable : un changement de compte d'une session
Codex passe toujours par la rotation avec résumé (0027 §5), c'est pourquoi il
n'a lieu qu'au seuil. Tous les comptes au seuil : l'agent passe en pause
(`blocked`, « tous les comptes … au seuil »), comme avant L30.

Le compte d'origine de la session courante est enregistré
(`agent_registry.session_account`, L39) : l'exécuteur l'écrit avec l'id de
session, `ameesh adopt` l'écrit pour une session interactive adoptée (compte
sous lequel son fichier a été trouvé), et il s'efface avec la session. Le
marqueur du flux `events.jsonl` ne sert plus que de repli pour les sessions
antérieures. Quand ce compte est inutilisable (identifiants retirés, forfait
saturé) et que la session n'est pas portable, la rotation ne peut pas résumer
sous l'ancien compte : la session neuve s'ouvre sur un **brief de reprise
déterministe** (voir `ameesh resume` dans [EXPLOITATION.md](EXPLOITATION.md)).

## 4. Failover order and caps (L226b, owner decision 2026-10-07)

"Primaries last, capped": the harness's default account (`~/.claude`,
`~/.codex`, declared without `path`) is the owner's own and only serves as a
last resort. Each account takes these optional settings in the host's
`accounts` key (defaults in brackets):

| setting | meaning |
|---|---|
| `priority` [default account 100, others 50] | rank in the failover order, lower first; the pace (L117) only breaks ties |
| `cap` [0.50 for the default account, none otherwise] | fixed cap on **every** window: above it, the account is ineligible |
| `overage` [`auto`] | may the account be billed beyond its plan? `auto` reads it from the harness (Claude `overageStatus`, Codex `credits.has_credits`), `allowed` or `none` declare it |
| `overage_cap` [0.80] | fixed cap on every window of an account that may be billed beyond its plan |
| `short_window_cap` [none] | fixed cap replacing the pace cap on the short window (5 h) |

A window is held to the lowest of: its pace cap (0019, or `short_window_cap`
on the short window), `cap`, and `overage_cap` when the account may be billed.
An account **in overage** (Claude `isUsingOverage`, Codex a window at 100 %
with credits) is never chosen. The caps apply to session continuity too: a
session on the primary leaves it once the primary is above 50 % of a window.

For the order "Claude tertiary > secondary > primary, Codex secondary >
primary" on a host that declared `primaire, secondaire, tertiaire`:

```json
"claude": [
  {"name": "primaire"},
  {"name": "secondaire", "path": "/home/me/.claude-secondary", "priority": 20},
  {"name": "tertiaire", "path": "/home/me/.claude-tertiary", "priority": 10}
]
```

Bureau's local sprint patch of 2026-10-11 (`~/.config/ameesh/sprint.json`,
`short_window_cap` 0.80 for the non-default accounts) is the setting
`"short_window_cap": 0.8` on those accounts.

`ameesh accounts list --json` shows `priority`, `overage` (setting, capable,
in use) and, per gauge, the `cap` the window is held to.

## 5. Authentication failure: `auth_blocked` (L226b)

A credential can stop working without notice: expired or revoked OAuth token,
"Please run /login", 401 `authentication_error`, gcloud "Reauthentication
failed". Before L226b the turn failed, the consigne went back to pending, and
the agent replayed the same turn until the stop after fast failures (L48).

**Credential health** (table `credential_health`, migration 0060): one row
per (host, harness, account); `account` is the declared account name, `''`
for the default credential of a harness without accounts. `state` is
`healthy`, `failing` or `unknown`; `source` says who wrote it: the sentinel
(`sentinel`, L226a), a failed turn (`turn`) or a human (`manual`).
`failing_since` keeps the start of the failure, `expires_at` the forecast
expiry the sentinel reports.

**End of turn.** A failed turn whose error, short answer (600 characters at
most) or stderr tail is an authentication failure
(`ameesh.auth_failure.classify`) is neither a fast failure nor replayed: the
turn's account is marked `failing` (source `turn`), the consigne goes back to
pending, the agent goes `blocked` with `status_text` "`auth_blocked:
<harness>/<account>`" and `last_error` "`auth_blocked: <reason>`". A turn that
fails otherwise (429, context, exit code) takes the usual path.

**Failover.** The account choice (§ 2 and § 4, `accounts.choose`) is
restricted to **healthy** accounts: a `failing` account is never chosen, not
even by session continuity. The next turn starts on a healthy account, with
the usual session resume or rotation, and the block is lifted.

**Never a paid fallback**: an account above its cap stays out, and a
harness with a plan account never falls back to a pay-per-token API key
(`type: api_key_env`) automatically (`ameesh accounts use` still can). Among
the API keys of a harness without plan accounts, failover stays possible.

**No healthy account under its cap**: the agent stays `auth_blocked`. It
consumes neither consigne nor mail, no turn is tried, and the `auth_blocked`
alert (pushed by default, urgent) goes to the responsible human. It resumes on
its own as soon as its account, or another, is healthy again. A forced account
(`ameesh accounts use`) that fails is not replaced: the agent stays blocked
until `ameesh accounts auto` or the reconnection. If the healthy accounts are
at their cap but a failing one is not, it is `auth_blocked`: reconnecting
unblocks. If every account is at its cap, failing or not, it is the usual
budget pause.

**Recovery.** After the reconnection (`claude /login` in the account's
folder, `gcloud auth login`…), the L226a sentinel sees the account healthy
and records it. Without it, declare it by hand:

```sh
ameesh accounts healthy claude secondaire   # declared account
ameesh accounts healthy claude              # harness without declared accounts
```

`ameesh accounts list --json` shows the table (`credentials`).
