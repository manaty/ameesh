---
type: Decision
title: "ameesh ne nomme aucun client ; il publie les interfaces qui permettent de bâtir ses façades"
description: "Pas de nom de client dans le projet ameesh ; des interfaces publiques pour développer des applications (dont mobiles natives) sans imposer Nexlink."
status: stable
tags: [confidentialite, interfaces, mobile, nexlink]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:20:00+02:00" }
---

# Contexte

La question Q6 portait sur une application mobile native pour une organisation
cliente : application ameesh générique ou intégration aux applications de
cette organisation.

# Décision

- **Le projet ameesh ne mentionne aucun client** : ni nom, ni système interne,
  ni dépôt d'une organisation cliente, dans le code, la documentation ou les
  exemples.
- ameesh **ne développe pas d'application pour un client** : il **prévoit les
  interfaces** qui permettront à une organisation de développer la sienne
  (notamment une application mobile native), **pour ne pas avoir à imposer
  Nexlink**.

# Conséquences

- Interfaces publiques et documentées, au même niveau que le connecteur Nexlink :
  - **approbation** : format de demande et de reçu, vérification par façade
    (WebAuthn, clé d'appareil ES256…), enrôlement d'un authentificateur dans le
    registre du canon ;
  - **fil et notifications** : lire et poster dans le fil d'un lot, recevoir les
    demandes qui attendent un humain (R17) ;
  - **identité** : liaison d'un membre `human:<id>` à ses authentificateurs.
- Nexlink est une façade parmi d'autres, intégrée nativement ; une application
  tierce sur ces interfaces a les mêmes possibilités.
- Les exemples et tests utilisent des organisations fictives.
- L'historique git du dépôt privé contient des mentions antérieures d'un
  client : à traiter avant le passage public (question ouverte).
