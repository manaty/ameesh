---
type: Study
title: "Étude — messagerie et annuaire open source"
description: "Matrix, Zulip, Mattermost, Rocket.Chat, Campfire, Stoat comparés pour les comptes d'agents, les fils par lot, le push et les licences."
status: draft
tags: [messagerie, annuaire, matrix, zulip, open-source]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-03T23:30:00+02:00" }
stale_after: 2027-04-03
sources:
  - { resource: "https://github.com/element-hq/synapse", title: "Synapse (AGPL-3.0)" }
  - { resource: "https://spec.matrix.org/latest/application-service-api/", title: "Matrix appservices" }
  - { resource: "https://element.io/server-suite/community", title: "Element Server Suite Community" }
  - { resource: "https://github.com/zulip/zulip/blob/main/LICENSE", title: "Zulip (Apache-2.0)" }
  - { resource: "https://zulip.readthedocs.io/en/latest/production/mobile-push-notifications.html", title: "Zulip push" }
  - { resource: "https://docs.mattermost.com/product-overview/editions-and-offerings.html", title: "Mattermost éditions" }
  - { resource: "https://docs.rocket.chat/docs/our-plans", title: "Rocket.Chat plans" }
---

# Comparatif (vérifié au 2026-10-03)

| | Matrix (Synapse + Element) | Zulip | Mattermost | Rocket.Chat |
|---|---|---|---|---|
| Licence serveur | AGPL-3.0 ou commerciale Element ; Tuwunel Apache-2.0 | Apache-2.0, sans open core dans le code | AGPL-3.0 (source), binaires Team MIT | MIT hors `ee/` propriétaire |
| Payant qui nous concerne | multi-locataire, LDAP/SCIM, audit, console (ESS Pro) | relais push au-delà de 10 utilisateurs | Team : 250 utilisateurs, pas de SSO ; rétention, audit = Enterprise | Starter 50 utilisateurs ; pas de SSO ni d'audit en Community |
| Comptes d'agents | **appservice** : espace de noms `@agent-*`, sans limite de débit, sans un jeton par agent | bot avec clé API | bots à jeton personnel | jetons personnels |
| Fil par lot | Space par projet, salle ou `m.thread` par lot | **un sujet par lot**, créé en postant | `root_id` | `tmid`, discussions |
| Push mobile auto-hébergé | via passerelle Element (contenu non transmis) ; 100 % libre seulement Android/UnifiedPush | relais Zulip obligatoire, sinon app recompilée | TPNS sans SLA, HPNS payant | passerelle avec quotas |
| Clés d'appareil humaines | oui (ed25519, cross-signing) — pas d'API de signature arbitraire | non | non | non |

Campfire (MIT) et Stoat (ex-Revolt, AGPL) : pas assez de fils, de bots ni d'annuaire.

# Recommandation

1. **Matrix** (Synapse, Element X) avec **un appservice ameesh** portant tous les
   comptes d'agents. Licences propres ; standard ouvert ; seul candidat avec des
   clés d'appareil humaines. Matrix sert de **transport** des demandes et reçus
   d'approbation, pas de preuve (voir [l'autorité humaine](autorite-humaine.md)).
2. **Zulip** en second : meilleur modèle « un sujet par lot », multi-organisation
   natif, Apache-2.0 complet ; faiblesse : le push mobile.

À écarter : Mattermost et Rocket.Chat (versions libres trop bridées depuis
2025-2026).

# Pièges

- **Push mobile** : toutes les applications des stores passent par le relais de
  leur éditeur. Push 100 % auto-hébergé seulement sur Android (UnifiedPush) ou
  avec sa propre application — un avantage de Nexlink.
- **Débit Matrix** : 0,2 message/s par défaut ; appservice `rate_limited: false`.
- **AGPL** : utiliser les API et appservices, ne pas modifier Synapse/Element.
- Dendrite est en maintenance ; le paysage Rust (Conduit, Tuwunel…) est fragmenté.

# Non vérifié

Licence de Mostlymatter (fork Framasoft de Mattermost) ; fils et bots de Stoat
et Campfire ; quotas push de Rocket.Chat ; prix d'Element.
