---
type: Decision
title: "Pas de travail sans réveil possible"
description: "On ne confie un lot, une délégation ou un rôle d'orchestrateur qu'à un agent qu'ameesh sait réveiller ; adoption et reprise deviennent des opérations d'ameesh ; les alertes de vivacité sont poussées au responsable humain ; l'identité ne vient jamais du dossier."
status: stable
tags: [orchestrateur, bail, sessions, alertes, delegation, cloisonnement]
decided_by: human:smichea
decision_date: 2026-10-07
attestation: "décision du propriétaire dans sa conversation avec claude3 (mesh-design) ; non signée"
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-07T13:00:00+02:00" }
---

# Contexte

Le 2026-10-07, un chantier s'est arrêté en silence. Son
orchestrateur était une session Codex interactive, sans bail et sans
responsable humain. ameesh ne pouvait ni la réveiller, ni lui remettre son
courrier, ni signaler qu'elle était arrêtée. Analyse complète :
[orchestration et vivacité](../etudes/orchestration-et-vivacite.md).

# Décision

1. **Mode d'agent explicite** : `exécuté` (mené par un exécuteur, sous bail) ou
   `externe` (session humaine, boîte seulement). Un agent externe a
   obligatoirement un responsable humain.
2. **Attribution gardée** : un lot, une délégation ou un rôle d'orchestrateur ne
   va qu'à un agent réveillable (connu, `exécuté`, admis sur un hôte dont
   l'exécuteur vit). Seul le responsable humain peut forcer une attribution à
   un agent externe ; il devient alors le destinataire des alertes du lot.
   Prolonge [0014](0014-orchestrateurs-a-tours-et-placement.md).
3. **Adoption et reprise sont des opérations d'ameesh** (`ameesh adopt`,
   `ameesh resume`), jamais des prompts. Le compte d'origine d'une session est
   enregistré. Si ce compte est au seuil, la règle de
   [0027](0027-bascule-automatique-entre-comptes.md) s'applique ; si l'ancien
   compte est inutilisable, ameesh construit un brief de reprise déterministe.
4. **Alertes de vivacité poussées** au responsable humain de l'agent ou du lot :
   `stopped_with_mail` (sauf arrêt manuel, avec une raison d'arrêt
   structurée), `orphan_lot`, `delegation_expired`.
5. **Délégation à échéance** : sans tour du délégué à l'échéance, le lot revient
   au délégant.
6. **L'identité ne vient jamais du dossier** : les hooks passent à la v1, une
   session externe est liée explicitement par l'identifiant de session de son
   harnais, et rien n'est remis sans liaison. `send all` est limité au
   chantier de l'expéditeur.

# Conséquences

- Lots L36 à L41 ([étude](../etudes/orchestration-et-vivacite.md#plan-de-lots-proposé)) ;
  migrations 0030 et 0031 réservées.
- Ordre : L36, puis L37, puis L38, L39 et L41 en parallèle, puis L40. Chaque
  lot est fusionné sur accord du propriétaire.
- L'orchestrateur du chantier bloqué est relancé à la main par son
  responsable, en attendant `ameesh adopt` / `ameesh resume` (L39).
