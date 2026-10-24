# Contrôle d'inclusion d'un paquet §12 — `scripts/paquet-inclusion.py` (#1644)

Ce document décrit le contrôle à passer **avant la CI complète et le feu** d'un
paquet d'intégration (canon `manaty/home`, `processus/lots.md` §12) : vérifier
qu'aucun lot admis, routé vers la coupure ou **prêt** n'a été silencieusement
omis à la composition, et que le contenu des lots composés est réellement dans
l'arbre de la tête.

## Le constat

Au paquet ameesh de 11h (15/10/2026), quinze lots admis ou routés par le pilote
ont été omis à la composition, sans note de retrait ni revert : #1124, #1056,
#388, #795, #1466, #1104, #1411, #1437, #1463, #414, #1511, #1253, #1349,
#1396, #1496. Le manifeste ne le disait pas, et l'ascendance ne le dit pas non
plus : après un revert, la tête du lot reste ancêtre de la tête du paquet
(#1638). Il faut donc confronter **les admissions**, **les lignes du manifeste**
et **le contenu de l'arbre**.

## Le constat #2404

Aux paquets ameesh h-0800 et h-0900 (18/10/2026), six lots cités par
l'auditeur comme prêts ou admis ont attendu un paquet de 1 h à 20 h : #2374,
#2377, #1280, #2274, #2282, #1272. La composition partait des lots cités par
les courriers (orchestrateur, pilote, auteurs) : un lot prêt que personne
n'avait cité n'entrait ni au manifeste ni dans ses « écartés ». #2374 y était
même « écarté » pour « verdicts anciens » alors que le pilote l'avait admis.

La correction a deux jambes, toutes deux dans ce dépôt :

1. `scripts/paquet-candidats.py` **calcule** la liste des lots prêts (état
   `qa`, gel effectif égal à la tête **publiée**, verdicts requis acquis sur
   cette tête, essai de fusion propre contre la cible). C'est la liste de
   départ de la composition ; un courrier peut ajouter une admission, jamais
   retirer un lot prêt du calcul.

   #2496 : un lot dont le seul motif est `conflict` est prêt **comme unité**
   quand une remise poussée sous `refs/ameesh/resolutions/<lot>/<cible>-…`
   (ou `union-<cible>-…`) a pour parents le gel relu et un commit de la
   cible, fusionne proprement dans la base, et qu'un verdict la couvre (sur
   le SHA de la remise, avec la classe la plus stricte du gel et du contenu
   composé — la remise contre la base —, au moins un avis indépendant même
   pour un lot léger, et jamais la dispense `-none` du gel) ou que c'est une
   union mécanique (ajouts purs des deux côtés, arbre égal à la fusion
   `merge=union` ; §2, aucun avis). La remise est alors la **source** à
   composer (`source`, et « remise … » dans le rapport). Un verdict sur la
   remise ne périme pas le gel de la branche ; il n'est mis de côté que pour
   une fusion à deux parents, jamais pour la tête publiée ni un SHA gelé : un
   blocage du gel reste. Un blocage sur la remise la refuse toujours. Sinon le
   motif `conflict` dit pourquoi chaque remise est refusée.
2. `scripts/paquet-inclusion.py --ready-file` confronte cette liste au
   manifeste : tout lot prêt doit être **composé**, ou figurer dans la table
   « Écartés (ce véhicule) » avec un **motif**. Une ligne sans motif ne compte
   pas.

## Partir de la liste calculée

```sh
# 1. la liste calculée, lue fraîche de origin (aucun filtre d'assigné)
scripts/paquet-candidats.py --repo . --ready-out "$TMPDIR/prets-0800.txt"

# 2. la composition part de ces lots (et des admissions du pilote) ; un lot
#    prêt non composé s'écrit au manifeste :
#
#    ## Écartés (ce véhicule)
#
#    | lot | motif |
#    |---|---|
#    | #2377 | second avis dû sur 5943fe46 (deux familles) |

# 3. le contrôle d'inclusion, avant la CI complète et le feu
scripts/paquet-inclusion.py --manifest reviews/integration-paquet-h-…md \
    --head origin/integration/paquet-h-… \
    --ready-file "$TMPDIR/prets-0800.txt" \
    --expected-file "$TMPDIR/admissions-0800.txt"
```

Sans source `--ready`/`--ready-file`, le contrôle s'arrête en `2` ; une source
fournie mais vide (« aucun lot prêt ») est un calcul valide, pas une absence.
`--no-ready` est **refusé** (#2464) : sur la recomposition de h-1300, il a
laissé passer #2384, prêt depuis 14:29, absent du manifeste sans ligne
« écarté ». Une recomposition recalcule la liste sur sa cible
(`paquet-candidats.py --ready-out`) ; elle ne saute pas le contrôle.

## Hold pilote (#3375)

Un lot sous **hold de pilote actif** n'est pas candidat, même gelé, avisé et
sans conflit : `packet_candidates.inventory` lit les retenues actives par
`planned_waits` (la même lecture que la stagnation : `hold:` d'un humain ou
d'un orchestrateur/conception, levé par `unhold:` ou un changement d'état) et
ajoute le refus `pilot-hold`, dont le détail commence par « hold pilote ». Le
composeur écrit ce motif dans la table « Écartés (ce véhicule) » : le lot
n'entre pas dans `--ready-out`, et le calcul ne le propose plus comme unité.

Le composeur peut passer outre, explicitement et pour une raison :

```sh
scripts/paquet-candidats.py --repo . \
    --hold-override 3231 \
    --hold-reason "premier paquet post-T0 (décision mesh-design 16:5xZ)" \
    --ready-out "$TMPDIR/prets-0800.txt"
```

La surcharge ne lève **que** le hold : un autre refus (gel, avis, conflit)
laisse le lot en attente, et le geste est alors refusé sans rien journaliser.
Quand elle est effective, la raison est écrite au journal du lot
(`hold-override: composition malgré le hold pilote de … — <raison>`, cible,
base et source citées) avant l'export de la liste prête. Le hold du pilote
reste en place : le calcul suivant écarte de nouveau le lot tant qu'un
`unhold:` (ou un changement d'état) ne l'a pas levé.

`paquet-candidats.py` ne compose rien et n'écrit dans aucun dépôt : il lit la
base, les jalons et Git (clone privé du radar, ou clone jetable),
et son code de retour est `1` si une lecture a échoué — une liste incomplète
ne doit pas servir de base à un paquet. Il reste en lecture seule, sauf le
geste explicite `--hold-override`, qui écrit sur le lot la note de la
surcharge. Dans ce cas `--ready-out` est retiré,
y compris un export d'une exécution précédente : `paquet-inclusion.py`
s'arrête alors sur un fichier absent (code `2`) au lieu de lire une liste
partielle. `--json` rend le schéma
`ameesh-packet-candidates/1` avec, par lot, la tête publiée, le gel effectif,
la classe, la porte des avis, l'essai de fusion et les **codes** de refus
(`freeze-not-head`, `verdicts-missing`, `conflict`, `pilot-hold`,
`already-in-target`, …).
Un lot y est dit prêt quand **aucun** refus n'est calculé.

## Usage

```sh
# liste attendue établie par le pilote (admissions et routages de la coupure)
scripts/paquet-inclusion.py \
    --manifest reviews/2026-10-15-paquet-23h.md \
    --head origin/integration/paquet-23h-20261015 \
    --expected-file "$TMPDIR/admissions-23h.txt"

# ou directement, et en JSON pour un rapport de tour
scripts/paquet-inclusion.py --manifest … --head <sha> \
    --expected 1124,1056,388,795 --json

# liste calculée des lots prêts (sortie de paquet-candidats.py --ready-out)
scripts/paquet-inclusion.py … --ready-file "$TMPDIR/prets-0800.txt"

# variante stricte : un contenu partiel ou vide compte comme un défaut bloquant
scripts/paquet-inclusion.py … --strict

# contrôle partiel assumé, sans liste attendue (admissions non vérifiées)
scripts/paquet-inclusion.py … --no-expected
```

- `--expected` / `--expected-file` : les lots attendus à la coupure (un id par
  ligne, `1124`, `#1124` ou `1124 admission pilote 05:17` ; les commentaires
  après `#` isolé sont ignorés). À défaut, une section du manifeste dont le
  titre contient « admissions » fournit la liste, en liste à puces **ou en
  tableau** : le manifeste devient auto-porteur, c'est la forme recommandée.
  Une liste prête ne dispense pas de celle-ci : sans admissions, le contrôle
  s'arrête en `2`, sauf `--no-expected` explicite — une sortie `0` laisserait
  croire que le contrôle « admissions × manifeste » a eu lieu.
- `--ready` / `--ready-file` : la liste **calculée** des lots prêts (#2404),
  telle que la produit `scripts/paquet-candidats.py --ready-out` (un id par
  ligne). Tout lot prêt doit être composé ou écarté avec un motif ; c'est la
  liste de départ de la composition, pas une seconde liste d'admissions. Un
  fichier fourni mais vide est un calcul valide (« aucun lot prêt »).
- `--no-ready` : **refusé** (#2464). Il servait à assumer un contrôle sans
  liste calculée ; utilisé sur la recomposition de h-1300, il a laissé passer
  #2384 sans ligne « écarté ». L'absence de source prête arrête désormais le
  contrôle en `2` : sur une recomposition, recalculer la liste sur la cible.
- `--no-expected` : assume explicitement un contrôle sans liste d'admissions ;
  le rapport dit alors « admissions × manifeste NON effectué ». Si une liste
  de lots prêts est fournie, le contrôle « prêts × manifeste » reste rendu.
- `--excused` / `--excused-file` : les lots dont le report ou le retrait a été
  décidé hors manifeste (par exemple un `REPORT 23h` du pilote).
- `--repo` (défaut `.`), `--head` (SHA ou réf), `--json`, `--strict`,
  `--no-expected`.

Le script est en **lecture seule** : aucun réseau, aucune écriture dans le
dépôt. Il lit le manifeste et l'historique git ; les commits cités par le
manifeste doivent être présents localement (sinon `git fetch` d'abord).

## Le trailer `ameesh-work:` des commits de composition (#2157)

Chaque commit de composition — une unité du paquet — porte une ligne
`ameesh-work: <id>` par lot qu'il porte, en fin de message :

```
vehicule 0400 : #2131 ef22692ba6a3 — détachement conditionnel CAS et PR détachée

ameesh-work: 2131
```

Sans elle, le commit ne désigne pas son lot : `ameesh work merged --sha
<commit>` le refuse, et `sync-branches` ne voit que le gel enregistré quand la
composition a porté une remise (#1628). Le trailer ne change ni l'arbre ni la
CI.

Pour l'écrire sans y penser, `scripts/paquet-compose.py` fait la fusion et le
message, avec un `--lot` par lot porté :

```sh
scripts/paquet-compose.py --lot 2131 --source ef22692ba6a3 --cut 0400 \
    --note "détachement conditionnel CAS et PR détachée"
```

Sur fusion propre, il crée le commit et affiche son SHA. Sur conflit, il ne
commite pas : le message complet est déjà dans `.git/MERGE_MSG` ; on résout, on
`git add`, puis `git commit --no-edit`. Le trailer reste en fin de message.

Le sujet nomme chaque lot avec la tête composée, `#<lot> <tête>` (#2360) : un
`#N` seul n'est qu'une citation. Une fois les unités composées, le **corps du
lot porteur** et le manifeste reprennent la ligne que le script imprime :

```sh
scripts/paquet-compose.py --units --base origin/main [--head <réf>]
# 2 unités de fusion, 3 lots portés : #2131 ef22692ba6a3, #2140 0a1b2c3d4e5f, #2157 112233445566
```

Les unités de fusion sont les commits de fusion de première parente depuis
`--base` qui portent `ameesh-work:` (tête = second parent) ; une mise à jour
depuis la cible n'en est pas une. Une unité peut porter plusieurs lots, listés
chacun avec sa tête ; un lot recomposé garde sa place, prend sa tête la plus
récente et son unité est le commit de composition le plus récent. Le compteur
annonce donc séparément les unités de fusion encore rattachées à un lot et les
lots portés (#2387). C'est cette forme `#<lot> <tête>` que le moniteur lit dans
le corps d'un porteur `integration/**` pour reconnaître qu'un lot attend la
fusion du paquet (#2335) ; un porteur qui ne liste ses lots que par `#N` ne
déclare aucune unité et leurs alertes restent.

La convention vaut pour les unités composées à partir de maintenant : un paquet
déjà validé n'est pas réécrit pour ce seul métadonné, son manifeste et son run
restent la preuve de ses lots.

## Ce qu'il vérifie

1. **Admissions et lots prêts × manifeste.** Chaque lot attendu ou **prêt**
   doit avoir une ligne de composition, ou une exclusion consignée : ligne de
   la table de composition dont la note dit retrait/revert/report, table dont
   l'en-tête dit « retiré/revert », table sous un titre « Écartés » /
   « Retraits » / « Reportés » — avec un **motif non vide** — ou mention en
   prose sous ces titres. Sinon : *inclusion manquante* (défaut bloquant), et
   pour un lot prêt le rapport le dit « ni composé ni écarté au manifeste ».
   Une ligne « écartée » sans motif, ou une mention en prose qui ne fait pas
   du lot le sujet de la ligne (pas de mot-clé de retrait, ou lot cité au
   passage) est une **explication faible** : elle ne lève jamais une inclusion
   manquante ; elle n'explique qu'un contenu partiel ou absent d'un lot
   composé, affiché « à confirmer ». Seul le lot **en tête de ligne** est le
   sujet d'une exclusion : un id cité dans le motif (« `#500` dépend de
   `#600` ») reste une référence faible et ne lève pas l'omission de `#600`.
2. **Manifeste × arbre.** Pour chaque entrée de composition, le delta du commit
   (`commit^..commit`, premier parent) doit encore être présent à la tête,
   vérifié **ligne par ligne**, pas par ascendance : toutes les lignes ajoutées
   significatives sont là (`présent`, ou `adapté` quand le blob a changé),
   une partie (`partiel`), aucune (`absent`), ou le commit ne change aucun
   fichier (`vide` : remise fraîche déjà portée par une autre entrée, à lire
   avant le feu). Un changement de **mode seul** (bit exécutable) n'émet aucun
   hunk : c'est un contenu identique, jamais un binaire. Une ligne de retrait
   est consignée comme telle ; si le contenu d'un lot pourtant retiré
   réapparaît, un avertissement le dit.
3. **Cohérence de la liste.** Les lots composés hors liste attendue sont
   affichés (la liste peut être incomplète), ainsi que les lots attendus et
   expliqués.

Sortie : un rapport lisible ou `--json` (schéma `ameesh-paquet-inclusion/1`).
Codes de retour : `0` aucun défaut ; `1` une inclusion manquante, un lot prêt
ni composé ni écarté avec un motif, un contenu absent non expliqué ou un commit
introuvable, ou bien, sous `--strict`, un contenu partiel ou vide non
expliqué ; `2` entrée invalide (manifeste ou tête introuvable, fichier de lots
absent ou illisible, aucune source de liste prête, `--no-ready` refusé, ou
aucune source de liste attendue sans `--no-expected`).

## Ce qu'il ne prouve pas

- Un contenu **partiel** n'est pas une omission : c'est le plus souvent une
  union ou une adaptation relue (les compositions suivantes réécrivent des
  lignes). Un contenu **vide** (aucun delta au commit) n'est pas une omission
  non plus, mais il ne prouve rien : la remise est peut-être portée par une
  autre entrée. Les deux sont listés « à lire avant le feu » ; `--strict` les
  rend bloquants.
- Les fichiers **binaires** sont signalés sans comparaison de contenu ; un
  changement de mode seul n'est pas un binaire et ne crée pas de faux signal.
- Le contrôle ne juge ni les avis, ni les familles de relecture, ni les
  dépendances : il ne remplace pas la qualification du pilote (§12), il évite
  qu'une omission passe inaperçue. Il ne juge pas non plus la **sincérité** du
  motif d'un « écarté » : il exige qu'il existe, et que la liste calculée des
  lots prêts ait été confrontée au manifeste.

## Validation sur le paquet 11h (réel, lecture seule)

Passé sur le manifeste du paquet 11h (`reviews/2026-10-15-paquet-11h.md`,
73 entrées) à la tête publiée `6700d48f0`, avec les quinze admissions omises
en liste attendue :

- **15/15 omissions détectées**, aucune autre ;
- `présent` 39 entrées, `partiel` 26 (unions et adaptations), `absent` 1
  (adaptation `#1374 × #988`, expliquée par le retrait §4), `vide` 4 (remises
  fraîches sans delta), `retrait_consigné` 3 ;
- **aucun faux « contenu absent »** sur les lots composés connus.

## Reproduction

```sh
git -C <dépôt du paquet> show 6700d48f0:reviews/2026-10-15-paquet-11h.md > "$TMPDIR/paquet-11h.md"
printf '%s\n' 1124 1056 388 795 1466 1104 1411 1437 1463 414 1511 1253 1349 1396 1496 \
    > "$TMPDIR/attendus-11h.txt"
scripts/paquet-inclusion.py --repo <dépôt du paquet> --manifest "$TMPDIR/paquet-11h.md" \
    --head 6700d48f0 --expected-file "$TMPDIR/attendus-11h.txt"
```

Tests ciblés (dépôt git jetable, sans base) :

```sh
python3 -m unittest tests.test_1644_paquet_inclusion
```
