---
type: Decision
title: "Persona et session : l'identité durable d'un agent est séparée de ses exécutions"
description: "Une persona (identité, rôle, responsable) est décrite au canon et n'a pas de position ; une session est une exécution éphémère de la persona dans un harnais, sur un hôte, placée par ameesh. Chaque humain a une persona particulière, son double numérique."
status: stable
tags: [persona, session, memoire, double-numerique, placement, harnais]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "décision du propriétaire dans sa conversation avec mesh-design ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T18:00:00+02:00" }
---

# Décision

1. **Persona** : l'identité durable d'un agent — nom, rôle, humain responsable,
   capacités (jamais `approve`) et politique ; elle a une mémoire, tenue par
   le harnais (voir plus bas). Le canon la
   décrit ; elle n'a **pas de position** (aucun hôte, aucun dossier).
2. **Session** : une exécution de la persona dans un harnais, sur un hôte, avec
   un modèle, un compte et un bail. Elle est éphémère, placée par ameesh
   ([0028](0028-ressources-des-hotes-et-repartition.md)), ouverte par lot
   ([0025](0025-une-session-par-lot.md)). **Une persona peut avoir plusieurs
   sessions en parallèle** (par exemple une par lot).
3. **Harnais** : les harnais tiers sont pris en charge (ACP, L16), notamment
   pour les abonnements qui ne s'utilisent qu'à travers leurs propres outils ;
   un harnais maison reste possible mais facultatif.
4. **Double numérique** : chaque humain du système a une persona particulière,
   son double numérique. Il n'approuve jamais
   ([0012](0012-autorite-par-ameesh-approve.md)) : l'autorité reste la passkey
   de l'humain.
5. **Placement** : le canon déclare des règles (contraintes de la persona,
   hôtes ou étiquettes d'hôtes admis, politique des hôtes), pas la position
   courante ; l'hôte et le dossier de travail d'une session sont de l'état
   d'exécution. Les fiches `Placement` actuelles deviennent des admissions sans
   `cwd`.

# Visibilité d'une persona (précision du propriétaire)

**Règle unique : une persona ne tourne sur la machine d'un humain que si cet
humain a accès au dépôt de la persona.** Le propriétaire d'une machine peut lire
tout ce qui s'y trouve : le contrôle se fait donc au placement, par les droits
du dépôt. Pour un serveur ou un hôte de cluster, la règle s'applique à son
responsable et à ceux qui l'administrent. Une persona réservée à quelques-uns
vit dans un dépôt réservé à ces personnes ; le double numérique d'un humain,
dans un dépôt privé à cet humain, ne tourne donc que chez lui — sans règle
particulière. Ce contrôle s'ajoute aux règles de placement
([0028](0028-ressources-des-hotes-et-repartition.md), lot L31).

# Choix retenus après l'étude (2026-10-05)

Voir [persona et harnais](../etudes/persona-et-harnais.md).

1. La mémoire de persona est un **contrat neutre** (format de fichiers dans un
   dépôt git par persona, texte de persona) ; un harnais maison est
   **facultatif** : ce ne serait qu'un harnais de plus qui lit ce contrat.
2. Les mémoires natives des harnais sont **désactivées** pour les sessions
   lancées par ameesh.
3. La visibilité est vérifiée par l'**API de la forge**, avec une clé `admins`
   dans la fiche `Host` ; la copie d'essai reste un repli pour un poste
   personnel.
4. L'identifiant d'accès limité au dépôt d'une persona est créé par le
   **responsable de l'hôte** et rangé dans les secrets de l'hôte.
5. La mémoire est consolidée par **la session elle-même**, aux points fixes
   (fin de lot, relais).
6. La fiche Persona fait l'objet d'un **lot à part** (P1).

# À préciser ultérieurement

- Les pouvoirs du double numérique (ce qu'il peut répondre au nom de son humain,
  signalement d'urgence).

**La mémoire relève du harnais, pas d'ameesh.** Elle suit un contrat neutre
(voir « Choix retenus » : fichiers Markdown dans un dépôt git par persona,
lisibles par tous les harnais) ; chaque harnais la charge au démarrage d'une
session et l'enrichit ensuite. Un harnais maison est facultatif. ameesh ne la stocke ni ne la définit ; il transmet
seulement au harnais l'identité de la persona qu'il fait tourner. Le double
numérique pourra lui aussi vivre hors d'ameesh ; la règle d'autorité reste dans
ameesh : aucun module n'approuve
([0012](0012-autorite-par-ameesh-approve.md)).

# Conséquences

- Refonte du profil du canon (`Agent` → persona) dans un lot à part (P1) ;
  `Placement` → admission et contrôle de visibilité au placement dans L31.
- Les comptes (L30) et le placement dynamique (L31) s'appliquent aux sessions.
- Pas de mémoire dans le cœur d'ameesh : le composant de mémoire de persona
  (lot P4 de l'étude) est séparé ; ameesh désigne la persona au lancement d'une
  session et lie la session aux versions de mémoire chargées.
