---
type: Decision
title: "Licence d'ameesh : AGPL-3.0-only, avec accord de contribution"
description: "ameesh sera publié sous GNU AGPL v3, variante only ; les contributions externes passent par un accord de contribution."
status: stable
tags: [licence, release]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:00:00+02:00" }
---

# Contexte

Le dépôt manaty/ameesh est privé jusqu'à la première release
([0001](0001-nom-ameesh.md)) ; une licence devait être choisie avant le passage
public. mesh-design recommandait Apache-2.0 (adoption, cohérence avec OKF) ;
le propriétaire a choisi l'AGPL.

# Décision

- ameesh est publié sous **GNU Affero General Public License v3**, variante
  **`AGPL-3.0-only`** : la version de la licence est fixée à la v3 ; seul un
  titulaire des droits sur le code concerné peut décider de la changer.
- Les **contributions externes** sont acceptées seulement sous un **accord de
  contribution** (CLA, à rédiger) destiné à laisser à Manaty la possibilité de
  relicencier (passage ultérieur à `or-later`, à une future version, ou double
  licence).

# Conséquences

- Quiconque fait tourner une version **modifiée** d'ameesh comme service
  accessible par le réseau doit en offrir le code source à ses utilisateurs.
- **Politique visée, conditionnée aux droits effectivement détenus** : pour le
  code dont Manaty détient les droits, Manaty n'est pas contraint par sa propre
  licence (double licence, intégration dans Nexlink). Pour le code d'autres
  auteurs, cette liberté dépend des droits concédés par le CLA, qui n'est pas
  encore rédigé ; **aucune garantie de relicenciement de code tiers** n'est
  donnée ici. La titularité des contributions produites par des agents
  opérés pour Manaty est à confirmer dans le même travail.
- `or-later` a été écarté : il permettrait de choisir une future version de la
  licence qui pourrait être plus permissive (par exemple sans la clause réseau).
  Avec un CLA adéquat, `only` laisse ouverte une évolution ultérieure.
- **Compatibilité des dépendances et des intégrations : étude à faire**, pas un
  résultat acquis. Elle portera sur les licences et versions exactes de chaque
  dépendance distribuée (bibliothèques WebAuthn, SDK MCP…) et sur la topologie
  des intégrations (appel par API de services sous AGPL comme Zammad ou
  Synapse, ou distribution combinée).
- À faire avant le passage public : fichier `LICENSE` (texte officiel),
  métadonnées du paquet, en-têtes SPDX, retrait du DSN de banc codé en dur
  (lot en cours chez deepseek7) ; CLA et sa vérification sur les PR ; étude de
  compatibilité des licences ; détection de secrets sur l'historique.
