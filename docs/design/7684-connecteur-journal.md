# Connecteur `journal` et signature de décision sous grant — #7684

> Lot #7684 (manaty/ameesh#5877), suite de #7631. Série « avatars, charte et
> porte d'actions ». Quatre gestes restent réservés (production, dépense,
> publication, signature de décision) ; la porte les journalise, elle ne les
> bloque pas. Ce lot déclare le connecteur qui les sert, inscrit la preuve
> d'exécution, fait passer la signature d'une décision sous un grant réservé,
> et signale un déploiement de production fait hors de la porte.

## 1. Le connecteur `journal` (module `dev`)

- `ACTION_CONNECTORS` du module `dev` : `journal`, avec les opérations
  `prod-deploy` (irréversible), `spend` (coûteux), `publish`, `decision-sign`,
  `decision-sign-key` (irréversibles). La **classe appliquée** reste celle du
  canon (`action_classes` du projet, #7628) : le connecteur ne fait que la
  déclaration par défaut. Sans classe au canon, l'action est refusée.
- Il **n'agit pas** : le bureau fait le geste avec ses outils, puis
  `ameesh action execute <id> --proof run=… --proof sha=… --proof resource=…`
  remet la preuve au connecteur au lancement. La preuve n'entre ni dans
  l'empreinte de l'action ni dans `actions.args` (elle est postérieure à
  l'accord) ; elle est écrite en JSON canonique dans `actions.external_ref`.
- Preuve : objet (ou chaîne = `resource`) dont les clés sont `run`, `sha`,
  `resource`, `ref` ; valeurs texte non vides, 1024 caractères au plus, sans
  caractère de contrôle ; une clé inconnue est refusée. Sans preuve,
  l'exécution rend `failed` avec « nothing recorded » : un nouvel essai
  porteur de la preuve reste possible (le geste, lui, est déjà fait hors de
  la porte).
- `reconcile` ne lit rien : la porte n'a pas d'effet externe à retrouver.
  La déduplication est garantie par l'écriture dans la ligne d'action.
- La vue `ameesh action journal` montre, pour une action réservée : geste,
  opération, cible, proposant, approbateur, accord (`go` ou grant), reçu,
  état, preuve, grant et réservation.

## 2. Signature de décision sous grant (`decision-sign`)

Une décision signée par l'avatar sur un grant (`--accord grant:ID`) passe par
la porte, dans la transaction de la signature :

1. une **action** `journal decision-sign` est proposée, avec
   `args = {"decision": <id>, "answer": <empreinte de la réponse>}`,
   `proposed_by = agent:<avatar>`, `target = <destinataire de la décision>` ;
   le canon du projet fixe la classe ;
2. elle est **approuvée en `standing` sur le grant cité** (`--accord
   grant:ID`) : ce grant doit couvrir ce bénéficiaire, cette cible, ce canon,
   cette classe et cette opération ; aucun autre grant couvrant ne peut s'y
   substituer (la liaison échoue si le grant cité est absent, révoqué, échu
   ou hors portée) ;
3. `receipts.standing_cover(..., only_grant=ID)` **réserve ce grant** sur
   cette action ; la réservation est liée à l'empreinte de la réponse par
   l'`args` de l'action ;
4. l'approbation Ed25519 de la réponse est créée, consommée par la décision,
   et la décision est close — le tout dans la même transaction.

La preuve de la réponse porte
`standing = {grant, reservation, key}` (`key` = identifiant de l'action).
`verify_proof` recontrôle que l'action décrit bien **cette** décision et
**cette** empreinte, que son grant lié et le `meta.accord.grant` de
l'approbation sont **le grant cité**, et que la réservation est vivante sur
ce grant.
Un échec certain (clé illisible, signature refusée, décision close ailleurs)
libère la réservation ; hors transaction (exécuteur médié), l'action de porte
est annulée en plus. En transaction, le retour arrière fait le nettoyage.

Refus (tous rendus par la porte, pas par la CLI) : grant échu, révoqué,
épuisé (plafond), d'un autre bénéficiaire ou d'une autre cible, ou dont
l'authentificateur n'est pas du canon de l'action. Une décision sur la clé
owner ou la provenance (`decision-sign-key`) n'est **jamais** couverte par un
grant, même si le canon nomme l'opération : l'avatar ne signe pas la clé de
l'owner.

La vue `ameesh action journal` ne montre que l'entrée de la signature (grant,
réservation, opération, signataire), pas l'action de porte qui a porté la
réservation : ce serait un doublon. L'action reste visible dans
`ameesh action list` (état `approved`), comme trace de la porte.

## 3. Durée des grants : le canon tranche

`receipts.Policy.max_standing` garde son défaut de 30 jours quand **aucune**
règle du canon n'existe. Dès que `standing_approvals` a des règles,
`register_standing` porte le plafond à la plus longue durée autorisée par le
canon ; la règle applicable reste vérifiée opération par opération, la plus
stricte gagnant. Un grant de 44 jours (mandat de charte) est donc acceptable
si le canon le permet, et reste refusé si le canon est plus court. La CLI n'a
pas son mot.

## 4. Demande de grant et gestes de charte

`ameesh action standing request` compose la demande à signer (forme
`ameesh-receipt/1` avec `standing`) : approbateur, connecteur, opérations,
classe, échéance, montant/devise, bénéficiaire, cibles, ligne et version de
charte, cadre signé `{frame, version}`, accord éventuel. La sortie est le JSON
à faire signer par `ameesh receipt sign <demande> --key <clé 0600>
--credential-id <id>`, puis à enregistrer par `ameesh action standing
register --receipt <reçu> --limits …` (le canon applique son plafond).

Procédure des deux grants de charte (geste bureau/owner, non fait par ce lot) :

```
ameesh action standing request --approver human:smichea --connector journal \
  --operation decision-sign --class irreversible --until <date> \
  --beneficiary agent:bureau --target human:smichea \
  --charter-line "…" --charter-version "…" \
  --charter-frame sign-decisions --frame-version 1 \
  --accord bureau#<seq> --requested-by agent:bureau --out /run/…/grant.json
ameesh receipt sign /run/…/grant.json --key <clé owner 0600> --credential-id <id>
ameesh action standing register --receipt <reçu> --limits <canon>
# idem pour le grant de publication store (store-publication-eas)
```

## 5. Détection d'un déploiement de production hors porte

Le cœur d'ameesh ne tient pas de journal des déploiements de production
(canon §14 : staging seulement). Le module `dev` déclare
`RESERVED_GESTURES = ("prod-deploy",)` et sert
`detect_reserved_gesture("prod-deploy", …)` ; le registre refuse un détecteur
déclaré sans appelable, pour qu'un trou de détection ne soit pas silencieux.

Contrat du journal externe : fichier JSONL désigné par
`AMEESH_PROD_DEPLOY_JOURNAL`, une ligne par déploiement
`{"id": "…", "target": "…", "ref": "…", "ts": <epoch>, "proof": {…}}`.
Les lignes illisibles sont ignorées ; un fichier absent ou illisible ne
détecte rien (jamais une erreur). La vue compare les valeurs de la ligne
(`id`, `ref`, preuve) aux preuves des actions `prod-deploy` de la période :
une ligne sans action correspondante produit le signal
`prod_deploy_unrecorded`, avec cible, réf et horodatage. La cible seule ne
suffit pas à apparier deux gestes.

## 6. Reste à faire (hors de ce lot)

- Le bureau signe et enregistre effectivement les deux grants de charte
  (`sign-decisions`, `store-publication-eas`) avec la clé owner : geste
  d'exécution, pas de code.
- Le système de déploiement doit fournir le journal JSONL ; sans lui, la
  détection est vide (et le dit dans la doc, pas dans la vue).
- L'action de porte d'une signature de décision reste `approved` : un lot de
  suite peut la clore en `confirmed` avec la preuve de signature, ou la
  retirer de `action list` si le bruit gêne l'exploitation.
