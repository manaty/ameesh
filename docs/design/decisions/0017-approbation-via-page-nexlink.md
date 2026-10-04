---
type: Decision
title: "ameesh-approve est joignable par le téléphone via une page Nexlink"
description: "Pour la bascule du chantier, le service d'approbation est exposé en HTTPS par une page Nexlink."
status: stable
tags: [ameesh-approve, nexlink, bascule, reseau]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:10:00+02:00" }
---

# Contexte

WebAuthn exige un contexte sécurisé (HTTPS) et un domaine stable (RP ID) ; le
téléphone n'atteint pas le `localhost` du PC. Trois options : réseau privé avec
certificat, page Nexlink, domaine dédié ([bascule v1](../bascule-v1.md), étape 3).

# Décision

Pour la bascule du chantier, **ameesh-approve est exposé via une page Nexlink**.

# Conséquences

- Le RP ID est le domaine de la page Nexlink ; un changement de domaine oblige à
  réenrôler les passkeys (ou à déclarer des origines liées, WebAuthn L3).
- Côté Nexlink : la page doit pouvoir servir (ou relayer en HTTPS) le service
  d'approbation qui tourne sous l'utilisateur dédié — à instruire avec
  l'orchestrateur Nexlink (lien avec la remarque « toute infrastructure
  existante devient une page Nexlink »).
- Rien n'est exposé avant que le service ait passé sa revue de sécurité (L7) ;
  la mise en ligne reste un acte du propriétaire.
