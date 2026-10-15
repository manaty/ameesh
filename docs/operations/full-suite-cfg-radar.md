# `cfg.radar` — qualification `full_suite` : carte et runbook (lot #916)

Carte préparée le 2026-10-13 sur la branche `review/916-full-suite-cfg-radar`,
**sans CI** (seule une poussée vers `main`/`release/**`, ou une PR, déclenche la
suite). Aucun geste de production n'a été fait pour l'écrire.

**Dépendance : #756 / PR [manaty/ameesh#633](https://github.com/manaty/ameesh/pull/633).**
**Ne rien appliquer avant** que ce code soit fusionné **et déployé** sur l'hôte :
le code d'avant refuse la clé inconnue `full_suite` (`radar.parse_settings`
lève « `radar.projects.<p>`: unknown key(s) full_suite »), et la configuration
de l'hôte deviendrait illisible pour l'exécuteur.

**État au 2026-10-15 :** #756 est **fusionné** (`main` `313d73458`, paquet 19:55
fusionné à 19:37Z) ; le **déploiement** du build qui le porte reste à faire
(le build installé ici, `ameesh 1.11.1`, ne connaît pas encore `full_suite`).
Tant que ce déploiement n'a pas eu lieu : **ne rien appliquer sur les hôtes**.

Objet : la porte de release L756 lit `radar.projects.<projet>.full_suite` sur
l'hôte ; aucune déclaration n'existe aujourd'hui, donc toute release qui exige
la suite complète est refusée au déploiement ([#916](https://github.com/manaty/ameesh/issues/916),
constat N1 de la R2 #633).

## 1. Ce que la porte lit (code de #633)

* La suite complète est une **identité** — un fichier
  `.github/workflows/*.yml` déclaré sous `full_suite`, résolu par l'API en
  identité de workflow (`state: active`). Un chemin absent, désactivé ou
  dupliqué lève : jamais « pas de suite ».
* Une suite ne certifie que si elle a, **sur le sha exact**, une exécution
  `push` ou `workflow_dispatch` **terminée** et de conclusion `success`. Le
  plan impacté d'une PR (`pull_request`) ne certifie jamais.
* `jobs` nomme les jobs **requis** : chacun doit exister et être vert, toutes
  ses instances de matrice comprises (`"psycopg *"` couvre `psycopg 1/4` … ;
  sans `*`, le nom est exact).
* Les jobs **non requis** doivent être `success` ou `skipped` : un job sauté
  par conception ne bloque pas ; `cancelled`, `neutral` ou illisible ne
  certifie rien (fail-closed — une part annulée au plafond ne vaut pas vert,
  #940).
* `tolerate` nomme les jobs tolérés (`continue-on-error`) : leur conclusion ne
  certifie ni ne bloque ; un job toléré **non terminé** laisse en revanche le
  rapport en `pending` (pas de vert tant qu'il n'est pas terminé).
* Une entrée réduite au chemin exige **tous** les jobs : à éviter dès qu'un job
  est conditionnel.

## 2. Carte — les projets

### 2.1 ameesh

```json
"ameesh": {
  "repo": "<clone de l'hôte>",
  "github": "manaty/ameesh",
  "full_suite": [
    {"path": ".github/workflows/tests.yml",
     "jobs": ["plan", "psycopg *", "psql *", "plateforme macOS", "tests"],
     "tolerate": ["bilan des impactés"]}
  ]
}
```

Pourquoi ces jobs (relevé sur l'exécution `workflow_dispatch` 38355907994) :

* `plan` requis : il décide des parts ; sans lui rien ne certifie.
* `psycopg *` et `psql *` requis : ce sont les parts qui exécutent les tests.
  Les checks de flux `tests (flux) …`, publiés au fil de l'eau, restent **non
  requis** et doivent être verts ou sautés : une part annulée au plafond les
  laisse non terminés, donc la porte refuse (c'est le refus du faux vert #940).
* `plateforme macOS` requis : la couche plateforme (L63) ne tourne que là.
* `tests` requis : l'agrégat, vert seulement si les parts le sont.
* `bilan des impactés` **toléré** : `continue-on-error: true`, jamais requis.
* `version number` et `lecteurs de la doc` : sautés sur un dispatch (PR de
  version seule / documentation seule), donc non requis et légitimement
  `skipped`.

Preuve d'un vert complet sur la suite : exécution **38350245246** (`push`,
`3754ffffbc6f`, `success`, 26 jobs) — le fixture des tests de #756.

### 2.2 nexlink

```json
"nexlink": {
  "repo": "<clone de l'hôte>",
  "github": "manaty/nexlink",
  "full_suite": [
    {"path": ".github/workflows/backend-suite.yml", "jobs": ["backend"]},
    {"path": ".github/workflows/website-suite.yml", "jobs": ["website"]},
    {"path": ".github/workflows/mobile-suite.yml", "jobs": ["mobile"]},
    {"path": ".github/workflows/desktop-suite.yml", "jobs": ["desktop"]},
    {"path": ".github/workflows/scripts-suite.yml", "jobs": ["scripts"]}
  ]
}
```

* Un seul job « parapluie » requis par suite. Les jobs conditionnels
  (`changed-paths` de backend, `Desktop suite (…)` du desktop) sont **sautés**
  sur un dispatch : non requis, donc `skipped` accepté. Vérifié sur les
  dispatches 38366848457 (backend), 38444083884 (website), 38406858502
  (mobile), 38372147722 (desktop), 38385460349 (scripts).
* `sql-invariant.yml` n'est **pas** déclaré : la règle b de Nexlink
  (`AGENTS.md`) le déclenche conditionnellement (« si develop a reçu des
  migrations depuis le dernier vert ») et la porte L756 ne sait pas exprimer
  « conditionnel » — le déclarer rendrait toute release dépendante d'un
  dispatch SQL. À trancher par l'orchestrateur Nexlink si la procédure de
  release l'exige.

## 3. Carte — les hôtes

| Hôte | `repo` ameesh | `repo` nexlink |
|---|---|---|
| pc-smichea | `~/development/manaty/ameesh` | `~/development/manaty/nexlink` |
| ameesh-agents-1 | `~/work/ameesh-release` | `~/work/nexlink-release` |
| ameesh-agents-2 | `~/work/ameesh-release` | `~/work/nexlink-release` |

Les chemins de pc-smichea sont ceux cités par `BASCULE.md` et les cartes
d'hôte : **à confirmer sur l'hôte** (`ls -d ~/development/manaty/ameesh
~/development/manaty/nexlink`) avant d'écrire. Sur les VM, il n'existe pas de
clone partagé ameesh : créer un clone dédié (voir §5). `repo` est un chemin
**de l'hôte**, lu par l'exécuteur et par `ameesh release check`, quel que soit
l'agent qui agit ; il doit contenir la branche de release et les refs de la
cible, et être **fetché** avant un contrôle (la porte ne fetche pas).

### 3.1 pc-smichea

**Quel fichier ?** La déclaration doit être dans le fichier **réellement lu par
le processus qui exécute la porte** (land/`release check`) : `AMEESH_CONFIG`
s'il est posé par l'unité, sinon `~/.config/ameesh/config.json`. Sur pc-smichea
les exécuteurs chargent des profils par `AMEESH_CONFIG` (drop-ins
`~/.config/systemd/user/ameesh-runner-agent@*.service.d/*.conf`, profils
`~/.config/ameesh/runner-profiles/*.json`) : relever d'abord
`systemctl --user show <unité> -p Environment -p DropInPaths`, puis déclarer
dans ce profil — dans les deux si plusieurs acteurs peuvent lancer la porte.
Le §5 (sauvegarde, fusion, validation) se fait sur ce même fichier, et le §5.3
se lance avec le même `AMEESH_CONFIG`.

La clé `radar` existe probablement déjà (c'est l'hôte désigné du radar de
conflits) : **ajouter** `github` et `full_suite` aux projets existants, sans
écraser `migrations`, `target`, `land`, `gates` ni `merge_method`.

```json
"radar": {
  "projects": {
    "ameesh": {
      "repo": "~/development/manaty/ameesh",
      "github": "manaty/ameesh",
      "full_suite": [
        {"path": ".github/workflows/tests.yml",
         "jobs": ["plan", "psycopg *", "psql *", "plateforme macOS", "tests"],
         "tolerate": ["bilan des impactés"]}
      ]
    },
    "nexlink": {
      "repo": "~/development/manaty/nexlink",
      "github": "manaty/nexlink",
      "full_suite": [
        {"path": ".github/workflows/backend-suite.yml", "jobs": ["backend"]},
        {"path": ".github/workflows/website-suite.yml", "jobs": ["website"]},
        {"path": ".github/workflows/mobile-suite.yml", "jobs": ["mobile"]},
        {"path": ".github/workflows/desktop-suite.yml", "jobs": ["desktop"]},
        {"path": ".github/workflows/scripts-suite.yml", "jobs": ["scripts"]}
      ]
    }
  }
}
```

### 3.2 ameesh-agents-1 et ameesh-agents-2

Même bloc, avec `"repo": "~/work/ameesh-release"` et
`"repo": "~/work/nexlink-release"`. Sur ces VM, aucune unité d'exécuteur ne
pose `AMEESH_CONFIG` (relevé du 2026-10-15 sur `ameesh-agents-1` :
`systemctl --user show 'ameesh-runner-agent@*' -p Environment -p DropInPaths`,
aucun drop-in ne le déclare) : le fichier lu est
`~/.config/ameesh/config.json`.

**Effet de bord** : la clé `radar` désigne l'hôte pour le **radar de conflits**
(L160b). La déclarer sur une VM y lance un passage après chaque relevé de
branches (toutes les ~300 s), avec courrier aux assignés sur conflit nouveau —
le premier passage signale tous les conflits courants. Si ce bruit n'est pas
voulu, ne déclarer que sur l'hôte qui exécute la file de fusion et les
contrôles de release (pc-smichea), et n'ajouter une VM que si un
`ameesh release check` y est réellement attendu.

## 4. Prérequis d'application (ordre imposé)

1. #756 fusionné (PR #633) et le paquet **déployé** sur l'hôte. Preuve, avec
   l'interpréteur de l'exécuteur :

   ```bash
   python3 -c "from ameesh import radar; print('full_suite' in radar.PROJECT_KEYS)"
   # attendu : True
   ```

2. Le clone de chaque projet existe et est à jour. Sur une VM :

   ```bash
   git clone --filter=blob:none https://github.com/manaty/ameesh.git ~/work/ameesh-release
   git clone --filter=blob:none https://github.com/manaty/nexlink.git ~/work/nexlink-release
   git -C ~/work/ameesh-release fetch --prune origin
   git -C ~/work/nexlink-release fetch --prune origin
   ```

3. `gh` authentifié sur l'hôte (la porte lit l'API : workflows, exécutions,
   jobs).

## 5. Runbook (après #756 seulement)

1. **Sauvegarde** du fichier retenu en §3.1 :

   ```bash
   F=~/.config/ameesh/config.json   # pc-smichea : le profil AMEESH_CONFIG de l'unité
   cp -a "$F" "$F.bak-$(date +%F-%H%M)"
   ```

2. **Fusion** du bloc §3 dans ce fichier (éditer, ne pas écraser le reste).
   JSON sans commentaire ni virgule finale.

3. **Validation du schéma** (lecture seule, aucun effet) :

   ```bash
   F=~/.config/ameesh/config.json   # même fichier qu'en §5.1
   python3 - "$F" <<'PY'
   import json, os, sys
   from ameesh import radar
   raw = json.load(open(os.path.expanduser(sys.argv[1])))
   s = radar.parse_settings(raw.get("radar") or {})
   for name, p in sorted(s.projects.items()):
       print(name, p.repo, p.github, len(p.full_suite))
   PY
   ```

   Attendu : `ameesh … 1` et `nexlink … 5` ; toute faute de schéma lève ici,
   avant l'exécuteur.

4. **Rechargement** : l'exécuteur lit la configuration à son démarrage.
   Redémarrer l'unité dont le profil a été modifié en §3.1
   (`systemctl --user restart ameesh-runner-agent@<agent>.service`), ou
   attendre son prochain redémarrage. `ameesh release check` lit le fichier
   directement : pas de redémarrage nécessaire pour lui.

5. **Contrôle à blanc** (lecture seule, aucune publication) — sur pc-smichea,
   lancer avec l'environnement de l'unité (`AMEESH_CONFIG=… python3 …`) :

   ```bash
   python3 - <<'PY'
   from ameesh.config import load
   from ameesh import radar
   p = radar.settings_of(load()).projects["ameesh"]
   print(radar.full_suite_ci(p, "3754ffffbc6f"))   # sha du run vert 38350245246
   PY
   ```

   Attendu sur ameesh : `{'runs': 1, 'suites': 1, 'green': True, …}`. Sur
   nexlink, aucun sha ne porte aujourd'hui les cinq dispatches verts à la
   fois : le rapport rend `missing`/`failed` par suite — c'est le constat
   attendu, il prouve que la résolution des workflows fonctionne.

   Quand un lot de release gelé est disponible, le contrôle complet est
   `ameesh release check <lot> --json` (périmètre, base, gel, suite).

6. **Trace** : noter dans #916 l'hôte, la date, le sha de configuration et la
   sortie du contrôle à blanc.

## 6. Retour arrière

Restaurer la sauvegarde (ou retirer les entrées `full_suite`), revalider par
l'étape 3, redémarrer l'unité. Rien d'autre n'est touché : aucune donnée, aucun
dépôt, aucun geste GitHub.

## 7. Pièges

* La porte n'accepte que `push` et `workflow_dispatch` : la procédure de
  release doit **dispatcher** chaque suite déclarée sur la tête gelée (branche
  de release), puis attendre le vert job par job. Un run de PR ne certifie pas.
* Un job requis renommé fait échouer la porte en « required job missing »
  (fail-closed), jamais en silence : garder §2 en phase avec
  `.github/workflows/tests.yml` et les suites Nexlink.
* `repo` doit être fetché avant le contrôle ; le radar, lui, ne fetche jamais.
* Sur une VM, le clone dédié et le courrier du radar sont les deux seuls
  effets — aucun test n'y est lancé par la porte (elle lit l'API).

## 8. Preuves de préparation (fait le 2026-10-13, sans prod)

* **Schéma** : les six blocs projet ci-dessus passent `radar.parse_settings`
  du code de #633 (worktree sur `aaad7117`) — 3 hôtes × (1 suite ameesh +
  5 suites nexlink), `repo`/`github`/`full_suite`/`jobs`/`tolerate` conformes.
* **Correspondance jobs** : lue sur des exécutions réelles
  (`gh run view <id> --repo <slug> --json jobs`) — ameesh
  `38350245246` (push vert, jobs `plan`, `psycopg 1..4/4`, `psql 1..6/6`,
  `plateforme macOS`, `tests (flux) …`, `bilan des impactés`, `tests`) et
  `38355907994` (dispatch rouge : la porte refuse `psycopg 1/4`, `psql 1/6`,
  `psql 5/6` et l'agrégat `tests`, sans job requis manquant) ; nexlink
  `38366848457`, `38444083884`, `38406858502`, `38372147722`, `38385460349`
  (dispatch verts, jobs conditionnels sautés acceptés).
* **Re-vérification du 2026-10-15, sur le code FUSIONNÉ de #756** (`main`
  `313d73458`, paquet 19:55 fusionné à 19:37Z ; worktree jetable, aucun geste
  de production) : les six blocs §2/§3 passent `radar.parse_settings` de
  `origin/main` ; `tests.yml` porte toujours `plan`, `psycopg *`, `psql *`,
  `plateforme macOS`, `tests` et le job toléré `bilan des impactés` ;
  `radar.settings_of` et `radar.full_suite_ci` sont présents. **Rien n'est
  encore appliqué** : le build installé (`ameesh 1.11.1`) ne connaît pas
  `full_suite` (`'full_suite' in radar.PROJECT_KEYS` → `False`) — la porte
  restante est le déploiement du build qui porte #756.
* **Aucune écriture** : pas de configuration modifiée, pas de run CI créé,
  pas de PR ouverte.
