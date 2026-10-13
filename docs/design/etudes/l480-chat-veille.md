---
type: Study
title: "L480 — veille du chat : servir les décisions et le courrier entre deux messages du propriétaire"
description: "Le chat du propriétaire (L123) ne prend un tour que sur un message humain : les décisions et le courrier arrivés entre deux messages ne sont pas présentés. Constat, briques existantes (L75, L78, L79, L121), voie cible (L76/L77) et intérim dans le lanceur (pty, événements servis comme des tours, bornes), avec l'ancrage de decisions.chat_session inchangé."
status: proposed
tags: [chat, veille, decisions, courrier, resident, pty]
generated: { by: "mty-amh-docs-01/deepseek-flash", at: "2026-10-13T13:50:00+00:00" }
sources:
  - { resource: "decisions/0035-agent-resident.md", title: "Décision 0035 (agent résident, acceptée le 2026-10-10)" }
  - { resource: "agent-resident.md", title: "Étude — agent résident, lots L75–L83" }
---

# Constat

Le 2026-10-12 au soir, quatre décisions (#11693, #11820, #11868, #11882) sont
arrivées entre 19:04 et 19:42 sans être présentées : `ameesh chat` n'exécute
`ameesh decisions` qu'au début d'un tour, et un tour ne s'ouvre que sur un
message du propriétaire. La consigne (`consignes/chat.md`, « À chaque tour »)
est donc juste ; c'est l'**absence de tours** qui est le défaut. Le
propriétaire l'a nommé : « c'est un défaut de conception, tu devrais être
lancé avec un goal ».

# Ce qui existe

| Brique | État | Ce qu'elle donne |
|---|---|---|
| `ameesh chat` (L123) | fusionné | session de harnais **interactive**, identité `chat-<humain>`, mode `externe`, **aucun exécuteur** ; l'ancrage `decisions.chat_session` exige que le processus `ameesh decide` descende du lanceur |
| `resident.py` (L75) | fusionné | pilotes de tour (`start/send/steer/interrupt/probe`) pour Claude stream-json, Codex app-server, ACP ; les demandes de permission du harnais y sont **refusées** |
| journal de conversation (L78) | fusionné | table + NOTIFY `ameesh_chat`, écrit par l'exécuteur d'un tour |
| client terminal (L79, #287) | PR #672, GO, en file CI | `ameesh chat <agent>` : historique, LISTEN, entrée tapée → courrier, `!urgent` / `!stop` |
| veille d'agent (L121) | fusionné | minuterie systemd → courrier `consignes/veille.md` ; **le courrier n'ouvre un tour que si un exécuteur réclame l'agent** |
| `ResidentWorker` (L76, #285) | PR #417 armée | le processus de harnais reste ouvert sous bail entre les tours |
| file d'entrée + autorité (L77) | **aucun lot** | messages humains en file, enveloppe, identité locale (`local_accounts`) |

# Voie cible (L76 + L77)

Le chat devient un agent `exécuté` comme les autres : l'exécuteur sert les
entrées (message humain, courrier, événement) comme des tours, le journal
(L78) et le client (L79) portent la conversation, et l'autorité d'un message
humain passe par l'enveloppe d'identité locale (L77) au lieu de l'ascendance
du processus. C'est la décision 0035 ; elle règle le cas général (plusieurs
humains, page Nexlink, rotation). Elle n'est **pas disponible** : L76 (#285)
n'est pas fusionnée et L77 (#286) est en intake, dépendante de #285.

# Intérim écrit, gardé au frigo : la veille dans le lanceur

Arbitrage du pilote (mesh-design, 2026-10-13 13:40) : **option 1** — attendre
L76 (#285) et L77 (#286) et brancher la veille du chat dessus ; pas d'intérim
par courrier, un agent externe n'est pas réveillé. L'intérim décrit ci-dessous
n'est **pas** par courrier (le harnais est réveillé dans sa session ouverte) ;
il est écrit et testé sur la branche `lot/480-chat-veille`, et n'est proposé à
la relecture que si le pilote le décide. Le lot #480 reste bloqué sur #285 et
#286.

Tant que L76/L77 manquent, `ameesh chat` garde la session interactive — et
donc l'ancrage humain intact — mais **le lanceur prête le terminal** au
harnais par un pty et sert lui-même les événements comme des tours :

```
$human ──frappes──► lanceur ──pty maître──► harnais interactif (TUI, permissions)
                     │  ▲
   décisions + courrier (sondage borné) ─────┘  ligne `[veille ameesh] …`
```

* **Sources** : décisions en attente adressées à `$human` (`decisions.pending`)
  et courrier non lu qui ouvrirait un tour (`mail.unread_active`), les mêmes
  requêtes que le réveil d'un exécuteur.
* **Service** : une ligne courte `[veille ameesh] …` écrite dans le pty, comme
  si `$human` l'avait tapée : le harnais ouvre un tour, la consigne lui dit de
  lancer `ameesh decisions` et de présenter, puis le courrier.
* **Bornes** : une lecture toutes les 30 s (aucune sonde CI) ; aucune écriture
  tant que rien n'est nouveau ; au plus un service toutes les 2 min ; pas
  d'écriture pendant les 3 s qui suivent une frappe de `$human` (une session
  active est déjà servie par la consigne) ; au-delà de 15 min d'attente, la
  ligne est servie quand même.
* **Recul** : la veille ne s'arme que si l'entrée et la sortie sont un
  terminal, et seulement dans le lanceur (jamais dans le client L79). Sans
  terminal, ou avec `--no-veille`, le lancement est exactement celui
  d'aujourd'hui. Une base injoignable au démarrage de la veille la désarme
  avec un avertissement, sans toucher à la session.
* **Ce qui ne change pas** : `decisions.chat_session` (ascendance du lanceur),
  le mode `externe`, l'absence de bail, les permissions interactives du
  harnais, `ameesh decide` seul chemin d'enregistrement d'une réponse.

# Limites, à reprendre en L76/L77

* un seul propriétaire à la fois (pas de file, pas d'enveloppe) ; la ligne
  injectée n'a pas d'autorité : elle demande de **présenter**, jamais de
  répondre ;
* perte du TUI si le harnais n'accepte pas l'écriture dans son entrée ;
* pas de service quand la session est fermée (le propriétaire reste seul juge
  de ce qu'il garde ouvert) ;
* le lanceur porte du code de terminal (pty, redimensionnement, nettoyage) :
  classe sensible, revue par une autre famille.
