# Pilote radar + file de fusion — Nexlink : carte et runbook (lot #183)

Carte préparée le 2026-10-13 sur la branche
`review/183-pilote-radar-queue-nexlink`, **sans PR ni CI** (seule une poussée
vers `main`/`release/**`, ou une PR, déclenche la suite). **Aucun geste de
production n'a été fait pour l'écrire** : rien n'est déclaré sur un hôte, rien
n'est mis en file.

Version corrigée le 2026-10-14 06:50Z : socle réel des profils sur `pc-smichea`
(`~/.config/ameesh/runner-profiles/`) et drop-in porteur trié **après**
`zz-compte-owner-1013.conf`, après relevé du bureau sur PC (14/10 06:47Z).

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
(l'installation vit dans un venv) : il se lit dans le **shebang du lanceur**,
jamais en supposant `<répertoire du lanceur>/python3` (bloc `AMEESH_PY`
ci-dessous). `AMEESH_CONFIG` est résolu en **chemin
absolu depuis le répertoire courant** du processus : le renseigner en absolu,
ou se placer dans le répertoire de l'unité.

```bash
CFG="${AMEESH_CONFIG:?chemin absolu du profil dédié (§3)}"   # refus si non renseigné
REPO=~/development/manaty/nexlink
AMEESH_BIN="$(readlink -f "$(command -v ameesh)")"
AMEESH_PY="$(sed -n '1s/^#! *//p' "$AMEESH_BIN")"   # interpréteur réel : shebang du lanceur
case "$AMEESH_PY" in */env\ *) AMEESH_PY="$(command -v "${AMEESH_PY##*/env }")";; esac
test -x "$AMEESH_PY" || { echo "interpréteur du lanceur illisible : $AMEESH_PY" >&2; exit 1; }
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

`AMEESH_PY` est l'**interpréteur réel du lanceur**, lu dans son shebang et
vérifié exécutable. Ne pas le déduire du répertoire du lanceur
(`dirname(readlink -f "$(command -v ameesh)")/python3`) : le raccourci est vrai
sur `ameesh-agents-1`, où `~/.local/bin/ameesh` est un lien vers le venv, mais
faux sur `pc-smichea`, où le lanceur est un script de `~/.local/bin` dont le
shebang est `/home/smichea/.local/share/ameesh/venv/bin/python3` — le répertoire
ne contient alors aucun `python3` (relevé orchestrateur, 14/10 07:44Z).

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
absente des trois profils locaux de `pc-smichea`
(`/home/smichea/.config/ameesh/config.json`,
`/home/smichea/.config/ameesh/runner-profiles/critiques-2.json`,
`/home/smichea/.config/ameesh/runner-profiles/relecteurs-codex1.json` ; chemins
réels relevés sur PC par le bureau le 14/10 06:47Z — le socle court
`/home/smichea/runner-profiles/` employé dans la première version de ce
document **n'existe pas**) ;
`ameesh conflicts --project nexlink` rend `reports=[]` et la file `develop` est
vide. Ne pas la déclarer sur deux hôtes : deux passes lanceraient deux
exécuteurs pour la même file (courrier radar en double) — le même raisonnement
vaut pour deux unités du même hôte, d'où le profil dédié ci-dessous.

Deux choix reviennent au bureau avant l'application :

* **un profil dédié, une seule unité** —
  `/home/smichea/.config/ameesh/runner-profiles/critiques-2.json` n'est pas le
  profil d'**une** unité mais de **trois** sur `pc-smichea` (`mesh-design`,
  auditeur, orchestrateur ; drop-ins vérifiés par `mesh-design` le 13/10 23:21) :
  y ajouter `radar.projects.nexlink` armerait
  trois passes radar à leurs prochaines reprises, donc trois exécuteurs pour la
  même file. Créer un **profil dédié** — copie du profil de comptes approprié,
  section `accounts` **inchangée**, plus la seule carte §2 — et le faire lire
  par **une seule unité** (drop-in `Environment=AMEESH_CONFIG=<chemin absolu>`
  **trié après** `zz-compte-owner-1013.conf`, §3bis — ou unité dédiée). Les
  trois unités existantes gardent `critiques-2.json`, sans clé `radar` ;
* **la méthode de recharge** — l'exécuteur lit la configuration à son
  démarrage : redémarrer **la seule unité désignée** (`systemctl --user restart
  ameesh-runner-agent@<agent>.service`), ou attendre son prochain redémarrage.
  Avant recharge, vérifier qu'une seule unité référence le profil dédié et que
  les autres n'ont pas la clé `radar` (§4, étape 4).

### 3bis. Proposition dev3 corrigée (14/10 06:50Z) — pièces exactes, à valider

Première version à 06:05Z, **corrigée** après le relevé du bureau sur PC
(14/10 06:47Z) : le socle proposé `/home/smichea/runner-profiles/` n'existe
pas, et un drop-in `10-radar-nexlink.conf` est trié **avant**
`zz-compte-owner-1013.conf`, donc écrasé par lui. La version corrigée ci-dessous
n'est **pas appliquée** : le bureau n'a reconfiguré aucune unité et n'a activé
aucun créneau.

| pièce | valeur |
|---|---|
| unité existante désignée | `ameesh-runner-agent@orchestrateur.service` |
| profil de comptes (source) | `/home/smichea/.config/ameesh/runner-profiles/critiques-2.json` (chemin effectif relevé par le bureau ; le littéral du drop-in peut être relatif, `config.load` le résout par `os.path.abspath`, `src/ameesh/config.py:_expand`) |
| profil dédié | `/home/smichea/.config/ameesh/runner-profiles/radar-nexlink.json` |
| drop-in porteur | `zzz-radar-nexlink.conf`, dans le **même répertoire** que `zz-compte-owner-1013.conf` (relevé par `systemctl --user show -p DropInPaths <unité>`, §4 étape 0) |
| contenu du drop-in | `[Service]` puis `Environment=AMEESH_CONFIG=/home/smichea/.config/ameesh/runner-profiles/radar-nexlink.json` |
| rechargement | `systemctl --user daemon-reload && systemctl --user restart ameesh-runner-agent@orchestrateur.service` |

**Pourquoi post-`zz`** : systemd lit les drop-ins d'une unité par ordre
alphabétique et, pour une même variable, la **dernière** affectation
`Environment=` l'emporte. `zz-compte-owner-1013.conf` fixe `AMEESH_CONFIG` au
profil partagé ; un `10-radar-nexlink.conf` serait lu avant lui et perdrait.
`zzz-` est trié strictement après `zz-`, dans le même répertoire. Le drop-in du
propriétaire n'est **ni modifié ni retiré** (consigne bureau du 14/10 06:47Z) :
ses réglages de comptes restent en vigueur. Si le répertoire de
`zz-compte-owner-1013.conf` n'est pas inscriptible (drop-in système), **ne rien
écrire** : la stratégie revient au bureau, explicitement revue.

**Pourquoi un chemin absolu dans le drop-in** : un `AMEESH_CONFIG` relatif est
résolu depuis le répertoire courant du processus, qui n'est pas le répertoire
des profils ; l'absolu lève l'ambiguïté entre le littéral du drop-in et le
chemin effectif relevé par le bureau.

Pourquoi l'orchestrateur : c'est le rôle qui tient la file et le jour de
pilote (§5) ; l'auditeur est en lecture seule et `mesh-design` ne lance ni
l'application ni le jour de pilote (§7.4). Le choix reste au bureau : les trois
unités lisent le même `critiques-2.json`, changer d'unité ne change que le nom
d'instance du drop-in.

**Diff exact** (aucune autre modification ; profil partagé
`critiques-2.json`, section `accounts` et deux autres unités non touchés) :

1. `cp -a "$SRC" "$CFG"` puis sauvegarde `cp -a "$CFG" "$CFG.bak-<date>"` ;
2. insertion du seul bloc `radar.projects.nexlink` de §2 dans `$CFG` ;
3. création de `$DROPIN` (`zzz-radar-nexlink.conf`), contenu exact :

   ```ini
   [Service]
   Environment=AMEESH_CONFIG=/home/smichea/.config/ameesh/runner-profiles/radar-nexlink.json
   ```

**Recharge et preuves** : §4 étapes 0, 3, 4 et 5 — état de départ,
`systemctl --user show -p Environment -p DropInPaths` (le nôtre en dernier),
environnement **réel** du processus (`/proc/<MainPID>/environ`), comptes
inchangés, contrôle à blanc. **Retour** : §6 — retirer **ce seul** drop-in et
le profil dédié ; `zz-compte-owner-1013.conf` reste intact. Un seul effet de
bord : le redémarrage de l'unité coupe le runner de l'orchestrateur le temps de
la reprise — c'est le geste du bureau (§7.4).

L'instance exacte de l'unité se contrôle sur l'hôte avant d'écrire :
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

### 3ter. Qualification bureau (14/10 07:33Z) — profil créé, schéma validé

Le bureau a exécuté la préparation du profil dédié sur `pc-smichea` et l'a
qualifiée (courrier bureau → dev3, 14/10 07:33Z ; preuve
`outils/183-profile-preparation-20261014.json`) :

| point | état constaté |
|---|---|
| profil dédié | `/home/smichea/.config/ameesh/runner-profiles/radar-nexlink.json`, mode 0600, créé depuis le socle réel |
| empreinte | SHA256 `b3b1c32ee4df9bd8933e9a9f37874e455cf0f17c63b1e0e91e1ff7be29357d7e` |
| `accounts` et non-`radar` | byte-sémantiquement identiques au socle (relevé bureau) |
| schéma | `radar.parse_settings` du venv exécuteur valide : repo `/home/smichea/development/manaty/nexlink`, cible `develop`, `land: "pr"` ; `main`/release/tags protégés, `develop` libre |
| drop-in / activation / restart | **aucun** : l'`AMEESH_CONFIG` de l'unité pointe encore le profil partagé |
| réserves | chemin du socle et ordre du drop-in (`zzz-` après `zz-`) **levées** |

**Reste à faire, et par qui** : écrire le seul drop-in `zzz-radar-nexlink.conf`,
`daemon-reload` puis `restart` de l'unique unité désignée (§3bis, §4), **dans un
créneau coordonné avec les déploiements `#1037`/`#1236`** (l'ancien code de porte
de famille est encore en place), ou qualifier le repli documentaire
`land: "off"` — décision orchestrateur/pilote, application bureau. Le **diff
complet est à revoir avant tout nouveau créneau** (exigence bureau) ; la
validation de schéma **n'est pas un feu** et n'autorise aucune activation par
elle-même.

### 3quater. Repli documentaire `land: "off"` — qualifié sur le code installé (demande orchestrateur 14/10 07:38Z)

L'orchestrateur Nexlink a demandé de **qualifier le repli documentaire
`land: "off"`** : schéma exact du code installé, garantie d'absence de fusion
automatique, comportement radar/file, retour à `land: "pr"` après les
déploiements `#1037`/`#1236`. **Préparation seulement** : aucun profil actif,
aucun drop-in, aucun `restart` ; le créneau `land: "pr"` reste à coordonner par
le bureau avec `#1037`/`#1236`.

**Schéma exact de la clé `land`** (lu dans `src/ameesh/radar.py`) :

| point | valeur |
|---|---|
| emplacement | `radar.projects.<projet>.land`, **par projet** (aucune clé globale `land`) |
| valeurs admises | `off` \| `local` \| `pr` (`LAND_MODES`, radar.py:71) |
| défaut | `off` si la clé est absente **ou vide** (radar.py:179, `value.get("land") or "off"`) |
| valeur invalide | `SettingsError` au chargement — `…land: off \| local \| pr expected` (radar.py:180-181), jamais un repli silencieux |
| clés voisines | toute clé hors `PROJECT_KEYS` (radar.py:61) est refusée (radar.py:162-165) |

La validation bureau du 14/10 07:33Z (`radar.parse_settings` du venv exécuteur
sur `land: "pr"`) **prouve que le code installé porte déjà la clé** : elle
n'existe qu'à partir de L160e2 (`3898d7e8`), absente de `v1.9.1` (vérifié :
`git ls-tree v1.9.1 --name-only src/ameesh/` ne contient ni `land.py` ni
`merge_queue.py`). Comme aucun tag `v1.10.0` n'existe dans le dépôt, le build
exact se contrôle sur l'hôte (§4 étape 0 le complète) :

```bash
AMEESH_BIN="$(readlink -f "$(command -v ameesh)")"
AMEESH_PY="$(sed -n '1s/^#! *//p' "$AMEESH_BIN")"   # interpréteur réel : shebang du lanceur
case "$AMEESH_PY" in */env\ *) AMEESH_PY="$(command -v "${AMEESH_PY##*/env }")";; esac
test -x "$AMEESH_PY" || { echo "interpréteur du lanceur illisible : $AMEESH_PY" >&2; exit 1; }
"$AMEESH_PY" -c "from ameesh import radar, land, merge_queue; print(radar.LAND_MODES, 'land' in radar.PROJECT_KEYS)"
ameesh work --help | grep -E 'queue|land'    # sous-commandes présentes
```

**Absence de fusion automatique — trois verrous indépendants** :

1. `radar.run_pass` n'appelle `run_land` **que** si `project.land != "off"`
   (radar.py:606-607) : avec `off`, la passe d'atterrissage n'existe pas ;
2. `land.run_pass` re-teste `if project.land == "off": return []` (land.py:931) ;
3. le geste manuel `ameesh work land <lot>` refuse aussi : `LandError` —
   « project X does not declare `land` (local \| pr) » (land.py:1000-1001).

Avec `off`, `develop` ne reçoit donc **aucun** commit de la file : ni bail, ni
portes exécutées par le radar, ni push, ni fusion de PR.

**Ce qui continue** : `run_queue` tourne **avant** le test de `land` et ne le
lit pas (radar.py:605) — la file **ordonne, rejoue empilé, éjecte** et écrit
rangs et raisons ; le relevé des branches, les conflits et résolutions
préparées, les réservations de migrations et le courrier (conflit nouveau, tête
bloquée) restent actifs. La fusion du rang 1 redevient le geste **manuel de
l'orchestrateur** (§5, §6), au vert exact de la CI et après le verdict de la
file.

**Retour à `land: "pr"` après `#1037`/`#1236`** : changer **une seule valeur**
dans le profil dédié (`"off"` → `"pr"`), puis redémarrer la seule unité
désignée — l'exécuteur lit la configuration à son démarrage (`config.load`,
runner.py:7307/7389), sans recharge à chaud. Aucun autre fichier, aucun
drop-in ; `zz-compte-owner-1013.conf` reste intact.

**Diff proposé au bureau** (préparation, pour revue) : le bloc
`radar.projects.nexlink` de §2 avec `"land": "off"` — **toutes les autres clés
sont inchangées** (`repo`, `github`, `target`, `migrations`, `protected`,
`gates`, `generated`). C'est aussi le défaut du parseur si la clé est absente ;
la poser explicitement rend le repli lisible et l'écart avec `pr` d'une ligne.
Ce diff a été appliqué par le bureau au profil dédié **inactif** le 14/10 07:45Z
(§3quinquies).

### 3quinquies. Préparation report-only bureau (14/10 07:45Z) — `land: "off"` posé sur PC

Le bureau a passé le profil dédié — toujours **inactif** — à `land: "off"` et
fourni une contre-preuve directe d'inertie (courrier bureau → dev3, 14/10
07:45Z ; preuve `outils/183-profile-report-only-20261014.json`) :

| point | état constaté |
|---|---|
| profil dédié | `radar-nexlink.json` **inactif**, `land: "off"` ; `accounts` et tout le non-`radar` identiques au profil partagé |
| schéma | `radar.parse_settings` du venv exécuteur valide |
| contre-preuve d'inertie | `land.run_pass(None, NoDb, <projet off>, {}, owner=bureau)` → `[]`, **sans aucun accès base** |
| effets de bord | aucun drop-in, aucun `restart`, aucune entrée en file, aucun geste GitHub |

La préparation **report-only** est donc prête — elle n'est pas une activation.
Le retour à `land: "pr"` se fait **après** `#1037`/`#1236` effectivement
déployés (portes de famille), les fusions restant manuelles et contrôlées d'ici
là. L'autorisation et la nomination de l'activation du profil restent à
coordonner **orchestrateur + mesh-design**, sur **une seule** unité : ni le parse
ni la validation de schéma ne valent feu (§3ter).

### 3sexies. Contrôle de fraîcheur avant activation — 14/10 15:14Z (dev3)

Demandé par `mesh-design` (15:11Z) : vérifier dossier, profil et unité unique
pendant la QA indépendante §10, pour fournir le **candidat de geste** à son
retour. Contrôle de **lecture seule** (pas d'accès à `pc-smichea` depuis
`ameesh-agents-1`) ; **aucune activation, aucune date** posée ici.

| pièce | état constaté | fraîcheur |
|---|---|---|
| dossier | `review/183-pilote-radar-queue-nexlink` @ `e134bba4` (tête inchangée depuis 07:47Z) | à jour ; cette section l'actualise |
| code déployé | gel `12408e2b8818` (`validation/bureau-deploiement-12408e2b`, construit sur `27df8891` = fusion #1037 + correctif #1283) — phases 1–5 de #1236 exécutées, canon `manaty/home#62` fusionné/synchronisé (source bureau 17:05) | contient **#1037** (vérifié par ascendance git), **pas #1127** |
| carte §2 | `radar.parse_settings` et `protected_target` rejoués sur le build installé d'`ameesh-agents-1` (1.10.0, venv = `12408e2b`) | sortie **identique** à §2 : cible `develop`, `main`/tags/releases protégés, `develop` libre, `land pr merge`, 2 portes, 1 généré |
| modules d'atterrissage | `radar.py`, `land.py`, `merge_queue.py` du build déployé **identiques octet à octet** à `origin/main` (`046a106a`) | §3quater (défaut `off`, trois verrous, retour `pr`) reste exact |
| `config.py` | le build déployé n'a **pas** #1127 : un `AMEESH_CONFIG` explicite absent retombe encore en silence sur la config historique | le `test -f` explicite du runbook (§4 étapes 0 et 3) reste indispensable |
| profil dédié | `radar-nexlink.json` **inactif**, `land: "off"` (bureau 07:45Z) ; le SHA d'après la pose du report-only n'est pas enregistré (le `b3b1c32e…` de §3ter est celui de la version `pr`) | à relire sur PC avant le geste (`sha256sum`), avec le contrôle « comptes inchangés » (§4 étape 5) |
| unité unique | `ameesh-runner-agent@orchestrateur.service` + drop-in `zzz-radar-nexlink.conf` (même répertoire que `zz-compte-owner-1013.conf`) ; aucun drop-in écrit au dernier relevé | à reconfirmer sur PC (§4 étape 0 : instance, `DropInPaths`, absence du nôtre) |
| portes Nexlink | `scripts/domains-export.mjs` (sha `0d40d0f1`) et `scripts/check-rollbacks.mjs` (sha `ff114d2a`) présents sur `develop` @ `1fd688a7` (14:30Z, lecture `gh`) | pas de renommage depuis la mesure du 13/10 ; comportement et durée non rejoués ici |

**Ce qui reste à obtenir** : retours de la QA indépendante §10 (`qa-01` pour
ameesh, `codex3` pour Nexlink), puis **feu distinct** et créneau coordonné.
**Candidat de geste au retour QA** : §4 étapes 0, 3, 4 et 5 avec
`land: "pr"` — profil dédié + drop-in `zzz-` + `daemon-reload` + redémarrage de
la **seule** unité désignée, preuves d'environnement réel et de comptes
inchangés ; si le feu n'est pas donné, maintien du report-only (§3quinquies) et
fusion manuelle du rang 1 par l'orchestrateur (§6).

### 3septies. Attestation du build 1.11.1 sur l'hôte du pilote — 15/10 19:30Z (dev3)

Le gate « le build de l'hôte doit porter `#1333` » est levé par deux relevés de
**lecture seule** sur `pc-smichea`, recoupés depuis `ameesh-agents-1` :

| pièce | état constaté | source |
|---|---|---|
| version installée | `ameesh --version` = **1.11.1** ; lanceur `~/.local/bin/ameesh` → `~/.local/share/ameesh/venv/bin/ameesh` (shebang de ce venv) ; `direct_url` = `src-0dffda4` | `mesh-design` 19:27Z ; `ameesh-cx2` 19:29Z |
| marqueur `#1333` | `radar.py` importé du `site-packages` de ce venv : SHA256 `6a44a7da506d858d0a814b2dd51c23a1509cd9d2c965cf216375cb6933f0a4ff`, identique octet à octet à `#1333` @`5918aa3e` et à `main` `3df9a1ee`/`0dffda4` ; `_branch_target` (toutes formes de branche), `_symbolic_head`, `target_commit`, certification `published_tip` présents | `mesh-design` 19:27Z ; `ameesh-cx2` 19:29Z |
| unité | `ameesh-runner-agent@orchestrateur.service` active/running (PID 1137685), `ExecStart` sur ce venv ; **aucun** `zzz-radar-nexlink.conf` dans ses `DropInPaths` (radar non armé) | `ameesh-cx2` 19:29Z |

Contre-vérifications faites depuis `ameesh-agents-1` (même version 1.11.1) :

* `sha256sum` du `radar.py` installé ici = `6a44a7da…` ; `git show
  5918aa3e:src/ameesh/radar.py` et `git show 3df9a1ee:src/ameesh/radar.py`
  donnent le **même** SHA (le paquet installé, le correctif et `main` sont le
  même fichier) ;
* `radar.parse_settings` rejoué sur le build 1.11.1 avec la carte §2
  (`land: "pr"`) → sortie **identique** à §2 (cible `develop`, `main`/tags/
  releases protégés, `develop` libre, `pr merge`, 2 portes, 1 généré) ;
  `radar`, `land`, `merge_queue` importables, `LAND_MODES = ('off', 'local',
  'pr')` ;
* portes Nexlink sur `develop` `febe26e7b` : blobs `scripts/domains-export.mjs`
  `0d40d0f1…` et `scripts/check-rollbacks.mjs` `ff114d2a…`, **inchangés** depuis
  la mesure verte du 13/10.

Reste au pré-vol §4 étape 0, **sur l'hôte** (lecture seule, non qualifiable
depuis `ameesh-agents-1`, sans accès à `pc-smichea`) : Node, `gh auth status`,
clone (`toplevel`, non-bare, `remote`, `fetch`), existence de `$SRC`
(`critiques-2.json`), présence du drop-in `zz-compte-owner-1013.conf` et état de
départ de l'unité (`Environment`, `DropInPaths`), état courant de
`radar-nexlink.json` (`land: "off"`, SHA256). Ensuite, inchangé : créneau,
**feu distinct** `mesh-design`, application par le bureau (§7.4), orchestrateur
Nexlink prévenu la veille.

Attention à la coupure : si `pc-smichea` est redéployé à la coupure 23:00 Paris
(21:00Z, canon §11bis), le pré-vol doit être **refait** sur le build déployé —
il portera toujours `#1333` (présent dans `main` depuis `3df9a1ee`, et `main`
est en avant) mais son SHA changera.

**Mise à jour 15/10 19:35Z — pré-vol §4.0 exécuté par `mesh-design`** (lecture
seule `pc-smichea`, build 1.11.1) :

| point | état constaté |
|---|---|
| outillage | Node `v26.8.1`, `gh auth` OK, clone non-bare, `remote`/`fetch origin` OK, imports et parse §2 OK ; `develop` `febe26e7b` |
| unité | `ameesh-runner-agent@orchestrateur.service` active (PID 1137685) ; `AMEESH_CONFIG` réel = `critiques-2` ; drop-in `zz-compte-owner-1013.conf` présent, **aucun** `zzz-radar-nexlink.conf` |
| profil dédié | `radar-nexlink.json` **inactif** `land: "off"`, SHA256 `aabd2245…` |
| **§4.5** | **`accounts=False`, `rest=False`** : l'ancien profil a gardé des `accounts.claude`/`accounts.codex` antérieurs au socle courant → **le profil doit être rafraîchi depuis le socle courant avant le geste** |
| candidat | `/home/smichea/.local/state/ameesh/.menage/tmp/mesh-design~aeca9888/183-radar-nexlink-land-pr-candidat.json`, SHA256 `311ebc66f7e53ab95435f03eb5209d482d172b2839cccafce922ed3eb3a70183` : copie du socle courant + seule carte §2 (`land: "pr"`), `accounts`/`rest` True |
| preuve pré-vol | `/home/smichea/.local/state/ameesh/.menage/tmp/mesh-design~aeca9888/183-prevol-20261015-avant-coupure.json` (citée par `mesh-design`) |

**Créneau réservé** : **16/10 11:30–12:00 Paris (09:30–10:00Z)**, après la
coupure de 09:00Z, conditionné à : opérateur disponible (bureau §7.4, ou
remplaçant nommé avec les deux pilotes), profil rafraîchi + §4.5 `True/True`,
pré-vol rejoué sur le build final déployé après la coupure 21:00Z, point sûr de
fin de tour de l'unité, puis **feu distinct** `mesh-design`. Le pré-vol actuel ne
vaut pas activation.

## 4. Runbook d'application (sur décision du bureau)

**Profil dédié** (§3, §3bis) — pas le `critiques-2.json` partagé. `SRC` est le
profil de comptes d'origine (sa section `accounts` est reprise telle quelle),
`CFG` le nouveau fichier dédié, `UNIT` l'unité existante désignée :

```bash
SRC=/home/smichea/.config/ameesh/runner-profiles/critiques-2.json  # profil de comptes d'origine
CFG=/home/smichea/.config/ameesh/runner-profiles/radar-nexlink.json # nouveau profil dédié
UNIT=ameesh-runner-agent@orchestrateur.service      # unité existante désignée (§3bis)
ZZ=$(systemctl --user show -p DropInPaths --value "$UNIT" | tr ' ' '\n' \
     | grep -E '/zz-compte-owner-1013\.conf$')      # drop-in du propriétaire, à préserver
DROPIN="${ZZ%/*}/zzz-radar-nexlink.conf"            # même répertoire, trié APRÈS zz
REPO=~/development/manaty/nexlink
AMEESH_BIN="$(readlink -f "$(command -v ameesh)")"
AMEESH_PY="$(sed -n '1s/^#! *//p' "$AMEESH_BIN")"   # interpréteur réel : shebang du lanceur
case "$AMEESH_PY" in */env\ *) AMEESH_PY="$(command -v "${AMEESH_PY##*/env }")";; esac
test -x "$AMEESH_PY" || { echo "interpréteur du lanceur illisible : $AMEESH_PY" >&2; exit 1; }
```

0. **Vérifier l'hôte** — version, outillage, clone **et le chemin réel du
   profil de comptes** (le parseur ne vérifie ni l'existence du clone ni celle
   du profil ; le socle court proposé le 14/10 06:05Z n'existait pas) :

   ```bash
   ameesh --version                              # attendu : 1.10.0 (aucun tag v1.10.0 dans le dépôt : le build se pinçe par l'import ci-dessous)
   node --version                                # attendu : 26.8.1
   gh auth status                                # lecture CI + fusion des PR manaty/nexlink
   git -C "$REPO" rev-parse --show-toplevel      # attendu : /home/smichea/development/manaty/nexlink
   git -C "$REPO" rev-parse --is-bare-repository # attendu : false
   git -C "$REPO" remote get-url origin          # attendu : git@github.com:manaty/nexlink.git
   git -C "$REPO" fetch --prune origin           # prouve l'accès du compte au dépôt
   "$AMEESH_PY" -c "from ameesh import radar, land, merge_queue; print(radar.LAND_MODES, 'land' in radar.PROJECT_KEYS, 'generated' in radar.PROJECT_KEYS)"
   # attendu : ('off', 'local', 'pr') True True  (les modules d'atterrissage et de file doivent s'importer)
   test -f "$SRC"                                # le socle doit exister
   test -n "$ZZ" || echo "zz introuvable : NE RIEN ÉCRIRE, stratégie à revoir"
   echo "drop-in porteur : $DROPIN"              # même répertoire que zz, nom post-zz
   systemctl --user show -p Environment -p DropInPaths "$UNIT"   # état de départ
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

3. **Valider le chargement** — le schéma du profil entier, celui que l'unité
   lira (`$CFG`, pas le partagé), avec l'interpréteur de l'exécuteur
   (`$AMEESH_PY`, celui du venv) : `radar.parse_settings` lève ici si la carte
   est fausse. **Puis** vérifier le dépôt par git :

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
   écrire `$DROPIN` (contenu exact en §3bis) : même répertoire que
   `zz-compte-owner-1013.conf`, nom trié **après** le sien ; ne jamais modifier
   ni retirer ce dernier. Relire la fusion systemd **avant** de redémarrer :

   ```bash
   systemctl --user daemon-reload
   systemctl --user show -p Environment -p DropInPaths "$UNIT"
   # attendu : DropInPaths liste .../zz-compte-owner-1013.conf PUIS
   #           .../zzz-radar-nexlink.conf (le nôtre en dernier) ;
   #           Environment contient
   #           AMEESH_CONFIG=/home/smichea/.config/ameesh/runner-profiles/radar-nexlink.json
   ```

   Puis redémarrer **cette seule** unité et prouver l'environnement **réel** du
   processus — l'exécuteur ne relit pas la configuration à chaud :

   ```bash
   systemctl --user restart "$UNIT"
   PID=$(systemctl --user show -p MainPID --value "$UNIT")
   tr '\0' '\n' < /proc/"$PID"/environ | grep '^AMEESH_CONFIG='
   # attendu : AMEESH_CONFIG=/home/smichea/.config/ameesh/runner-profiles/radar-nexlink.json
   systemctl --user cat "$UNIT"     # drop-ins effectivement lus, zz inclus
   ```

   La chaîne de preuve est alors complète : systemd (`Environment`,
   `DropInPaths`) → `$CFG` → chargement `radar` (étape 3) → `accounts`
   inchangés (étape 5).

   Les autres unités restent sur `critiques-2.json` ; une seule unité référence
   `$CFG`, aucune autre n'a la clé `radar`.

5. **Prouver que les comptes sont inchangés — puis contrôle à blanc.** Le
   profil dédié doit être le profil source **plus** la seule carte §2 :

   ```bash
   "$AMEESH_PY" - "$SRC" "$CFG" <<'PY'
   import copy, json, sys
   src = json.load(open(sys.argv[1]))
   dst = json.load(open(sys.argv[2]))
   reste = copy.deepcopy(dst)
   projets = reste.get("radar", {}).get("projects", {})
   projets.pop("nexlink", None)
   if not projets:
       reste.get("radar", {}).pop("projects", None)
   if not reste.get("radar"):
       reste.pop("radar", None)
   print("accounts inchangés :", src.get("accounts") == dst.get("accounts"))
   print("reste identique    :", src == reste)
   PY
   # attendu : deux True
   ```

   Contrôle à blanc (lecture seule) :

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
* **Configuration** : restaurer la sauvegarde de l'étape 1, ou supprimer le
  profil dédié et retirer **le seul** `$DROPIN` (`zzz-radar-nexlink.conf`),
  puis `systemctl --user daemon-reload && systemctl --user restart $UNIT` —
  **cette seule unité**. `zz-compte-owner-1013.conf` n'est **jamais** retiré ni
  modifié (consigne bureau du 14/10 06:47Z) : l'unité reprend le profil partagé
  `critiques-2.json`. Prouver le retour par
  `systemctl --user show -p Environment -p DropInPaths $UNIT` (le nôtre a
  disparu) puis, après redémarrage, `/proc/<MainPID>/environ`.

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
   dev3 corrigée (14/10 06:50Z, §3bis)** : unité existante
   `ameesh-runner-agent@orchestrateur.service`, profil
   `/home/smichea/.config/ameesh/runner-profiles/radar-nexlink.json` dérivé de
   `/home/smichea/.config/ameesh/runner-profiles/critiques-2.json`, drop-in
   `zzz-radar-nexlink.conf` (même répertoire que `zz-compte-owner-1013.conf`,
   trié après lui ; le drop-in du propriétaire reste intact), recharge
   `daemon-reload` + `restart` de cette unité ; preuves d'environnement effectif
   et de comptes inchangés en §4 ; à valider par le bureau avant tout créneau.
6. **Créneau du 14/10** — **le créneau 08:30 CEST n'est pas activé** : le
   bureau l'a refusé sur la première proposition (réserve du 14/10 06:47Z,
   socle inexistant et drop-in trié avant `zz`) et n'a reconfiguré aucune unité
   ni armé aucun créneau. Un **nouveau créneau ne sera proposé qu'après
   qualification** des pièces corrigées (§3bis) par le bureau ; la **liste
   initiale des PR** est fixée au démarrage par l'orchestrateur Nexlink, hors
   sensibles `915`, `695` et `943` en vol.
   **Mise à jour 14/10 07:4xZ** : la qualification bureau est arrivée (§3ter) —
   profil dédié créé (SHA256 `b3b1c32e…`), schéma validé par le venv exécuteur,
   réserves levées, rien d'appliqué (aucun drop-in, aucun restart). Le repli
   documentaire `land: "off"` est qualifié sur le code installé (§3quater,
   demande orchestrateur 07:38Z) et le bureau l'a posé sur le profil dédié
   **inactif** (report-only, §3quinquies, 07:45Z ; contre-preuve d'inertie sans
   accès base) ; le créneau `land: "pr"` reste à coordonner avec les
   déploiements `#1037`/`#1236`, et l'activation du profil à autoriser/nommer
   par orchestrateur + mesh-design sur une seule unité.
   **Mise à jour 14/10 15:14Z** : contrôle de fraîcheur §3sexies — les
   déploiements `#1037`/`#1236` sont exécutés (gel `12408e2b`, phases 1–5,
   canon `home#62` synchronisé) et la carte, les modules et les portes Nexlink
   sont vérifiés sur ce build ; le geste d'activation (profil `pr` + drop-in +
   redémarrage de l'unique unité) reste conditionné aux retours QA §10 et à un
   **feu distinct**. Aucune date ni activation.
7. **Portes** — les deux portes locales suffisent-elles pour le pilote, ou
   faut-il des portes par chemin (`scripts/__tests__/*.test.mjs`) dès le
   premier jour ?
8. **Acceptation du jour pilote (Nexlink) — 15/10 13:19Z** — l'orchestrateur
   Nexlink accepte le jour pilote sur la base du présent dossier
   (`@3c921745` : profil `radar-nexlink.json`, unité unique `orchestrateur`,
   `land: "pr"` sur `develop`). Planification une fois le build 1.11.1 déployé
   en staging, avec pré-vol re-qualifié et feu `mesh-design` distinct ;
   l'orchestrateur est prévenu la veille du créneau, les paquets Nexlink
   restent sur le chemin actuel jusque-là. (source : courrier orchestrateur
   15/10 13:18:59Z, #58691)
   **Mise à jour 15/10 19:30Z** : le build en staging sur `pc-smichea` est
   attesté `1.11.1`, `radar.py` = `6a44a7da…` (= `#1333` = `main 3df9a1ee`),
   unité `orchestrateur` active sans drop-in radar — relevés `mesh-design`
   (19:27Z) et `ameesh-cx2` (19:29Z), recoupés depuis `ameesh-agents-1`
   (§3septies). Le gate « build doit porter `#1333` » est levé ; restent le
   pré-vol §4 étape 0 sur l'hôte, le **créneau** et le **feu distinct**.
   **Mise à jour 15/10 19:35Z** : pré-vol §4.0 exécuté par `mesh-design` —
   outillage/clone/parse/unité OK, mais **§4.5 `accounts=False`/`rest=False`**
   (profil dédié à rafraîchir depuis le socle courant ; candidat SHA
   `311ebc66…` préparé hors configuration). **Créneau réservé : 16/10
   11:30–12:00 Paris (09:30–10:00Z)**, après la coupure de 09:00Z, sous les
   conditions du §3septies ; préavis formel transmis à l'orchestrateur Nexlink
   le 15/10 19:4xZ (cc bureau, `mesh-design`). Accusé de l'orchestrateur
   (19:40Z) : créneau accepté côté pilote Nexlink, liste initiale composée à la
   coupure de 09:00Z et envoyée avec sa confirmation et son point sûr avant
   09:30Z.
