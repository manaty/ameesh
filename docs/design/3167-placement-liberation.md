# Placement et libération des ressources (manaty/ameesh#3167)

Spécification co-écrite avec l'owner le 2026-10-28 (session du bureau). Les
décisions marquées **[owner]** sont les siennes ; le reste en découle.

Lots : **A = #4147** (empreinte, état inactif, alertes), **B = #4148**
(transitions et adaptateurs). Partie C, hors ameesh :
thepackengers/devops#472 (pools Kapsule de TPK).

## 0. Le problème

Un mesh garde aujourd'hui sa capacité allumée même sans travail. Nuit du
2026-10-28 chez TPK : un pod non évacuable placé sur le nœud le plus vide a
gardé un 4e nœud toute la nuit (`scaleDown: NoCandidates`). Côté Manaty,
agents-1, agents-2 et agents-3 restent allumés la nuit alors que les agents
toujours actifs (orchestrateur, auditeur, mesh-design) tiendraient sur un
seul hôte. Rien dans ameesh ne montre cette capacité tenue à vide.

Ce qui existe déjà et sur quoi on s'appuie :

- `host_underused` (`sous_utilisation.py`) : hôte presque vide, mais
  seulement **quand un autre hôte est sous pression ou que des agents
  attendent**. C'est une suggestion de déplacement. Il ne dit pas « le mesh
  est au repos et cette capacité peut être rendue ».
- Drainage et retour d'hôte (L232e, `host_lifecycle.py`) : demandes
  persistantes, effets par l'assistant de handoff.
- Tenue d'hôte (#254, `host_holds.py`) : arrêt voulu, borné, qui fait taire
  les alertes de l'hôte.
- La mesure publiée par hôte (`resources.measure`, `ameesh hosts`) : CPU,
  mémoire, charge, tours. Ce que l'hôte **réserve** n'y figure pas.

## 1. Vocabulaire

- **Travail d'un hôte** **[owner]**. Un hôte travaille si l'une de ces
  conditions est vraie pour lui ou pour un de ses agents :
  1. un tour est actif, ou un bail de lot est tenu par un de ses exécuteurs ;
  2. un run ou Job déclaré par un demandeur (par exemple IMA) lui est
     attaché ;
  3. un lot actionnable ou du courrier non lu attend un de ses agents ;
  4. un réveil de lot est dû dans moins de `idle_after_s` pour un de ses
     agents.
- **Inactif** : aucune de ces conditions depuis `idle_after_s`, 30 min par
  défaut **[owner]**, réglable par mesh.
- **Réservé** **[owner]** : dans un conteneur ou un pod, les limites lues
  dans le cgroup de l'exécuteur (`cpu.max`, `memory.max`), sans aucune API
  Kubernetes. Sur une VM, ou si le cgroup dit `max`, la machine entière
  (nombre de CPU, `MemTotal`). Coût en €/h si la fiche d'hôte du canon donne
  un tarif (`tarif_eur_h`).
- **Baseline et burst** **[owner]**. Un mesh garde au plus `baseline_hosts`
  hôtes toujours allumés, 2 par défaut : le leader et une réserve pour
  l'élection (règle owner du 2026-10-22). Tous les autres hôtes sont
  **burst** : ils n'ont pas vocation à rester allumés sans travail. La fiche
  d'hôte dit `capacity: baseline | burst`. Une fiche muette vaut `burst`,
  avec une garde : tant que le mesh déclare moins de `baseline_hosts`
  hôtes baseline, les hôtes du leader élu et de sa réserve (agents
  critiques, `critical_agents`) comptent comme baseline. Sans cette garde,
  un mesh sans déclaration pourrait se drainer jusqu'au leader. B ne draine
  **jamais** l'hôte qui porte le leader élu.

## 2. Partie A, #4147 : voir et signaler (lecture seule)

### 2.1 Empreinte par hôte

La mesure de chaque hôte gagne les champs suivants :

- `reserved_cpu` et `reserved_mem_bytes` (§1) ;
- `used_cpu` et `used_mem_bytes`, déjà mesurés ;
- `active_turns`, `leases`, `runs` ;
- `state` : `busy` | `idle` | `draining` | `idle-drained` (B remplit les
  deux derniers) ;
- `idle_since`, `capacity` (`baseline`/`burst`), et `cost_eur_h` si connu.

Ces champs s'affichent dans `ameesh hosts` (ligne `footprint`), dans
`ameesh show` et dans `ameesh doctor`. `ameesh hosts --json` les porte tels
quels.

### 2.2 Alertes

Destinataires : **les agents seulement** **[owner]**, c'est-à-dire le
pilote, l'auditeur et les briefs. Elles ne sont pas dans `notify.types` par
défaut, donc pas de push à l'owner. Elles apparaissent dans le bilan du
bureau.

- **`host_idle_reserved`** : un hôte **burst** inactif depuis
  `idle_after_s` et toujours allumé. Le message dit ce qu'il réserve
  (CPU/mémoire, €/h) et qui le tient (agents, baux, runs). Il propose
  l'action : drainage (B), tenue, arrêt de la réserve.
- **`baseline_oversized`** : plus de `baseline_hosts` hôtes inactifs
  **ensemble** pendant `idle_after_s`. Le message nomme les hôtes à passer en
  burst et les agents toujours actifs à regrouper.

Comme les autres alertes, elles se taisent sous une tenue d'hôte (#254) et
pendant un drainage en cours. Un hôte sans relevé frais n'est pas inactif :
il est perdu, et les alertes existantes s'en chargent.

### 2.3 Acceptation de A

- Un hôte sans travail depuis 30 min apparaît `idle` avec son empreinte et
  son coût.
- Un hôte burst inactif 30 min déclenche `host_idle_reserved`, qui nomme ses
  détenteurs.
- Trois hôtes inactifs ensemble avec `baseline_hosts = 2` déclenchent
  `baseline_oversized`, qui nomme l'hôte à passer en burst.
- Témoins : réservé lu dans un cgroup simulé (limites et `max`), sur une VM,
  et sous une tenue d'hôte (silence).

## 3. Partie B, #4148 : libérer et reprendre (après A)

### 3.1 Déclenchement **[owner]**

ameesh draine **seul** un hôte burst inactif depuis `idle_after_s`. Il le
fait par le drainage L232e existant, sans mécanisme nouveau. Il le fait
revenir seul quand du travail arrive pour un agent qui n'est admis que sur
des hôtes drainés. Les hôtes baseline ne sont jamais drainés
automatiquement.

### 3.2 Ordre strict (le point sûreté)

- **Vers la libération** : `busy` → `draining`. L'hôte n'accepte plus aucun
  bail. Les tours en cours finissent, dans la borne de drainage de
  l'exécuteur (30 min). Ensuite `idle-drained`, puis crochet
  `idle-drained`, puis l'adaptateur rend la ressource.
- **Vers la reprise** : crochet `busy` d'abord, puis l'adaptateur rend
  l'hôte non évacuable ou le rallume. ameesh n'accepte un bail qu'**après**
  le succès du crochet.
- Il n'y a donc jamais de bail sur un hôte qu'on est en train de rendre.
  Témoin de course : un bail demandé pendant la transition est refusé, ou
  servi par un autre hôte. Aucun tour n'est tué par une éviction.

### 3.3 Interface : commande-crochet **[owner]**

La fiche d'hôte ou la config déclare
`lifecycle_hook: <commande>`. ameesh l'appelle avec `busy`, `draining` ou
`idle-drained` et l'hôte en argument, puis attend son code de sortie. Un
échec garde l'état précédent et lève une alerte. ameesh ne détient **aucun**
droit cloud ni Kubernetes : seuls les adaptateurs en ont.

### 3.4 Adaptateurs **[owner : les deux]**

1. **Kubernetes (TPK)**. Le crochet annote le pod de l'exécuteur avec
   `cluster-autoscaler.kubernetes.io/safe-to-evict`, `false` sur `busy` et
   `true` sur `idle-drained`. Le pod de service IMA suit le même principe par
   un crochet IMA. Le `terminationGracePeriodSeconds` du pod couvre le
   drainage (≥ 35 min). Livré avec thepackengers/devops#472.
2. **VM Scaleway (Manaty)**. Sur `idle-drained` d'un hôte burst, le crochet
   éteint la VM, comme `ci-autoscale` de nexlink-ci-1. Sur `busy`, il la
   rallume et attend la mesure fraîche de l'hôte avant de rendre la main.
   Les identifiants Scaleway restent ceux de l'adaptateur, jamais ceux
   d'ameesh. Une VM éteinte ne facture plus le calcul. La rallumer est une
   dépense automatique que l'owner a acceptée pour les hôtes burst.

### 3.5 Acceptation de B

- Un hôte burst inactif 30 min est drainé puis rendu (pod évacuable ou VM
  éteinte), sans tour tué.
- Du travail pour un de ses agents le fait revenir, avec le crochet `busy`
  avant tout bail.
- Les hôtes baseline restent allumés.
- La nuit, le mesh Manaty retombe à 2 hôtes allumés.

## 4. Partie C, côté TPK (thepackengers/devops#472)

Hors ameesh, réalisée par les agents TPK. Décisions **[owner]** du
2026-10-28 :

- Pool `baseline` de `tpk-stg` : 2 × DEV1-L, fixe, sans autoscaling. Il porte
  le pod de service IMA, `ima-document-runtime` et les hôtes ameesh d'IMA.
- Pool `burst` : DEV1-L, min 0, max 8, pour les Jobs d'implémentation et de
  test. Le plafond ne monte que sur mesure, quand des runs IMA attendent.
- Terraform fait foi. Le max 24 posé à la main hors Terraform est abandonné.
- Les agents TPK réalisent C par #472. Le maillage Manaty ne touche ni leurs
  agents ni leur cluster.

Le correctif provisoire est thepackengers/issue-manager-agent#1116,
fusionné.

## 5. Hors périmètre
- Le placement des pods par ameesh : ameesh ne programme pas de pods.
- La pause opérateur bornée : manaty/ameesh#3014 (lot #3961).
