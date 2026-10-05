---
type: Decision
title: "ameesh-approve : trois hébergements au choix, un nom d'hôte et un RP ID propres à chaque équipe"
description: "PC avec relais Nexlink (abonnement), page Nexlink dédiée, ou serveur de l'équipe ; RP ID WebAuthn exactement égal à l'hôte de l'équipe, jamais un domaine parent commun."
status: stable
tags: [ameesh-approve, webauthn, hebergement, equipes, nexlink]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "décision produit du propriétaire (06:22) transmise par l'orchestrateur Nexlink, rapportée par mesh-design ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T06:45:00+02:00" }
---

# Décision

1. Quand une équipe adopte ameesh, elle choisit où tourne **son** ameesh-approve :
   (1) sur son PC, joignable par le relais Nexlink, contre un petit abonnement ;
   (2) sur une page Nexlink dédiée ; (3) sur son propre serveur, la
   configuration d'ameesh indiquant simplement l'URL.
2. **Un service par équipe**, jamais une page commune : chaque équipe a son
   hôte `H`, et son **RP ID WebAuthn est exactement `H`**. Aucun RP ID parent
   commun (`ameesh.nexlink.ph`, `nexlink.ph`), aucun partage par chemin, aucune
   relation ancêtre/descendant entre les RP ID de deux équipes.
3. Le nom `ameesh.nexlink.ph` est **réservé** : il ne sert ni de RP ID ni de
   contenu. L'installation du propriétaire prend un hôte d'équipe comme les
   autres (proposé : `manaty.ameesh.nexlink.ph`, à confirmer après l'analyse
   same-site de Nexlink) ; aucune passkey n'ayant été enrôlée, le changement est
   sans coût.
4. Changer d'hébergement sans réenrôler les passkeys suppose de garder le même
   `H`, la même origine et le même registre ; changer de `H` impose un
   réenrôlement.

# Conséquences

- Contrat détaillé : [ameesh-approve, trois hébergements](../ameesh-approve-hebergement-equipes.md).
- Côté ameesh, lot L27 : `approve_url` distincte de `public_url` et API
  ouvrable explicitement, profil strict (RP ID égal à `H`, origine unique,
  refus d'un RP ID égal à une zone ou à un parent réservé), concordance de la
  politique entre le service et les vérificateurs, droits DB propres à approve,
  procédure de bascule à `H` constant.
- Côté Nexlink : espace de noms (après l'analyse same-site), attribution
  exclusive et non réattribuable des noms, passerelle et tunnels par équipe,
  conservation du nom à la sortie de l'abonnement.
- Précise [0017](0017-approbation-via-page-nexlink.md) : la page servie par
  Nexlink est l'instance d'une équipe sur son propre hôte.
