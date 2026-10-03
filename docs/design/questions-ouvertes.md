---
type: Reference
title: "Questions ouvertes de conception"
description: "Ce qui attend une décision du propriétaire ou une étude, après la session du 2026-10-03."
status: draft
tags: [questions, decisions]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:30:00+02:00" }
---

# Attend le propriétaire

| # | Question | Recommandation de mesh-design |
|---|---|---|
| Q1 (ex-M3) | Quel Postgres durable ? La question change de nature : si Nexlink ou une messagerie porte les messages, ameesh n'a besoin que d'un état d'exécution. | Banc local tant qu'aucun agent réel n'est branché ; décider après le modèle de données v2. |
| Q2 (ex-M4) | Première machine en plus du PC. | Un petit VPS, avec d'abord les agents DeepSeek (clé d'API, pas d'identifiants d'abonnement sur un serveur). Toute dépense attend l'accord du propriétaire. |
| Q3 | La cérémonie de clé Ed25519 sur le PC est-elle abandonnée au profit d'ameesh-approve (passkey sur le téléphone) ? | Oui : une clé lisible par les agents ne prouve rien. |
| Q4 | Licence d'ameesh avant le passage public. | À instruire (compatibilité avec les dépendances AGPL éventuelles côté connecteurs). |
| Q5 | L'orchestrateur Nexlink devient-il un agent à tours (réveillé par sa surveillance) pour pouvoir quitter le PC ? | Oui à terme ; c'est ce qui rend utile une deuxième machine. |

# Attend une étude ou un prototype

- Profil ameesh d'OKF Federation : types `Agent`, `Team`, `WorkPackage`,
  `Pipeline` ; liaison `human:<id>` → passkey / clé d'appareil ; `agent:<id>` → compte.
  Une partie pourrait remonter dans okf-federation.
- Isolation des agents (un utilisateur Unix ou un conteneur par agent) : coût,
  compatibilité avec les harnais et leurs sessions.
- File des décisions par humain (R17) : où vit-elle (Nexlink, ameesh, canon) ?
- Allocation des agents partagés entre projets : règle déclarative dans le canon.
- Analyse détaillée des projets homonymes (tlhc/amesh, samchung95/amesh,
  code-rabi/amesh) pour en reprendre les bonnes idées.
