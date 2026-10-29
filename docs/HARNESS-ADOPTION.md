# Veille des versions : adoption (#3264 C, lot #4293)

Ce document décrit le versant **adoption** de la veille des versions de
harnais et de modèles (spec `docs/design/3264-veille-versions.md`, PR
manaty/ameesh#3265 ; canon §7, owner du 28/10/2026). A détecte une version,
B rejoue les 3 derniers lots livrés, C adopte.

## Règle

| Rejeu (B) | Prix | Geste de C |
|---|---|---|
| régression, ou régression de qualité | quelconque | **aucune adoption**, le lot est clos avec son rapport |
| meilleure ou équivalente, qualité sans régression | **égal** | automatique : canari, puis hôte par hôte |
| meilleure ou équivalente, qualité sans régression | plus cher, nouveau fournisseur ou changement de forfait | **décision de l'owner** (`ameesh decide`), aucun hôte changé ; la décision porte le rapport et le coût projeté sur 7 jours |

Le prix est **recalculé** : pour un modèle, le barème de `cost.py` (fichier du
poste, sinon `DEFAULT_PRICES`) compare les deux modèles — `deepseek-flash` →
`deepseek-pro` est `higher` même si le rapport écrit `equal`. Un prix inconnu
ou incomplet (modèle absent du barème, tarifs du rapport incomplets des deux
côtés) ne vaut **jamais** `equal` : il part en décision owner. Les tarifs du
rapport ne peuvent pas rendre « égal » un prix que le barème dit plus cher ;
un tarif commun que le rapport dit plus cher suffit à refuser l'automatique.
Un fournisseur ou un forfait qui change est nommé (`new_provider`,
`plan_change`). Un modèle plus cher sans projection à 7 jours est bloqué, pas
adopté.

## Rapport de rejeu (contrat avec B, #4292)

C lit **la sortie réelle de B** : une note de rejeu sur le lot
d'évaluation, qui nomme le fichier JSON écrit par `ameesh replay run` :

```
Rejeu comparatif deepseek 0.2.0-rc.2 → 0.2.1-rc.4 : meilleure.
Banc : #1 (light), #2 (normal), #3 (sensitive) ; coût, durée, tests du lot et note aveugle dans le rapport.
Rapport : /chemin/absolu/report.json
Empreinte : <sha256 du fichier de rapport>
```

L'empreinte est celle des **octets** du fichier (`ameesh replay report --note
FICHIER` la calcule). C la vérifie avant de lire le rapport : une note sans
empreinte, ou un fichier modifié après la note, est refusée (TOCTOU, #4312).

Le fichier est le schéma `ameesh-replay-report/1` (`evaluated.kind`
`harness|model`, `evaluated.old/new` avec `label`, `binary`, `version`,
`model` ; `constants.model` et `effort` ; `lots[*].runs.old/new` avec
`cost_usd` et `duration_s` ; `conclusion` `better|equal|regression|incomplete`).
C convertit ce rapport : `equal` devient `equivalent`, `incomplete` bloque
toute adoption, les bornes du canari sont les mesures de l'ancienne version
±10 % (coût et durée), et pour un modèle le prix est comparé au barème de
`cost.py`. Le coût projeté à 7 jours d'un modèle plus cher est la dépense
réelle des 7 derniers jours du harnais × le rapport de coût ancien → nouveau
mesuré par B ; sans cette mesure, la décision owner est bloquée.

La note doit être écrite par l'**assigné du lot d'évaluation** : une note d'un
autre agent est ignorée. Le fichier doit être lisible par l'hôte qui exécute
le tick (évaluation et tick sur le même hôte, ou chemin partagé) ; sinon
`ameesh harness adopt --report FICHIER` reste le geste manuel.

Le format normalisé `ameesh-veille-report/1` (objet JSON en note ou fichier)
reste accepté pour un rapport fourni à la main ; il porte les champs ci-dessous :

```json
{
  "schema": "ameesh-veille-report/1",
  "evaluation_lot": 4278,
  "harness": "deepseek",
  "object": "harness",
  "from_version": "0.2.0-rc.2",
  "to_version": "0.2.1-rc.4",
  "model": "deepseek-flash",
  "conclusion": "better",
  "quality": {"regression": false, "note": 7.5, "defects": []},
  "price": {"kind": "equal", "projected_7d_usd": 4.2},
  "bounds": {"cost_usd": [0.005, 0.02], "duration_s": [5, 20]},
  "old_binary": "/opt/old/dsh",
  "new_binary": "/opt/new/dsh",
  "bench": [{"lot": 1, "class": "light"}]
}
```

- `object` : `harness` (choix par `harness_bin`) ou `model` (choix par
  `model`) ; défaut `harness`.
- `model` : modèle du rejeu ; pour un harnais il sert à vérifier le prix au
  barème. Absent ou inconnu : prix `unknown` → décision owner.
- `conclusion` : `better`, `equivalent` ou `regression` (B : `equal`).
- `quality.regression` : `true` suffit à refuser.
- `price.kind` : `equal`, `higher`, `new_provider` ou `plan_change`.
- `bounds` : au moins `cost_usd` ou `duration_s` ; sans borne mesurable, pas
  de canari concluant. `new_binary` ne sert que de binaire de canari (exécutable
  sur l'hôte du tick) ; les autres hôtes passent par la carte explicite.
- Un rapport est **figé** sur son lot avec son empreinte ; un second rapport
  différent sur le même lot est refusé (une nouvelle évaluation est un lot).

## Cibles de la bascule

Seuls les agents encore sur la version évaluée sont candidats au canari et à
la bascule :

- **harnais** : `harness_bin` posé → il doit être l'ancien binaire du rapport ;
  `harness_bin` NULL → la version de l'hôte lue par `harness_versions` doit
  être `from_version`. Une version inconnue est exclue (fail-closed).
- **modèle** : `model` posé → il doit être `from_version` ; `model` NULL suit
  la précédence du **runner** : fichier d'état `model` de l'agent sur son hôte,
  puis défaut du descripteur. Le tick ne peut lire ce fichier que sur son
  propre hôte : un agent distant dont `model` est NULL est exclu
  (fail-closed), jamais supposé au défaut du descripteur (#4312).

Un agent déjà sur un autre binaire/modèle n'est ni canari, ni remplacé, ni
compté dans « entre deux tours » ; l'hôte du canari reste dans l'ordre de
bascule pour ses autres agents.

## Commandes

```bash
# démarre l'adoption ; sans --report, lit la note de rejeu du lot (B ou C)
ameesh harness adopt 4278 --report /tmp/rapport-4278.json \
    --bin ameesh-agents-1=/opt/harness/0.2.1-rc.4/bin/dsh \
    --bin ameesh-agents-2=/opt/harness/0.2.1-rc.4/bin/dsh

# un seul --bin /chemin s'applique à tous les hôtes connus du harnais
ameesh harness adopt 4278 --bin /opt/harness/0.2.1-rc.4/bin/dsh

# avance (canari, bascule, retour) ; appelé par ameesh-harness-adoption.timer
ameesh harness tick
ameesh harness tick --dry-run --json

# état lisible ou machine
ameesh harness show-adoption 4278 [--json]

# retour arrière explicite, tracé dans le lot
ameesh harness rollback-adoption 4278 --reason "régression observée sur agents-2"
```

`--agent NOM` force l'agent du canari (il doit être sur la version évaluée) ;
par défaut, le premier agent au repos de cette version (un agent qui tourne
n'est jamais candidat). `--requester NOM` désigne l'agent qui dépose la
décision owner (par défaut l'assigné du lot). `--bin` est facultatif pour le
canari : `new_binary` du rapport B y sert de chemin (il doit être exécutable
sur l'hôte du tick). Pour les **autres** hôtes, aucune valeur par défaut : il
faut la carte explicite `AMEESH_HARNESS_ADOPTION_BINS` (ou
`~/.config/ameesh/harness-adoption.json`) :

```json
{"bins": {"deepseek": {"0.2.1-rc.4": {
  "ameesh-agents-1": "/opt/harness/0.2.1-rc.4/bin/dsh",
  "ameesh-agents-2": "/opt/harness/0.2.1-rc.4/bin/dsh"}}}}
```

Un `--bin HOTE=CHEMIN` surcharge la carte. Un chemin de binaire doit être
sous un **préfixe contrôlé** (`~/.local/`, `/opt/`, `/usr/local/`, `/usr/`,
plus `AMEESH_BIN_DIR` et `AMEESH_HARNESS_ADOPTION_PREFIXES`, séparés par `:`) :
un chemin absolu hors de ces racines est refusé avant tout geste (#4312). La
décision owner est **signée**
(`needs_signature`) et sa preuve est revérifiée avant chaque geste.

Le tick **découvre** aussi les lots d'évaluation ouverts dont le titre
commence par « Évaluer » et qui portent une note de rejeu sans adoption — note
de B (`Rejeu comparatif … Rapport : chemin`) ou note C (`veille-report:`) :
il les adopte sans qu'un humain lance `adopt`. C'est le chemin normal
A → B → C ; `ameesh harness adopt` reste disponible pour un rapport fourni en
fichier ou pour forcer un agent de canari. Le rapport découvert n'est lu que
s'il a été noté par l'**assigné du lot** : une note d'un autre agent ne
déclenche aucune adoption. Le fichier nommé par B doit être lisible par
l'hôte du tick (même hôte, ou chemin partagé).

## Canari et bascule

1. **Canari** : un agent de la version évaluée passe sur la nouvelle version
   (ou le nouveau modèle). Il dure **24 h ou 10 tours** ; son démarrage est
   soumis à la même fenêtre de 20 minutes qu'une bascule (calendrier
   illisible = pas de canari). Les tours sont lus
   dans `turn_costs` (médiane du coût et de la durée). Si une médiane sort des
   bornes du rejeu, ou si aucun tour n'a été mesuré, l'agent est remis sur la
   version précédente et l'adoption est refusée. À l'échéance des 24 h, moins
   de **3 tours mesurés** ne font pas un canari concluant : refus et retour à
   la version précédente (#4312).
2. **Bascule hôte par hôte** : après un canari conforme, chaque hôte du
   harnais passe à son tour. L'hôte du canari vient en premier ; les autres
   sont ordonnés par nom. Une bascule attend :
   - qu'aucun agent **cible** de cet hôte ne soit en tour (`status =
     running`) — « entre deux tours » ;
   - que la prochaine coupure de release soit à **au moins 20 minutes**
     (calendrier compilé du canon ; un calendrier illisible bloque la
     bascule, il ne l'autorise pas).
3. **Fin** : `done`, avec la version d'origine et la carte des valeurs
   inverses dans le lot.

## Inverse

Le retour est consigné dans le lot et dans l'état de l'adoption
(`inverse.version`, `inverse.restore`). Il est appliqué :

- automatiquement quand le canari sort des bornes, ou quand la preuve de la
  décision owner n'est plus valide avant un hôte ;
- par `ameesh harness rollback-adoption <lot> --reason …`, à tout moment, y compris
  après `done` : chaque agent retrouve exactement son `harness_bin` ou son
  `model` d'avant la bascule (y compris `NULL`).

## Ce que C ne fait pas

- **Il n'installe pas** la nouvelle version : le binaire doit être présent sur
  l'hôte (distribution de l'hôte, L17). Un chemin absent est un « hôte non
  prêt » à la résolution de l'adaptateur ; le tour n'est pas lancé. C'est
  volontaire : aucune archive npm n'est tirée par un tick.
- Il ne change jamais le modèle ou l'effort **par jugement** hors rapport de
  rejeu (spec §5) : cela reste `ameesh set` ou une décision owner.
- Il ne ferme pas le lot d'évaluation à la place de son assigné ; une
  régression y laisse la note « aucune adoption », le lot se ferme avec son
  rapport.

## Mise en service

Lecteur d'abord, canon ensuite : le code C lit `mechanics`/`release_cutoff`
comme les autres gardes de coupure ; aucune nouvelle clé de
`processus/mecanique.yaml` n'est introduite par ce lot. Les 20 minutes sont la
marge de la règle owner (§4) et du canon §7.

Sur **un** hôte qui a la base (le poste de release ou un hôte d'agents) :

```bash
install -m 0644 deploy/systemd/ameesh-harness-adoption.service \
                   deploy/systemd/ameesh-harness-adoption.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ameesh-harness-adoption.timer
journalctl --user -u ameesh-harness-adoption -f
```

La carte des binaires installés (voir plus haut) est lue automatiquement par
le tick ; `new_binary` ne couvre que l'hôte du canari, et un binaire absent ou
non exécutable sur l'hôte du tick est refusé avant tout geste.

Le tick est sans effet tant qu'aucune adoption n'est ouverte. Un seul geste
tourne à la fois : verrou consultatif **de transaction**
(`pg_advisory_xact_lock`, effectif sous psql comme sous psycopg) ; plusieurs
hôtes peuvent porter le timer sans double geste. Chaque transition laisse une
note
`harness-adoption:` au journal du lot, en plus de la table
`harness_adoptions` (migration 0505).

## Portée des changements

- `agent_registry.harness_bin` : binaire du harnais pour un agent ; `NULL`
  garde le comportement d'hôte (`harness_bins`, PATH). `ameesh set <agent>
  harness_bin=/chemin/absolu` le pose, une valeur vide l'efface.
- **Un `harness_bin` n'est lancé que s'il est déclaré sur l'hôte** (#4488) :
  carte des binaires installés (`AMEESH_HARNESS_ADOPTION_BINS` ou
  `~/.config/ameesh/harness-adoption.json`, toutes versions, pour cet hôte),
  `harness_bins` de la configuration, ou `AMEESH_<HARNAIS>_BIN`. Un chemin
  écrit en base par une autre identité ne peut donc pas faire exécuter un
  binaire arbitraire avec les identifiants du compte ; la garde est dans
  l'exécuteur et dans `ameesh attach`, au moment de la lecture. Installez la
  carte sur chaque hôte qui porte des agents épinglés : un chemin fourni par
  `--bin` ou `new_binary` mais absent de la carte de l'hôte est refusé
  (« hôte non prêt »), jamais lancé.
- L'exécuteur relit la ligne de l'agent au début de chaque tour ; un tour en
  cours ne change jamais de binaire sous lui. La même valeur suit l'agent en
  session interactive (`ameesh attach`).
- Un binaire absent est refusé à la résolution (hôte non prêt), jamais
  remplacé en silence par le binaire du PATH.
