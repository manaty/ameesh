# Contrôle d'inclusion d'un paquet §12 — `scripts/paquet-inclusion.py` (#1644)

Ce document décrit le contrôle à passer **avant la CI complète et le feu** d'un
paquet d'intégration (canon `manaty/home`, `processus/lots.md` §12) : vérifier
qu'aucun lot admis ou routé vers la coupure n'a été silencieusement omis à la
composition, et que le contenu des lots composés est réellement dans l'arbre de
la tête.

## Le constat

Au paquet ameesh de 11h (15/10/2026), quinze lots admis ou routés par le pilote
ont été omis à la composition, sans note de retrait ni revert : #1124, #1056,
#388, #795, #1466, #1104, #1411, #1437, #1463, #414, #1511, #1253, #1349,
#1396, #1496. Le manifeste ne le disait pas, et l'ascendance ne le dit pas non
plus : après un revert, la tête du lot reste ancêtre de la tête du paquet
(#1638). Il faut donc confronter **les admissions**, **les lignes du manifeste**
et **le contenu de l'arbre**.

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
  Sans aucune de ces sources, le contrôle s'arrête en `2` : une sortie `0`
  laisserait croire que le contrôle « admissions × manifeste » a eu lieu.
- `--no-expected` : assume explicitement un contrôle sans liste attendue ; le
  rapport dit alors « admissions × manifeste NON effectué » et seuls les
  contrôles « manifeste × arbre » et « cohérence » sont rendus.
- `--excused` / `--excused-file` : les lots dont le report ou le retrait a été
  décidé hors manifeste (par exemple un `REPORT 23h` du pilote).
- `--repo` (défaut `.`), `--head` (SHA ou réf), `--json`, `--strict`,
  `--no-expected`.

Le script est en **lecture seule** : aucun réseau, aucune écriture dans le
dépôt. Il lit le manifeste et l'historique git ; les commits cités par le
manifeste doivent être présents localement (sinon `git fetch` d'abord).

## Ce qu'il vérifie

1. **Admissions × manifeste.** Chaque lot attendu doit avoir une ligne de
   composition, ou une exclusion consignée : ligne de la table de composition
   dont la note dit retrait/revert/report, table dont l'en-tête dit
   « retiré/revert », ou mention en prose sous un titre « Retrait »/« Report ».
   Sinon : *inclusion manquante* (défaut bloquant). Une mention en prose qui ne
   retire pas le lot (pas de mot-clé de retrait, ou lot qui n'est pas le sujet
   de la ligne) est une **explication faible** : elle ne lève jamais une
   inclusion manquante ; elle n'explique qu'un contenu partiel ou absent d'un
   lot composé, affiché « à confirmer ».
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
Codes de retour : `0` aucun défaut ; `1` une inclusion manquante, un contenu
absent non expliqué ou un commit introuvable, ou bien, sous `--strict`, un
contenu partiel ou vide non expliqué ; `2` entrée invalide (manifeste ou tête
introuvable, ou aucune liste attendue sans `--no-expected`).

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
  qu'une omission passe inaperçue.

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
