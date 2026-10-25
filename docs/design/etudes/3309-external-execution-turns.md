---
type: Study
title: "3309 — tours d'exécution externe natifs : contrat public, exactly-once, usage, heartbeat et révocation"
description: "Étude de la demande IMA #2480 (suite de #2202) : un exécuteur hors de l'arbre de processus du runner ouvre un tour ameesh, avec bail, réservation de courrier, session, corrélation exactly-once, reporting d'usage par modèle, heartbeat et révocation ; réutilise l'exécuteur médié et son contrat stable plutôt qu'une seconde API. Décision owner #132239 (25/10/2026) : option c — attendre le supervisor natif #2202 (S1–S6), IMA reste sur son intérim v1.16, aucun turn.open public pour l'instant ; étude conservée et rattachée à #2202."
status: decided
tags: [api, tour, bail, exactly-once, usage, heartbeat, revocation, executeur-medié, ima, contrat, spending]
generated: { by: "mty-amh-impl-02/deepseek", at: "2026-10-24T07:10:00+00:00" }
sources:
  - { resource: "https://github.com/manaty/ameesh/issues/2480", title: "IMA — Native external-execution turns (follow-up of #2202)" }
  - { resource: "https://github.com/manaty/ameesh/issues/2202", title: "Native support for bounded external-sandbox reviewer turns (closed)" }
  - { resource: "https://github.com/manaty/ameesh/issues/2167", title: "Supported turn-context and turn-observation API (lot #2894)" }
  - { resource: "2202-external-sandbox-turns.md", title: "Note approuvée #2202, décision owner #120571 (2026-10-21) — l'alternative « API d'exécuteur externe » y est la v2 laissée ouverte" }
  - { resource: "l2894-turn-context-api.md", title: "Note approuvée #2167, décision owner #120568 (2026-10-21) — contexte de tour versionné, lots A et B non implémentés" }
  - { resource: "../../../src/ameesh/mediated_executor/contract.json", title: "contrat fermé /api/exec/v1, version 1.9.0, 72 opérations" }
  - { resource: "../../../src/ameesh/mediated_executor/dispatcher.py", title: "une transaction par opération, idempotence, fence, audit" }
  - { resource: "../../../src/ameesh/runner.py", title: "ouverture du tour local : bail, réservation de courrier, marqueur, réservation de compte" }
  - { resource: "../../../src/ameesh/storage/postgres/costs.py", title: "grand livre `turn_costs` et `spent`, exclusion actuelle de `source='device'`" }
  - { resource: "../../../src/ameesh/storage/postgres/budgets.py", title: "enveloppe journalière par compte, reserve/settle/kept" }
  - { resource: "../../../src/ameesh/migrations/0443_account_daily_budget.sql", title: "enveloppe journalière, `turn_costs.billing_account`" }
  - { resource: "../../../docs/EXECUTEUR-MEDIEE.md", title: "identité d'exécuteur, jetons, fence, révocation, contrat 1.x" }
  - { resource: "../../../docs/versions/1.17.0.md", title: "migration 0471 : la clé de dépense devient exacte ; c'est la rupture que le pont IMA ne sait pas absorber" }
  - { resource: "ameesh decide show 132239", title: "Décision owner #132239 (25/10/2026, signée) : option c — attendre le supervisor natif #2202 (S1–S6), IMA reste sur son intérim v1.16, aucun turn.open public" }
---

> **Décision owner #132239 (25/10/2026, signée) : option c.** Attendre le
> supervisor natif #2202 (S1–S6) ; IMA reste sur son intérim v1.16 ; **aucun
> `turn.open` public dans `/api/exec/v1` pour l'instant**. Cette étude est
> conservée sur la branche `lot/3309-external-execution-turns` et rattachée à
> #2202 ; le lot #3309 est clos livré hors cible (note), sans code.

# Besoin

IMA a migré ses rôles sur ameesh le 24/10/2026 (thepackengers/issue-manager-agent#704 et
PR #929) mais exécute encore ses tours avec un **intérim côté IMA** sur ameesh
v1.16.0 : IMA envoie un courrier à un agent du pool, le runner ameesh prend le
bail, réserve l'enveloppe et lance un harnais mince (shim `AMEESH_CODEX_BIN` /
`AMEESH_DSH_BIN`) ; ce harnais attend la fin du Job IMA puis imprime un faux
flux d'usage (`turn.completed` / `status step_end`) pour que le grand livre
enregistre la dépense. Un pont privé lit en SQL `agent_mailbox.meta->'consigne'`
et `spend_pending.created_at` pour recoller le tour et sa dépense.

Cette demande (#2480), suite de #2202, veut quatre choses :

1. **ouvrir un tour pour un exécuteur externe** avec le bail, la réservation et
   la session qu'ameesh fournit déjà, plus un **correlation id exactly-once** ;
   le pont privé disparaît ;
2. **rapporter l'usage par l'API** plutôt que par un faux flux : jetons **par
   modèle**, **part cachée**, drapeau **`estimated`** ;
3. une sémantique de **heartbeat et de révocation** pour un exécuteur **hors de
   l'arbre de processus** du runner ;
4. un **contrat stable entre versions** : IMA reste épinglé à v1.16 tant que la
   rupture de clé de dépense de ≥1.17 (#2166/#2893, migration 0471) n'est pas
   absorbée.

Le besoin réel n'est pas « une API de plus » : c'est **le même tour ameesh**
(bail, époque, courrier, marqueur comptable, enveloppe de compte, grand livre,
révocation) dont **l'exécution sort du processus du runner**, que ce soit un
Job Kubernetes, un pod demandeur, ou un conteneur supervisé par #2202. Deux
notes déjà approuvées encadrent la réponse :

- #2167 (décision **#120568**) : un document de contexte de tour versionné
  (`ameesh-turn-context/1`) et une API d'observation en lecture ;
- #2202 (décision **#120571**) : le supervisor natif qui garde le tour chez le
  runner, avec IMA en lecture seule.

# Ce qui existe déjà

| Fait | Où aujourd'hui | Ce qui manque pour #2480 |
|---|---|---|
| bail et époque | `agent_registry`, `leases.claim/renew/release` ; `begin_turn`/`end_turn` fencés par `(owner, epoch)` | une ouverture composée qui prend le bail **sans** runner local |
| réservation de courrier | `mailbox.reserve` (jeton, TTL), identité de session | une sélection exactly-once par `correlation_id` |
| marqueur comptable | `spend_pending` (une ligne par agent), `turn_id` (#796) | un usage multi-modèle, `estimated`, rattaché au tour |
| enveloppe journalière | `account_budgets` / `account_reservations` (0443), `reserve`/`settle`/`kept` | la classe d'un exécuteur **déclarant** son usage, comptée dans `spent` |
| ressources du tour | `turn_resources` (`turn_id`, host, conteneurs, statut) | état `external` et expiration côté serveur |
| grand livre | `turn_costs` (une ligne par tour), `spend_key` unique et **opaque** | `estimated`, ventilation par modèle, source `external` comptée |
| identité d'exécuteur | enrôlement P-256, jetons d'accès/de session, révocation (L110) | un jeton **de tour** lié à `(executor, turn, epoch)` |
| idempotence | `exec_idempotency` par `Idempotency-Key` d'opération | unicité `(executor, correlation_id)` et rejeu du contexte |
| API distante | `/api/exec/v1`, contrat fermé **1.9.0**, 72 opérations, audit, rate limit | opérations composées `turn.open/usage/heartbeat/close/get` |
| API de lecture | `/api/v1` (`ameesh serve`), **127.0.0.1 seulement**, lecture seule | ne peut pas servir un exécuteur distant ; ne pas l'étendre en écriture |
| contexte de tour #2167 | approuvé, **lots A/B non implémentés** | le même document, produit par l'API d'exécuteur |
| supervisor #2202 | approuvé, **S1–S6 non implémentés** | ne couvre pas un Job créé par IMA (modèle inverse) |

Deux constats pèsent sur le choix :

- **Le contrat `/api/exec/v1` est déjà l'API publique des exécuteurs distants** :
  transaction unique, contrôle de portée, fence sous `FOR UPDATE`, idempotence
  rejouable, audit, limite de débit, révocation d'exécuteur. Il est versionné et
  testé par des jeux dorés. Une seconde API d'exécution externe en `/api/v1`
  (local, sans identité d'exécuteur) dupliquerait exactement ces mécanismes.
- **#2202 a rejeté l'alternative « l'API externe ouvre le tour » pour son
  pilote**, pas pour toujours : sa section « Alternatives » dit qu'« une v2
  pourrait offrir une API étroite `sandbox.admit/bind/result` pour d'autres
  consommateurs, bâtie sur les mêmes tables/fonctions, sans changer le modèle
  natif ». #2480 est cette v2, généralisée : un exécuteur de pool, pas
  seulement un relecteur borné.

# Décision à prendre

| Option | Contenu | Avantages | Coûts / risques |
|---|---|---|---|
| **a. Exécuteur médié, v2** (recommandée) | l'exécuteur enrôlé **ouvre lui-même** le tour via `/api/exec/v1` ; opérations composées `turn.open/usage/heartbeat/close/get` ; nouvelle classe d'usage **déclaré** (`source='external'`, comptée, `estimated`) | réutilise identité, fence, idempotence, audit, révocation et contrat stable existants ; répond littéralement aux 4 demandes ; supprime le shim et le pont | confie l'ouverture du tour au consommateur ; admet une dépense **déclarée** non vérifiable par le relais ; SQL + argent + concurrence → sensible, deux familles |
| **b. Harnais externe piloté par le runner** | le runner ouvre le tour comme aujourd'hui ; l'exécuteur rejoint avec un contexte signé et appelle `usage`/`heartbeat` ; pas d'ouverture externe | plus proche de #2167/#2202 ; aucun déplacement de l'autorité de bail ; plus petit | ne satisfait pas « l'exécuteur ouvre son tour » ; IMA garde un shim ; le Job reste dépendant d'un processus ameesh |
| **c. Supervisor natif #2202 d'abord** | le runner crée et supervise le Job Kubernetes (S1–S6) | déjà approuvé ; IMA ne détient rien | ne couvre pas le pod demandeur ; 35–47 h avant le premier tour IMA ; IMA reste sur son intérim en attendant |
| **d. API neuve `/api/v1/external-turns`** | nouvelle surface, nouvelle authentification, nouveaux schémas | indépendance | duplique l'exécuteur médié ; `/api/v1` est local et en lecture seule ; deux contrats à maintenir ; refusée |

**Recommandation : a**, avec un garde-fou explicite : la classe « usage
déclaré » n'est admise que pour un exécuteur enrôlé, avec un **plafond dur par
tour** (`max_usd`) réservé à l'ouverture, un contrôle serveur à chaque
rapport, et une alerte si l'estimation s'écarte du barème. Sans cette décision,
le lot ne peut pas coder l'ouverture externe : #120571 l'a écartée pour le
pilote, et la dépense déclarée est une décision d'argent.

# Conception recommandée (option a)

## 1. Identité, placement et frontière de confiance

L'exécuteur est un **appareil enrôlé** (L110, `ameesh device enroll`), avec un
hôte `H` et un jeton d'accès. Il n'y a ni nouvelle clé, ni nouveau compte :

- l'agent doit être **admis sur `H`** : la même conjonction que
  `agents.claimable` / `canon_claim_predicate_sql` (`Placement`, tags,
  `admitted_hosts`) ; sinon `forbidden_scope` ;
- le bail est `owner = exec:<executor_id>:<hôte>:<pid>` (forme existante) ;
  `lease_epoch` vient de `claim`, incrémenté une fois ; le TTL est borné par
  `Host.policy.lease_ttl` ;
- la révocation de l'exécuteur (jeton, porte, drain) refuse immédiatement
  toute opération (`executor_revoked`) ; c'est la révocation de dernier recours ;
- un **jeton de tour** (`amt1.…`, jeton opaque haché dans `executor_tokens`,
  même mécanisme que `issue_session_token`, nouveau `kind='turn'` ; la
  contrainte `executor_tokens.kind` reçoit la valeur par une migration
  additive, E2) est émis à l'ouverture, lié à `(executor_id, turn_id, epoch)`,
  d'audience `/api/exec/v1/turn/*` et de TTL `min(deadline, heartbeat TTL)`.
  Il borne les dégâts d'un jeton d'exécuteur volé et porte la « session » du
  tour. Le jeton d'accès de l'appareil reste nécessaire pour `turn.open` ; le
  jeton de tour suffit ensuite. Le jeton n'est jamais écrit dans un courrier,
  un journal ou un résultat.

**Frontière de confiance.** L'exécuteur est *dans* le périmètre de sécurité du
mesh (il détient un jeton d'appareil), pas un humain. Deux classes :

- **relayée** : l'exécuteur passe par `/api/exec/v1/llm/`, le relais mesure et
  applique le plafond ; la dépense est vérifiée par ameesh ;
- **déclarée** (`reported`) : l'exécuteur appelle son fournisseur avec sa
  propre clé ; ameesh ne peut pas vérifier les jetons. C'est cette classe que
  #2480 demande (pod IMA). Elle n'est admise que si le canon le dit
  (`Agent.external.class: reported` ou `relay`) et si le plafond par tour est
  dur côté serveur (§4). Sans `external:` sur la fiche Agent, ou sans
  `Host.policy.external`, l'ouverture est refusée (fail-closed).

## 2. Ouverture exactly-once et enregistrement de corrélation

Nouvelle table additive (numéro réservé par `ameesh work migration 3309` au
moment du code) :

```sql
create table external_turns (
    turn_id          text primary key,              -- turn_resources.turn_id
    correlation_id   text not null,                 -- fourni par l'exécuteur
    executor_id      text not null,
    agent            text not null,
    owner            text not null,                 -- exec:<id>:...
    lease_epoch      bigint not null,
    host             text not null,
    session          text,                          -- session de l'exécuteur, jamais agent_registry.session_id
    request_ids      bigint[] not null default '{}',-- le sous-ensemble autorisé de requests[]
    mail_token       text not null,                 -- jeton de réservation, jamais exposé
    work_item_id     bigint,
    spend_key        text not null unique,          -- opaque, le même que marqueur/grand livre
    marker_created_at text not null,                -- identité exacte du marqueur (#2166)
    billing_account  text,
    reserved_usd     numeric(12,6) not null,
    max_usd          numeric(12,6) not null,        -- plafond dur du tour
    model            text,
    class            text not null
        check (class in ('relay','reported')),
    limits           jsonb not null,                -- instantané immuable
    state            text not null default 'open'
        check (state in ('open','running','closing','succeeded','failed',
                         'cancelled','revoked','expired','lost')),
    opened_at        timestamptz not null default now(),
    deadline_ts      timestamptz not null,
    last_heartbeat_at timestamptz not null default now(),
    closed_at        timestamptz,
    revoked_at       timestamptz,
    revoke_reason    text,
    source           jsonb not null,                -- version ameesh + contrat
    unique (executor_id, correlation_id)
);
create index external_turns_open on external_turns (state, deadline_ts);
create index external_turns_agent on external_turns (agent, state);

create table external_turn_usage (
    turn_id              text not null references external_turns(turn_id),
    report_id            text not null,             -- idempotence du rapport
    model                text not null,
    input_tokens         bigint not null default 0,
    cached_input_tokens  bigint not null default 0,
    cache_write_tokens   bigint not null default 0,
    output_tokens        bigint not null default 0,
    reported_usd         numeric(12,6),
    priced_usd           numeric(12,6),
    estimated            boolean not null,
    price_fallback       text,
    reported_at          timestamptz not null default now(),
    primary key (turn_id, report_id, model)
);
```

`turn.open` est **une seule transaction** (même réservoir, même discipline que
le répartiteur) :

1. `select ... from agent_registry where name = $agent for update` ; refus si
   `lease_owner` vivant et non expiré (`agent_busy`), si l'agent n'est pas
   admis sur `H` (`forbidden_scope`), ou si `spend_pending` porte déjà un
   marqueur non réclamé (`accounting_pending`) ;
2. `claim` (owner `exec:…`, TTL borné), `begin_turn` ;
3. `mailbox.reserve(recipient=agent, owner, epoch, token=<jeton de
   réservation>, ids=request_ids, porteur="external", ttl)` ; le jeton est
   aléatoire, stocké dans `external_turns.mail_token`, jamais exposé ; le TTL
   couvre `deadline_ts` plus la grâce de clôture. Les lignes rendues forment
   `requests[]` ; une demande absente de `request_ids` n'est **pas** autorisée
   pour ce tour (#2167 : la présence dans `requests[]` est l'autorisation, pas
   `delivered_ts`) ;
4. `pending_spend.put_get(agent, start_index=0, turn=<kind>, model,
   work_item_id, turn_id)` — marqueur **avant** toute dépense (L13 B5) ; on
   capture dans la même écriture son identité exacte (`created_at` texte à la
   microseconde, #2166) et `spend_key`, tous deux stockés pour un effacement
   conditionnel exact à la clôture ;
5. `turn_resources.open_turn(turn_id, agent, H, pgid=null, label="external")` ;
6. `storage.budgets.account_reserve(billing_account, agent, usd=max_usd,
   spend_key)` : `reserved_usd = max_usd` de la fiche ; si l'enveloppe du jour
   est pleine, refus `budget_exceeded` **sans** bail ni courrier réservé
   (transaction annulée) ;
7. `insert into external_turns ...` ; le `correlation_id` est unique par
   exécuteur. L'`Idempotency-Key` de l'opération reste obligatoire (seconde
   barrière pour un rejeu HTTP immédiat), mais c'est la ligne durable
   `(executor_id, correlation_id)` qui décide du rejeu au-delà des 24 h de
   l'idempotence d'opération.

Rejeu : même `correlation_id` + même empreinte canonique → **200** avec le
**même** document de contexte (`Idempotency-Replayed: true`), sans nouvelle
réservation ; même `correlation_id` + contenu différent → **409
`correlation_mismatch`** ; un autre exécuteur avec le même `correlation_id`
ne collisionne jamais (clé composite). Une coupure entre le commit et la
réponse est donc sans effet : l'exécuteur rejoue et récupère son tour. Si le
tour a avancé entre-temps, le rejeu rend le contexte **courant** (état, bail,
consommation) et non un instantané périmé ; c'est le même `turn_id`.

Le `correlation_id` est fourni par le client (8–128 caractères, même forme que
`Idempotency-Key`). Le serveur peut aussi en dériver un si absent ? **Non** :
il est obligatoire, c'est la garantie exactly-once demandée.

## 3. Document de contexte rendu (contrat consommateur)

L'ouverture rend une enveloppe `ameesh-external-turn-open/1` : `context` est
le document `ameesh-turn-context/1` de #2167, augmenté d'un bloc `external`
additif et **sans secret** ; `auth` porte le jeton de tour, jamais dans
`context`, jamais dans `turn.get`.

```json
{
  "schema": "ameesh-external-turn-open/1",
  "context": {
    "schema": "ameesh-turn-context/1",
    "generated_ts": 1792833000.123,
    "turn": {
      "id": "8f3c…",
      "agent": "ima-reviewer-12",
      "host": "ima-k8s",
      "kind": "external",
      "started_ts": 1792832999.900,
      "session": "ima-session-…",
      "work_item_id": "3309",
      "correlation_id": "ima-7f3a-…"
    },
    "lease": {"owner": "exec:7f3a:ima-k8s:4121", "epoch": 42,
              "expires_ts": 1792833299.900},
    "requests": [
      {"id": 123456, "sender": "ima-dispatch", "kind": "request",
       "created_ts": 1792832900.0, "delivered_ts": null,
       "work_item_id": "3309", "body": "…", "payload": {"ima": {}}}
    ],
    "accounting": {
      "spend_key": "ima-reviewer-12:0:2026-10-24T07:09:59.123456Z",
      "state": "open",
      "billing_account": "openai-manaty",
      "reserved_usd": 2.0,
      "max_usd": 2.0
    },
    "limits": {
      "deadline_ts": 1792834799.900,
      "max_seconds": 1800,
      "max_mail": 20,
      "usage_report": "turn.usage",
      "heartbeat_s": 60
    },
    "external": {"class": "reported", "executor_id": "7f3a…",
                 "contract": "ameesh-exec-contract/1", "version": "1.10.0"}
  },
  "auth": {"token": "amt1.…", "expires_ts": 1792833299.900,
           "audience": "/api/exec/v1/turn/"}
}
```

Règles :

- `spend_key` est **opaque** : le consommateur ne le parse pas, ne le
  reconstruit pas, ne le compare pas à un float. C'est la réponse à la rupture
  ≥1.17 : la clé peut changer de forme dans une version mineure, le document
  ne change pas de schéma. Le pont IMA actuel qui lit `spend_pending` et joint
  `created_at` disparaît.
- `requests[]` est **l'ensemble d'autorisation** du tour : la présence de
  l'`id` vaut autorisation, même si `delivered_ts` est nul (la remise est
  réglée après le lancement, comme #2167). Une demande retirée avant
  l'ouverture n'y est pas ; une demande retirée après l'ouverture reste
  autorisée pour ce tour (elle ne peut plus être retirée, la réservation la
  tient).
- `turn.session` est la session de **l'exécuteur** (fournie à l'ouverture) ;
  `agent_registry.session_id` n'est **jamais** écrite par cette voie : une
  reprise de session ameesh et une session externe ne se mélangent pas.
- `auth.token` n'apparaît **que** dans l'enveloppe d'ouverture et dans un
  `heartbeat` qui le renouvelle, jamais dans `context` ni dans `turn.get`.
  Aucun DSN, clé de fournisseur, secret ne figure dans le document de
  contexte.

## 4. Reporting d'usage

`turn.usage` accepte un **rapport** idempotent :

```json
{"schema": "ameesh-turn-usage/1",
 "turn_id": "8f3c…",
 "report_id": "r-17",
 "usage": [
   {"model": "gpt-6.1-sol", "input_tokens": 120000,
    "cached_input_tokens": 90000, "cache_write_tokens": 0,
    "output_tokens": 8000, "usd": 0.41, "estimated": true},
   {"model": "deepseek-flash", "input_tokens": 30000,
    "cached_input_tokens": 0, "output_tokens": 2000,
    "usd": 0.012, "estimated": false}
 ]}
```

Réponse :

```json
{"schema": "ameesh-turn-usage/1", "turn_id": "8f3c…",
 "report_id": "r-17", "accepted": true, "replayed": false,
 "usage": {"input_tokens": 150000, "cached_input_tokens": 90000,
           "output_tokens": 10000, "usd": 0.422,
           "cached_share": 0.375, "estimated": true},
 "remaining_usd": 1.578, "revoked": false}
```

Sémantique :

- **par modèle** : une ligne `external_turn_usage` par `(report_id, model)` ;
  `cached_share = cached_input_tokens / max(1, input_tokens +
  cached_input_tokens)` (part des jetons d'entrée servis par le cache) ; les
  compteurs bruts restent la source.
- **`estimated`** : `true` si le coût vient d'une estimation de l'exécuteur ou
  d'un barème incomplet ; le serveur recalcule avec le barème
  (`DEFAULT_PRICES`/`$AMEESH_PRICES`) quand le modèle est connu et retient
  `max(recalculé, déclaré)` pour ne jamais sous-compter ; l'écart > 10 % lève
  une alerte `external_usage_drift`. Modèle sans prix : repli prudent de #225
  (`price_fallback`, jamais zéro).
- **plafond dur** : chaque rapport est comparé au cumul du tour ; si
  `cumul + rapport > max_usd`, le serveur refuse **409 `budget_exceeded`**,
  **révoque** le tour (`revoked`, motif `budget`) et publie l'événement. Le
  `max_usd` est réservé à l'ouverture sur l'enveloppe du compte ; le règlement
  final réserve le rapporté, le reste est relâché. Une classe `reported` ne
  peut donc pas dépasser son plafond par l'API, même si elle peut mentir à la
  baisse ; l'écart est signalé et le plafond par tour borne l'exposition.
- **idempotence** : `(turn_id, report_id)` unique ; même rapport + même
  contenu → rejeu ; même `report_id` + contenu différent → **409
  `report_mismatch`** ; un rapport après clôture → **409 `turn_closed`**, pas
  d'écriture au grand livre (audit + alerte `late_usage`).
- **grand livre** : une seule ligne `turn_costs` par tour à la clôture (le
  total des rapports), avec `source='external'`, `estimated` (nouvelle colonne
  additive, `true` si un rapport l'est), `price_fallback` si un modèle n'a pas
  de prix, `spend_key`, `turn_id`, `work_item_id`, `billing_account`. La
  ventilation par modèle vit dans `external_turn_usage` et se lit par
  `turn.get`/CLI. `spent` **compte** `source='external'` (contrairement à
  `device`, qui reste la déclaration d'un appareil médié dont le relais a déjà
  mesuré la dépense) ; `spent_between` et `paid_lines` suivent la même règle.
- **pas de double compte** : le relais, s'il est utilisé, écrit ses propres
  lignes `source='relay'` ; la classe `relay` interdit `turn.usage` avec un
  `usd` déclaré (le relais fait foi), ou l'ignore et le signale. La classe
  `reported` n'a pas de lignes relais (sinon double compte) : un contrôle
  vérifie l'absence de lignes `relay` pour le `turn_id`.

## 5. Heartbeat, révocation, expiration

`turn.heartbeat` (jeton de tour) :

```json
{"schema": "ameesh-turn-heartbeat/1", "turn_id": "8f3c…",
 "expires_ts": 1792833299.900, "state": "running",
 "remaining_usd": 1.578, "revoked": false}
```

- fence `(executor_id, turn_id, epoch)` + jeton de tour ; renouvelle le bail
  pour `min(ttl, deadline - now)` ; met à jour `last_heartbeat_at` ; rend
  `expires_ts` et `remaining_usd`. Le contrat impose à l'exécuteur un
  heartbeat au moins tous les `ttl/3` (60 s par défaut) ; le TTL est borné par
  `Host.policy.external.turn_ttl_s`. Le **premier** heartbeat (ou le premier
  rapport d'usage) marque le travail commencé et remet le courrier réservé
  (`mailbox.deliver`) ; avant lui, le tour reste `open` et le courrier n'est
  pas remis, donc une expiration le relâche proprement (au plus une fois).
- bail perdu, tour révoqué, expiré ou clôturé : **409** avec le reçu terminal
  (`turn_lost`, `turn_revoked`, `turn_closed`) ; l'exécuteur **doit** arrêter
  son Job et ne plus appeler le relais. C'est la seule « révocation » possible
  hors arbre de processus : ameesh ne peut pas tuer le Job, il **ferme la
  porte** et le dit.
- **expiration** : un balayeur du serveur (thread léger toutes les 15 s, ou
  `ameesh external-turn sweep` sur l'hôte) prend les tours `open/running` dont
  `deadline_ts` est passée, les passe `expired`, libère le bail, publie
  `external_turn` et clôt : courrier **relâché** (non remis) pour qu'il soit
  redistribué, `turn_resources` fermées, enveloppe de compte **kept** (issue
  inconnue, jamais zéro) sauf rapport final accepté, alerte
  `external_turn_expired`. Un redémarrage du serveur rattrape par la même
  requête indexée.
- **révocation** : `ameesh external-turn revoke <turn_id> --reason` (owner,
  pilote ou bureau) écrit `revoked_at`/`revoke_reason`, publie l'événement
  `external_turn` (canal SSE, payload `{turn_id, state, reason}`), puis laisse
  une grâce (`revoke_grace_s`, 60 s) à l'exécuteur pour un dernier rapport et
  une clôture propre ; passé la grâce, le balayeur clôt `revoked`, garde
  l'enveloppe si aucun rapport final, et n'accepte plus rien. La révocation
  d'exécuteur (jeton/porte) est immédiate et couvre tous ses tours.
- **événement** `external_turn` : le canal est ajouté à `events.CHANNELS` ;
  un exécuteur peut s'y abonner (`/events`) ou simplement sonder le heartbeat
  (le contrat ne dépend pas de SSE).
- **adoption** : l'exécuteur peut redémarrer et rejouer son `correlation_id`
  (même tour vivant → contexte rejoué) ou `turn.get` par `turn_id` (reçu
  terminal). Il n'y a pas d'adoption par UID de Job comme #2202 : le Job est
  la responsabilité de l'exécuteur ; ameesh ne corrèle que le tour.

## 6. Clôture exactly-once

`turn.close` :

```json
{"schema": "ameesh-turn-close/1", "turn_id": "8f3c…", "epoch": 42,
 "status": "succeeded", "result": {"verdict": "ok", "sha": "…"}}
```

Le `result` est facultatif, borné à 64 Kio, assaini (pas de secret, pas de
transcript brut), et conservé 90 jours. Une transaction :

1. transition conditionnelle `open/running → closing` (`UPDATE … WHERE state IN
   (...) RETURNING`) ; perdant → reçu existant si même statut, sinon **409
   `turn_closed`** ;
2. ligne `turn_costs` (ou zéro rapport → `usd=0`, `estimated=false`) ;
3. `pending_spend.clear` conditionnel par `(agent, marker_created_at,
   spend_key)` — l'identité exacte capturée à l'ouverture, jamais un float
   relu (#2166) ;
4. si le courrier n'a pas déjà été remis au premier heartbeat, remise
   (`mailbox.deliver`) pour `succeeded`, sinon `mailbox.release` (le courrier
   redevient disponible) ;
5. `turn_resources.close_turn(orphan=false)` ; `end_turn` ; relâche du bail ;
6. `account_budget.settle(spend_key, state='settled')` avec le montant réel
   (le reste de la réservation est relâché) ;
7. `state='succeeded'`, `closed_at`, `result` ; reçu immuable.

Reçu (`ameesh-turn-receipt/1`), stable et rejouable :

```json
{"schema": "ameesh-turn-receipt/1", "turn_id": "8f3c…",
 "status": "succeeded", "closed_ts": 1792833600.0,
 "usage": {"input_tokens": 150000, "cached_input_tokens": 90000,
           "output_tokens": 10000, "usd": 0.422,
           "cached_share": 0.375, "estimated": true},
 "requests": [123456], "spend_key": "…", "executor_id": "7f3a…"}
```

Un `close` rejoué avec le même statut rend le même reçu (200) ; avec un autre
statut → 409. Un `usage` après clôture → 409 sans écriture. Un `close` sans
rapport final : la ligne de grand livre est à zéro **uniquement** si l'état
est `failed`/`cancelled` avant tout travail ; sinon l'enveloppe est `kept`
(issue inconnue) et le tour est `lost`/`expired`, pas `succeeded` — une
clôture `succeeded` sans usage est refusée (`usage_required`), sauf **rapport
de zéro explicite** (`usage: []`, `zero_usage: true`) pour un tour qui n'a
fait aucun appel de modèle : le reçu porte alors `usd=0`, `estimated=false`,
et l'enveloppe est relâchée.

## 7. Machine à états

```
open ── heartbeat/usage ──▶ running ──close──▶ succeeded | failed | cancelled
  │                            │
  │                            ├── deadline / bail perdu ──▶ expired
  │                            ├── révocation ─────────────▶ revoked
  │                            └── serveur perdu / inconnu ─▶ lost
  └── close avant premier heartbeat ──▶ succeeded | failed | cancelled
```

`open`, `running` et `closing` sont vivants ; `succeeded`, `failed`,
`cancelled`, `revoked`, `expired`, `lost` sont terminaux. Seul un terminal
libère le bail, le courrier et le créneau de concurrence. `closing` dure le
temps de la transaction ; une clôture bloquée est reprise par le balayeur
après `close_timeout_s` (30 s) avec le même rejeu.

## 8. Contrat et versionnement

- Le contrat `/api/exec/v1` passe de **1.9.0** à **1.10.0** : ajout de cinq
  opérations (`turns.open`, `turns.usage`, `turns.heartbeat`, `turns.close`,
  `turns.get`), de la portée **E** (« jeton de tour, `(executor, turn, epoch)`
  forcés ») et des schémas ci-dessus. Aucune opération retirée, aucun champ
  retiré/renommé/retypé ; les enums sont additifs et un consommateur traite
  une valeur inconnue comme `unknown`, jamais comme succès.
- Les jeux dorés (`tests/golden/mediated_executor/`) et le test
  `test_l107_executor_contract` sont régénérés ; le test de compatibilité
  rejoue un client 1.9 (corps et en-têtes) contre le serveur 1.10 : toutes les
  opérations 1.9 restent servies à l'identique.
- Rupture = `/2` publié en parallèle au moins une version mineure ; `/1` reste
  servi. Le document `ameesh-turn-context/1` suit la même règle : champs
  optionnels additifs seulement.
- `spend_key` reste **opaque** dans tous les schémas ; aucun code ameesh ne
  décompose la clé écrite (hors le repli historique de compatibilité qui la
  reconstruit pour un marqueur d'avant 0471), et le consommateur ne doit pas
  le faire. C'est la garantie qui permet à IMA de rester compatible quand la
  clé change de forme.
- L'API de lecture `/api/v1` ne change pas ; `turn.get` est la lecture de
  l'exécuteur. Si un humain veut voir un tour externe, le CLI
  `ameesh external-turn show <id>` et le grand livre suffisent.

## 9. Sécurité, bornes, exploitation

- **Autorisation** : jeton d'appareil pour `turn.open` ; jeton de tour pour le
  reste ; portée E vérifiée sous `FOR UPDATE` avec le bail ; `(executor, turn,
  epoch)` forcés par le serveur, jamais lus du corps.
- **Bornes** : corps ≤ `MAX_BODY_BYTES` (1 Mio, limite existante du serveur) ;
  `correlation_id` et `report_id` 8–128 caractères ; ≤ 50 modèles par
  rapport ; jetons ≤ 10^12 ; `usd` ≤ `max_usd` ; `request_ids` ≤ 50 ;
  `result` ≤ 64 Kio ; rate limit par exécuteur (existant) ; audit de chaque
  écriture (`exec_audit`) sans jeton, sans corps, sans secret ; purge à
  90 jours.
- **Canon** : `Agent.external` (`class`, `max_usd`, `max_seconds`, `max_mail`,
  `max_concurrent`, `providers`) et `Host.policy.external` (`enabled`,
  `billing_account`, `turn_ttl_s`, `max_usd`, `revoke_grace_s`). Sans bloc,
  refus. La porte de canon (bureau) relit la fiche et son digest, comme
  #2202 §6 ; une fiche périmée/révoquée ferme l'ouverture.
- **Concurrence** : `max_concurrent` par agent (défaut 1) compte les tours
  vivants sous le verrou de placement ; `max_agents` de l'hôte s'applique au
  bail ; le balayeur et l'ouverture se disputent la même ligne de façon
  fencée.
- **Alertes** : `external_turn_expired` (tour sans heartbeat),
  `external_usage_drift` (écart barème > 10 %), `external_usage_over_reserve`
  (rapporté > réservé), `late_usage` (rapport après clôture),
  `external_turn_revoked`. Aucune n'est poussée par défaut aux humains ;
  l'auditeur et les pilotes les voient.
- **Pas de production** : cette note ne déploie rien ; la qualification réelle
  (un Job Kubernetes d'essai, un vrai Postgres, le chemin documentaire #819)
  est un lot séparé avec go owner pour le staging.

## 10. Tests et qualification

- **Sans base** : contrat (table + schémas + golden + portée E + jeton de
  tour), machine à états, arithmétique d'usage (cache, arrondis, plafond,
  repli), refus fermés.
- **Avec Postgres réel** :
  - deux `turn.open` concurrents, même `correlation_id` → un seul tour, le
    second rejoue ;
  - même `correlation_id`, contenu différent → 409 `correlation_mismatch` ;
  - deux exécuteurs, même agent → un seul bail, l'autre `agent_busy` ;
  - bail expiré, heartbeat après révocation, `close` concurrent, `usage`
    après `close`, `close` sans usage, `result` trop gros ;
  - enveloppe pleine → `budget_exceeded` sans réservation ni courrier ;
  - `spent` compte `external` et **pas** `device` (non-régression) ;
  - balayeur : expiration, `kept`, remise/relâche du courrier, redémarrage ;
  - `turn_resources` fermé, marqueur effacé, reçu immuable.
- **Bout en bout HTTP** : faux exécuteur (open → heartbeat → usage → close),
  rejeu après coupure réseau, révocation pendant le tour, redémarrage du
  serveur, absence de fuite de jeton/corps dans les logs et l'audit.
- **Qualification IMA** (lot séparé, côté IMA) : remplacer le pont et le faux
  flux, vérifier `spend_key` opaque, le chemin #819 (≤ 3 s hors modèle), un
  Job Kubernetes réel en staging, et le retour à v1.16 impossible pendant la
  bascule (le schéma `ameesh-turn-context/1` est additif).

# Livraison proposée

| Lot | Contenu | Classe | Estimation |
|---|---|---|---|
| E1 | tables `external_turns`/`external_turn_usage`, domaine `ExternalTurns` (open/usage/heartbeat/close/get), réservation de courrier et d'enveloppe, tests de course Postgres | sensible (SQL, argent, concurrence) | 2–3 j |
| E2 | opérations de contrat 1.10.0, portée E, jeton de tour, golden/OpenAPI, compatibilité 1.9 | sensible (droits, contrat) | 1,5–2 j |
| E3 | balayeur, révocation, canal SSE, alertes, `ameesh external-turn` CLI | sensible (concurrence, exploitation) | 1 j |
| E4 | docs (`EXECUTEUR-MEDIEE.md`, `docs/api/`), observabilité, runbook | léger | 0,5–1 j |
| E5 | adaptation IMA + qualification Kubernetes/staging (côté IMA, go owner) | hors ameesh | 3–5 j |

Total ameesh ~5–7 j, à comparer à l'estimation de 6 h du lot #3309 : la
demande n'est pas une journée, c'est un contrat. Le lot #3309 devrait devenir
la **note + décision** puis être scindé en E1–E4 (ou porter au minimum E1+E2
dans une session dédiée).

**Intégration.** E1–E4 passent par un **paquet** (§12), pas par une CI de lot
entre deux coupures ; les migrations sont **additives** et appliquées en
staging **avant** de relancer les exécuteurs (§11 bis) ; E1, E2 et E3 sont
sensibles (SQL, droits, argent, concurrence) et exigent deux familles
distinctes, E4 est léger. Aucune action de staging ou de production n'est
incluse dans cette note.

# Alternatives écartées

- **Harnais externe piloté par le runner (option b)** : le runner ouvre le
  tour, l'exécuteur rejoint avec un document signé. C'est le plus petit
  diff et le plus proche de #2167 A/B, mais il ne satisfait pas « l'exécuteur
  ouvre son tour » et laisse IMA dépendante d'un shim ameesh ; il reste le
  repli si l'owner refuse la classe « usage déclaré ».
- **Supervisor natif #2202 (option c)** : déjà approuvé, mais il ne couvre pas
  un Job créé par IMA et il coûte S1–S6 avant le premier tour. Il reste la
  voie pour un relecteur borné que le runner doit réellement superviser.
- **API neuve `/api/v1/external-turns` (option d)** : `/api/v1` est local
  (127.0.0.1), en lecture seule, sans identité d'exécuteur ; il faudrait
  réinventer jetons, fence, idempotence, audit et révocation. Refusée.
- **Réutiliser `turn_costs.insert` tel quel** : il est forcé `source=device`
  (hors plafond), ne porte pas `estimated`, et une ligne par modèle violerait
  l'unicité de `spend_key`. Refusée : le grand livre garde une ligne par tour,
  la ventilation vit à côté.
- **Faire porter le `correlation_id` par `Idempotency-Key` seulement** :
  l'idempotence d'opération est purgée à 24 h et n'est pas unique par agent ;
  un rejeu après 24 h créerait un second tour. Refusée : la corrélation est
  une ligne durable.

# Décision demandée

**Question.** Approuvez-vous les tours d'exécution externe natifs comme une
**v2 de l'exécuteur médié** (`/api/exec/v1` 1.10.0), avec ouverture du tour
par l'exécuteur, corrélation exactly-once, usage par modèle (`estimated`,
part cachée) et heartbeat/révocation, **y compris une classe d'usage déclaré
comptée dans les plafonds** et bornée par un `max_usd` par tour réservé à
l'ouverture ?

- **a. Oui, exécuteur médié v2** (recommandé) : E1–E4 après la décision ;
- **b. Non, garder le tour chez le runner** : implémenter l'option b
  (contexte + usage + heartbeat pour un harnais externe piloté par le
  runner), IMA garde un shim ;
- **c. Attendre le supervisor #2202** : ne rien ouvrir à l'exécuteur, IMA
  reste sur son intérim v1.16 ;
- **d. Amender** : préciser la classe d'usage, le plafond, l'identité ou le
  découpage.

Tant que la décision n'est pas prise, **aucun code n'est écrit** : la
dépense déclarée, l'ouverture du bail par un consommateur et le contrat
public sont trois décisions sensibles que #120571 n'a pas couvertes pour ce
cas.

# Décision prise

**Décision owner #132239** (human:smichea, signée avec la clé de l'owner,
approbation #104, `ameesh decide show 132239`) : **option c** — attendre le
supervisor natif **#2202 (S1–S6)** ; IMA reste sur son intérim v1.16.

Conséquences :

- **aucun `turn.open` public** dans `/api/exec/v1` pour l'instant : ni la v2
  de l'exécuteur médié (option a) ni le harnais externe piloté par le runner
  (option b) ne sont implémentés ;
- cette étude est **conservée** sur la branche
  `lot/3309-external-execution-turns` et **rattachée à #2202** : les lots
  S1–S6 du supervisor natif sont la voie retenue pour les tours d'exécution
  externe ;
- IMA garde son intérim v1.16 (mail + pont privé) ; le suivi externe reste
  manaty/ameesh#2480 ;
- le lot #3309 est **clos livré hors cible** (note), sans fusion de code — il
  n'y a pas de code.

# Sources

- manaty/ameesh#2480, #2202, #2167 ; notes `2202-external-sandbox-turns.md`,
  `l2894-turn-context-api.md` ; décisions owner #120571, #120568, **#132239
  (option c)**.
- `src/ameesh/mediated_executor/contract.json` (1.9.0), `dispatcher.py`,
  `server.py`, `identity.py`, `scope.py`.
- `src/ameesh/runner.py` (ouverture du tour), `src/ameesh/storage/interface.py`
  (`Leases`, `Mailbox`, `PendingSpend`, `TurnCosts`, `TurnResources`,
  `Budgets`), `src/ameesh/storage/postgres/costs.py`, `budgets.py`.
- `docs/EXECUTEUR-MEDIEE.md`, `docs/EXPLOITATION.md`, `docs/versions/1.17.0.md`,
  `src/ameesh/migrations/0443_account_daily_budget.sql`, `0471_spend_pending_exact_key.sql`.
