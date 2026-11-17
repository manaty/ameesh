---
type: Design
title: "Clones d'avatar sur le mesh Manaty : hôte propre intenable avec un forfait, trois options pour l'owner"
description: "Décision demandée par le lot #7681 (manaty/ameesh#5875) : (a) provisionner un tenant api-key sur une VM d'agents, (b) ajouter un mode `avatar.children: same-host` explicite et borné, ou (c) attendre l'hôte/pod dédié (#7633, conception #7659). Constat mesuré : aucun hôte partagé, aucun tenant, et l'avatar d'essai est en forfait, alors qu'un hôte partagé n'admet que `api-key` ; (a) ne débloque donc pas le clone sans compte api-key. Recommandation : (c) maintenant, (a) si l'owner veut des clones et accepte la dépense ; (b) est prêt sur branche mais s'écarte de la décision 11."
status: draft
tags: [avatar, spawn, enfants, placement, hote-partage, forfait, securite]
generated: { by: "mty-amh-docs-01/deepseek", at: "2026-11-17T05:35:00Z" }
sources:
  - { resource: "manaty/ameesh#5875", title: "Clones d'avatar impossibles sur le mesh Manaty : children own-host sans hôte partagé, et spawn sans nom mal expliqué" }
  - { resource: "lot #7681", title: "lot de suite de la qualification #7635 (manaty/ameesh#5826)" }
  - { resource: "docs/design/5322-avatar-humain.md", title: "Avatar d'humain : §3.4 hôte partagé, §3.6 placement des enfants, §4.1 limite de l'allowlist" }
  - { resource: "manaty/home#291 (#7647)", title: "fiche du bureau, `avatar.children: own-host`" }
  - { resource: "courrier #223620", title: "consigne du lot #7681 : aucune action irréversible ou de production, aucun merge" }
  - { resource: "mesh-design, courrier du 17/11", title: "la décision est à l'owner (a/b/c) ; correctif sans nom et tests de (b) sur branche autorisés, aucune fusion de (b) avant la réponse" }
---

# Clones d'avatar : décision de placement (#7681)

**Lot #7681**, décision demandée par la qualification des avatars #7635. Cette
note ne fusionne rien et ne provisionne rien : le merge et le geste root sont
hors du lot (#223620). La décision revient à **l'owner** (mesh-design,
17/11) ; elle est posée par `ameesh decide ask --lot 7681`.

## 0. Recommandation

Pour le mesh Manaty, qui n'a **qu'un seul humain** (`human:smichea`) et dont
l'avatar d'essai — comme le futur avatar du bureau — tourne sur un **forfait
Claude** :

1. **Recommandation : (c) attendre l'hôte/pod dédié** (#7633, conception
   #7659). C'est la seule option qui ne crée ni frontière de sécurité nouvelle
   ni dépense, et qui reste dans le modèle de la spec owner (décision 11 :
   clones dans leur propre pod ou sous leur propre utilisateur). Le clone
   n'est pas bloquant pour la bascule du bureau : l'avatar délègue par lots.
2. **(a) est la voie compatible si l'owner veut des clones plus tôt**, mais
   elle exige un compte Anthropic `api-key` et, en pratique, un avatar
   `api-key` : un hôte `policy.shared` n'admet que `api-key` (Q2 #213954), et
   `place_child` évalue **l'avatar** sur l'hôte d'arrivée. C'est donc une
   dépense nouvelle et un changement de compte de l'avatar, pas un simple
   geste root.
3. **(b) `avatar.children: same-host` est prêt sur branche** (code, tests,
   note), mais je ne le recommande pas : il partage l'UID et le runner du
   parent, ce qui s'écarte de la décision 11. Il ne devrait être retenu que
   si l'owner l'accepte explicitement, avec les bornes ci-dessous.
4. Aucune fusion de (b) avant la réponse de l'owner ; le correctif du refus
   sans nom est sorti dans le **lot #7686**, branche dédiée depuis main.

| option | garanties | coût | ce que voit l'owner |
|---|---|---|---|
| (a) tenant api-key | UID et runner propres, conforme à la décision 11 | compte Anthropic api-key, avatar admis en api-key, PR canon `policy.shared`/`tenants`, geste root bureau, deux avis | une dépense récurrente et un changement de compte de l'avatar |
| (b) `same-host` | clone immédiat, sans dépense ; borné au mesh mono-humain, hôte non partagé, allowlist `tools`, workdir neuf | même UID/runner que le parent ; l'enfant peut lire les fichiers du parent ; écart à la décision 11 | une capacité de clone tout de suite, avec un risque de sécurité assumé |
| (c) attendre #7633/#7659 | aucun risque ni dépense, reste dans la spec | clone indisponible jusqu'à l'hôte/pod dédié | la règle « l'avatar se clone » attend, sans action |

## 1. Constat mesuré (17/11 vers 05:10Z)

- `ameesh host tenant list` → **« no tenant »** : aucune ligne `host_tenants`.
- Aucune fiche Host ne déclare `policy.shared` ni `policy.tenants` (AG1, AG2,
  AG3, `manaty-mesh-1`, `pc-smichea`).
- `ameesh placement check --agent avatar-essai` : admis sur AG1/AG3,
  **mode `subscription`** uniquement ; les autres hôtes sont refusés (non
  admis, harnais/fournisseur, ou `max_agents: 0`).
- `ameesh accounts list` : les trois comptes Claude sont des **forfaits**
  (`subscription`) ; aucun compte Anthropic `api-key`. Le seul compte
  `api-key` listé est DeepSeek, **invalide** (`DEEPSEEK_API_KEY` absent).
- `ameesh agent spawn avatar-essai-clone1 --by avatar-essai --ttl 30m
  --tool Read` (AG1, 04:57:41Z, #223591) : `spawn-no-placement`, l'hôte AG3
  est refusé « host is not shared with its tenant ».

## 2. Option (a) : provisionner un tenant sur une VM

Le geste est un geste **bureau ou humain**, jamais un agent
(`docs/HOST-HANDOFFS.md`, § « Shared multi-tenant host »). Le plan exact est
rendu par :

```bash
ameesh host tenant add human:smichea --host ameesh-agents-1
```

Il crée `ameesh-smichea` (home 0700, linger), l'unité
`ameesh-runner-tenant@smichea.service` (`AMEESH_TENANT=human:smichea`,
`AMEESH_RUNNER_ID=ameesh-agents-1/smichea`), puis, sur l'hôte cible :

```bash
ameesh host tenant add human:smichea --host ameesh-agents-1 --register \
  --actor human:<id>|bureau
```

Il faut aussi une PR canon qui passe la fiche Host en `policy.shared: true`,
`policy.tenants: [human:smichea]` et `credential_modes: [api-key]`, avec deux
avis (§2, sensible : droits, comptes, infra). `canon check` refuse un hôte
`shared` sans tenant, un tenant non Member, et un mode autre que `api-key`.

**Pourquoi (a) ne débloque pas l'avatar d'essai tel qu'il est.**
`place_child` évalue **l'avatar** sur l'hôte d'arrivée
(`placement.evaluate(canon, root_card.title, host)`). L'hôte partagé n'admet
que `api-key` ; l'avatar est `subscription` : il est refusé avant même la
liaison de tenant. Il faut donc, en plus du geste root, un **compte Anthropic
`api-key`** (dépense nouvelle, go owner) et un avatar admis en `api-key`.
Aujourd'hui aucun des deux n'existe : le tenant seul ne suffit pas.

## 3. Option (b) : `avatar.children: same-host`

Valeur **distincte** de `avatar.children`, jamais par défaut ; `own-host`
reste le défaut et garde son sens. Un enfant `same-host` est placé sur
**l'hôte de son parent**, avec :

- un **dossier de travail neuf** (`{agent}` dans `policy.workspaces` de
  l'hôte), jamais le worktree du parent ;
- les **mêmes UID et runner que le parent** : ce mode ne promet **aucune**
  isolation d'UID ; il n'est donc pas une frontière de sécurité ;
- `admitted_hosts` **réduit à cet hôte** : l'enfant ne peut pas être déplacé
  ailleurs par le placement ;
- la liste `tools` du parent **explicite et non vide** (sinon refus) : le
  clone hérite d'un sous-ensemble de cette allowlist, et un enfant sans
  allowlist ne doit pas tourner sous l'UID du parent ;
- un refus si l'hôte est `policy.shared` (on reste dans le modèle tenant),
  si son `responsible` n'est pas celui de l'avatar (mesh mono-humain), ou si
  le `spawn` n'est pas lancé **depuis l'hôte du parent** (le workdir et
  l'UID prouvés sont ceux de cet hôte).

Ce que le mode **ne** fait pas : il ne crée ni utilisateur, ni runner, ni
clé ; il ne copie aucun credential ; il ne prétend pas que l'enfant est
isolé. La limite de §4.1 du design reste vraie : un enfant sous le même UID
peut lire les fichiers du parent ; l'allowlist d'outils est une garde contre
l'erreur et l'injection, pas une frontière contre un enfant malveillant.
D'où les bornes ci-dessus et un avertissement `canon check`
(`avatar-children-same-host`) à ajouter.

**État sur la branche** (`lot/7681-avatar-clone-placement-naming`) : le mode
est implémenté dans `avatar_children.py` (bornes), `canon_sync.py` et
`storage/postgres/canon.py` (la garde de lignée est levée pour ce seul mode,
jamais pour `own-host`), la CLI affiche « same-host placement… no UID
isolation », et 9 témoins couvrent les bornes dans
`tests/test_7107_avatar_enfants.py`. **Rien n'est fusionné.**

**Prérequis opérationnel sur AG1/AG3** : les fiches Host ne donnent de dossier
neuf qu'aux agents nommés (`workspaces.dirs.avatar-essai`) ou aux équipes
(`work_roots`) ; `avatar-essai` n'a pas d'équipe. Pour rejouer le clone, il
faut une racine par enfant sur l'hôte (`work_root: "~/work/{agent}"`, ou une
entrée `work_roots` pour l'équipe de l'avatar) : c'est une **PR canon**
(manaty/home), pas un geste de ce lot. Sans elle, `same-host` refuse
`spawn-no-placement` « no per-child work directory », comme `own-host`.

## 4. Option (c) : pas de clone avant l'hôte/pod dédié

L'avatar continue de déléguer par **lots** (ce que fait déjà le bureau), et
les clones attendent l'hôte/pod dédié : #7633, conception #7659. C'est
l'option qui ne demande ni geste root, ni compte, ni dépense, et qui ne
change pas le modèle de sécurité de la spec. Coût : la règle « l'avatar se
clone pour les tâches » (décision owner du 12/11) reste non satisfaite sur
Manaty jusqu'à #7633.

## 5. Décision et suite

- **Décision owner** : `ameesh decide ask --lot 7681`, options a/b/c,
  recommandation **c**, échéance au créneau owner du **2026-11-18** (owner
  absent aujourd'hui). La décision est signée.
- **(b) sur branche** : prêt à être relu si l'owner le retient ; aucune
  fusion avant la réponse, et deux familles d'avis (mode de sécurité).
- **Correctif sans nom** : **lot #7686**, branche
  `lot/7686-spawn-naming-refusal` depuis main, commit `12919d3f6`, tests
  `tests/test_468_agent_naming.py` 25/25. Indépendant de la décision : il
  entre au prochain paquet sans (b).
- **Après la réponse** :
  - (a) : le geste root reste bureau/humain ; lot de suite pour le compte
    `api-key`, la PR canon et le replay de l'étape 4 de #7635 ;
  - (b) : relire/fusionner la branche, mettre à jour
    `docs/design/5322-avatar-humain.md` §3.6 (mode `same-host` et sa limite)
    et ajouter l'avertissement `canon check`, puis rejouer `agent spawn
    avatar-essai-clone1 --by avatar-essai --ttl 30m --tool Read` sur AG1,
    `child report`, retrait de l'enfant ;
  - (c) : clore la limite connue, la rattacher à #7633/#7659, et laisser
    #7635 se clore sans clone.
