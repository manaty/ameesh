---
type: Study
title: "Étude — boucle de détection et de réparation automatique des bugs"
description: "Comment un orchestrateur de tickets existant détecte et répare automatiquement les bugs remontés par les interactions, ce qui marche, ses limites, et la transposition générique à ameesh."
status: draft
tags: [auto-reparation, signaux, triage, verification, cloture]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-04T03:05:00+02:00" }
stale_after: 2027-04-04
---

# Source

Étude en lecture seule d'un orchestrateur de tickets existant, en production,
qui reçoit des signaux d'erreurs et de conversations, ouvre des lots, fait
implémenter les correctifs par des agents, vérifie, fait relire et livre. Les
références précises (fichiers, lignes) sont conservées hors de ce dépôt (règle
[0015](../decisions/0015-interfaces-des-facades.md)).

# Le circuit observé

1. **Signaux** : webhooks d'un outil de suivi d'erreurs (avec trace et
   occurrences), alertes d'audit (rafales d'erreurs serveur, empreinte
   déterministe, récidive = commentaire), conversations avec un agent (qui trie
   bug / évolution / question et cherche les doublons), vérificateur de données
   (audit de lignes de production + second regard contradictoire, verdict
   « expliqué / correction de données / correction de code », classe active ou
   historique), outil de tickets externe (routage vers le dépôt avec seuil de
   confiance). Corrélation avec les déploiements par fenêtre temporelle.
2. **Déduplication en trois étages** : empreinte structurée (type, zone,
   observé, attendu, discriminants), préfiltre bon marché, décision finale
   conservatrice (préférer les faux négatifs). Un seul lot actif par ticket.
3. **Triage** : bug → réparation automatique ; évolution → jamais
   automatiquement, attente d'un humain si le demandeur n'est pas de l'équipe ;
   correction de données → proposition revue par un humain, jamais exécutée
   seule. Échecs d'exécution → une relance, puis un ticket d'auto-guérison
   dédupliqué ; blocages d'infrastructure envoyés aux ops, pas au demandeur.
4. **Réparation** : clone et branche dédiés, reproduire d'abord, plus petit
   correctif, tests obligatoires, rapport ; exécution lourde isolée ; secrets
   retirés de l'environnement de l'agent, jeton de push injecté seulement au
   push. **Gardes sur le diff** : substituts de dépendances, portée autorisée,
   changements de contrat (DDL, routes, champs de réponse, schémas d'outils)
   → attente d'un humain avec **approbation liée à l'empreinte du diff** et
   patch rejouable.
5. **Vérification** : commandes de vérification locales, puis CI, puis QA
   automatique (couverture selon le type de fichier, CI verte), correcteur de
   CI rouge, boucle bornée à deux ; conflits signalés, jamais résolus seuls ;
   suivi des PR laissées en plan.
6. **Clôture** : fusion vers la branche d'intégration après QA verte ; la
   promotion vers la pré-production reste humaine ; notification idempotente du
   demandeur à chaque étape.

# Limites observées (à ne pas reproduire)

- décisions lues dans du texte libre ou des réactions de messagerie (une
  réaction sans rapport a démarré une exécution) ;
- **approbation implicite par le silence** après un délai d'escalade ;
- une seule réplique, espaces de travail éphémères, état en mémoire → lots
  orphelins à répétition ; interrupteurs non durables ;
- transitions déduites par corrélation, sans journal d'événements ;
- pas de surveillance après déploiement ni de réouverture sur récidive ;
- fausses alertes de la porte de gouvernance et approbations restées sans
  réponse ; un agent de support qui créait plus de travail qu'il n'en
  économisait.

# Transposition à ameesh (capacités génériques)

| # | Capacité | ameesh a déjà | Manque |
|---|---|---|---|
| A1 | entrée des signaux (webhooks authentifiés, e-mail, fil) | fil, boîte | table `signals`, adaptateurs |
| A2 | classifieur (nature, gravité, confiance ; infra vs décision) | — | tout |
| A3 | empreinte et déduplication à deux niveaux (occurrence, classe) | — | tout |
| A4 | corrélation avec une version déployée | — | tout |
| A5 | **politique d'auto-réparation au canon** (sources, natures, gravités, budgets, chemins sensibles, interrupteur durable) | canon, politiques de revue | le schéma |
| A6 | exécution de réparation (reproduire d'abord, rapport, gardes de diff) | exécuteur, sessions | gardes de diff |
| A7 | porte de changement sur l'empreinte du diff, patch rejouable | reçus, porte d'actions | branchement |
| A8 | preuves de vérification (commande, sortie, empreinte) | états `qa`/`build` | preuves |
| A9 | demande à un humain (destinataire résolu par le canon, échéance par gravité, remplaçants, escalade) — **jamais d'approbation implicite pour une action irréversible** | `waiting_human`, file des décisions | délais et escalade |
| A10 | surveillance après livraison, réouverture sur récidive, notification du demandeur | — | tout |
| A11 | auto-guérison des exécutions (relance, puis lot) | — | tout |
| A12 | balayage avec chien de garde, lots bloqués, résumé dans le fil | partiel | le reste |
