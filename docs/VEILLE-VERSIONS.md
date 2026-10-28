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

Le prix est **recalculé** : un rapport qui dit `equal` alors que le tarif
d'entrée, de cache ou de sortie augmente est traité comme `higher` ; un
fournisseur ou un forfait qui change est nommé. Un modèle plus cher sans
projection à 7 jours est bloqué, pas adopté.

## Rapport de rejeu (contrat avec B, #4292)

Un objet JSON, schéma `ameesh-veille-report/1`, noté sur le lot d'évaluation
(une ligne `veille-report: {…}` ou un bloc ```` ```json ````) et/ou passé à
`ameesh veille adopt --report FICHIER`. Champs requis :

```json
{
  "schema": "ameesh-veille-report/1",
  "evaluation_lot": 4278,
  "harness": "deepseek",
  "object": "harness",
  "from_version": "0.2.0-rc.2",
  "to_version": "0.2.1-rc.4",
  "model": "deepseek-flash",
  "provider": "deepseek",
  "effort": "max",
  "conclusion": "better",
  "quality": {"regression": false, "note": 7.5, "defects": []},
  "price": {
    "kind": "equal",
    "from": {"input": 0.30, "cached_input": 0.006, "output": 1.20},
    "to":   {"input": 0.30, "cached_input": 0.006, "output": 1.20},
    "projected_7d_usd": 4.2
  },
  "bounds": {"cost_usd": [0.005, 0.02], "duration_s": [5, 20]},
  "bench": [{"lot": 1, "class": "light"}]
}
```

- `object` : `harness` (une version du binaire, choix par `harness_bin`) ou
  `model` (un identifiant de modèle, choix par `model`) ; défaut `harness`.
- `conclusion` : `better`, `equivalent` ou `regression`.
- `quality.regression` : le verdict de relecture ; `true` suffit à refuser.
- `price.kind` : `equal`, `higher`, `new_provider` ou `plan_change`.
- `bounds` : les bornes du canari, dérivées du rejeu (bruit ±10 % sur le coût
  et la durée, ±1 point sur la note). Au moins `cost_usd` ou `duration_s` est
  requis : sans borne mesurable, le canari ne peut pas conclure et l'adoption
  est bloquée.
- Un rapport est **figé** sur son lot avec son empreinte. Un second rapport
  différent sur le même lot est refusé : une nouvelle évaluation est un
  nouveau lot.

## Commandes

```bash
# démarre l'adoption ; le rapport est lu dans le fichier, sinon dans le journal
ameesh veille adopt 4278 --report /tmp/rapport-4278.json \
    --bin ameesh-agents-1=/opt/harness/0.2.1-rc.4/bin/dsh \
    --bin ameesh-agents-2=/opt/harness/0.2.1-rc.4/bin/dsh

# un seul --bin /chemin s'applique à tous les hôtes connus du harnais
ameesh veille adopt 4278 --bin /opt/harness/0.2.1-rc.4/bin/dsh

# avance (canari, bascule, retour) ; appelé par ameesh-veille.timer
ameesh veille tick
ameesh veille tick --dry-run --json

# état lisible ou machine
ameesh veille show 4278 [--json]

# retour arrière explicite, tracé dans le lot
ameesh veille rollback 4278 --reason "régression observée sur agents-2"
```

`--agent NOM` force l'agent du canari ; par défaut, le premier agent au repos
du harnais (un agent qui tourne n'est jamais candidat). `--requester NOM`
désigne l'agent qui dépose la décision owner (par défaut l'assigné du lot).

Le tick **découvre** aussi les lots d'évaluation ouverts dont le titre
commence par « Évaluer » et qui portent une note `veille-report:` sans
adoption : il les adopte sans qu'un humain lance `adopt`. C'est le chemin
normal A → B → C ; `ameesh veille adopt` reste disponible pour un rapport
fourni en fichier ou pour forcer un agent de canari.

## Canari et bascule

1. **Canari** : un agent du harnais passe sur la nouvelle version (ou le
   nouveau modèle). Il dure **24 h ou 10 tours** ; les tours sont lus dans
   `turn_costs` (médiane du coût et de la durée). Si une médiane sort des
   bornes du rejeu, ou si aucun tour n'a été mesuré, l'agent est remis sur la
   version précédente et l'adoption est refusée.
2. **Bascule hôte par hôte** : après un canari conforme, chaque hôte du
   harnais passe à son tour. L'hôte du canari vient en premier ; les autres
   sont ordonnés par nom. Une bascule attend :
   - qu'aucun agent du harnais de cet hôte ne soit en tour (`status =
     running`) — « entre deux tours » ;
   - que la prochaine coupure de release soit à **au moins 20 minutes**
     (calendrier compilé du canon ; un calendrier illisible bloque la
     bascule, il ne l'autorise pas).
3. **Fin** : `done`, avec la version d'origine et la carte des valeurs
   inverses dans le lot.

## Inverse

Le retour est consigné dans le lot et dans l'état de l'adoption
(`inverse.version`, `inverse.restore`). Il est appliqué :

- automatiquement quand le canari sort des bornes ;
- par `ameesh veille rollback <lot> --reason …`, à tout moment, y compris
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
install -m 0644 deploy/systemd/ameesh-veille.service \
                   deploy/systemd/ameesh-veille.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ameesh-veille.timer
journalctl --user -u ameesh-veille -f
```

Le tick est sans effet tant qu'aucune adoption n'est ouverte. Un seul tick
tourne à la fois (verrou consultatif en base) ; plusieurs hôtes peuvent porter
le timer sans double geste. Chaque transition laisse une note
`veille-adoption:` au journal du lot, en plus de la table
`harness_adoptions` (migration 0505).

## Portée des changements

- `agent_registry.harness_bin` : binaire du harnais pour un agent ; `NULL`
  garde le comportement d'hôte (`harness_bins`, PATH). `ameesh set <agent>
  harness_bin=/chemin/absolu` le pose, une valeur vide l'efface.
- L'exécuteur relit la ligne de l'agent au début de chaque tour ; un tour en
  cours ne change jamais de binaire sous lui. La même valeur suit l'agent en
  session interactive (`ameesh attach`).
- Un binaire absent est refusé à la résolution (hôte non prêt), jamais
  remplacé en silence par le binaire du PATH.
