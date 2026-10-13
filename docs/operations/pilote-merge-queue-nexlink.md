# Pilote radar + file de fusion — Nexlink : carte et runbook (lot #183)

Carte préparée le 2026-10-13 sur la branche
`review/183-pilote-radar-queue-nexlink`, **sans PR ni CI** (seule une poussée
vers `main`/`release/**`, ou une PR, déclenche la suite). **Aucun geste de
production n'a été fait pour l'écrire** : rien n'est déclaré sur un hôte, rien
n'est mis en file.

Objet : préparer le jour de pilote **L160f** ([#183](https://github.com/manaty/ameesh/issues/183)) —
Nexlink confie l'ordre et l'exécution de ses fusions vers `develop` à ameesh
(décision #11882 de l'owner, acceptation de l'orchestrateur Nexlink du 12/10
sous conditions). Le pilote est proposé le **14/10**, après la fenêtre REL-11.

## 1. Périmètre du pilote

Ce que le pilote exerce, et rien d'autre :

* l'agent **gèle** son lot sur un commit
  (`ameesh work milestone <lot> frozen --sha <sha>`), obtient le **verdict de
  revue** sur ce commit (`… verdict --verdict ok --sha <sha>`), puis **entre
  dans la file** (`ameesh work queue add <lot>`) au lieu de demander un feu de
  fusion ;
* le **radar** range la file (priorité, puis les lots sans conflit, puis ceux
  dont les conflits sont préparés, puis l'arrivée) et la **rejoue empilée**
  dans son clone privé ; un lot dont la branche a bougé après le gel, ou dont
  un conflit n'est couvert par aucune résolution, est **éjecté** et son agent
  prévenu ;
* avec `land: pr`, ameesh **fusionne la tête de file** : fusion de la cible
  dans son clone, régénération des fichiers générés, portes sur l'arbre
  fusionné, poussée du commit de fusion sur la branche du lot, puis fusion de
  la PR (`gh pr merge --match-head-commit`) dès que sa CI est verte ;
* **hors file** : `main`, les tags, les releases et les déploiements restent
  des gestes d'orchestrateur (les motifs `protected` de §2 le refusent même :
  la file n'accepte qu'une cible non protégée, `develop` par défaut).

Condition 1 de l'orchestrateur Nexlink (« une release qui porte L160e,
déployée sur l'hôte ») : ameesh **1.10.0** porte la file (L160e) et le
atterrissage (L160e2) ; vérifié sur `ameesh-agents-1` (`ameesh --version` →
`ameesh 1.10.0`, `radar.PROJECT_KEYS` contient `land`), puis confirmé sur
`pc-smichea` (1.10.0, `mesh-design` 13/10 23:15).

## 2. Carte — l'entrée `nexlink` de `cfg.radar`

À ajouter sous `radar.projects` du profil désigné (§3), **sans
écraser les autres projets** ni les autres clés de `nexlink` (`full_suite`
viendra de [#916](https://github.com/manaty/ameesh/issues/916), après #756) :

```json
"nexlink": {
  "repo": "~/development/manaty/nexlink",
  "github": "manaty/nexlink",
  "target": "develop",
  "migrations": "supabase/migrations",
  "protected": ["main", "desktop-v*", "nexlink-releases*", "refs/tags/*", "release/*"],
  "land": "pr",
  "gates": [
    "node scripts/domains-export.mjs --check",
    "node scripts/check-rollbacks.mjs"
  ],
  "generated": [
    {"path": "backend/domains.json",
     "regen": "node scripts/domains-export.mjs",
     "check": "node scripts/domains-export.mjs --check"}
  ]
}
```

Pourquoi chaque clé (au regard des conditions de l'orchestrateur) :

* `target: develop` — la file et le relevé des branches visent `develop` ;
  une cible introuvable est une erreur du relevé, visible dans
  `ameesh work sync-branches` ;
* `protected` — les motifs de **cible protégée** (L160g) : la file refuse le
  lot dont la cible résolue est `main`, une branche de release ou un tag.
  `protected_target` (radar) confronte chaque motif à la cible **telle
  qu'écrite**, à sa branche (`origin/develop` → `develop`) et à sa réf
  complète (`refs/heads/…`, `refs/tags/…`) : il faut donc couvrir les deux
  formes, courte et qualifiée.

  | motif | couvre |
  |---|---|
  | `main` | `main`, `origin/main`, `refs/heads/main` |
  | `desktop-v*` | `desktop-v1.2.3` (tag en forme courte) |
  | `nexlink-releases*` | `nexlink-releases-0.2.11`, `origin/nexlink-releases-0.2.11` |
  | `refs/tags/*` | tout tag qualifié (`refs/tags/desktop-v1.2.3`, `refs/tags/v1.0.0`) |
  | `release/*` | `release/1.10`, `origin/release/1.10`, `refs/heads/release/1.10` |

  Mesuré le 13/10 (paquet 1.10.0) : `origin/main` → `main`,
  `refs/tags/desktop-v1.2.3` → `refs/tags/*`,
  `origin/nexlink-releases-0.2.11` → `nexlink-releases*`,
  `origin/release/1.10` → `release/*`, et **`develop`/`origin/develop` →
  `None`** (non protégés). La cible effective d'un lot est le `branch_target`
  du lot, sinon le `target` du projet (merge_queue) : `develop` n'est donc pas
  une liste blanche, et un lot qui vise `main` est refusé à l'entrée en file ;
* `migrations: supabase/migrations` — active la sonde des numéros : un
  fichier `NNN_*.sql` ajouté par un lot sans réservation ameesh est signalé
  (condition 2 ; depuis #701, les réservations vivent dans ameesh,
  `ameesh work migration`, L122 — plus de `RESERVATIONS.md`) ;
* `generated: backend/domains.json` — le fichier produit par
  `scripts/domains-export.mjs` est **régénéré à chaque fusion**, jamais pris
  d'une fusion ni de `rerere`, puis vérifié : une résolution `rerere` d'un
  fichier généré serait fausse en silence (condition 3). Vérifié le 13/10 sur
  l'instantané de `develop` `c3b51be63` : la régénération sans `--check`
  réécrit `backend/domains.json` **à l'identique**, puis `--check` passe ;
* `gates` — deux portes **locales courtes**, exécutées sur l'arbre fusionné
  (conditions 4, repli « portes locales ») : `domains-export --check`
  (backend/domains.json en phase) et `check-rollbacks.mjs` (chaque migration
  numérotée a son document de retour). Pur Node, aucun accès base, aucun
  `npm install` ; mesurées le 13/10 sur le même instantané : 0,09 s
  (324 tables / 55 modules) et 0,05 s (385 migrations / 241 rollbacks), les
  deux vertes. La CI GitHub reste le **préalable d'entrée en file** (une PR
  dont la CI n'est pas verte sur la tête gelée est refusée par
  `ameesh work queue add`) ;
* `land: "pr"` — ameesh fusionne lui-même la tête de file par l'API, méthode
  `merge` par défaut (la pratique Nexlink : `--merge --match-head-commit`).
  Risque résiduel documenté (fenêtre entre la dernière lecture de la PR et
  `gh pr merge`) ; c'est la décision de l'owner #11882. Le repli `land: "off"`
  (la file ordonne, l'orchestrateur fusionne le rang 1) est immédiat, sans
  redéploiement.

**Validation** — lire le **profil réellement désigné** par l'unité (sur
`pc-smichea`, l'unité de `mesh-design` lit `runner-profiles/critiques-2.json`,
pas `config.json`), avec l'interpréteur de l'exécuteur, pas `python3` du
système (l'installation vit dans un venv). `AMEESH_CONFIG` est résolu en
**chemin absolu depuis le répertoire courant** du processus : le renseigner en
absolu, ou se placer dans le répertoire de l'unité.

```bash
CFG="${AMEESH_CONFIG:-$HOME/.config/ameesh/config.json}"   # profil désigné par l'unité
REPO=~/development/manaty/nexlink
AMEESH_PY="$(dirname "$(readlink -f "$(command -v ameesh)")")/python3"
"$AMEESH_PY" - "$CFG" <<'PY'
import json, os, sys
from ameesh import radar
raw = json.load(open(os.path.expanduser(sys.argv[1])))
s = radar.parse_settings(raw.get("radar") or {})
p = s.projects["nexlink"]
print(p.repo, "|", p.target, "|", p.github, "|", p.migrations, "|", p.protected,
      "|", p.land, p.merge_method)
print("gates:", list(p.gates))
print("generated:", [(g.path, g.regen, g.check) for g in p.generated])
for ref in ("origin/main", "origin/develop", "refs/tags/desktop-v1.2.3",
            "origin/nexlink-releases-0.2.11", "origin/release/1.10"):
    print("protected %-34s -> %s" % (ref, radar.protected_target(p, ref)))
PY
git -C "$REPO" rev-parse --show-toplevel      # le parseur ne vérifie pas le dépôt
```

Résultat obtenu le 2026-10-13 avec le paquet **1.10.0 installé**
(`…/venv/lib/python3.12/site-packages/ameesh/`, sans `sys.path`) ; le chemin
affiché est l'expansion de `~` **sur la machine de contrôle**
(`ameesh-agents-1`) — sur `pc-smichea` il donnera `/home/smichea/…` :

```
/home/ameesh/development/manaty/nexlink | develop | manaty/nexlink | supabase/migrations | ('main', 'desktop-v*', 'nexlink-releases*', 'refs/tags/*', 'release/*') | pr merge
gates: ['node scripts/domains-export.mjs --check', 'node scripts/check-rollbacks.mjs']
generated: [('backend/domains.json', 'node scripts/domains-export.mjs', 'node scripts/domains-export.mjs --check')]
protected origin/main                        -> main
protected origin/develop                     -> None
protected refs/tags/desktop-v1.2.3           -> refs/tags/*
protected origin/nexlink-releases-0.2.11     -> nexlink-releases*
protected origin/release/1.10                -> release/*
```

`radar.parse_settings` valide le **schéma** de la carte (une clé inconnue, un
`land` sans portes ou un `protected` qui n'est pas une liste de chaînes
lèvent), **pas l'existence du dépôt** : un `repo` inexistant passe le parseur.
`git -C "$REPO" rev-parse --show-toplevel` et `--is-bare-repository` (attendu
`false`) le vérifient séparément ; valider les deux avant d'écrire.

## 3. Hôte, profil et clone

La clé `radar` désigne l'hôte qui fait tourner le radar **et** la file (une
passe après chaque relevé de branches, ~300 s). **Rien n'est actif pour nexlink
aujourd'hui** : relevé par `mesh-design` le 13/10 23:15, la clé `radar` est
absente des trois profils locaux de `pc-smichea` (`config.json`,
`runner-profiles/critiques-2.json`, `runner-profiles/relecteurs-codex1.json`) ;
`ameesh conflicts --project nexlink` rend `reports=[]` et la file `develop` est
vide. Ne pas la déclarer sur deux hôtes : deux passes lanceraient deux
exécuteurs pour la même file (courrier radar en double).

Deux choix reviennent au bureau avant l'application :

* **un seul exécuteur, un seul profil** — le fichier de configuration
  réellement lu par l'unité qui portera le radar. L'unité de `mesh-design` lit
  `runner-profiles/critiques-2.json`, pas `config.json` ; les autres profils et
  les comptes restent en place, un seul fichier est modifié ;
* **la méthode de recharge** — l'exécuteur lit la configuration à son
  démarrage : redémarrer l'unité (`systemctl --user restart
  ameesh-runner-agent@<agent>.service`), ou attendre son prochain redémarrage.

Hôte et clone, confirmés de première main le 13/10 :

* `pc-smichea` : ameesh **1.10.0**, Node **26.8.1**, `gh` authentifié
  (`mesh-design`, 23:15) ;
* clone `/home/smichea/development/manaty/nexlink`, **non-bare**
  (`git rev-parse`, orchestrateur, 23:11).

Le schéma et l'existence du dépôt sont deux contrôles distincts :
`radar.parse_settings` valide le premier, `git -C <clone> rev-parse
--show-toplevel` (et `--is-bare-repository` → `false`) le second.

## 4. Runbook d'application (sur décision du bureau)

Profil désigné par l'unité — sur `pc-smichea`, le fichier retenu par le bureau
(§3), pas forcément `config.json` :

```bash
CFG="${AMEESH_CONFIG:-$HOME/.config/ameesh/config.json}"   # profil réellement lu par l'unité
REPO=~/development/manaty/nexlink
AMEESH_PY="$(dirname "$(readlink -f "$(command -v ameesh)")")/python3"
```

0. **Vérifier l'hôte** — version, outillage et clone (le parseur ne vérifie ni
   l'un ni l'autre) :

   ```bash
   ameesh --version                              # attendu : 1.10.0
   node --version                                # attendu : 26.8.1
   gh auth status                                # lecture CI + fusion des PR manaty/nexlink
   git -C "$REPO" rev-parse --show-toplevel      # attendu : /home/smichea/development/manaty/nexlink
   git -C "$REPO" rev-parse --is-bare-repository # attendu : false
   "$AMEESH_PY" -c "from ameesh import radar; print('land' in radar.PROJECT_KEYS, 'generated' in radar.PROJECT_KEYS)"
   # attendu : True True
   ```

1. **Sauvegarder le profil désigné** (lui seul) :

   ```bash
   cp -a "$CFG" "$CFG.bak-$(date +%F-%H%M)"
   ```

2. **Fusionner** le bloc §2 dans `radar.projects` (éditer, ne rien écraser ;
   JSON sans commentaire ni virgule finale).

3. **Valider** le schéma du profil entier (lève ici si la carte est fausse ;
   même interpréteur qu'à l'étape 0), **puis** vérifier le dépôt par git :

   ```bash
   "$AMEESH_PY" - "$CFG" <<'PY'
   import json, os, sys
   from ameesh import radar
   raw = json.load(open(os.path.expanduser(sys.argv[1])))
   s = radar.parse_settings(raw.get("radar") or {})
   for name, p in sorted(s.projects.items()):
       print(name, p.repo, p.target, p.land, len(p.gates), len(p.generated))
   PY
   git -C "$REPO" rev-parse --show-toplevel   # parse_settings ne vérifie pas le dépôt
   ```

4. **Recharger** : redémarrer l'unité qui porte le profil désigné
   (`systemctl --user restart ameesh-runner-agent@<agent>.service`), ou
   attendre son prochain redémarrage ; l'exécuteur ne relit pas la
   configuration à chaud.

5. **Contrôle à blanc** (lecture seule) :

   ```bash
   AMEESH_CONFIG="$CFG" ameesh conflicts --project nexlink   # état de départ : reports=[]
   AMEESH_CONFIG="$CFG" ameesh work queue show               # état de départ : file vide
   ```

   Après une première passe, le radar signale les conflits **courants** entre
   lots Nexlink (courrier passif aux assignés) : c'est attendu et sans effet de
   bord.

6. **Tracer** dans #183 : profil modifié, hôte, date, sauvegarde, sortie des
   contrôles, et le nom de qui a appliqué.

## 5. Jour de pilote — déroulé

| Qui | Geste |
|---|---|
| auteur du lot | tests ciblés verts, `work milestone <lot> frozen --sha <sha>` |
| relecteur | `work milestone <lot> verdict --verdict ok --sha <sha>` (classes sensibles) |
| auteur du lot | CI verte sur la tête gelée, puis `ameesh work queue add <lot>` (avis Nexlink : l'auteur entre après son verdict, sur feu pilote explicite) |
| ameesh (radar) | range la file, rejoue empilé, éjecte ce qui ne passe pas, prévient |
| ameesh (`land: pr`) | fusionne le rang 1 : cible + résolutions, régénère, portes, pousse sur la branche du lot, puis `gh pr merge --match-head-commit` au vert de la CI |
| orchestrateur Nexlink | `ameesh work queue show` (rangs et raisons), exceptions ; `main`/tags/releases/déploiements restent ses gestes |

Surveillance : `ameesh work show <lot>` (« file : queued vers develop, rang n »),
`ameesh conflicts`, l'alerte `merge_blocked` (une tête de file qui n'avance
plus depuis 30 min), le fil du lot et le courrier. Un lot en file **ne reçoit
plus de commit** : pour repousser, `ameesh work queue remove <lot>` d'abord.

## 6. Retour arrière

* **File** : `ameesh work queue remove <lot> [-m "pourquoi"]` rend le lot à
  son agent (il peut repousser, puis re-entrer).
* **Atterrissage** : repasser `land` à `"off"` dans la configuration (ou
  retirer l'entrée `nexlink`), recharger l'exécuteur : la file continue
  d'ordonner, l'orchestrateur fusionne le rang 1 à la main.
* **Configuration** : restaurer la sauvegarde de l'étape 1 et recharger.

## 7. Décisions et points ouverts

Tranché le 13/10 :

1. **Hôte et chemin du clone** — `pc-smichea` et
   `/home/smichea/development/manaty/nexlink` (non-bare), confirmés de première
   main (orchestrateur 23:11, `mesh-design` 23:15).
2. **`land: "pr"` d'emblée** — avis Nexlink maintenu : `land pr`, `develop`
   seulement, premiers lots légers ; le repli `land: "off"` reste immédiat.
3. **Qui entre en file** — l'**auteur du lot après son verdict**, sur feu
   pilote explicite (avis Nexlink).
4. **Qui applique** — le **bureau** tient l'application et la configuration ;
   `mesh-design` ne lance ni l'application ni le jour de pilote. La
   préparation ne vaut pas lancement.

À confirmer par le bureau avant démarrage :

5. **Profil et recharge** — un seul exécuteur et un seul profil parmi ceux de
   `pc-smichea`, et la méthode de recharge (§3).
6. **Créneau du 14/10** — après REL-11 ; la **liste initiale des PR** est
   fixée au démarrage par l'orchestrateur Nexlink, hors sensibles `915`, `695`
   et `943` en vol.
7. **Portes** — les deux portes locales suffisent-elles pour le pilote, ou
   faut-il des portes par chemin (`scripts/__tests__/*.test.mjs`) dès le
   premier jour ?
