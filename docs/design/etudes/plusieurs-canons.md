---
type: Study
title: "Étude — plusieurs canons sur un même hôte (deux organisations)"
description: "Pourquoi ameesh ne sait lire qu'un canon, ce qui casse si l'on en synchronise deux, et le modèle retenu : un canon identifié par sa fédération, des effets de synchronisation bornés à ce canon ; lots L42–L45."
status: stable
tags: [canon, federation, multi-canon, synchronisation, placement]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-07T15:30:00+02:00" }
sources:
  - { resource: "decisions/0005-canon-okf.md", title: "Décision 0005 (canon OKF fédéré)" }
  - { resource: "decisions/0008-agents-dans-le-canon.md", title: "Décision 0008 (agents dans le canon)" }
  - { resource: "decisions/0029-persona-et-session.md", title: "Décision 0029 (persona, admissions)" }
---

# Contexte

Le propriétaire travaille avec deux canons à la fois : manaty
(`~/canon`, fédération `manaty`) et celui d'une seconde organisation, appelée
ici « Acme » (`~/development/acme/home`, fédération `acme-ingenierie`). Demande d'un agent de cette organisation, 2026-10-07 : un de ses agents ne
peut pas être déclaré, et `ameesh agent spawn … --by <agent>` échoue (« pas
d'humain responsable résolu », R14). Le propriétaire classe le besoin « très
important ».

# Ce que le code suppose

Un seul canon, partout, et aucune trace de l'identité d'une fédération.

* **Configuration** : `canon` et `canon_ref` sont des chaînes
  (`config.py:141-147`). Tous les appelants passent par
  `canon.from_config(cfg)` : exécuteur, `canon` CLI, `mesh`, `exploitation`.
* **Identité** : l'`id` de `federation.yaml` n'est lu que pour l'affichage.
  Une fiche est référencée `membre:chemin@commit`. Or les deux fédérations
  nomment leur racine `home` : les références se confondent.
* **Synchronisation** (`canon_sync.sync`) :
  - Elle lit toutes les lignes du registre de l'hôte, quelle que soit leur
    origine.
  - Elle **arrête** (`stop_removed`) toute ligne dont la fiche n'est pas dans
    le canon synchronisé et dont le membre est lu. Synchroniser A arrête donc
    les agents de B, et réciproquement, à chaque passe de l'exécuteur.
  - `sync_packages` retire de même les paquets de travail de l'autre canon.
* **État** : `canon_state` a pour clé l'hôte seul, et la réclamation exige
  `status = 'ok'` pour l'hôte. Un canon invalide bloque donc **tous** les
  agents de l'hôte, quel que soit leur canon.
* **Authentificateurs** : le journal des synchronisations est une séquence
  globale qui exige la descendance des commits racines. B est refusé après A.
  `revoke_absent` révoque les humains absents du canon synchronisé.
* **Noms** : `agent_registry.name` est la clé primaire. Hôtes, membres,
  équipes et paquets ont aussi des noms globaux.
* **Le canon Acme est invalide pour ameesh.** Ses
  `org/agent-harness/agents/*.md` portent `type: Agent`, mais ce sont des
  définitions de sous-agents Claude Code (`name`, `tools`), sans `title`.
  `fiche-title-missing` est une erreur bloquante globale.

# Décision proposée

1. **Liste de canons.** La configuration de l'hôte porte
   `canons: [{path, ref, untrusted}]` (ou `AMEESH_CANONS`). `canon` et
   `canon_ref` restent acceptés comme liste à un élément. Le **premier** canon
   est le canon par défaut.
2. **Un canon est identifié par sa fédération** (`federation.yaml` `id`).
   * Les références deviennent `<fédération>/<membre>:<chemin>@<commit>`.
   * Le registre, les paquets de travail, l'état du canon et le journal des
     authentificateurs portent une colonne `canon` (migration 0032).
   * Les lignes existantes, sans canon, appartiennent au canon par défaut.
3. **Chaque synchronisation ne touche que son canon.**
   * Arrêts, effacements de responsable, retraits de paquets et révocations
     ne visent que les lignes de ce canon.
   * L'état est tenu par `(hôte, canon)`, et la réclamation regarde l'état
     du canon de l'agent : fermeture **par canon**, plus globale.
4. **Les noms restent globaux ; le premier canon qui déclare un nom le garde.**
   Un nom d'agent déclaré par un second canon y devient une erreur limitée à
   cette fiche (`canon-name-conflict`), jamais une prise de contrôle. Même
   règle pour les identifiants de paquets.
   Un nom **libéré** (fiche retirée de son canon, agent arrêté) peut être
   repris par un autre canon : c'est ainsi qu'un agent change de canon sans
   perdre sa boîte.
5. **Humains par canon.** `human:<id>` se résout dans le canon de l'agent.
   * Un humain qui travaille dans les deux canons y a une fiche `Member` dans
     chacun.
   * Ses authentificateurs sont synchronisés par canon et ne sont révoqués
     que par le canon qui les a déclarés.
6. **Hôte décrit par chaque canon.**
   * Chaque canon qui place des agents sur `pc-smichea` y a sa fiche `Host`.
   * L'admission d'un agent se juge avec la fiche de son canon.
   * Les limites physiques que l'exécuteur applique (`max_agents`, seuils de
     ressources) sont **les plus strictes** des fiches de l'hôte.
7. **Périmètre ameesh dans un canon partagé.** `federation.yaml` peut déclarer
   `ameesh: {scope: <dossier>}`. Les fiches du profil ameesh (`Agent`, `Host`,
   `Placement`, `Member`, `WorkPackage`) ne sont alors lues que sous ce
   dossier. Les `type: Agent` étrangers ailleurs dans le canon, comme les
   sous-agents Claude Code d'Acme, sont ignorés. Sans cette clé,
   tout le canon est lu, comme aujourd'hui pour manaty.

# Plan de lots

| Lot | Contenu | Risque |
|---|---|---|
| **L42** — identité et synchronisation bornée | liste de canons ; `canon` sur registre, paquets et état (migration 0032) ; références qualifiées ; suppressions et retraits limités au canon synchronisé ; `canon_state` par `(hôte, canon)` et prédicat de réclamation par canon ; conflit de nom local à la fiche ; l'exécuteur synchronise chaque canon | élevé (cœur) |
| **L43** — périmètre et hôte par canon | `ameesh.scope` ; admission avec la fiche `Host` du canon de l'agent ; limites physiques = les plus strictes ; `spawn` cherche le créateur dans tous les canons | moyen |
| **L44** — authentificateurs par canon | journal par canon, révocation limitée au canon déclarant, `ref` de confiance par canon | élevé (confiance) |
| **L45** — outillage et accueil d'Acme | `canon check/show/sync` multi-canon (`--canon <id>`) ; documentation ; PR sur acme/home : `ameesh: {scope: ameesh}`, fiches `Member` smichea, `Host` pc-smichea, `Agent` acme-docs et acme-securite-docs, admissions | faible |

Ordre : L42, puis L43 et L44 en parallèle, puis L45.

# En attendant

Aucun agent d'une autre organisation n'est déclaré dans le canon manaty, même
à titre provisoire : chaque organisation déclare ses agents dans son propre
canon, une fois L42–L45 déployés. Ne pas créer d'agent avec
`--by mesh-design` : il hériterait du responsable et de l'équipe ameesh, ce
qui fausse R14.
