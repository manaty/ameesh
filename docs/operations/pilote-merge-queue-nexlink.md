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

À ajouter sous `radar.projects` du **profil dédié** (§3), **sans
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

**Validation** — lire le **profil dédié retenu** (§3), celui que l'unité
désignée lira après la recharge — pas le `critiques-2.json` partagé par trois
unités — avec l'interpréteur de l'exécuteur, pas `python3` du système
(l'installation vit dans un venv). `AMEESH_CONFIG` est résolu en **chemin
absolu depuis le répertoire courant** du processus : le renseigner en absolu,
ou se placer dans le répertoire de l'unité.

```bash
CFG="${AMEESH_CONFIG:?chemin absolu du profil dédié (§3)}"   # refus si non renseigné
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
exécuteurs pour la même file (courrier radar en double) — le même raisonnement
vaut pour deux unités du même hôte, d'où le profil dédié ci-dessous.

Deux choix reviennent au bureau avant l'application :

* **un profil dédié, une seule unité** — `runner-profiles/critiques-2.json`
  n'est pas le profil d'**une** unité mais de **trois** sur `pc-smichea`
  (`mesh-design`, auditeur, orchestrateur ; drop-ins vérifiés par
  `mesh-design` le 13/10 23:21) : y ajouter `radar.projects.nexlink` armerait
  trois passes radar à leurs prochaines reprises, donc trois exécuteurs pour la
  même file. Créer un **profil dédié** — copie du profil de comptes approprié,
  section `accounts` **inchangée**, plus la seule carte §2 — et le faire lire
  par **une seule unité** (drop-in `Environment=AMEESH_CONFIG=<chemin absolu>`
  ou unité dédiée). Les trois unités existantes gardent `critiques-2.json`,
  sans clé `radar` ;
* **la méthode de recharge** — l'exécuteur lit la configuration à son
  démarrage : redémarrer **la seule unité désignée** (`systemctl --user restart
  ameesh-runner-agent@<agent>.service`), ou attendre son prochain redémarrage.
  Avant recharge, vérifier qu'une seule unité référence le profil dédié et que
  les autres n'ont pas la clé `radar` (§4, étape 4).

### 3bis. Proposition dev3 (14/10 06:05Z) — pièces exactes, à valider

Préparée pour la validation du bureau demandée le 14/10 05:57Z (courrier
bureau → dev3), avant le créneau proposé du 14/10 08:30 CEST. Aucun geste
appliqué : rien n'existe sur `pc-smichea` de ce qui suit.

| pièce | valeur |
|---|---|
| unité existante désignée | `ameesh-runner-agent@orchestrateur.service` |
| profil source (comptes) | `/home/smichea/runner-profiles/critiques-2.json` |
| profil dédié | `/home/smichea/runner-profiles/radar-nexlink.json` |
| drop-in | `~/.config/systemd/user/ameesh-runner-agent@orchestrateur.service.d/10-radar-nexlink.conf` |
| rechargement | `systemctl --user daemon-reload && systemctl --user restart ameesh-runner-agent@orchestrateur.service` |

Pourquoi l'orchestrateur : c'est le rôle qui tient la file et le jour de
pilote (§5) ; l'auditeur est en lecture seule et `mesh-design` ne lance ni
l'application ni le jour de pilote (§7.4). Le choix reste au bureau : les trois
unités lisent le même `critiques-2.json`, changer d'unité ne change que le nom
d'instance du drop-in.

**Diff exact** (aucune autre modification ; le profil partagé
`critiques-2.json`, la section `accounts` et les deux autres unités ne sont pas
touchés) :

1. `cp -a "$SRC" "$CFG"` puis sauvegarde `cp -a "$CFG" "$CFG.bak-<date>"` ;
2. insertion du seul bloc `radar.projects.nexlink` de §2 dans `$CFG` ;
3. création du drop-in, contenu exact :

   ```ini
   [Service]
   Environment=AMEESH_CONFIG=/home/smichea/runner-profiles/radar-nexlink.json
   ```

**Recharge** : §4 étapes 3 à 5 (validation du schéma, contrôle d'unicité
`systemctl --user show -p Environment` + `systemctl --user cat`, contrôle à
blanc). **Retour** : §6 (retirer le drop-in, `rm` du profil dédié, recharger
cette seule unité). Un seul effet de bord : le redémarrage de l'unité coupe le
runner de l'orchestrateur le temps de la reprise — c'est le geste du bureau
(§7.4).

L'instance exacte du drop-in se contrôle sur l'hôte avant de l'écrire :
`systemctl --user list-units 'ameesh-runner-agent@*'` et le bail du registre
donnent le nom d'instance (`runner-orchestrateur@laptop` → `orchestrateur`) ;
depuis `ameesh-agents-1` je n'ai ni DNS ni accès à `pc-smichea`, la
correspondance n'est donc pas re-vérifiée ici.

Hôte et clone, confirmés de première main le 13/10 :

* `pc-smichea` : ameesh **1.10.0**, Node **26.8.1**, `gh` authentifié
  (`mesh-design`, 23:15) ;
* clone `/home/smichea/development/manaty/nexlink`, **non-bare**, `origin` =
  `git@github.com:manaty/nexlink.git` (`git rev-parse` et lecture de l'hôte —
  orchestrateur 23:11, auditeur 23:19) ;
* `radar.projects.nexlink` **absent de la configuration chargée** sur cet hôte
  (auditeur, 23:19) — cohérent avec `reports=[]` et la file vide.

Le schéma et l'existence du dépôt sont deux contrôles distincts :
`radar.parse_settings` valide le premier, `git -C <clone> rev-parse
--show-toplevel` (et `--is-bare-repository` → `false`) le second.

## 4. Runbook d'application (sur décision du bureau)

**Profil dédié** (§3, §3bis) — pas le `critiques-2.json` partagé. `SRC` est le
profil de comptes d'origine (sa section `accounts` est reprise telle quelle),
`CFG` le nouveau fichier dédié, `UNIT` l'unité existante désignée :

```bash
SRC=/home/smichea/runner-profiles/critiques-2.json  # profil de comptes d'origine
CFG=/home/smichea/runner-profiles/radar-nexlink.json # nouveau profil dédié
UNIT=ameesh-runner-agent@orchestrateur.service      # unité existante désignée (§3bis)
DROPIN=~/.config/systemd/user/$UNIT.d/10-radar-nexlink.conf
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
   git -C "$REPO" remote get-url origin          # attendu : git@github.com:manaty/nexlink.git
   git -C "$REPO" fetch --prune origin           # prouve l'accès du compte au dépôt
   "$AMEESH_PY" -c "from ameesh import radar; print('land' in radar.PROJECT_KEYS, 'generated' in radar.PROJECT_KEYS)"
   # attendu : True True
   ```

1. **Créer le profil dédié et le sauvegarder** — copie du profil de comptes
   (section `accounts` inchangée) ; le profil partagé `critiques-2.json` n'est
   **pas** modifié :

   ```bash
   cp -a "$SRC" "$CFG"                          # accounts reprise telle quelle
   cp -a "$CFG" "$CFG.bak-$(date +%F-%H%M)"
   ```

2. **Ajouter** le bloc §2 sous `radar.projects` du profil dédié `$CFG` (éditer,
   ne rien écraser ; JSON sans commentaire ni virgule finale) ; le profil
   partagé reste intact.

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

4. **Faire pointer une seule unité sur le profil dédié, puis la recharger** —
   drop-in `Environment=AMEESH_CONFIG=<chemin absolu de $CFG>` sur l'unité
   désignée (`$DROPIN`, contenu exact en §3bis) ; les autres unités restent sur
   `critiques-2.json`. Contrôler `systemctl --user show -p Environment $UNIT`
   et `systemctl --user cat $UNIT` (drop-ins) : une seule unité référence
   `$CFG`, aucune autre n'a la clé `radar`. Puis
   `systemctl --user daemon-reload && systemctl --user restart $UNIT` ;
   l'exécuteur ne relit pas la configuration à chaud.

5. **Contrôle à blanc** (lecture seule) :

   ```bash
   AMEESH_CONFIG="$CFG" ameesh conflicts --project nexlink   # état de départ : reports=[]
   AMEESH_CONFIG="$CFG" ameesh work queue show               # état de départ : file vide
   ```

   Après une première passe, le radar signale les conflits **courants** entre
   lots Nexlink (courrier passif aux assignés) : c'est attendu et sans effet de
   bord.

6. **Tracer** dans #183 : profil dédié créé, unité désignée, hôte, date,
   sauvegarde, sortie des contrôles, et le nom de qui a appliqué.

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
* **Configuration** : restaurer la sauvegarde de l'étape 1 (ou supprimer le
  profil dédié et retirer le drop-in `AMEESH_CONFIG` de `$UNIT`, qui reprend
  `critiques-2.json`), puis `systemctl --user daemon-reload && systemctl --user
  restart $UNIT` — **cette seule unité**.

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

5. **Profil dédié, unité et recharge** — un **profil dédié** (copie du profil
   de comptes, `accounts` inchangée) lu par **une seule unité** de
   `pc-smichea` ; `critiques-2.json` reste partagé par les trois unités, sans
   clé `radar` ; la recharge est le redémarrage de cette seule unité (§3, §4).
   `mesh-design` (13/10 23:21) confirme : ne pas ajouter `radar` au profil
   partagé, sinon les trois unités s'arment à leurs reprises. **Proposition
   dev3 (14/10 06:05Z, §3bis)** : unité existante
   `ameesh-runner-agent@orchestrateur.service`, profil
   `/home/smichea/runner-profiles/radar-nexlink.json` dérivé de
   `/home/smichea/runner-profiles/critiques-2.json`, drop-in
   `10-radar-nexlink.conf`, recharge `daemon-reload` + `restart` de cette
   unité ; à valider par le bureau avant le créneau.
6. **Créneau du 14/10** — après REL-11 ; la **liste initiale des PR** est
   fixée au démarrage par l'orchestrateur Nexlink, hors sensibles `915`, `695`
   et `943` en vol.
7. **Portes** — les deux portes locales suffisent-elles pour le pilote, ou
   faut-il des portes par chemin (`scripts/__tests__/*.test.mjs`) dès le
   premier jour ?
