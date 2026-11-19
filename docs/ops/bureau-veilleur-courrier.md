# Veilleur de courrier du bureau : remise par `agent-mail ack` (#7836)

Source : constat de #5772 P2c du 19/11 05:26Z, lot #7836
(manaty/ameesh#6059), réponse du bureau du 19/11 05:39Z (#227259).
Staging seulement (§14).

## Le constat

Le bureau lit son courrier par un veilleur hors exécuteur :

- sur **bureau-goal**, `watch.sh` boucle toutes les 60 s jusqu'à une heure
  donnée et appelle
  `ssh root@AG2 'runuser -u ameesh -- …/venv-courant/bin/python /tmp/w-bg.py <dernier id>'` ;
  à la première sortie non vide, il l'affiche et s'arrête. La session du
  bureau lit la sortie, pose elle-même le pointeur (`last.txt`, dans son
  scratchpad) puis relance ;
- sur **ameesh-agents-2**, `/tmp/w-bg.py` se connecte avec le DSN de
  `~ameesh/.config/ameesh/config.json`, donc avec le rôle `ameesh`, et fait
  un `SELECT` sur `agent_mailbox` (`id > pointeur`, `recipient = 'bureau'`)
  et sur `work_item_events` (notes du PC). C'est une lecture pure : il ne
  pose **jamais** `delivered_at`.

Conséquence : 8 079 messages du bureau restaient non remis. Le 19/11, la
porte pre-act de #5772 P2c a refusé l'acte KMS sur un courrier de lot du
04/11 (#190395, informatif), et le hook officiel n'a rien livré sur BG — la
connexion limitée n'y inscrit pas le hook, et un appel manuel sans le JSON
du harnais ne remet rien.

## La voie ameesh : `agent-mail ack`

`agent-mail ack <id>…` remet les messages **par le même protocole que
l'exécuteur et le hook** : réserver les ids sous un jeton, puis solder cette
réservation. Aucun `UPDATE` direct de `delivered_at` n'est nécessaire.

- **Idempotent** : un message déjà remis est rendu « already delivered »,
  sans réécrire son horodatage ; une seconde session de la même identité
  peut donc le présenter sans le compter deux fois.
- **Sans vol** : une réservation active d'une autre remise n'est jamais
  prise ; le message ressort « reserved by another delivery », la commande échoue et
  il reste à la remise qui le tient (ou à la suivante après expiration).
- **Sans perte** : le message reste en base et lisible par
  `agent-mail show <id>` pour l'autre session de l'identité partagée.
- **Tracé** : `meta.consigne.porteur` porte le lecteur qui a affiché le
  message (`ack@bureau-goal:526a0a74` avec `--by`, sinon `ack@<hôte>`), et
  le jeton de réservation reste sur la ligne.
- **Borné** : `--ids-from` lit un inventaire (premier champ TSV, ou un id
  par ligne), `--batch` borne chaque transaction (200 par défaut, 1 000 au
  plus), `--limit` borne le total (10 000 au plus). Un lot interrompu se
  reprend : les messages déjà remis sont ignorés.
- **STOP protégés** : en mode stock (`--ids-from`, ou `--skip-stop`), un
  message lié à un lot encore ouvert dont le corps contient un STOP
  (`stop`, `arrêt`, `suspend…`, `annul…`, `halt`) n'est **pas** remis : il
  est listé « STOP on open lot #N » et reste non lu. La porte pre-act ne
  cherche un STOP que dans le courrier postérieur à `--since` ; acquitter en
  bloc un ancien STOP le ferait disparaître de la porte. On le relit et on le
  règle par son auteur (levée) ou par un ack nominatif après lecture.

## Procédure (geste du bureau)

Le geste sur BG reste celui du bureau ; le dépôt ameesh livre la commande
et cette procédure.

### 1. Après affichage, acquitter les ids affichés

Dans `/tmp/w-bg.py`, **après** avoir affiché un message (même tronqué à
1 200 caractères : `one.py` sert à lire la suite) et **jamais** pour un
message filtré (« [BG », « coupure déjà taguée »), appeler la commande avec
l'id affiché. Un seul appel pour tous les ids d'une même sortie est
préférable :

```sh
AGENT_MAIL_NAME=bureau /…/venv-courant/bin/agent-mail ack \
  --by "ack@bureau-goal:<session>" 190395 190397
```

- `AGENT_MAIL_NAME=bureau` : l'identité du veilleur (le watcher n'a pas de
  liaison de session).
- `--by` : la session BG qui a lu la sortie, pour le journal de réservation.
- Si l'acquittement échoue, le veilleur sort en erreur et la session ne pose
  pas `last.txt` : la sortie sera relue, pas perdue.
- Avant la release qui contient ce lot, exécuter la même commande depuis un
  clone de la branche :
  `PYTHONPATH=<clone>/src <venv>/bin/python -m ameesh.cli ack …`.

### 2. Solder le stock (8 079 messages)

L'inventaire déjà fait par le bureau
(`inventaire-non-livre-20261119T0528Z.tsv`, 0600 sur BG ; colonnes id,
expéditeur, date UTC, lot, première ligne) sert directement : le premier
champ est l'id.

```sh
# contrôle sans écriture
ssh root@AG2 'runuser -u ameesh -- env AGENT_MAIL_NAME=bureau /…/venv-courant/bin/agent-mail ack \
  --ids-from - --skip-stop --by "ack@bureau-goal:<session>" --dry-run' \
  < inventaire-non-livre-20261119T0528Z.tsv

# acquittement borné, reprenable
ssh root@AG2 'runuser -u ameesh -- env AGENT_MAIL_NAME=bureau /…/venv-courant/bin/agent-mail ack \
  --ids-from - --skip-stop --batch 200 --by "ack@bureau-goal:<session>"' \
  < inventaire-non-livre-20261119T0528Z.tsv
```

Le compte final (`ack: N acknowledged, …, N STOP skipped (open lot)`) et les
compteurs de progression sont sur la sortie ; chaque message porte son
`porteur` dans `meta.consigne`. Un message qui n'est pas adressé au bureau
n'est pas acquitté et ressort au bilan ; un message déjà remis n'est pas
réécrit. Le tri STOP est actif par défaut en mode stock ; `--skip-stop` le
rend explicite.

### 2 bis. Régler les STOP protégés

Le `--dry-run` puis le stock listent les ids « skipped: STOP on open lot
#N ». Pour chacun :

1. lire le message (`agent-mail show <id>`) et l'état du lot #N ;
2. si le STOP est encore valable, laisser le message non lu et le faire
   lever par son auteur (ou par le pilote) ; ne pas l'acquitter ;
3. s'il est caduc (décision prise, STOP déjà levé), l'acquitter par son
   numéro **après lecture** : `agent-mail ack <id> --by …`. Un id
   positionnel n'applique pas le tri STOP — c'est le geste de lecture ;
4. relancer le stock : les messages réglés ne reviennent plus, les autres
   sont « déjà remis » et ignorés.

### 3. Identité partagée BG/PC

La règle confirmée par le bureau : la session du PC ne lit pas cette boîte,
elle travaille par les notes de lot. L'acquittement est donc une **remise
d'identité** : ce que la session BG a affiché est remis pour `bureau`, et
le message reste lisible par `agent-mail show` pour l'autre session. Pas
d'acquittement par session, pas de seconde table de lecture.

Ne jamais acquitter depuis le PC un message que le PC n'a pas affiché : le
premier acquittement gagne, le second est un no-op, mais un message acquitté
sans avoir été lu serait perdu pour les deux sessions.

### 4. Vérifier

```sh
agent-mail show <id>                 # lisible après acquittement
psql … -c "select count(*) from agent_mailbox
           where recipient='bureau' and delivered_at is null"   # décroît
ameesh gate pre-act --lot <lot> --kind kms --since <ISO> --dry-run
                                     # une fois #7831 fusionné (borne 24 h),
                                     # ne refuse plus sur un ancien courrier ;
                                     # avant, la porte peut encore refuser
                                     # sur ce message non remis
```

Les tests du dépôt : `tests/test_7836_mail_ack.py` (remise, porte pre-act,
tiers refusé, idempotence BG/PC, réservation active, inventaire TSV, lots
bornés, STOP de lot ouvert en stock et en nominatif, identité `execute`
refusée, repli fichier refusé).

### 5. Retour

L'acquittement ne supprime rien. Un message acquitté par erreur reste
lisible par `show` ; on le relaie par lot ou on le renvoie, on ne remet pas
`delivered_at` à nul (la connexion limitée l'interdit, et le protocole ne
défait pas une remise).
