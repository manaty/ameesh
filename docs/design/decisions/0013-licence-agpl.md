---
type: Decision
title: "Licence d'ameesh : AGPL v3"
description: "ameesh sera publié sous GNU AGPL v3."
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

ameesh est publié sous **GNU Affero General Public License v3**.

# Conséquences

- Quiconque fait tourner une version **modifiée** d'ameesh comme service
  accessible par le réseau doit en offrir le code source à ses utilisateurs.
- **Manaty, titulaire des droits, n'est pas contraint par sa propre licence** :
  il peut proposer une licence commerciale à côté (double licence) et intégrer
  ameesh à Nexlink librement. Pour garder cette possibilité, **les contributions
  externes** devront être couvertes par un accord de contribution (CLA ou
  cession) — question ouverte Q7.
- Dépendances : MIT, BSD, Apache-2.0, LGPL et MPL-2.0 sont compatibles avec
  l'AGPL v3 (py_webauthn BSD-3, python-fido2 BSD-2, SDK MCP…). Se connecter par
  API à des services AGPL (Zammad, Synapse) ne pose pas de question.
- À faire avant le passage public : fichier `LICENSE` (texte officiel),
  métadonnées du paquet, en-têtes SPDX, retrait du DSN de banc codé en dur,
  détection de secrets sur l'historique.
