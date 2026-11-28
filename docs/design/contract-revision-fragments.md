# Révisions du contrat module v1 : un fragment par lot, un numéro attribué au paquet

Décision du pilote ameesh sur la proposition de l'auditeur (analyse de nuit
#235877, point 1), lot #8395. Le constat : la révision du
[contrat module v1](../api/module-contract-v1.md) se décidait dans chaque lot.
Deux lots qui touchent le contrat se suivaient donc en série — #8340 a été
regelé cinq fois, avec quatre rebases ou renumérotations (1.11 → 1.12), et la
file a perdu 60 à 80 h-agent le 27/11 en partie pour cette raison.

Un lot écrit désormais un **fragment sans numéro** ; le composeur du paquet
attribue les révisions, plie les fragments et commite le résultat. Le numéro
n'est plus une décision de lot : il n'y a plus de conflit d'en-tête entre deux
lots.

## Ce qu'un lot qui change le contrat fait

1. Il crée `docs/api/module-contract-v1.d/<lot>.md` (`<lot>` = son identifiant
   numérique, par exemple `8395.md`).
2. La première ligne du fichier commence par `adds:`, suivie du texte de la
   révision à partir de « adds ». Le premier paragraphe (jusqu'à la première
   ligne vide) est la ligne de révision. Pour désigner la révision précédente,
   il écrit `{previous}` : le composeur le remplace par le numéro réellement
   attribué moins un. Une référence « additive to 1.N » écrite en clair est
   aussi réécrite vers cette révision précédente.
3. Après une ligne vide, il peut écrire les sections que la révision ajoute
   (`## …` et leur contenu). Elles sont insérées au paquet dans l'ordre des
   lots, avant l'ancre `<!-- contract-revision-sections: … -->` du contrat.
   Sans section, le fragment s'arrête au paragraphe `adds:`.
4. Il ne touche **ni l'en-tête** (`Contract revision: **1.N, date**`), **ni la
   liste des révisions**, **ni** la valeur `"contract"` de
   `module-contract-v1.surface.json`, **ni** `CONTRACT_VERSION` dans
   `src/ameesh/public/__init__.py`. Le composeur les aligne au paquet.
   Le reste du document (tableaux, sections existantes) reste modifiable
   directement par le lot ; c'est le bloc d'en-tête qui ne se sérialise plus.

Exemple de fragment :

```markdown
adds: `ameesh exemple --json` (#8395, manaty/ameesh#6630): the bounded
read-only view of the example; it is additive to {previous}.

## Exemple (#8395)

The object uses schema `ameesh-example/1`; a missing fact is `null`, never
permission.
```

Les tests de contrat (`tests/test_7665_module_contract.py` et apparentés,
surface JSON, couverture) acceptent un arbre de lot qui porte des fragments non
pliés : l'en-tête, la surface et `CONTRACT_VERSION` restent à la révision de la
base. Au paquet, l'arbre plié est vérifié : aucune révision en double, aucun
fragment restant, en-tête, surface et `CONTRACT_VERSION` d'accord.

## Ce que le composeur fait

`scripts/paquet-auto.py`, après les unités de fusion et avant les contrôles
locaux, exécute un pliage mécanique (`scripts/paquet-auto.py` s'appuie sur
`ameesh.contract_fragments`) :

- il lit les fragments des lots composés dans l'ordre du manifeste (l'ordre des
  unités) ; un fragment dont le lot n'est pas composé, un nom de fichier qui
  n'est pas `<lot>.md`, un fragment mal formé, ou un lot qui porte à la fois un
  fragment et une révision écrite à l'ancienne refusent la composition en
  nommant le lot ;
- il attribue les numéros à partir de la révision de l'en-tête de la base, en
  continuant `1.N` ; il date l'en-tête du jour de composition ;
- il insère chaque ligne `Revision 1.N adds …` après la dernière révision
  existante ; la liste des révisions est un bloc de paragraphes consécutifs
  terminé par une ligne vide (le paragraphe de mapping qui suit reste intact),
  et les sections des fragments vont avant l'ancre
  `<!-- contract-revision-sections: … -->` ;
- il met à jour `"contract"` dans la surface et `CONTRACT_VERSION`, puis
  supprime les fragments ;
- il crée **un commit de composition au contenu déterministe** (même arbre,
  même date de composition ⇒ même contenu ; son SHA dépend aussi des dates
  auteur/committer posées par git), dont le SHA et le tableau révision → lot
  sont cités dans la section `## Contrat module v1` du manifeste. Le manifeste
  de paquet est la preuve : le contrôle d'inclusion lit ce commit, n'exempte
  que les lignes que le pliage a effectivement retirées ou réécrites (les
  autres lignes du lot restent exigées à la tête), vérifie que le contenu des
  fragments s'y retrouve et refuse un commit cité qui touche autre chose que
  le contrat, sa surface, `CONTRACT_VERSION` et les fragments cités.
- la porte du véhicule (#8417) relit le même commit : vérifié, il n'est pas un
  delta propre du paquet (comme une fusion mécanique, aucun avis de véhicule) ;
  non cité, non vérifiable ou touchant un autre chemin, il reste un delta
  propre, avis exigé.

Le pliage est idempotent : un arbre déjà plié ne porte plus de fragment, et le
composeur ne retouche pas son en-tête.

## Lots en vol à l'ancienne manière

Un lot déjà gelé qui a écrit son numéro (`Revision 1.12 adds …` et l'en-tête)
reste accepté pendant la transition. Le composeur retrouve le paragraphe ajouté
par l'unité dans son diff, le renumérote dans l'ordre du manifeste et réécrit
sa référence « additive to … » vers la révision qui le précède réellement. Le
lot n'est **pas regelé** : c'est une adaptation mécanique, sans avis
(canon §2).

## Fichiers

- `docs/api/module-contract-v1.d/<lot>.md` : le fragment.
- `docs/api/module-contract-v1.d/README` : cette convention, en bref.
- `src/ameesh/contract_fragments.py` : lecture, validation, pliage.
- `scripts/paquet-auto.py` : le pliage au paquet et sa citation au manifeste.
- `scripts/paquet-inclusion.py` : lit le commit de pliage du manifeste et
  n'accuse pas un lot d'avoir perdu ses lignes de contrat repliées.
- `src/ameesh/review_contract_fold.py` : la vérification du commit de pliage,
  partagée par `paquet-inclusion` et `packet_reviews.own_changes` (#8417).
