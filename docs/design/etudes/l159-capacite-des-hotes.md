---
type: Study
title: "Étude — capacité des hôtes prêtée au mesh, contrat commun avec Nexlink Compute"
description: "Prêter aux agents d'ameesh la capacité inutilisée des VM de l'organisation sans gêner leurs applications. Un contrat de « garde de capacité » commun à ameesh et à Nexlink Compute : offre, mesure commune, ameesh-host-state/1 étendu sans nouvelle version, paliers pour céder la place, plafonds posés par le noyau. Deux modes sur une VM (exécuteur complet sous slice plafonnée, déport des suites lourdes) ; sécurité ; mise en service sur la VM de tests ; lots estimés (L159a–L159i, N1–N3)."
status: draft
tags: [capacite, hotes, garde, porte-d-hote, cgroups, systemd, compute, deport, placement]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-11T04:09:00+02:00" }
sources:
  - { resource: "decisions/0028-ressources-des-hotes-et-repartition.md", title: "Décision 0028 (ressources des hôtes, contre-pression, déplacement)" }
  - { resource: "decisions/0031-plusieurs-canons.md", title: "Décision 0031 (limites physiques : la fiche la plus stricte)" }
  - { resource: "../../../src/ameesh/resources.py", title: "Mesure et pression de l'hôte (L31, L73, L106)" }
  - { resource: "../../../src/ameesh/hostcheck.py", title: "Hôte prêt à mener ses agents (L106)" }
  - { resource: "../../../src/ameesh/placement.py", title: "Placement gouverné (C4, R18, L31)" }
  - { resource: "../../../src/ameesh/canon.py", title: "Fiches Host, HostPolicy, Placement" }
  - { resource: "../../../src/ameesh/relocation.py", title: "Déplacement entre hôtes admis (L31)" }
  - { resource: "branche lot/v170-voie-b : docs/IMAGE-EXECUTEUR.md, docs/EXECUTEUR-MEDIEE.md, src/ameesh/mediated_executor/", title: "Exécuteur médié, porte d'hôte, enrôlement, image (L107–L115, non fusionnés)" }
  - { resource: "nexlink:desktop/src/compute/ (develop)", title: "Garde d'inactivité (#93), offre, PROV-2, PROV-3, D13, mode natif (#115–#117)" }
  - { resource: "nexlink:docs/specs/compute-agent-turn.md (branche 94b)", title: "Contrat Nexlink du tour d'agent, v0.2 (#94, non fusionné)" }
---

# Contexte

## Le besoin

Le propriétaire, les 10 et 11 octobre : utiliser la capacité inutilisée des VM
de l'organisation pour les agents d'ameesh, **sans déranger les applications**
qu'elles servent. Et : « on chevauche Nexlink Compute, il faudra s'assurer que
ce qu'on fait dans ameesh est réutilisable par Nexlink Compute ».

Nexlink Compute prête l'inactivité d'appareils personnels : des builds
aujourd'hui, des tours d'agent ameesh en cours d'intégration. Une VM
d'application qui prête ses creux pose le même problème, côté serveur.

## Le constat

Le poste du propriétaire est le seul hôte de onze agents.

* **2026-10-10** : la charge a atteint 43 pour 12 CPU, pour un seuil de 24
  fixé par 0028. Plus aucun tour ne partait, orchestrateur compris. Les tours
  retardés ont cumulé environ 80 minutes.
* **2026-10-11** : le `/tmp` du poste, un tmpfs de 12 Go, a franchi 80 % à
  02:58 et s'est rempli vers 03:38. Tous les tours se sont bloqués.
* **2026-10-10, VM du mesh** (4 vCPU, 8 Go, sans swap) : le processus `node`
  d'un harnais a atteint 4,1 Go de mémoire résidente, et l'OOM du noyau l'a
  tué. L'unité de l'exécuteur n'avait aucun plafond.

Les suites de tests lourdes (Postgres en conteneur, jest, e2e) font
l'essentiel de la charge CPU et de `/tmp`. Les harnais font l'essentiel de
la mémoire : 2 à 4 Go par agent actif.

## La capacité disponible

Inventaire en lecture seule du 2026-10-11. On n'en garde ici que des
agrégats. Méthode :

* marge mémoire : 50 % du plus bas `MemAvailable` sur 7 jours (sysstat) ;
* marge CPU : 50 % de la part inactive, soit au créneau de 10 min le plus
  chargé (« aux pics »), soit en moyenne (« hors pics »).

Résultats :

* 23 VM allumées : 73 vCPU, 182 Go de mémoire.
* **Environ 31 Go réutilisables, sur deux VM seulement** :
  - une **VM de tests** : 8 vCPU dédiés, 32 Go. Elle prête ≈ 14 Go, ≈ 1 vCPU
    aux pics et ≈ 3 vCPU hors pics. Elle sert nos suites de tests, rien
    d'autre, mais son disque racine de 10 Go n'a que 1,9 Go libres ;
  - une **VM de CI** : 16 vCPU, 64 Go. Elle prête ≈ 18 Go, ≈ 2 vCPU aux pics
    et ≈ 7 vCPU hors pics. Ses jobs se concentrent sur quatre heures de
    l'après-midi. Son utilisateur `runner` est membre du groupe `docker`.
* Au total : ≈ 3 vCPU garantis aux pics, ≈ 10 hors pics, jusqu'à 12 la nuit.
  À 4 Go par agent, cela fait **environ 7 agents**.
* Les douze petites VM accessibles offrent ensemble ≈ 6,5 Go, **éparpillés
  par tranches de 0,2 à 1,3 Go**, soit moins qu'un agent par VM. Presque
  toutes servent de la production : elles sont **inutilisables pour des
  agents**.
* La VM du mesh n'a aucune marge.

Le contrat doit donc valoir pour n'importe quel hôte. Le pilote, lui, se
joue sur une ou deux grosses VM, en commençant par la VM de tests.

# Ce qui existe

## Dans ameesh (`main`)

* **Mesure et contre-pression** (L31, décision 0028, L73, L106 ;
  `resources.py`) :
  - Chaque exécuteur relève son hôte : mémoire disponible, swap, charge sur
    1 min, CPU, disque libre du dossier de travail, tmpfs de `/tmp`,
    alimentation et tours en cours. Les relevés vont dans `host_resources`,
    gardés 7 jours.
  - Les seuils sont au canon (`policy.resources` de la fiche `Host`), avec
    des défauts prudents.
  - Un seuil franchi bloque les **nouveaux** tours. En pression critique, les
    agents de plus faible priorité sont mis en pause, **jamais au milieu d'un
    tour**.
  - L31b rend l'attente visible ; un orchestrateur passe sous une pression
    non critique ; un épisode tient en deux lignes de journal.
* **Hôte prêt** (L106, `hostcheck.py`) : `ameesh doctor --harness` vérifie
  les binaires des harnais et les unités d'exécuteur activées. Il mesure la
  disponibilité de l'outillage, pas celle de la capacité.
* **Fiches et placement** (`canon.py`, `placement.py`, décisions 0014, 0029,
  0031) :
  - La fiche `Host` porte un responsable, des administrateurs, des étiquettes
    et une politique : harnais, fournisseurs, modes d'identifiants,
    `max_agents`, seuils, dossiers de travail, ménage.
  - Une admission (`Placement`) nomme les hôtes admis d'un agent.
  - Avec plusieurs canons, les limites physiques retenues sont les plus
    strictes des fiches.
* **Déplacement** (0028 §5, `relocation.py`) : entre deux tours, un agent
  quitte un hôte sous pression pour un hôte admis disponible. Le choix va au
  plus de mémoire, puis à la plus faible charge. La session suit si son
  stockage est partagé ; sinon, rotation avec résumé.

## Dans ameesh (voie B, branche `lot/v170-voie-b`, non fusionnée)

L'exécuteur médié (L107–L115) a été conçu pour les appareils de Nexlink
Compute. Trois de ses pièces servent ici telles quelles.

* **La porte d'hôte** (L112, `mediated_executor/gate.py` et `host_gate.py`).
  Un processus extérieur, le runner Compute, écrit l'état
  `ameesh-host-state/1` dans un fichier (fichier temporaire puis `rename`)
  ou le pousse sur une socket. L'exécuteur répond par `ameesh-host-ack/1`.

  ```json
  {"schema": "ameesh-host-state/1", "state": "draining", "seq": 8,
   "until_ts": null, "drain_deadline_ts": 1791640090.12,
   "caps": {"max_concurrent": 1, "cpu_share": 0.5, "memory_mb": 4096},
   "reason": "user_active"}
  {"schema": "ameesh-host-ack/1", "seq": 8, "state": "draining",
   "in_turn": [], "held": [], "drained": true, "ts": 1791640040.12}
  ```

  Selon l'état, l'exécuteur :
  - en `available`, réclame dans la limite `min(caps.max_concurrent,
    max_agents)` ;
  - en `draining`, ne réclame plus. Chaque tour s'arrête au point sûr, avec
    préemption à l'échéance (90 s par défaut). Les baux sont rendus, puis
    l'acquittement dit `drained: true` ;
  - en `stopped`, arrête tout immédiatement.

  Le retour à `available` relance la réclamation sans geste humain.

  Un état illisible vaut `stopped` pour un hôte médié et `available` pour un
  hôte classique (`host_gate_fallback`). Un hôte classique peut déjà avoir
  une porte (`AMEESH_HOST_GATE=file`). L'exécuteur n'applique que
  `caps.max_concurrent` : `cpu_share` et `memory_mb` circulent, mais
  personne ne les fait respecter côté ameesh.
* **L'hôte volatil** : `policy.volatile: true` donne un bail de 90 s,
  renouvelé toutes les 30 s ; `policy.lease_ttl` le règle.
* **L'enrôlement** (L110) :
  - un humain habilité émet un code à usage unique, lié au mesh, à l'hôte et
    à une liste d'agents (`ameesh host enroll`) ;
  - l'appareil s'enrôle avec une clé P-256 (`ameesh device enroll
    --code-file`) et obtient des jetons courts ;
  - la révocation relâche les baux.

  S'y ajoutent l'image `ameesh-executor/1` (L114 : uid 10001, aucun secret de
  fournisseur, sorties 0 à 6) et l'essai de bout en bout (L115 : sept étapes
  vertes en conteneur). Ensemble, ils forment le mode « appareil sans accès
  à la base ».

## Dans Nexlink Compute (`develop`, 2026-10-11)

Les numéros (#93, #94, #115 à #117) sont ceux des tâches du mesh Nexlink,
pas des issues GitHub.

* **Garde d'inactivité** (#93, fusionnée ; `desktop/src/compute/state.ts`,
  `idle-guard.ts`, `resources.ts`, `conditions.ts`). Elle écrit
  `ameesh-host-state/1` « exactement » ; le format propre du plan initial
  (`status.json`, cinq états) a été abandonné au gel.
  - **Signaux** : cinq minutes sans saisie, secteur, réseau non mesuré,
    fenêtre horaire de l'offre. Avant la réclamation seulement, s'y ajoutent
    un CPU occupé à 30 % au plus et 2 Gio de mémoire libre. Pendant le
    travail, rien n'est mesuré (« la VM est la charge »). Il n'y a pas de
    runner sous Linux.
  - **Écriture** : une sonde toutes les 2 s, et l'état n'est réécrit que
    s'il change. `seq` part de l'horloge. Les `caps` valent
    `max_concurrent: 1` ; `cpu_share` et `memory_mb` viennent de l'offre
    (mémoire libre au moment de l'offre, multipliée par la part consentie).
  - **Raisons** : `idle`, `user_active`, `battery`, `window_end`,
    `metered`. La pause du propriétaire, le bail perdu et l'arrêt deviennent
    tous `revoked`.
  - **Actions** : sans travail en cours, préemption immédiate. Avec du
    travail, `draining`, puis arrêt de la VM. La pause du propriétaire donne
    `stopped`. Ni gel, ni pause-reprise, ni baisse de priorité.
  - **Acquittement** : le lecteur, durci, n'admet que les clés `schema`,
    `seq`, `drained` et `agents_in_turn`. **Il refuse donc l'acquittement
    réel d'ameesh** (`state`, `in_turn`, `held`, `ts`). L'exécuteur n'est
    jamais reconnu, et le retrait tombe à 15 s au lieu de 90 s. La branche
    de #94 corrige ce lecteur.
* **Tour d'agent** (#94 ; branches `agent/compute-agent-turn` et `…-94b`,
  non fusionnées). Le contrat `compute-agent-turn` v0.2 a convergé avec la
  voie B. Le drapeau `COMPUTE_AGENT_TURN_ENABLED` est éteint, et `develop`
  n'admet que la classe `build`.
  - Un job de classe `agent` réserve le bac à sable de l'appareil pour une
    fenêtre (4 h proposées).
  - Il y lance l'image `ameesh-executor/1` d'un seul mesh, qui s'enrôle
    (L110).
  - Le bail Compute, de 120 s (renouvelé toutes les 40 s), couvre le bail
    ameesh de 90 s.
  - `window.json` fait le compte de la fenêtre à sa fin.
* **PROV-2 et PROV-3** (fusionnés, activation fermée) :
  - réseau du Mac (vzNAT) ;
  - une VM par couple (appareil, mesh) : Lima sur Mac, WSL sur Windows ;
  - l'exécuteur tourne dans un conteneur Podman sans root (`--cpus=2
    --memory=3g --pids-limit=512 --read-only --cap-drop=ALL
    --stop-timeout=100`), et ne sort que vers le serveur du mesh, par un
    mandataire.
  - **La taille de la VM d'agent est fixe** (2 vCPU, 4 Gio, 40 Gio) et ne
    dépend pas de l'offre. D13 dimensionne, elle, la VM de build selon
    l'offre (`nexlink-compute-sizing/1`), comme une allocation et non comme
    un plafond d'usage.
* **Mode natif** (#115 à #117 : admission, consentement et effacement
  écrits en fonctions pures, sans câblage de production) : des builds de
  bureau hors VM, sous un compte standard, sur macOS et
  Windows x64. **Aucun agent n'y est admis.** Ses plafonds (`cpuPercent`,
  `memoryBytes`, `outputBytes`, `durationSeconds`) passent par un port
  `enforceResources` qu'aucun OS n'implémente encore.
* **Offre** : `ComputeOffer` v1, signée, envoyée au hub et non au mesh. Elle
  porte les cœurs, la mémoire, le disque, des conditions (`idle`, `ac`,
  `wifi`), une fenêtre en heure locale, les parts consenties et l'egress par
  jour. Défauts : de 22 h à 7 h, 25 % du CPU et de la mémoire, 20 % du
  disque.
  La spécification mère prévoit davantage, sans que rien n'en soit
  construit : un profil `when` / `caps` / `on_context_change` (`pause`,
  `finish_current` ou `return`), l'adaptation continue et un budget
  partagé.

## Ce qui manque

1. **La mesure ne distingue pas l'application de l'invité.** Sur une VM
   prêtée, la charge et la mémoire relevées par ameesh comptent celles des
   agents eux-mêmes. Côté Nexlink, rien n'est mesuré pendant le travail.
2. **Rien ne plafonne un agent d'ameesh.** L'unité `ameesh-runner-agent@`
   n'a ni `MemoryMax`, ni `CPUWeight`, ni slice : c'est la cause de l'OOM de
   la VM du mesh.
3. **Céder la place est trop lent.**
   - La contre-pression n'arrête que les nouveaux tours.
   - Un tour en cours dure jusqu'à 15 min, et le travail lancé en fond garde
     20 min de grâce.
   - L'arrêt par signal draine jusqu'à 30 min.
4. **Les `caps` ne disent pas l'enveloppe réelle.** Côté ameesh, rien ne les
   fait respecter. Côté Nexlink, ils viennent de l'offre, alors que la VM
   d'agent est fixe (2 vCPU, 4 Gio) : ils peuvent dépasser la réalité ou
   rester en dessous. Disque, E/S et durée n'y figurent pas.
5. **Les fenêtres sont pauvres.** `until_ts` ignore la durée maximale du
   job, et rien ne dit quand la prochaine fenêtre s'ouvre.
6. **L'acquittement réel est refusé** par le lecteur de Nexlink sur
   `develop`.
7. **Les chemins divergent dans la documentation d'ameesh** (voie B).
   `EXECUTEUR-MEDIEE.md` et le défaut de `gate.py` disent
   `/run/ameesh-gate/state.json`. `IMAGE-EXECUTEUR.md` et Nexlink disent
   `/run/ameesh-gate/state/state.json`. La forme par dossier est la bonne :
   un `rename` n'est visible à travers un montage que si l'on monte le
   dossier.
8. **Nexlink a trois vocabulaires de plafonds** (offre, dimensionnement,
   mode natif), sans lien avec les `caps` de la porte.

# 1. Le contrat commun : la garde de capacité

## 1.1 Rôles

| Rôle | Sur une VM de l'organisation | Sur un appareil prêté |
|---|---|---|
| **Hôte prêteur** | VM d'application, VM de CI, VM de tests | portable, poste fixe |
| **Garde** (privilégiée, côté hôte) | `ameesh guard`, service système (à créer, L159e) | le runner Nexlink Compute (macOS, Windows) ; sous Linux, `ameesh guard` pourra servir |
| **Invité** (sans privilège) | exécuteur ameesh classique ou médié, ou job déporté | exécuteur médié dans la VM d'agent (PROV-3) |
| **Mesh** | voit l'offre, l'état et la mesure (`ameesh hosts`), place et déplace | idem, par `PUT /host/availability` |

La garde mesure, décide et **fait respecter**. Les plafonds sont posés par le
noyau (cgroups) ou par l'hyperviseur (taille de la VM), jamais laissés à la
bonne volonté de l'invité. L'invité obéit à l'état, rend ses baux et
acquitte. C'est déjà le partage de Nexlink : le runner écrit l'état et tient
la VM, l'exécuteur obéit.

## 1.2 Principes

1. **L'application gagne toujours.** L'invité vit dans une slice de faible
   priorité, sous des plafonds durs. Quand l'application reprend, il cède
   la place **sans attendre la fin de son tour**.
2. **Un seul format.** On étend `ameesh-host-state/1` par des champs
   **facultatifs**, que le lecteur d'ameesh ignore s'il ne les connaît pas
   (`GateState.from_json`).
   - **Aucune valeur d'état n'est ajoutée** en version 1 : un état inconnu
     est illisible, et un hôte classique retomberait alors sur `available`.
   - **L'acquittement ne change pas**, car le lecteur de Nexlink n'admet
     qu'une liste fermée de clés, et c'est voulu.
   - Un changement incompatible ouvre une version 2.
3. **Une seule mesure.** Les définitions sont les mêmes sur une VM et sur
   un appareil. La capacité prêtable se calcule sur la consommation de
   l'application, **jamais sur celle de l'invité** : sinon, l'invité se
   chasserait lui-même.
4. **Fermé par défaut.** Sur un hôte prêteur, une porte illisible, absente
   ou périmée vaut `stopped`, même pour un exécuteur classique
   (`host_gate_fallback: stopped`).
5. **Trois niveaux, toujours décroissants** : offre déclarée ≥ plafonds
   accordés (`caps`) ≥ consommation.
   - Sur une VM, l'offre est revue par le responsable de l'hôte, au canon.
   - Sur un appareil, elle est consentie par son titulaire, dans Nexlink.

## 1.3 L'offre : ce que l'hôte prête

Sur une VM, l'offre est **déclarative**. Elle vit dans la fiche `Host` du
canon et se revoit par PR, comme les seuils de 0028. La garde la lit dans sa
configuration locale ; `ameesh hosts` signale tout écart entre les deux.

```yaml
---
type: Host
title: vm-exemple
responsible: human:…
tags: [pret]
policy:
  volatile: true            # bail de 90 s : un invité tué est vite repris ailleurs
  max_agents: 3
  credential_modes: [api-key]
  work_root: /srv/ameesh-guest/work
  lend:                     # nouveau (L159a) ; absent = l'hôte ne prête rien
    cpu_max: 3              # vCPU au plus (quota)
    cpu_weight: 10          # poids face à l'application (100 par défaut)
    memory_max: 14GiB       # plafond dur de tout l'invité
    agent_memory_max: 4.5GiB  # plafond dur par agent ou par job
    disk_max: 80GiB         # volume dédié de l'invité
    windows: ["mon-fri 18:00-14:00 UTC", "sat-sun"]   # absent = toujours
    reclaim:                # quand l'application reprend (seuils de L'APPLICATION)
      memory_available_min: 6GiB
      cpu_pressure_max: 20%       # PSI cpu « some », moyenne 60 s, hors invité
      memory_pressure_max: 2%     # PSI mémoire « some », moyenne 60 s
      busy_units: ["actions.runner.*"]   # VM de CI : jobs en cours
      busy_max: 4
    drain_seconds: 90       # échéance du retrait
    calm_seconds: 600       # hystérésis avant de prêter de nouveau
---
```

Sur un appareil, l'offre est la `ComputeOffer` que le titulaire a
consentie. La fiche `Host` de l'appareil garde `policy.volatile` et
`max_agents` (souvent 1), et le mesh refuse toute réclamation au-delà.
Il faut aussi une table de correspondance de l'offre vers les `caps`, et
des `caps` vers la VM et le conteneur. Elle remplacerait les trois
vocabulaires actuels de Nexlink (N2).

## 1.4 La mesure commune

La garde publie dans l'état un objet `measure`, défini de la même façon
partout. Les champs que `resources.sample()` relève déjà gardent leur nom ;
les autres sont nouveaux.

| Champ | Définition |
|---|---|
| `ts`, `window_s` | instant du relevé ; fenêtre des moyennes (60 s) |
| `cpu_count` | CPU logiques de l'hôte (sur un appareil : ceux de la machine, pas ceux de la VM) |
| `app_cpu` | CPU consommés **hors invité**, en moyenne sur la fenêtre : le total moins l'invité (`cpu.stat` de sa slice, ou processus de la VM sur un appareil) |
| `guest_cpu` | CPU consommés par l'invité |
| `mem_total_bytes`, `mem_available_bytes`, `swap_used_bytes` | `/proc/meminfo` de l'hôte, comme aujourd'hui |
| `guest_mem_bytes` | `memory.current` de la slice de l'invité, ou mémoire de la VM |
| `psi` | `{cpu_some, mem_some, mem_full, io_some}` en %, moyenne sur 60 s, pour l'hôte ; `app_psi`, mêmes clés pour le groupe de l'application quand il est lisible |
| `disk_free_bytes` | volume de l'invité ; `app_disk_free_bytes` : le plus bas des systèmes de fichiers de l'application |
| `tmp_*` | occupation de `/tmp` s'il est en tmpfs (L73) |
| `on_ac`, `battery_percent`, `metered` | alimentation (L106) et réseau mesuré ; `null` sur une VM |
| `user_idle_s` | secondes depuis la dernière saisie ; `null` sur une VM |
| `busy` | nombre d'unités d'activité de l'application en cours (jobs de CI) ; `null` sans `busy_units` |
| `lendable` | `{cpu, memory_bytes, disk_bytes}` : capacité prêtable, selon la formule ci-dessous |

**Formule.** C'est celle de l'inventaire, rendue continue.

* **Historique.** La garde garde 7 jours d'historique local, à pas de
  10 min. Au premier démarrage, elle part de sysstat s'il existe, sinon
  d'une valeur prudente (25 % de la mémoire, 1 CPU).
* **Mémoire.** `lendable.memory_bytes = min(memory_max, h × M7)`. M7 est le
  plus bas, sur 7 jours, de `mem_available + guest_mem` : la mémoire que
  l'application a laissée libre à son pire moment. La marge `h` vaut 0,5.
* **CPU.** `lendable.cpu = min(cpu_max, h × (cpu_count − A7))`. A7 est le
  95e centile de `app_cpu` par créneau de 10 min, sur 7 jours.
* **Disque.** `lendable.disk_bytes = min(disk_max, disk_free − 2 Gio)`.

La même mesure alimente `ameesh hosts` et le déplacement (0028 §5). Un hôte
prêteur s'y classe par `lendable.memory_bytes`, et non par sa mémoire libre
brute. Sur le poste aussi, la PSI apporte ce que la charge ne dit pas : une
charge de 43 n'indique pas combien souffre l'utilisateur, la PSI si (L159d).

## 1.5 L'état : `ameesh-host-state/1` étendu

Les champs actuels ne changent pas : `schema`, `state`, `seq`, `until_ts`,
`drain_deadline_ts`, `caps` et `reason`. L'état est écrit par la garde et
lu par l'invité, dont le lecteur ignore les champs inconnus. Tous les
champs ajoutés sont donc facultatifs.

| Champ | Sens | Lecteur ancien |
|---|---|---|
| `expires_ts` | l'état n'est plus valable après cet instant. La garde le réécrit au tiers de sa durée (30 s par défaut). Un état périmé vaut illisible, donc `stopped` | l'ignore et garde le dernier état ; les plafonds du noyau restent posés |
| `until_ts` | précisé : la plus proche des deux fins, celle de la fenêtre et celle du job (`max_seconds`) | inchangé |
| `caps.cpu_share` | précisé : part des CPU **de l'hôte** (quota `cpu_share × cpu_count`) | inchangé |
| `caps.memory_mb` | précisé : plafond dur de **tout** l'invité, c'est-à-dire l'enveloppe réelle et non l'offre | inchangé |
| `caps.cpu_weight`, `caps.io_weight` | poids face à l'application (1 à 10 000) | ignorés |
| `caps.memory_high_mb` | plafond doux : au-delà, le noyau récupère d'abord la mémoire de l'invité | ignoré |
| `caps.agent_memory_mb` | plafond dur par agent ou par job déporté | ignoré |
| `caps.disk_mb`, `caps.tmp_mb`, `caps.max_job_s` | disque, `/tmp` et durée de l'invité | ignorés |
| `caps.enforced` | les plafonds que la garde fait respecter elle-même (noyau, hyperviseur) ; les autres sont indicatifs | ignoré |
| `freeze` | `{since_ts, until_ts}` : tours gelés par la garde (palier 2). L'exécuteur ne compte pas ce temps dans ses délais de tour | ignoré ; le gel est borné à 60 s, sous les délais |
| `windows` | prochaines fenêtres `[{start_ts, end_ts}]`, pour planifier | ignoré |
| `offer` | l'offre en vigueur (section 1.3), pour l'affichage et le contrôle d'écart | ignorée |
| `measure` | la mesure commune (section 1.4) | ignorée |
| `guard` | `{kind: "ameesh-guard" ou "nexlink-compute", version, host_kind: "vm" ou "device"}` | ignoré |

**Raisons.** Le texte libre est déjà admis. On fixe les valeurs suivantes :

* de Nexlink, les raisons existantes et `metered`, plus trois qui
  remplacent un `revoked` trompeur : `owner_pause`, `lease_lost`,
  `shutdown` ;
* des VM : `app_memory`, `app_cpu`, `app_busy` (jobs de CI), `disk_low`,
  `maintenance`, `guard_stopping`.

**Chemins.** On garde ceux de l'image et de Nexlink :

* `/run/ameesh-gate/state/state.json`, dossier en lecture seule pour
  l'invité ;
* `/run/ameesh-gate/ack/ack.json`, dossier inscriptible par l'invité.

Le défaut de `gate.py` et `EXECUTEUR-MEDIEE.md` (voie B) s'y alignent à
la fusion (L159f).

**Relais au mesh.** `ameesh-exec-availability/1` relaie au serveur les mêmes
ajouts. C'est un ajout de contrat (1.2), sans rupture : le serveur normalise
déjà le corps en écartant les champs inconnus.

## 1.6 Céder la place : les paliers

La garde évalue ses signaux toutes les 5 s, avec des moyennes sur 10 s pour
réagir et sur 60 s pour décider. Elle monte d'un palier dès qu'un seuil de
reprise est franchi. Elle ne redescend qu'après `calm_seconds` passées sous
tous les seuils (hystérésis), en incrémentant `seq`.

| Palier | État écrit | Ce que fait la garde | Ce que fait l'invité |
|---|---|---|---|
| **0 — prêt** | `available`, `caps` = prêtable | slice à poids faible (`CPUWeight=10`, `IOWeight=10`), `CPUQuota`, `MemoryHigh`, `MemoryMax` ; `MemoryMax` par agent | réclame dans la limite de `max_concurrent` |
| **1 — réduit** | `available`, `caps` abaissés | ramène `max_concurrent` au nombre de tours en cours ; réduit le quota CPU ; ramène `MemoryHigh` à l'usage, et le noyau récupère le cache de l'invité | ne lance plus de tour au-delà |
| **2 — gel** (CPU ou E/S seulement) | `available` + `freeze` | gèle les unités des agents, exécuteur compris, et les jobs (`systemctl freeze`), 60 s au plus. Un gel ne libère pas de mémoire : il ne sert jamais pour elle | rien pendant le gel ; le bail de 90 s, renouvelé toutes les 30 s, y survit ; au dégel, le temps gelé ne compte pas dans les délais du tour |
| **3 — retrait** | `draining`, échéance à `drain_seconds` | dégèle, attend l'acquittement | arrête son tour au point sûr, travail de fond compris ; rend ses baux ; acquitte `drained: true` |
| **4 — arrêt** | `stopped` | 30 s après l'échéance sans `drained` : `systemctl kill`, puis arrêt de la slice | arrêt immédiat |

Ces paliers donnent un sens précis au `on_context_change` de la
spécification Nexlink :

* `pause` correspond au palier 2 ;
* `finish_current` au palier 3, avec une échéance longue ;
* `return` au palier 4.

**Filets indépendants de la garde** :

* le `MemoryMax` de la slice borne l'OOM à l'invité ;
* `OOMScoreAdjust=500` sur les processus de l'invité : l'OOM global les
  choisit en premier ;
* systemd-oomd surveille la slice, s'il est présent.

Une garde arrêtée laisse ses derniers plafonds en place (ce sont des
propriétés de la slice), et son état périme à `expires_ts`.

**Signaux de reprise** :

* sur une VM : mémoire disponible de l'application, PSI hors invité,
  `busy_units` (jobs de CI), disque de l'application, fin de fenêtre ;
* sur un appareil : retour de l'utilisateur, batterie, réseau mesuré, fin
  de fenêtre (#93). Pendant le travail, la mesure par ressource (N1)
  permettra de réduire avant de tout arrêter.

La réaction se compte en secondes aux paliers 0 à 2, puis en
`drain_seconds` au palier 3. L'application ne voit jamais l'invité prendre
plus que ses `caps`.

## 1.7 Plafonds : correspondances

| Plafond | Linux, cgroups v2 | systemd | Appareil (VM d'agent, PROV-3) |
|---|---|---|---|
| `cpu_share` | `cpu.max` | `CPUQuota=` (part × CPU × 100 %) | vCPU de la VM et `--cpus` du conteneur (aujourd'hui fixes : 2) |
| `cpu_weight` | `cpu.weight` (ou `cpu.idle` si le noyau et systemd le permettent) | `CPUWeight=` | priorité du processus de la VM (à étudier) |
| `memory_mb` | `memory.max` de la slice | `MemoryMax=` | mémoire de la VM et `--memory` du conteneur (aujourd'hui 4 Gio et 3 Gio) |
| `memory_high_mb` | `memory.high` | `MemoryHigh=` | — |
| `agent_memory_mb` | `memory.max` de l'unité de l'agent | `MemoryMax=` sur `ameesh-guest-runner@` | `--memory` du conteneur (un agent à la fois) |
| `io_weight` | `io.weight` | `IOWeight=` | — |
| `disk_mb` | taille du volume dédié | `ReadWritePaths=` limité au volume | disque de la VM (aujourd'hui 40 Gio) |
| `tmp_mb` | `/tmp` privé sur le volume | `PrivateTmp=yes`, `TMPDIR` | tmpfs du conteneur (256 Mio) |
| gel | `cgroup.freeze` | `systemctl freeze` / `thaw` | pause de la VM si l'hyperviseur la permet (facultatif) |
| retrait forcé | `cgroup.kill` | `systemctl kill`, `stop` | arrêt de la VM (`limactl stop`, `wsl --terminate`) |

## 1.8 L'acquittement

`ameesh-host-ack/1` ne change pas : `seq`, `state`, `in_turn`, `held`,
`drained` et `ts`. Le lecteur de Nexlink doit simplement admettre ces clés
(N2).

Les jobs déportés (mode b) n'acquittent pas. La garde les voit comme des
scopes (`ameesh-job-*.scope`) dans la slice de l'invité, et les arrête
elle-même à l'échéance.

## 1.9 Qui implémente quoi, et comment rester d'accord

* **ameesh publie le contrat** : `docs/GARDE-DE-CAPACITE.md`, les schémas
  JSON des deux messages et des **jeux dorés** (L159a). Un jeu doré associe
  des entrées brutes (`/proc/meminfo`, PSI, `cpu.stat`, offre, historique)
  aux sorties attendues : `lendable`, palier, état écrit. Ils vivent sous
  `tests/dore/capacity_guard/`, comme ceux de l'exécuteur médié.
* **ameesh fournit** la garde des VM Linux (`ameesh guard`, L159e) et
  l'invité : exécuteur classique ou médié, et le côté VM du déport. Comme
  Nexlink n'a pas de runner sous Linux, la même garde pourra servir un
  appareil Linux.
* **Nexlink garde son runner** comme garde des appareils. Il écrit les
  champs ajoutés et fait passer les mêmes jeux dorés dans ses tests (N1).
  Il fait aussi correspondre les `caps` à l'enveloppe réelle de la VM
  d'agent (N2).
* **En (a2), une VM réutilise la recette de lancement de PROV-3** : même
  conteneur, mêmes options, mêmes montages de la porte. Le conteneur se
  comporte alors de la même façon sur un appareil et sur une VM.
* **Un champ nouveau** se propose par PR sur le contrat d'ameesh, avec son
  jeu doré. Le lecteur d'ameesh l'ignore tant qu'il ne le connaît pas.

# 2. Deux modes sur une VM

## 2.1 Mode (a) : un exécuteur ameesh complet sous une slice plafonnée

L'hôte prêteur devient un hôte du mesh comme un autre, admis pour quelques
agents. Le déplacement de 0028 y envoie un agent quand le poste est sous
pression et que la VM est `available`.

* **Unités** : `ameesh-guest-runner@<agent>.service`. Ce sont des unités
  **système**, ce qui évite de dépendre de la délégation des cgroups au
  gestionnaire utilisateur.
  - Identité et plafonds : `User=ameesh-guest`, `Slice=ameesh-guest.slice`,
    `MemoryHigh` et `MemoryMax` par agent, `OOMScoreAdjust=500`, plus le
    durcissement de la section 3.
  - Porte : `AMEESH_HOST_GATE=file`, `AMEESH_HOST_GATE_FALLBACK=stopped`,
    avec les chemins de la section 1.5.
  - Arrêt : `AMEESH_DRAIN_SECONDS=120`, `turn_grace_seconds` court,
    `TimeoutStopSec=180`.
* **Deux variantes** :
  - **(a1) classique** : accès direct à la base du mesh. Il faut un pair
    WireGuard, un rôle Postgres limité (L56) et les secrets de l'agent sur
    la VM (compte du harnais, jeton de forge). Possible sur `main` dès que
    la porte y est (L159f).
  - **(a2) médié** (voie B) : aucun accès à la base, aucune clé de
    fournisseur (relais de modèle), un enrôlement révocable. C'est l'image
    `ameesh-executor/1`, lancée comme PROV-3, et la garde écrit la porte. Il
    faut que la voie B soit fusionnée et que le serveur du mesh soit
    joignable en HTTPS. Les harnais au forfait n'y vont pas.
* **Fiche et admission** :
  - fiche `Host` avec `policy.lend`, `volatile`, `max_agents`, et un
    `work_root` sur le volume de l'invité ;
  - admission `hosts: [poste, vm]` pour les agents choisis ;
  - le déplacement doit aussi consulter l'état de la porte de l'hôte
    d'arrivée (L159f).

## 2.2 Mode (b) : déporter seulement les suites lourdes

L'agent reste sur le poste. Ses commandes lourdes (suite complète, e2e,
tests SQL avec Postgres en conteneur, constructions) partent sur la VM. Ce
mode règle la part CPU et `/tmp` du problème, sans mettre aucun secret de
l'agent sur la VM.

**(b1) par SSH**, faisable tout de suite sur la VM de tests :

* **Côté poste**, `ameesh offload run [--profile …] -- <commande>` :
  - prend un **instantané exact de l'arbre de travail**, par un index
    temporaire, `git write-tree` et `git commit-tree`. Ni l'index ni les
    références de l'agent ne bougent. Les fichiers non suivis sont compris,
    les ignorés exclus ;
  - l'envoie en **paquet git différentiel** vers un miroir du dépôt sur la
    VM ;
  - y lance la commande dans un dossier de job jetable. La sortie et le
    code de retour reviennent tels quels.
* **Côté VM**, un utilisateur `ameesh-guest`, sans sudo ni groupe `docker` :
  - sa clé SSH est restreinte (`restrict`, commande forcée
    `ameesh-offload-shell`) à quatre verbes : `put`, `run`, `status` et
    `cancel`. Ni shell, ni transfert de port ;
  - les conteneurs de test passent par **Podman sans root**, dans la slice
    de l'utilisateur ;
  - les dépendances sont mises en cache, par empreinte du fichier de
    verrouillage ;
  - chaque job est un scope (`systemd-run --user --scope`) plafonné à
    `agent_memory_mb` ;
  - des jetons de concurrence (`flock` ; trois sur la VM de tests) font la
    file, et le client affiche l'attente.
* **Obéissance à la porte** :
  - aucun nouveau job hors `available` ;
  - en `draining`, les jobs finissent jusqu'à l'échéance, puis la garde les
    tue ;
  - en `stopped`, ils sont tués tout de suite.
* **Repli** : quand la VM refuse ou arrête un job, le client relance la
  commande sur le poste (`--fallback local`). Sans `AMEESH_OFFLOAD_HOST`,
  ou si la VM est injoignable, la commande tourne aussi sur le poste. La
  consigne des agents dit quelles commandes passent par `ameesh offload`.

**(b2) par un runner** : un runner GitHub Actions étiqueté sur la VM. L'agent
le déclenche (`workflow_dispatch`) sur sa branche poussée, puis suit le run
avec `gh run watch`.

* Avantages : les journaux restent dans la forge, et il n'y a pas de clé SSH
  à gérer.
* Prix : une poussée et 20 à 60 s de latence.
* Sur un **dépôt public**, il faut un groupe de runners limité aux workflows
  choisis, et l'approbation des contributions extérieures.

On le garde pour les dépôts dont la CI tourne déjà sur nos VM.

## 2.3 Choix

| | (b1) déport SSH | (a1) exécuteur classique | (a2) exécuteur médié |
|---|---|---|---|
| Ce qui quitte le poste | CPU et `/tmp` des suites | des agents entiers (mémoire et CPU) | des agents entiers |
| Secrets de l'agent sur la VM | aucun | compte du harnais, jeton de forge, accès à la base | une clé d'exécuteur révocable |
| Dépend de | rien de nouveau | porte sur `main` (L159f), L56, WireGuard | voie B fusionnée, serveur HTTPS |
| Délai | 1 à 2 jours | environ une semaine | après la voie B |
| VM admissibles | VM de tests ; VM de CI après décision | VM de tests | toutes, VM de CI comprise |

**Recommandation** :

1. (b1) d'abord, sur la VM de tests, pour soulager le poste cette semaine ;
2. puis (a1) sur la même VM, pour deux ou trois agents ;
3. (a2) enfin, pour les VM qu'on ne contrôle pas entièrement, à commencer
   par la VM de CI.

Si la VM dédiée aux agents de L163 est créée, elle reçoit (a1) avant la VM
de tests. Elle n'a aucune application à protéger, et la VM de tests garde le
déport (b1). Le contrat s'y applique quand même : plafonds par agent
(L159c), mesure (L159d), et une garde réduite aux planchers de l'hôte.

# 3. Sécurité et isolement

* **Utilisateur dédié** `ameesh-guest` : pas de sudo, aucun groupe de
  l'application, pas de `docker`. Son home et son travail sont sur un volume
  dédié (`/srv/ameesh-guest`, `0700`).
  - Les unités système posent `NoNewPrivileges`, `ProtectSystem=strict`,
    `ReadWritePaths=/srv/ameesh-guest`, `ProtectHome=tmpfs`, `PrivateTmp`,
    `PrivateDevices`, `ProtectKernelTunables` et `RestrictSUIDSGID`.
  - `ProtectProc=invisible` cache les processus des autres utilisateurs,
    dont les lignes de commande portent parfois des secrets.
  - `ProtectControlGroups` empêche l'invité de relever ses propres
    plafonds.
  - Le mode (b) laisse les espaces de noms utilisateur ouverts, car Podman
    en a besoin.
* **Secrets de l'application** : l'invité ne lit ni ses fichiers ni son
  environnement. `InaccessiblePaths=` couvre ses dossiers de configuration
  et de données, ainsi que `/run/docker.sock`. Un contrôle d'installation,
  `ameesh guard check`, cherche sous l'identité de l'invité tout fichier de
  l'application qu'il pourrait lire.
* **Secrets de l'agent** (a1) : home `0700`, ou `LoadCredential=` de
  systemd. La frontière de confiance reste le root de la VM : tout ce qui
  est root, ou équivalent, lit ces secrets. Une VM n'accueille donc un agent
  à secrets que si tous ceux qui y ont un accès root sont dignes de ces
  secrets.
* **VM de CI : `runner` dans le groupe `docker`.** Ce groupe vaut root, car
  `docker run -v /:/h` lit tout le disque.
  - N'importe quel job de CI, y compris le code d'une PR, lirait les comptes
    des harnais, les jetons de forge et le mot de passe de base d'un agent
    (a1). Il pourrait aussi tuer l'invité ou relever ses plafonds.
  - **Aucun agent à secrets (a1) sur la VM de CI** tant que ce groupe est
    donné aux jobs.
  - Pour y prêter quand même, deux voies :
    1. le mode (a2), qui n'y laisse qu'une clé d'exécuteur révocable. Le
       pire cas devient l'usurpation de cet exécuteur, bornée à ses agents
       admis et à ses plafonds de relais ;
    2. une CI en Docker sans root, avec un utilisateur par runner. C'est un
       chantier des mainteneurs de la CI.
  - Le mode (b1) y reste possible : les jobs déportés n'emportent aucun
    secret, et ne rapportent que des journaux de tests.
* **Rôle Postgres limité** :
  - (a1) : le rôle `ameesh_executeur` de L56 (PR #23 : données oui, schéma
    non) et un rôle de connexion par hôte, que `pg_hba` n'admet que depuis
    l'adresse WireGuard de la VM. Limite connue : tous les exécuteurs
    partagent les mêmes droits sur les données ;
  - (a2) : aucun rôle Postgres ;
  - (b) : des bases jetables, propres au job (Podman, socket locale), jamais
    la base de l'application ni celle du mesh.
* **Réseau** :
  - entrant : rien en mode (a). En mode (b), SSH seulement, depuis le poste
    et la VM du mesh (groupe de sécurité ou WireGuard) ;
  - sortant : des règles nftables par uid de l'invité (`meta skuid`)
    refusent les ports locaux de l'application (base, cache, API sur la
    boucle locale) et le réseau interne, sauf la base du mesh en (a1). Le
    HTTPS sortant reste permis pour les fournisseurs et la forge. En (a2),
    seul le serveur du mesh est joignable, comme dans PROV-3.
* **Ce qui reste** : l'invité partage le noyau de l'application.
  L'isolement est celui de Linux entre utilisateurs, pas celui d'une VM.
  Prêter une VM de production d'un client demanderait une micro-VM, ou de ne
  pas prêter. L'inventaire a déjà tranché : ces VM sont trop petites.

# 4. Mise en service

## 4.1 VM pilote : la VM de tests

* **Pourquoi elle** :
  - elle appartient à notre projet : aucune donnée de client, aucune
    production, et seule la clé du propriétaire y est installée ;
  - elle garde plus de 28 Go libres pendant les suites, et elle est
    inactive le reste du temps ;
  - son « application », ce sont nos suites de tests. Y déporter celles des
    agents prolonge son usage, en mieux encadré : un utilisateur dédié
    plutôt que root, des plafonds, une file.
* **Condition** : un volume dédié de 100 Go, monté sur `/srv/ameesh-guest`,
  plutôt qu'une racine agrandie. Un invité qui remplit son volume n'atteint
  pas le système. Porter aussi la racine à 20 Go donnerait de l'air à
  Docker, déjà serré.

## 4.2 Étapes

1. **Préparation** (propriétaire) : volume, accès SSH depuis le poste,
   choix des modes.
   - Un script d'installation versionné et relu (`deploy/guest/install.sh`,
     L159b) prépare la VM : utilisateur, volume, plafonds de la slice
     utilisateur, Podman sans root, clé restreinte, règles nftables.
   - Le propriétaire le lance lui-même sur la VM : aucun agent ne modifie la
     configuration persistante d'une VM.
2. **Déport (b1)** : `ameesh offload` sur le poste, et une consigne des
   agents qui déporte les suites complètes et e2e. Une semaine de mesure.
3. **Plafonds partout** (L159c) : `MemoryMax` par agent, sur le poste et la
   VM du mesh, avec la slice `ameesh-agents.slice`. Cette étape ne dépend de
   rien d'autre, et elle corrige l'OOM de la VM du mesh.
4. **Garde** (L159e) sur la VM de tests, d'abord en observation : elle écrit
   l'état et la mesure, sans plafond dynamique.
5. **Exécuteur (a1)** sur la VM de tests, ou d'abord sur la VM dédiée de
   L163 si elle existe :
   - deux agents d'abord, sous DeepSeek par `dsh` (sans compte au
     forfait), avec des admissions `[poste, vm]` ;
   - une semaine de mesure ;
   - puis trois agents.
6. **VM de CI** : seulement après la décision sur l'isolement (section 3).
   On n'y prête qu'en dehors des heures de CI, en (b1) ou en (a2).

## 4.3 Retour arrière

* **(b1)** : retirer `AMEESH_OFFLOAD_HOST`, et les agents testent de nouveau
  sur le poste. Puis retirer la clé de `authorized_keys`, supprimer
  l'utilisateur et détacher le volume. L'application n'a jamais été touchée.
* **(a)** :
  1. `ameesh guard drain` (ou écrire `stopped`) : les agents rendent leurs
     baux ;
  2. une PR sur le canon retire l'admission : les agents repartent sur le
     poste au tour suivant ;
  3. désactiver les unités ;
  4. supprimer le rôle de connexion de l'hôte (a1), ou lancer `ameesh host
     revoke` (a2) ;
  5. supprimer l'utilisateur et la slice.
* **La garde** : `systemctl disable --now ameesh-guard`, puis remettre les
  propriétés de la slice. Rien d'autre n'a changé sur la VM.

## 4.4 Mesures de succès

Chaque étape se mesure une semaine avant et une semaine après. Les sources
sont les journaux de l'exécuteur (lignes de début et de fin d'épisode de
pression, L31b) et `host_resources` (7 jours).

| Mesure | Avant (10 et 11 octobre) | Cible |
|---|---|---|
| Tours retardés par la pression du poste | ≈ 80 min dans la journée du 10 | moins de 10 min par jour |
| Charge du poste | jusqu'à 43 pour 12 CPU | 95e centile sous 18, aucun épisode critique |
| `/tmp` du poste | plein le 11 vers 03:38 | jamais au-dessus de 80 % |
| Durée médiane d'un tour avec suite de tests | à relever (étape 2) | −30 % |
| Suites propres de la VM prêtée | à relever | 90e centile dégradé de 10 % au plus |
| Application de la VM prêtée | — | aucun OOM ; PSI mémoire hors invité sous 1 % hors retraits |
| Retraits | — | 99 % acquittés `drained` avant l'échéance |
| Usage | 0 | heures de tours ou de jobs hébergés par jour |

# 5. Lots

Les durées sont en heures de travail d'agent, revue comprise. Les dates
partent du lundi 12 octobre. La garde (L159e) attend le contrat (L159a) ;
le reste avance en parallèle.

## 5.1 ameesh

| Lot | Contenu | Dépend de | Durée | Date |
|---|---|---|---|---|
| **L159a** — contrat | `docs/GARDE-DE-CAPACITE.md` ; schémas JSON de l'état étendu et de l'acquittement ; `policy.lend` au canon (`canon check`) ; jeux dorés `tests/dore/capacity_guard/` ; chemins `state/` et `ack/` | cette étude | 5 h | 12/10 |
| **L159b** — déport (b1) | `deploy/guest/install.sh` (lancé par le propriétaire) ; `ameesh-offload-shell` (`put`, `run`, `status`, `cancel`) ; `ameesh offload run` (instantané, paquet différentiel, jetons de concurrence, repli local) ; consigne des agents | — | 8 h | 12–13/10 |
| **L159c** — plafonds par agent | `ameesh-agents.slice` ; `MemoryHigh` et `MemoryMax` par unité d'agent, sur le poste et la VM du mesh ; `OOMScoreAdjust` ; contrôle dans `ameesh doctor` | — | 3 h | 12/10 |
| **L159d** — mesure commune | dans `resources.py` : PSI, parts de l'application et de l'invité, `lendable` ; colonnes de `host_resources` (migration) ; `ameesh hosts` ; seuil `max_cpu_pressure` au poste ; déplacement classé par `lendable` | L159a | 6 h | 13/10 |
| **L159e** — `ameesh guard` | service système des VM Linux : offre, historique, paliers 0 à 4, plafonds par `systemctl set-property --runtime`, gel, `expires_ts`, attente de l'acquittement, `guard check`, `guard drain` ; jeux dorés | L159a, L159d | 12 h | 14–15/10 |
| **L159f** — porte sur `main` | porte de L112 pour l'exécuteur classique (fusion de la voie B, ou extraction de la seule porte) ; chemins par dossier ; `expires_ts`, `freeze` ; un retrait arrête aussi le travail de fond (grâce de fin de tour) ; défauts d'un hôte prêteur ; déplacement qui consulte la porte d'arrivée | décision sur la voie B | 6 h | 15/10 |
| **L159g** — pilote (a1) | unités `ameesh-guest-runner@` ; PR sur le canon (`Host` avec `lend`, admissions) ; deux agents, puis trois ; rapport de mesures | L159c, L159e, L159f, L56 | 4 h + 7 jours | du 16/10 au rapport du 23/10 |
| **L159h** — VM de CI | selon la décision : fenêtre et `busy_units` pour (b1), ou image (a2) avec la garde | décision du propriétaire, rapport de L159g | 6 h | à partir du 26/10 |
| **L159i** — (a2) sur une VM | conteneur `ameesh-executor/1` lancé par la garde comme PROV-3 (volume, porte, code d'enrôlement, arrêt en 100 s, codes de sortie) | voie B fusionnée, serveur du mesh en HTTPS | 4 h | à partir du 28/10 |

Total côté ameesh : environ 54 h, plus une semaine d'observation.

**Lots voisins**, déjà ouverts :

* **L161** : tests choisis selon l'impact, erreurs remontées en temps réel.
  Moins de suites lourdes à déporter, et le client de L159b peut relayer
  les erreurs au fil de l'eau.
* **L162** : rien de lourd en tmpfs, nettoyage des `/tmp` abandonnés. C'est
  le complément, sur le poste, du `/tmp` privé de l'invité.
* **L163** : une VM dédiée aux agents. C'est le premier hôte de (a1) si elle
  existe (section 2.3).

## 5.2 Nexlink : ce qui revient aux auteurs de #93 (claude1) et de #94 (claude2)

| Lot | Qui | Contenu | Dépend de | Durée | Date |
|---|---|---|---|---|---|
| **N1** — garde d'inactivité | auteur de #93 | écrire `expires_ts` et réécrire l'état au tiers de sa durée ; raisons distinctes (`owner_pause`, `lease_lost`, `shutdown`) ; `windows` ; `measure` selon les définitions communes, CPU et mémoire de l'hôte hors VM **pendant** le travail ; `guard` ; jeux dorés d'ameesh dans ses tests | L159a | 6 h | 14–15/10 |
| **N2** — tour d'agent | auteur de #94 | fusionner le lecteur d'acquittement de 94b (clés réelles `state`, `in_turn`, `held`, `ts`), sans quoi le retrait reste à 15 s ; `until_ts` = la plus proche de la fin de fenêtre et de `max_seconds` ; `caps` égaux à l'enveloppe réelle (VM d'agent et conteneur), ou VM dimensionnée selon l'offre comme D13 ; `caps.enforced` ; une seule table offre → `caps` → VM | N1 | 8 h | 16–17/10 |
| **N3** — contrat écrit | auteur de #94 | `compute-agent-turn` : `in_turn` et `held` (et non `agents_in_turn`) ; chemins `state/` et `ack/` ; renvoi au contrat de la garde | L159a | 0,5 h | 14/10 |

Aucun de ces lots ne bloque le pilote sur une VM. Ils rendent le même
contrat vrai sur les appareils.

## 5.3 Gestes du propriétaire

| Geste | Pour | Durée | Quand |
|---|---|---|---|
| Décider : VM pilote ; ordre (b1), puis (a1), puis (a2) | tout | 10 min | 11 ou 12/10 |
| Ajouter un volume de 100 Go à la VM de tests (et porter la racine à 20 Go) | L159b | 15 min | 12/10 |
| Ouvrir SSH vers la VM de tests depuis le poste et la VM du mesh | L159b | 10 min | 12/10 |
| Lancer `deploy/guest/install.sh` sur la VM (avec `!`) | L159b, L159e | 15 min | 12–13/10 |
| Trancher la fusion de la voie B (ou l'extraction de la porte) | L159f, L159i | 10 min | 14/10 |
| Pair WireGuard de la VM et rôle Postgres (fusion de L56, PR #23) | L159g | 20 min | 15/10 |
| Comptes admis sur une VM (DeepSeek seul d'abord ; forfaits ensuite ?) | L159g | 10 min | 15/10 |
| Relire la PR du canon (`Host` avec `lend`, admissions) | L159g | 10 min | 16/10 |
| VM de CI : vitesse de la CI ou prêt ; isolement du groupe `docker` (avec les mainteneurs de la CI) | L159h | 20 min | 24/10 |

# 6. Questions au propriétaire

1. **Ordre des modes** : (b1) cette semaine sur la VM de tests, puis (a1) ?
   Recommandation : oui.
2. **Volume** : 100 Go dédiés à l'invité plutôt qu'une racine agrandie ?
   Recommandation : le volume, plus 10 Go de racine pour Docker.
3. **VM de CI** : prêter seulement hors des heures de CI, en (b1) ou en
   (a2), et jamais en (a1) tant que `runner` est dans `docker` ?
   Recommandation : oui. La vitesse de la CI reste prioritaire ; les
   paliers 1 à 3 se déclenchent sur `busy_units`.
4. **Forfaits sur une VM** : un compte Claude ou Codex au forfait peut-il
   servir un agent (a1) sur la VM de tests ? Recommandation : DeepSeek
   d'abord, les forfaits après la séparation des comptes par organisation.
5. **Nom du champ** : `policy.lend` dans la fiche `Host` ?
6. **Premier hôte de (a1)** : la VM dédiée de L163, si elle est créée, ou la
   VM de tests ? Recommandation : la VM dédiée.
