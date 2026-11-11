# Banc local de la grappe PostgreSQL du mesh (lot #6902, EP8.6b)

Banc **jetable** à trois nœuds pour rejouer, sans aucun geste sur les hôtes du
mesh, les scénarios de bascule d'EP8.6 : perte du primaire, coupure réseau,
poste client éteint, perte de deux nœuds. Chaque nœud conteneurisé porte etcd
3.5, Patroni 4.x et PostgreSQL 17, comme les trois hôtes de l'étude
[`replique-base-mesh.md`](../../../docs/design/etudes/replique-base-mesh.md).

Ce répertoire est une **préparation**. Il ne touche ni `installer-serveur.sh`,
ni `terraform`, ni `sauvegarder-base.sh`, ni aucun hôte réel. Le geste réel
reste EP8.6 (#6863), sur décision du pilote ou du bureau.

## Sûreté

- La cible par défaut d'`exercer.py` est `banc` : trois conteneurs locaux, un
  réseau Docker privé `172.30.77.0/24`, aucune adresse du mesh.
- La cible `grappe` exige `--geste-reel` **et** un fichier de grappe qui
  déclare explicitement chaque commande autorisée. Le dépôt n'en fournit
  aucune : sans ces commandes, le script refuse le geste.
- `--lecture-seule` (cible grappe) ne fait que lire l'état, sans geste.
- `--geste-reel` avec la cible `banc` est refusé : le banc est toujours local.
- Aucun secret n'entre dans le dépôt. `generer-tls.sh` (#6870) écrit le
  matériel TLS et `secrets.env` sous `$BANC_ETAT`, avec les permissions
  d'origine ; les conteneurs les copient dans leur système de fichiers.
- `banc.sh demonter` ne supprime des fichiers que si `$BANC_ETAT/etat.json`
  porte le marqueur du banc (schéma `ameesh-banc-grappe/1` et projet dérivé
  du chemin). Un dossier `--etat` quelconque, ou un montage interrompu avant
  l'écriture du marqueur, n'est jamais effacé : seuls les conteneurs et le
  réseau étiquetés sont retirés, et le message le dit.

## Prérequis

- Docker avec le droit de créer un réseau, de monter des répertoires et
  d'ajouter `NET_ADMIN` aux conteneurs (la coupure réseau utilise `iptables`
  **dans** le conteneur, ce qui laisse `docker exec` disponible pour observer
  le nœud isolé).
- `python3`, `bash`, `openssl` (pour `generer-tls.sh`), environ 1 Go libre.
- Les gabarits et générateurs de #6870 (`deploy/serveur/patroni/`) et les
  scripts de réplication de #6868 (`deploy/serveur/replication/`) sont les
  sources ; le banc ne les modifie pas.

## Démarrage rapide

```bash
cd deploy/serveur/patroni/banc

# 1. Monter le banc (construit l'image une fois).
./banc.sh monter

# 2. État, journal, coupure réseau d'un nœud.
./banc.sh etat
./banc.sh journal banc-1 100
./banc.sh couper banc-3
./banc.sh retablir banc-3

# 3. Démonter et tout supprimer.
./banc.sh demonter
```

L'exercice complet, sous le créneau `ameesh heavy` (le banc est lourd) :

```bash
ameesh heavy -- python3 deploy/serveur/patroni/banc/exercer.py --cible banc
```

Le rapport est écrit par défaut dans un dossier `mktemp` et affiché à la fin.
`--rapport DOSSIER` fixe le dossier ; `--etat DOSSIER` fixe l'état du banc (le
défaut est sous `$TMPDIR`). `--garder` laisse le banc monté après l'exercice.

## Topologie du banc

| Nœud | Adresse du banc | Rôle | Priorité Patroni | Tags |
|---|---|---|---|---|
| `banc-1` | `172.30.77.11` | candidat primaire | 100 | `failover_priority: 100` |
| `banc-2` | `172.30.77.12` | candidat primaire | 90 | `failover_priority: 90` |
| `banc-3` | `172.30.77.10` | votant etcd, standby asynchrone | 0 | `nofailover: true`, `nosync: true` |
| `pc` | `172.30.77.50` | poste client (écriture continue) | — | — |

Les configurations sont rendues par `generer-configuration.py` à partir de
`topologie-banc.json`, avec les mêmes modèles et les mêmes invariants que la
grappe réelle (un seul synchrone, hub `nosync`, `pg_rewind`, `use_slots`,
`failsafe_mode: false`). Le banc ne réutilise pas les adresses du mesh.

État produit :

| Élément | Emplacement |
|---|---|
| configurations rendues | `$BANC_ETAT/config/<nœud>/` |
| matériel TLS et `secrets.env` | `$BANC_ETAT/tls/` |
| constats d'exécution | `$BANC_ETAT/constats.json` |
| journal des écritures du poste | `$BANC_ETAT/client/ecritures.jsonl` (ajouté, jamais tronqué ; une ligne = `id`, `jeton` unique, nœud, date ; `reinitialiser` recrée le dossier) |
| `etat.json` du banc | `$BANC_ETAT/etat.json` |
| données PostgreSQL et etcd | `$BANC_ETAT/donnees/<nœud>/` et `$BANC_ETAT/etcd/<nœud>/`, supprimées par `banc.sh demonter` (propriété `postgres` dans le conteneur, nettoyage en root depuis l'image) |

## Le script d'exercice

`exercer.py` rejoue les quatre scénarios, avec une réinitialisation complète du
banc avant chacun (sauf `--sans-reinitialiser`) :

| Scénario | Geste | Critères |
|---|---|---|
| `perte-primaire` | `docker kill` du primaire | nouveau primaire < 60 s (cible) et < 300 s (exigence), RPO 0 sur le jalon et les écritures confirmées, aucun moment à deux primaires, retour de l'ancien primaire en standby par `pg_rewind` |
| `coupure-reseau` | `iptables` DROP entrant/sortant hors `lo` sur le primaire | nouveau primaire et démotion de l'ancien, RPO 0, aucun moment à deux primaires, rétablissement et rejonction |
| `pc-eteint` | arrêt du conteneur `pc` | primaire et quorum etcd inchangés, aucune écriture du poste éteint, journal des confirmations conservé au redémarrage, reprise des écritures |
| `deux-noeuds` | perte du primaire et du standby synchrone, le hub `nofailover` survit | le survivant ne se promeut pas (1 votant etcd sur 3), aucune écriture sans quorum, aucun split brain, jalon présent après le retour des deux nœuds — **limite attendue de l'étude**, pas une survie |

Mesures produites : délai jusqu'au nouveau primaire, RPO, durée des périodes
à deux primaires, délai de démotion de l'ancien primaire, délai de retour,
présence de `pg_rewind`, état etcd, rôles finaux. Chaque geste est précédé
d'au moins 20 écritures confirmées, pour que la comparaison RPO ne porte pas
sur deux lignes.

Pour le RPO, la fenêtre de panne est **close par l'arrêt du poste** dès le
nouveau primaire connu : le journal est figé, et chaque confirmation qu'il
contient — y compris celles obtenues pendant la bascule, donc après la
photographie prise avant le geste — est comparée à la branche finale après
retour et `pg_rewind`. L'identité comparée est le couple `(id, jeton)` :
un id absent est une perte, un id présent avec un autre jeton est une
substitution sur branche divergente ; une confirmation sans jeton est
invérifiable. L'un ou l'autre rend le verdict `blocked`.

### Format du rapport

`exercer.py` écrit `rapport.json` et `rapport.md` dans le dossier de rapport.
Le JSON porte le schéma `ameesh-banc-grappe/1` et les champs suivants, prévus
pour être joints au lot EP8.6 :

```json
{
  "schema": "ameesh-banc-grappe/1",
  "lot": 6902,
  "cible": "banc",
  "genere_le": "2026-11-11T13:00:00Z",
  "environnement": {"hote": "...", "noeuds": [], "versions": "..."},
  "constats": [],
  "scenarios": [
    {"nom": "perte-primaire", "verdict": "ok", "mesures": {}, "preuves": {}, "notes": []}
  ],
  "conclusion": {"verdict": "ok", "scenarios_executes": 4, "ok": 4, "limites": 0, "bloques": 0}
}
```

`verdict` vaut `ok`, `limite` (critère de cible manqué mais exigence tenue, ou
limite documentée de l'étude) ou `blocked`.

## Cible grappe réelle (EP8.6)

La même mécanique de scénarios peut être pointée sur la vraie grappe, mais le
dépôt ne contient **aucune commande de geste réel**. Un fichier de grappe doit
décrire les nœuds, le poste et, pour chaque action, la commande exacte que
l'exploitant autorise :

```json
{
  "grappe": "ameesh",
  "pc": {"hote": "<hôte-ssh-du-poste>"},
  "noeuds": [
    {"nom": "ameesh-agents-1", "hote": "<hôte-ssh>", "adresse": "<adresse>", "nofailover": false},
    {"nom": "ameesh-agents-2", "hote": "<hôte-ssh>", "adresse": "<adresse>", "nofailover": false},
    {"nom": "manaty-mesh-1", "hote": "<hôte-ssh>", "adresse": "<adresse>", "nofailover": true}
  ],
  "actions": {
    "perte_noeud": "<commande autorisée>",
    "retour_noeud": "<commande autorisée>",
    "coupure_reseau": "<commande autorisée>",
    "retablissement_reseau": "<commande autorisée>",
    "arret_pc": "<commande autorisée>",
    "demarrage_pc": "<commande autorisée>"
  },
  "table_jalon": "public.banc_ep86",
  "creer_table_jalon": false,
  "ssh": ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"],
  "sudo": "sudo -n"
}
```

Utilisation :

```bash
# Lecture seule, sans geste et sans --geste-reel.
python3 exercer.py --cible grappe --grappe-config grappe.json --lecture-seule

# Exercice complet : chaque action est prise dans le fichier, après revue.
python3 exercer.py --cible grappe --grappe-config grappe.json --geste-reel
```

Sans `actions`, toute tentative de geste est refusée avec un message explicite.
La table `table_jalon` est nécessaire pour mesurer le RPO ; `creer_table_jalon`
doit rester `false` sur une grappe déjà en service, sauf geste validé. EP8.6
reste responsable du contenu des commandes, de la fenêtre et du retour arrière.

## Constat d'exécution : `etcd3.hosts` de #6870

La première exécution du banc a révélé un défaut réel dans
`generer-configuration.py` (#6870) : `valeurs_communes()` rend
`etcd3.hosts` sous la forme `https://hôte:2379,...`, alors que Patroni 4.x
attend `hôte:port` et applique `protocol: https`. Patroni refuse alors de
démarrer :

```
ValueError: Invalid IPv6 URL
```

`corriger-configuration.py` corrige **la copie du banc** et écrit
`$BANC_ETAT/constats.json` ; le rapport cite l'écart. Ce n'est pas une
correction de #6870 : un **lot correctif de #6870 doit partir avant EP8.6**,
sinon la vraie grappe ne démarrera pas avec ces configurations.

## Fichiers

| Fichier | Rôle |
|---|---|
| `Dockerfile` | image du nœud : PostgreSQL 17, etcd 3.5.17 (empreinte #6870), Patroni 4.x, `iptables` |
| `entree-noeud.sh` | copie les configurations hors des montages en lecture seule, démarre etcd puis Patroni |
| `client-ecriture.sh` | écriture continue du poste client, journal des identifiants confirmés |
| `topologie-banc.json` | topologie du banc, sans adresse du mesh |
| `banc.sh` | monter / état / couper / tuer / réinitialiser / démonter |
| `exercer.py` | scénarios, mesures, rapport Markdown et JSON |
| `corriger-configuration.py` | correction locale d'`etcd3.hosts` et constat #6870 |
| `tests/test_6902_banc_grappe.py` | contrats hors Docker (topologie, génération, correction, refus du geste réel, rapport) |

## Dépannage

- `docker: unknown command: docker compose` : le banc n'utilise que `docker`,
  pas le plugin compose.
- Un conteneur reste arrêté : `./banc.sh journal <nœud> 200` ; les causes les
  plus fréquentes sont un échec de bootstrap Patroni ou un pair etcd
  injoignable.
- Après une exécution interrompue : `./banc.sh demonter` nettoie par étiquette
  `ameesh.banc.etat=<dossier>` même sans `etat.json`.
- La cible < 60 s peut être manquée si la grappe est déjà dégradée : le rapport
  donne le délai mesuré et distingue la cible de l'exigence < 300 s.
