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
  des gestes d'orchestrateur (la cible protégée `main` le refuse même).

Condition 1 de l'orchestrateur Nexlink (« une release qui porte L160e,
déployée sur l'hôte ») : ameesh **1.10.0** porte la file (L160e) et le
atterrissage (L160e2) ; vérifié sur `ameesh-agents-1` (`ameesh --version` →
`ameesh 1.10.0`, `radar.PROJECT_KEYS` contient `land`). **À reconfirmer sur
l'hôte désigné** (§4, étape 0).

## 2. Carte — l'entrée `nexlink` de `cfg.radar`

À ajouter sous `radar.projects` de la configuration de l'hôte désigné, **sans
écraser les autres projets** ni les autres clés de `nexlink` (`full_suite`
viendra de [#916](https://github.com/manaty/ameesh/issues/916), après #756) :

```json
"nexlink": {
  "repo": "~/development/manaty/nexlink",
  "github": "manaty/nexlink",
  "target": "develop",
  "migrations": "supabase/migrations",
  "protected": ["main"],
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
* `protected: ["main"]` — la file ne fusionne jamais vers `main` : elle refuse
  le lot avec « cible protégée » (condition 5 : main, tags et releases restent
  des gestes d'orchestrateur) ;
* `migrations: supabase/migrations` — active la sonde des numéros : un
  fichier `NNN_*.sql` ajouté par un lot sans réservation ameesh est signalé
  (condition 2 ; depuis #701, les réservations vivent dans ameesh,
  `ameesh work migration`, L122 — plus de `RESERVATIONS.md`) ;
* `generated: backend/domains.json` — le fichier produit par
  `scripts/domains-export.mjs` est **régénéré à chaque fusion**, jamais pris
  d'une fusion ni de `rerere`, puis vérifié : une résolution `rerere` d'un
  fichier généré serait fausse en silence (condition 3) ;
* `gates` — deux portes **locales courtes**, exécutées sur l'arbre fusionné
  (conditions 4, repli « portes locales ») : `domains-export --check`
  (backend/domains.json en phase) et `check-rollbacks.mjs` (chaque migration
  numérotée a son document de retour). Pur Node, quelques secondes, aucun
  accès base ; la CI GitHub reste le **préalable d'entrée en file** (une PR
  dont la CI n'est pas verte sur la tête gelée est refusée par
  `ameesh work queue add`) ;
* `land: "pr"` — ameesh fusionne lui-même la tête de file par l'API, méthode
  `merge` par défaut (la pratique Nexlink : `--merge --match-head-commit`).
  Risque résiduel documenté (fenêtre entre la dernière lecture de la PR et
  `gh pr merge`) ; c'est la décision de l'owner #11882. Le repli `land: "off"`
  (la file ordonne, l'orchestrateur fusionne le rang 1) est immédiat, sans
  redéploiement.

**Validation de schéma** — lire la configuration avec l'interpréteur de
l'exécuteur, pas `python3` du système (l'installation vit dans un venv) :

```bash
AMEESH_PY="$(dirname "$(readlink -f "$(command -v ameesh)")")/python3"
"$AMEESH_PY" - <<'PY'
import json, os
from ameesh import radar
raw = json.load(open(os.path.expanduser("~/.config/ameesh/config.json")))
s = radar.parse_settings(raw.get("radar") or {})
p = s.projects["nexlink"]
print(p.repo, "|", p.target, "|", p.github, "|", p.migrations, "|", p.protected,
      "|", p.land, p.merge_method)
print("gates:", list(p.gates))
print("generated:", [(g.path, g.regen, g.check) for g in p.generated])
print("protected main:", radar.protected_target(p, "origin/main"),
      "| develop:", radar.protected_target(p, "origin/develop"))
PY
```

Résultat obtenu le 2026-10-13 avec le paquet **1.10.0 installé**
(`…/venv/lib/python3.12/site-packages/ameesh/`, sans `sys.path`) ; le chemin
affiché est l'expansion de `~` **sur la machine de contrôle**
(`ameesh-agents-1`) — sur `pc-smichea` il donnera `/home/smichea/…` :

```
/home/ameesh/development/manaty/nexlink | develop | manaty/nexlink | supabase/migrations | ('main',) | pr merge
gates: ['node scripts/domains-export.mjs --check', 'node scripts/check-rollbacks.mjs']
generated: [('backend/domains.json', 'node scripts/domains-export.mjs', 'node scripts/domains-export.mjs --check')]
protected main: main | develop: None
```

`radar.parse_settings` accepte la carte ; `protected_target(p, "origin/main")`
→ `main`, `protected_target(p, "origin/develop")` → `None`. Un chemin `repo`
invalide, une clé inconnue ou un `land` sans portes lèvent **au chargement de
la configuration**, donc avant tout passage d'exécuteur : la valider avant de
l'écrire.

## 3. Hôte désigné

La clé `radar` désigne l'hôte qui fait tourner le radar **et** la file (une
passe après chaque relevé de branches, ~300 s). Le radar de conflits ameesh
tourne déjà sur **`pc-smichea`** (l'hôte du radar — à confirmer : la clé
`radar.projects` y existe pour `ameesh`, cf. carte #916) ; c'est là qu'ajouter
`nexlink`, et **pas** sur `ameesh-agents-1`/`-2` sans décision : déclarer la
clé sur une VM y lancerait une seconde passe (courrier radar en double, et
deux exécuteurs pour la même file).

À confirmer sur l'hôte avant d'écrire :

```bash
ls -d ~/development/manaty/nexlink          # chemin du clone (sinon l'ajuster)
node --version                              # les portes sont du Node
gh auth status                              # lecture CI + fusion des PR manaty/nexlink
git -C ~/development/manaty/nexlink fetch --prune origin
```

## 4. Runbook d'application (sur décision du pilote / du bureau)

0. **Vérifier la version installée** sur l'hôte (la file et le `land` sont
   dans 1.10.0) :

   ```bash
   ameesh --version                            # attendu : 1.10.0
   AMEESH_PY="$(dirname "$(readlink -f "$(command -v ameesh)")")/python3"
   "$AMEESH_PY" -c "from ameesh import radar; print('land' in radar.PROJECT_KEYS, 'generated' in radar.PROJECT_KEYS)"
   # attendu : True True
   ```

1. **Sauvegarder** la configuration :

   ```bash
   cp -a ~/.config/ameesh/config.json ~/.config/ameesh/config.json.bak-$(date +%F-%H%M)
   ```

2. **Fusionner** le bloc §2 dans `radar.projects` (éditer, ne rien écraser ;
   JSON sans commentaire ni virgule finale).

3. **Valider** la configuration entière (lève ici si le schéma est faux ;
   même interpréteur qu'à l'étape 0) :

   ```bash
   "$AMEESH_PY" - <<'PY'
   import json, os
   from ameesh import radar
   raw = json.load(open(os.path.expanduser("~/.config/ameesh/config.json")))
   s = radar.parse_settings(raw.get("radar") or {})
   for name, p in sorted(s.projects.items()):
       print(name, p.repo, p.target, p.land, len(p.gates), len(p.generated))
   PY
   ```

4. **Recharger** : l'exécuteur lit la configuration à son démarrage ;
   redémarrer l'unité de l'hôte
   (`systemctl --user restart ameesh-runner-agent@<agent>.service`), ou
   attendre son prochain redémarrage.

5. **Contrôle à blanc** (lecture seule) :

   ```bash
   ameesh conflicts --project nexlink     # après une première passe : rapport du radar
   ameesh work queue show                 # attendu : aucune file (le pilote n'a pas commencé)
   ```

   Le premier passage signale les conflits **courants** entre lots Nexlink
   (courrier passif aux assignés) : c'est attendu et sans effet de bord.

6. **Tracer** dans #183 : hôte, date, sauvegarde, sortie des contrôles, et le
   nom de qui a appliqué.

## 5. Jour de pilote — déroulé

| Qui | Geste |
|---|---|
| auteur du lot | tests ciblés verts, `work milestone <lot> frozen --sha <sha>` |
| relecteur | `work milestone <lot> verdict --verdict ok --sha <sha>` (classes sensibles) |
| auteur du lot (ou orchestrateur) | CI verte sur la tête gelée, puis `ameesh work queue add <lot>` (`enter` ne restreint pas l'acteur ; à trancher au §7) |
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

## 7. Points à trancher avant d'appliquer

1. **Hôte et chemin du clone** — `pc-smichea` (radar existant) et
   `~/development/manaty/nexlink` sont la proposition ; à confirmer sur
   l'hôte.
2. **Qui applique** — le pilote (`mesh-design`), le bureau, ou un agent de
   `pc-smichea` ; le présent lot ne fait aucun geste de production.
3. **Fenêtre du pilote** — 14/10 après REL-11 (proposition de
   l'orchestrateur Nexlink), avec la liste ordonnée des PR prêtes de
   `develop`.
4. **`land: "pr"` d'emblée ou premier jour en `land: "off"`** — le risque
   résiduel de la fusion par API est documenté et accepté (#11882) ; le repli
   est immédiat.
5. **Portes** — les deux portes locales suffisent-elles pour le pilote, ou
   faut-il des portes par chemin (`scripts/__tests__/*.test.mjs`) dès le
   premier jour ?
6. **Qui entre en file** — l'auteur du lot après son verdict, ou
   l'orchestrateur (la commande accepte les deux acteurs ; le gain attendu
   suppose que l'auteur le fasse).
