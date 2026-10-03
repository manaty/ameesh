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
  **`AGPL-3.0-only`** : les termes sont figés à la v3, personne d'autre que
  Manaty ne peut les faire évoluer.
- Les **contributions externes** sont acceptées seulement sous un **accord de
  contribution** (CLA) qui laisse à Manaty la possibilité de relicencier
  (passage ultérieur à `or-later`, à une future version, ou double licence).

# Conséquences

- Quiconque fait tourner une version **modifiée** d'ameesh comme service
  accessible par le réseau doit en offrir le code source à ses utilisateurs.
- **Manaty, titulaire des droits, n'est pas contraint par sa propre licence** :
  il peut proposer une licence commerciale à côté (double licence) et intégrer
  ameesh à Nexlink librement ; l'accord de contribution préserve cette liberté
  quand des contributeurs externes arrivent.
- `or-later` a été écarté : il permettrait de choisir une future version de la
  licence qui pourrait être plus permissive (par exemple sans la clause réseau).
  Grâce à l'accord de contribution, `only` ne ferme aucune porte.
- Dépendances : MIT, BSD, Apache-2.0, LGPL et MPL-2.0 sont compatibles avec
  l'AGPL v3 (py_webauthn BSD-3, python-fido2 BSD-2, SDK MCP…). Se connecter par
  API à des services AGPL (Zammad, Synapse) ne pose pas de question.
- À faire avant le passage public : fichier `LICENSE` (texte officiel), CLA et
  sa vérification sur les PR,
  métadonnées du paquet, en-têtes SPDX, retrait du DSN de banc codé en dur,
  détection de secrets sur l'historique.
