---
type: Decision
title: "L'exécuteur médié : un appareil sans accès à la base, par l'API d'exécuteur du serveur du mesh"
description: "Un exécuteur ameesh sur un appareil prêté (VM Nexlink Compute) parle au serveur du mesh par /api/exec/v1 : backend de stockage distant à liste fermée de 61 opérations, fencing serveur par (owner, epoch) pour chaque écriture, enrôlement par un humain habilité avec clé P-256, révocation qui relâche les baux, jamais d'approbation, porte d'inactivité venue de Compute, relais de modèle au serveur pour la clé payée au token, dépôt de travail par paquet git lié au bail et effacé en fin de bail."
status: proposed
tags: [v2, executeur, api, compute, appareil, bail, fencing, relais, identite]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T16:00:00+02:00" }
sources:
  - { resource: "../etudes/executeur-mediee.md", title: "Étude : l'exécuteur médié" }
  - { resource: "0012-autorite-par-ameesh-approve.md", title: "Décision 0012" }
  - { resource: "0028-ressources-des-hotes-et-repartition.md", title: "Décision 0028" }
  - { resource: "0029-persona-et-session.md", title: "Décision 0029" }
  - { resource: "0033-meshes-etanches-hebergement-et-identite.md", title: "Décision 0033 (branche, PR #15)" }
---

Numéro à attribuer à la fusion (« 00xx » : plusieurs numéros à partir de
0035 sont proposés sur des branches).

# Contexte

Le propriétaire veut utiliser les appareils de ses enfants quand ils sont
inutilisés (2026-10-10), par la voie B : l'appareil n'a aucun accès direct à
la base. Nexlink Compute fournit la VM (Lima, WSL2) et l'état d'inactivité ;
sa spécification (§3.2) attend d'ameesh une API d'exécuteur médiée.

# Décision

1. **Backend de stockage distant.** L'exécuteur garde sa logique ; sur un
   appareil médié, `storage.of()` rend un `RemoteStorage` qui implémente par
   HTTP **les 61 opérations** dont l'exécuteur et la session du harnais ont
   besoin (23 lectures, 38 écritures). Toute autre opération est refusée. La
   table des opérations, avec la portée de chacune, est le contrat
   (`/api/exec/v1`, dans `ameesh serve`, authentification distincte de
   `/api/v1`).
2. **Le serveur refait le fencing.** Chaque écriture d'un agent porte
   (agent, owner, epoch) ; le serveur recontrôle le bail vivant, l'hôte et
   l'exécuteur sous verrou de ligne, dans la transaction de l'opération. Un
   bail perdu rend la valeur de refus de l'opération. Idempotence par
   `Idempotency-Key`. Le réveil passe par un flux SSE (repli : attente
   longue) filtré par hôte, sans donnée.
3. **Synchronisation du canon, visibilité, placement, soldes, échéance des
   délégations et déplacements restent au serveur.** L'exécuteur médié ne
   lit ni n'écrit le canon.
4. **Identité de l'appareil.** Un humain habilité (responsable de l'hôte,
   coordinateur, suppléant) émet un code d'enrôlement à usage unique, lié au
   mesh et à l'hôte ; l'exécuteur enrôlé a sa clé P-256 (liaison facultative
   à la clé d'appareil Nexlink), obtient des jetons de 10 min par assertion
   ES256 et des jetons de session liés au bail pour le harnais. La
   révocation invalide les jetons et relâche les baux. Un mesh par
   enrôlement ; deux organisations, deux exécuteurs.
5. **Jamais d'approbation** depuis l'appareil (0012) : aucune route vers
   approbations, nonces, autorisations, authentificateurs ni exécution
   d'action ; le rôle Postgres de l'API n'y a aucun droit. L'agent peut
   seulement proposer une action.
6. **Inactivité.** Le runner Compute écrit l'état (`available`, `draining`,
   `stopped`) dans un fichier ou une socket de la VM ; l'exécuteur réclame
   seulement en `available`, termine au point sûr et rend ses baux en
   `draining` (interruption à l'échéance de drainage), acquitte ; le serveur
   refuse toute réclamation d'un hôte indisponible. Un hôte volatil a un bail
   court (90 s proposés).
7. **Secrets : relais de modèle au serveur.** La clé du fournisseur payé au
   token ne quitte pas le serveur ; le relais mesure l'usage, écrit le coût
   qui fait foi et refuse au-delà des plafonds. Le coût déclaré par l'appareil
   est indicatif. Pas de harnais au forfait sur un appareil tiers.
8. **Données.** Seules partent les personas et les lots dont le dépôt est
   lisible par tous les humains qui ont l'accès physique (0029). Le dépôt de
   travail arrive en paquet git préparé par le serveur, lié au bail ; les
   commits reviennent en paquet, poussés par le serveur sur une branche de
   l'agent ; le dossier et la session sont effacés en fin de bail.

# Conséquences

- Lots L107–L115 (étude, section 7) ; L107 fige le contrat, puis quatre
  agents travaillent en parallèle.
- NX-COMPUTE-3 se débloque sur ce contrat ; ce que Nexlink doit fournir :
  l'état d'inactivité et l'attente de l'acquittement, la PROV de la VM
  (volume persistant, allow-list réduite au serveur du mesh), une machine
  d'essai, et, facultativement, la signature d'un défi par la clé d'appareil.
- Le profil du canon gagne `credential_modes: [relay]`, le bail d'un hôte
  volatil et, si retenu, `occupants` dans la fiche `Host`.
