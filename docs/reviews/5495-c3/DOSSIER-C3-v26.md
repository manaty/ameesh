# #5495 — dossier C3 v2.6 : la prise L2 se mesure sur le tick (60 s), pas sur `running` après la préparation

Auteur : mty-amh-impl-04 (deepseek), lot **#6763**, 10/11/2026. Delta à `DOSSIER-C3-v24.md`
(v2.5.1 `dfbfdd541` conservée). **AUCUN GESTE.** Source : auditeur #209624 point 4, rapport
bureau #6532 du 10/11 19:17Z, séquence C3 du 10/11 19:13–19:15Z (événements `staging.request`
640316, 640354, 640355 ; run `a35a7a4f8bc147f09f6854b0218bf595`). Ce delta ne change ni la
séquence de relève ni les gestes du bureau : il fixe la **mesure** de la prise et l'outil qui la
juge.

## 0. Constat : 124,5 s de `running` ne sont pas 124,5 s de prise

La séquence du 10/11 19:13Z a mesuré 124,5 s entre la soumission et la ligne `running` :

| Événement | Horodatage | Contenu |
|---|---|---|
| `staging.request` 640316 | 19:13:36,758Z | `state=pending`, `deadline=19:18:36,754Z` (`start_timeout_s` = 300 s) |
| `staging.request` 640354 | 19:15:41,275Z | `state=running`, `host=ameesh-agents-1`, `run=a35a7a4f…`, `started=19:15:41,275Z` |
| `staging.request` 640355 | 19:15:43Z | `state=failed` (C5 de #6532, hors de ce delta) |

Le dossier v2.4 (§2.4 point 6) juge cette prise sur la ligne `running`, avec une borne de 90 s.
Or le tick L2 est la minuterie `ameesh-staging.timer` :

```ini
OnActiveSec=60s
OnUnitInactiveSec=60s
```

soit une passe par minute, sans `RandomizedDelaySec` — ce que `docs/DEPLOIEMENT-HOTE-AGENTS.md`
dit déjà : « La minuterie passe chaque minute ; seul l'hôte qui détient le bail vivant du rôle
commence une tentative. » La borne de 90 s vaut donc **1,5 × la période réelle du tick** (60 s) et
mesure la **prise au tick suivant**, pas la mise en route.

Le code de l'exécuteur sépare ces deux instants (`src/ameesh/staging_executor.py`, ordre de
`tick`) : les empreintes de la passe sont calculées d'abord, puis le lien d'exécution
`execution` (#6520) est écrit **avant** la préparation longue (opérations d'hôte, `git fetch`
borné 60 s, CI à la forge, régressions) ; la ligne `running` n'est écrite qu'après. Un `git
fetch` d'une minute suffit à expliquer 124,5 s après une prise au premier tick. Le runtime du
10/11 (`venv-97285a0`, empreintes de 640354) précède #6520 : la prise n'était pas observable,
seule la ligne `running` l'était. Ce n'est ni un tick manqué ni un écart de la borne de prise ;
c'est une **prise non mesurée** par l'ancien runtime.

Conséquence : une relève future qui juge `running` contre 90 s signalera un faux écart à chaque
`git fetch` lent. La prise se juge sur `execution` ; `running` garde sa propre borne, l'échéance
de prise `start_timeout_s` (300 s par défaut, portée par `deadline`).

## 1. Remplacements dans v2.4 (v2.5 et v2.5.1 incluses)

| v2.4 | v2.6 |
|---|---|
| §2.4 point 4 « au tick suivant (≤90 s) » | « au tick suivant (période 60 s, borne 90 s = 1,5 période, mesurée sur le journal L2) » ; le journal AG2 doit dire `standby` / « another role holder » sans départ de séquence. |
| §2.4 point 5 `submit <SHA>` | Relever `TS=$(date +%s)` **immédiatement avant** la soumission ; `TS` sert de `--depuis` au juge (le `_at` de la ligne `pending` est postérieur de moins d'une seconde). |
| §2.4 point 6 « Prise par L2 AG1, borne 90 s : la demande passe `running`… » | « **Prise par L2 AG1**, borne 90 s = 1,5 × la période réelle du tick L2 (60 s) : la **première ligne `staging.request` `pending` portant `execution`** (lien du bail d'exécution de la passe, #6520), écrite après les empreintes et avant `git fetch` borné 60 s, la CI forge et les régressions, est à ≤ 90 s de `TS`. La ligne `running` qui suit est la **mise en route** : elle n'est pas jugée sur 90 s ; elle est bornée par `deadline` (`start_timeout_s`, 300 s par défaut). Runtime antérieur à #6520 (pas de champ `execution`) : consigner « prise non mesurée », sans conclure à un écart. Consigner R et l'heure. Échec : ne pas lancer un second exécuteur ; STOP, consigner, `undrain` et restauration. » |
| Critère C3 « une demande `pending`, puis `running` avec `host=…`, `run=R`, `started>=E1` » | « une demande `pending` ; une **prise** (première ligne `pending` avec `execution`) à ≤ 90 s de `TS` ; puis `running` avec `host=ameesh-agents-1`, `run=R`, `started>=E1`. Runtime sans `execution` : prise non mesurée, non bloquante. » |
| §2.5 « Le juge doit rendre : un seul run, `completed`, … » | Ajouter : « la prise ≤ 90 s (ou non mesurée si le runtime précède #6520) ; `evaluer-c3.py` rend la ligne `KO prise L2…` au-delà de la borne, et `N/M prise L2 non mesurée` — jamais un KO — quand `execution` est absent. » |

Le reste (options, C1–C6, retour, preuves, gestes bureau, SHA actif de v2.5) est inchangé.

## 2. Règle de mesure

| Grandeur | Valeur | Source |
|---|---|---|
| Période réelle du tick L2 | 60 s | `deploy/systemd/ameesh-staging.timer` (`OnUnitInactiveSec=60s`) ; `docs/DEPLOIEMENT-HOTE-AGENTS.md` (« la minuterie passe chaque minute ») |
| Borne de prise | 90 s = 1,5 × 60 s | une période pour le tick suivant + une demi-période pour le démarrage de la passe et l'écriture du lien d'exécution |
| Marqueur de prise | première ligne `staging.request` `pending` avec `execution` | `staging_executor.tick` (#6520) : écrite avant `git fetch`, la CI forge et les régressions |
| Borne de mise en route | `deadline` = `start_timeout_s` (300 s par défaut) | `staging_executor.submit` ; `docs/DEPLOIEMENT-HOTE-AGENTS.md` |
| Runtime sans `execution` | prise **non mesurée** | runtime antérieur à #6520 (ex. `97285a0` du 10/11) : ne jamais conclure à un écart |

`evaluer-c3.py` applique cette règle : `TICK_PERIOD_S = 60`, `PICKUP_BOUND_S = 1,5 × 60 = 90`,
option `--borne-prise` pour un autre tick, et le témoin `temoin-apres.sh` échoue si la minuterie
déployée et la constante divergent. Le juge reste sans réseau et sans écriture.

## 3. Témoins

- **Local** (`temoin-apres.sh`, sans SSH ni base) : C3 réussi avec prise à 60 s → OK ; prise à
  124 s → KO ; prise à 60 s puis `running` à 124 s → OK ; runtime sans `execution` → `N/M`, rc 0 ;
  soumission absente → `N/M`, rc 0 ; minuterie à 60 s et borne 90 s → garde de dérive OK.
- **Base du 10/11 19:13Z** : la fixture réelle (événements 640316/640354/640355) rend
  `N/M prise L2 non mesurée : aucune ligne pending avec execution (runtime antérieur à #6520,
  ou prise non tracée) — pas un écart` ; les KO restants sont ceux de C5 (état `failed`), pas la prise.
- **Relève future** : après une coupure qui installe un runtime ≥ #6520 (le champ `execution`),
  la première ligne `pending` avec `execution` est la prise ; 124 s de `running` ne sont plus
  signalées comme un écart de prise.

## 4. Pièces

| Fichier | Rôle |
|---|---|
| `DOSSIER-C3-v26.md` | Ce delta (mesure de la prise, borne dérivée du tick) |
| `evaluer-c3.py` | Delta v2.6 : critère de prise sur `execution` (N/M si absent), constante `TICK_PERIOD_S` = 60, borne 90, option `--borne-prise` |
| `temoin-apres.sh` | Delta v2.6 : témoins de prise (60 s OK, 124 s KO, non mesurée), garde de dérive minuterie ↔ constante |
| `DOSSIER-C3-v24.md`, `DOSSIER-C3-v25.md` et pièces v2.4/v2.5 | Conservés ; `sonde-c3-v22.sh`, `lecteur-c3.py`, `fenetre-release.sh` inchangés |
| `MANIFEST.json` | sha256 et delta v2.5 → v2.6 |
