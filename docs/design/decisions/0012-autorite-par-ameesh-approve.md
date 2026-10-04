---
type: Decision
title: "Abandon de la cérémonie Ed25519 sur le PC ; autorité humaine par ameesh-approve"
description: "La signature d'un humain se fait hors de portée des agents : passkey (WebAuthn), application Nexlink, ou application mobile native ; un seul format de reçu."
status: stable
tags: [autorite, signature, ameesh-approve, mobile]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:50:00+02:00" }
sources:
  - { resource: "../etudes/autorite-humaine.md", title: "Étude autorité humaine" }
---

# Contexte

L'étape 3 de `BASCULE.md` prévoyait une clé Ed25519 du propriétaire dans un
fichier `0600` sur le PC. Les agents tournent sous le même utilisateur Unix :
ils peuvent lire ce fichier, et tous peuvent enregistrer une clé `owner` en base.

# Décision

- **Option A** : la cérémonie de clé Ed25519 sur le PC est **abandonnée**.
  L'autorité d'un humain passe par **ameesh-approve** : la clé n'est jamais sur
  une machine d'agents ; le reçu est lié à l'empreinte exacte, avec échéance et
  usage unique.
- **Population mixte** : parmi les humains, **certains utiliseront Nexlink,
  d'autres non**. Les premiers approuvent depuis l'application Nexlink (clés
  d'appareil non exportables) ; les autres par une passkey (page WebAuthn).
- Une **application mobile native** pourra être nécessaire pour certaines organisations : ameesh en prévoit les interfaces ([0015](0015-interfaces-des-facades.md)).

# Conséquences

- ameesh-approve a **plusieurs façades d'approbation** derrière **un seul format
  de reçu** et une seule vérification : page WebAuthn (open source, par défaut),
  application Nexlink (native), application mobile native développée par une
  organisation sur les interfaces publiques d'ameesh (Keystore / Secure
  Enclave ; affiche elle-même l'objet et signe — le meilleur « ce que vous
  voyez est ce que vous signez »).
- Le registre des clés publiques vit dans le canon OKF (PR revue) et associe
  chaque humain à un ou plusieurs authentificateurs ; la politique peut exiger
  un niveau (application native ou clé matérielle pour les actions sensibles).
- Le code Ed25519 de la v1 est conservé pour la provenance des agents (R13) et
  les tests ; le modèle de reçu (échéance signée, nonce consommé, empreinte) est
  repris par ameesh-approve.
- Demande de changement à instruire après les gels de sécurité : vérification
  des reçus WebAuthn/ES256, rôles de la fédération à la place de
  `owner`/`agent`, clés publiques lues dans le canon, étape 3 de `BASCULE.md`
  remplacée.
