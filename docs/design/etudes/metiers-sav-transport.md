---
type: Study
title: "Étude — métiers : SAV par e-mail et coordination transport"
description: "Ce qu'il faut pour qu'ameesh fasse travailler une équipe de SAV et des coordinateurs de transport : piles open source, agents non-code, gouvernance des actions irréversibles."
status: draft
tags: [metiers, sav, transport, gouvernance]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-03T23:30:00+02:00" }
stale_after: 2027-04-03
sources:
  - { resource: "https://github.com/zammad/zammad", title: "Zammad (AGPL-3.0)" }
  - { resource: "https://github.com/OCA/delivery-carrier", title: "OCA delivery-carrier" }
  - { resource: "https://github.com/karrioapi/karrio", title: "Karrio" }
  - { resource: "https://www.odoo.com/documentation/20.0/developer/reference/external_rpc_api.html", title: "Odoo API JSON-2" }
  - { resource: "https://arxiv.org/abs/2506.12469", title: "Niveaux d'autonomie (Feng et al., 2025)" }
  - { resource: "https://goteleport.com/blog/owasp-top-10-agentic-applications", title: "OWASP Top 10 agentique" }
---

# SAV qui répond aux mails entrants

- **IMAP (IDLE) + SMTP** comme socle (Gmail et Microsoft 365 ne parlent pas JMAP) ;
  JMAP en option pour un serveur auto-hébergé (Stalwart).
- Petit volume : brancher l'e-mail directement sur ameesh (un fil = un lot, bail
  sur la conversation). Dès qu'il faut SLA, files, macros, historique client :
  **Zammad** (AGPL-3.0, API REST complète, webhooks, serveur MCP communautaire ;
  son IA classe et priorise sans envoyer au client). FreeScout : API payante ;
  Chatwoot : `enterprise/` propriétaire, pensé pour le chat ; UVdesk à l'arrêt.

# Coordination transport

- **Odoo Community** (LGPL-3.0) + **OCA `delivery-carrier`** (AGPL-3.0) : les
  connecteurs transporteurs natifs d'Odoo sont réservés à Enterprise. Viser
  l'API **JSON-2** (XML/JSON-RPC dépréciés depuis la 19.0).
- **Karrio** (LGPL-3.0, sauf `ee/`) comme passerelle transporteurs : DHL, UPS,
  FedEx, Chronopost en production ; GLS et DPD absents.
- **Fleetbase** (AGPL-3.0) seulement pour des tournées d'enlèvement en propre.
- **Une organisation qui a déjà ses propres systèmes métier** (gestion du
  transport, devis…) branche d'abord ses API comme outils métier ; Odoo et
  Karrio sont la référence open source pour les autres.

# Agents hors code

Le modèle « tour headless sur session persistante » reste valable. Ce qui change :

- l'espace de travail n'est plus un worktree git mais un **objet métier**
  (`ticket:1234`, `picking:WH/OUT/0042`) : le bail porte sur lui ;
- la revue porte sur une **action proposée** (brouillon, étiquette,
  remboursement), pas sur un commit ;
- le déclencheur est **externe** (mail, webhook) avec un SLA.

Harnais : Claude Agent SDK, OpenAI Agents SDK (pause sérialisable
`needs_approval`), Codex, goose, OpenHands. « Computer use » en dernier recours
(portails sans API).

# Gouvernance

| Approbation humaine signée | Sans approbation |
|---|---|
| tout envoi au client ; remboursement, avoir, geste commercial ; réservation ou annulation d'enlèvement, achat d'étiquette ; changement d'adresse ou de valeur déclarée ; montant au-dessus d'un seuil ; suppression ou fusion de données client | lecture, classement, résumé, brouillon, devis, suivi |

Autonomie progressive par type d'action (cinq niveaux, Feng et al. 2025), montée
de niveau sur métriques mesurées. AI Act art. 50 (en vigueur depuis le
2026-08-02) : mention IA dans les messages sortants. Moffatt c. Air Canada :
l'entreprise répond de son agent.

# Ce qu'ameesh doit ajouter

- interfaces : **canal entrant**, **canal sortant** (seulement via action
  approuvée), **outils métier** (MCP, classés lecture / réversible /
  irréversible / coûteux), **action proposée signée**, **politique d'autonomie** ;
- exigences : baux sur objets externes ; **idempotence** des actions
  irréversibles ; journal d'audit opposable ; **contenu entrant non fiable**
  (injection de prompt) ; mention IA ; RGPD ; identités de bot dédiées.
