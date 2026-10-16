# Candidature et admission au train

Le composeur provisoire et le train n'ont pas encore le même point d'entrée.
`~/Work/outils/compose-paquet.py` lit directement les lots `qa` par SQL ;
`train_admissions.Admissions` lit les générations inscrites dans la file.
Le contrat de candidature existant est `plan_git.freeze_candidate`, livré
par #1756 au commit `53382b8a7a023ee7294d8a87add28afa4b582370`.
Ce lecteur doit servir à l'inventaire des candidatures ; il ne constitue
ni une preuve historique de relecture, ni une écriture de gel ou de file.

Les cas signalés par mesh-design le **16 octobre 2026 à 02:44**, courrier
**#69382** et note du parent #1606, donnent les frontières suivantes :

| Cas | Diagnostic et comportement attendu | Couverture consommateur |
|---|---|---|
| #753 : ancien gel `d4c2bfc2`, GO sur `b18f40ad` | #1756 expose `declared-freeze-stale` et le candidat du verdict. Le train confronte toujours le gel de la génération admise à la branche publiée ; un refus nomme chaque lot, sa branche, le gel et la tête publiée. | Probe interbranches, puis test Git de deux branches déplacées. |
| #1734 : classe légère sans avis | La politique versionnée peut exiger zéro avis préalable. L'absence d'avis n'enlève pas la candidature. Les auteurs historiques restent connus et un blocage explicite reste bloquant. | Parcours politique → admission SQL → historique Git avec zéro avis, plus refus sensible et blocage léger. |
| #1651 : résolution qualifiée `0cd883bc8cdd`, tête brute en conflit | Le port explicite consomme la remise qualifiée sur sa base exacte et conserve son parent. Une remise incompatible est reportée avec son motif et ses dépendants ; les autres membres peuvent composer leur paquet. | Git réel : tête brute conflictuelle, reçu accepté, mauvais contexte refusé, reprise du même plan et maintien du parent. La remise réelle de #1651 reste chez le composeur. |

Les états de #753 et #1734 ont été régularisés par le pilote ; ces numéros
désignent les régressions à prévenir, pas un relevé courant de leur admission.
La matrice ne modifie pas le paquet en cours ni les transports du composeur.

## Raccord de #1756

Le probe charge uniquement l'objet `plan_git.py` du commit fournisseur exact,
sans fusion de branche. Il éprouve quatre parcours de ce consommateur :
gel déclaré périmé, gel déclaré absent, léger sans avis et nouveau gel après
un avis sur une autre tête. Les deux premiers sont visibles par le lecteur
de candidature, mais ne peuvent remplacer implicitement une génération de
file ni les contrôles du lecteur historique. Le probe ne rejoue pas les
tests du fournisseur.

```sh
PYTHONPATH=src:. .venv/bin/python reviews/1467-selection-contract/probe.py \
  --supplier . --commit 53382b8a7a023ee7294d8a87add28afa4b582370
```

Git est réel ; jalons, stockage et provenance sont des fixtures explicites.
`train_candidates.Inventory` raccorde désormais l'inventaire `qa` à ce
lecteur explicite. Il lit tous les lots `qa` et tous leurs jalons sans limite
de page, filtre le projet et conserve les diagnostics du fournisseur. Un lot
sans branche, sur une autre cible ou de release reste nommé avec sa cause.
Le résultat porte le candidat et son origine ; il ne crée ni gel, ni génération
de file, ni preuve de relecture ou de CI. Le lecteur de branche publiée et
l'admission historique restent des étapes distinctes.

```python
inventory = train_candidates.Inventory(
    db, project, candidate=qualified_plan_git.freeze_candidate
).read()
```

Le fournisseur est obligatoire, sans repli sur une nouvelle dérivation locale
du gel. Deux parcours du probe ci-dessus consomment son objet exact à travers
l'inventaire, avec Git réel : candidat déclaré sans écriture et candidat issu
d'un avis sans transformation en gel. Quatre frontières isolées et une lecture
SQL de plus de 200 lots couvrent les diagnostics, le périmètre et l'absence
de troncature.

Validation de ce raccord : quatre contrôles isolés et six parcours du probe
verts ; la lecture SQL 205 lots contre une page de 200 est verte avec psycopg
et psql sous `ameesh heavy`. Le contrat médié est inchangé et ses 19 contrôles
restent verts. Aucun lecteur de candidature ni profil n'est activé.

### Frontière d'inscription illustrée par Nexlink #2005

Le courrier **#80142 de codex3, 16 octobre 2026 à 12:22 Paris**, décrit le gel
`5dfba839f55641ec66f736ab3dbe7751f1e5181d`, ses avis exacts **8770/8772** et le
refus de `work queue add` faute de CI individuelle. Cette commande passe par
`merge_queue.readiness`, qui exige encore une CI de lot avant `queue_add`.
À l'inverse, `Admissions.qualify(..., mode="packet")` ne lit aucune CI
individuelle, mais ne voit que les générations déjà inscrites dans cette file.

L'inscription explicite d'un candidat qualifié pour
le train dans la file persistante doit valider la branche publiée, les
avis historiques et la politique, puis recevoir sa génération du stockage
réel. L'inventaire ne fabrique pas cette génération et ne contourne pas la
porte individuelle. La CI complète du paquet, sur son arbre exact et avec un
manifeste qui couvre ce membre, reste due avant promotion ; l'absence de CI
individuelle n'est jamais exposée comme un statut vert. Le paquet 26 et la
demande d'admission manuelle de codex3 à impl-02 restent hors de ce raccord.

### Qualification préalable sans génération

`Preparation` relie maintenant le candidat déclaré à la tête publiée, à la
politique versionnée et à la lecture historique complète. Il relit le lot
`qa` et ses bornes projet/branche/cible, puis fetch la base et la branche dans
le clone privé. Un SHA abrégé est résolu dans ces objets ; une branche déplacée
ou supprimée reste refusée même si le clone possède encore l'ancien commit.

```python
prepared = train_candidates.Preparation(
    db, project, repository=repository, history=recorded_history,
    policy=qualified_review_policy,
).prepare(candidate, current_base)
```

Le sujet rendu ne porte aucune génération. `Recorded.qualify_subject` partage
les contrôles historiques de `Recorded.qualify`, dont le dernier gel exact,
les auteurs et les avis indépendants ; le second conserve sa preuve d'admission
existante avec génération. Un candidat dérivé d'un verdict reste visible dans
l'inventaire, sans pouvoir devenir un gel déclaré par ce lecteur.

Huit cas Git réel/stockage simulé couvrent succès, SHA abrégé, branche distante
déplacée/supprimée, sujet modifié, base différente, gel renouvelé, preuves
manquantes, léger sans avis, blocage et release. Le probe fournisseur exact
ajoute le parcours inventaire → préparation. Un cas Git/SQL réel prouve que
la préparation laisse la file vide et les jalons inchangés, puis alloue
explicitement une génération dans le stockage de test et la qualifie en mode
`packet`, sans CI individuelle. Aucun profil ou écrivain du runner n'est activé.

### Inscription explicite et reprise

Source : **mesh-design, 16 octobre 2026 à 13:16**, réponse aux courriers
80193/80993. Le pilote qualifie la préparation de l'inscription packet dans
la file existante, distincte de `readiness` individuel, après les contrôles
branche/gel natif/provenance/historique/politique. Il exige le `frozen_id`
attendu, une reprise idempotente et une relecture des preuves et de la vraie
génération avant effets ; aucune file réelle ni le paquet 26 ne sont concernés.

```python
result = train_candidates.Enrollment(
    preparation, admissions, initial=qualified_initial_queue_history,
).enroll(
    candidate, current_base, actor=bound_actor,
)
```

`Enrollment` réalise la préparation, appelle `queue_add(..., packet=snapshot)`
puis refetch la branche et appelle `Admissions.qualify(..., "packet")`. Le
stockage verrouille le lot, compare son état `qa`, son projet, sa branche,
sa cible, son type et le dernier gel natif, puis insère sans remplacer une
ligne existante. Une reprise ne rend que sa génération exacte encore `queued` ;
elle n'alloue pas de nouveau numéro et n'ajoute pas d'événement. Une ligne
d'une autre admission, en cours d'atterrissage ou éjectée reste intacte.

La suppression physique de `queue_remove` nécessite une trace durable pour
ne pas transformer un retrait en nouvelle candidature. Les reçus
(`kind=packet|removed`, `frozen_id`, `generation`) sont inscrits dans
`integration_train_queue_receipts`, ajoutée à la migration 0442 encore en
préparation, dans la transaction de l'allocation ou du retrait. Le retrait
prend le même verrou de lot avant la ligne de file et conserve le `frozen_id`
de la ligne supprimée, même si le lot a depuis été regelé. Ces traces servent
uniquement à conserver la génération d'une reprise ou à refuser une réadmission ;
elles ne certifient aucun avis ni CI. Aucune opération d'API n'est ajoutée.
Un retrait individuel est également conservé, mais l'inscription individuelle
explicite reste possible.

Cette séparation corrige le signalement de **dev7, 16 octobre à 13:38** :
une note libre `ameesh-queue-receipt/1` pouvait auparavant forger une reprise.
Le lecteur ignore désormais toutes ces notes, y compris celles écrites par
un ancien binaire ou un autre chemin du journal ; aucune n'est importée comme
preuve typée. Les chemins `work note`, `_event` et `queue_update(note=...)`
ne peuvent créer de reçu typé, quel que soit l'acteur du journal. Un ancien
binaire qui écrit ce préfixe reste dans ce même journal ignoré.
Deux témoins SQL ont reproduit les défauts avant correction (fausse reprise,
retrait F1 après regel F2). Après correction, les 34 tests d'admission et
trois contrôles de compatibilité de la file passent avec psycopg et psql ;
les 19 contrôles du contrat médié passent également. Un troisième témoin
vérifie qu'une fausse note de retrait ne supplante pas une allocation typée.

Le cadrage du pilote du **16 octobre à 14:16** précise la frontière de
confiance : l'adaptateur n'écrit les reçus que dans `queue_add(packet=...)`
et `queue_remove`, depuis l'identité de la ligne allouée ou supprimée sous
verrou, dans leur transaction. Il n'expose aucun écrivain générique de reçu ;
le contrat médié refuse `work.queue_add`, `work.queue_remove` et
`work.queue_update` et n'ajoute aucune opération. `work.note` reste admise,
mais n'écrit que le journal libre, jamais un reçu.
Ce contrôle des chemins applicatifs ne constitue pas une séparation des
droits SQL. Le rôle exécuteur existant reçoit `SELECT, INSERT, UPDATE, DELETE`
sur toutes les tables et les futures tables du schéma dans
[`role-executor.sql`](../../deploy/sql/role-executor.sql), hors exceptions
explicites qui ne comprennent pas ces reçus. Un détenteur de ce rôle ou le
propriétaire peut donc les forger par SQL direct. La migration 0442 ne change
pas ce modèle de confiance et n'offre aucune résistance à cet acteur.
Une séparation de rôles nécessiterait un changement distinct de ce contrat,
pas une propriété supposée de cette table.

Les témoins complémentaires provoquent une erreur SQL réelle à l'insertion
de chacun des deux types de reçus, après modification de file et écriture du
journal : une seconde connexion retrouve l'état antérieur complet, puis un
nouvel essai écrit exactement le reçu attendu. Un rollback après retrait et
reçu rétablit également la ligne et permet sa reprise exacte. Le cas F1/F2
vérifie sur la seconde connexion que seul F1 porte le reçu de retrait.
Validation de ce complément : 14 témoins `PacketAllocationTest` verts avec
psycopg (9,7 s) puis psql, et les 19 contrôles du contrat médié verts (33 tests
en 69,8 s pour cette seconde commande), sous `ameesh heavy`. Les deux erreurs
SQL sont provoquées par une contrainte temporaire du schéma jetable ; elles
ne changent aucun rôle ni table réelle. Aucune suite générale n'est rejouée.

Un ancien `queue_remove` pouvait avoir supprimé la ligne avant l'introduction
de ces traces. L'absence de ligne et de reçu ne prouve donc pas une première
inscription. Le lecteur explicite `initial(db, subject=...)` doit qualifier
ce fait depuis ses sources originales ; seul `True` permet une insertion
initiale via `packet_initial`. Aucun lecteur réel n'est choisi par défaut.
La seule absence de ligne ou une prose libre ne suffit pas ; la qualification
doit aussi couvrir la transition des anciens écrivains qui ne traçaient pas
leurs retraits.
Sans source qualifiée, `queue-history-unknown` reporte le membre. La reprise
d'une allocation déjà tracée n'a pas besoin de ce bootstrap, et même un
lecteur favorable ne peut effacer un retrait tracé. Les tests utilisent une
qualification initiale simulée sur leur schéma jetable ; ils ne qualifient
pas l'historique réel de #2005. Dev7 confirme le 16 octobre à 13:38 qu'aucune
source existante ne permet ce bootstrap. Sa proposition de déclencheur de retrait
et de coupure pour les gels futurs reste distincte, sans lecteur ajouté ici.

`enroll_many` renvoie séparément les admissions et les reports nommés par lot,
et poursuit les candidatures indépendantes. Un refus après allocation conserve
sa ligne pour les contrôles du train ; il ne supprime ni ne réinscrit une
génération concurrente.

Les témoins SQL utilisent deux connexions pour le regel au même SHA, le
retrait et l'allocation concurrente. Ils couvrent également rollback, refus
d'un sujet changé, conservation des générations existantes et reprise après
suppression. Les parcours Git/SQL vérifient la relecture après allocation :
un retrait ou un avis bloquant nouvellement commis produit un refus, sans
effet de publication. Le runtime conserve ses propres revalidations avant
publication et promotion. Le raccord à un profil actif et les fournisseurs
échéance canon / suites et événements Nexlink restent distincts.

Le témoin de reprise consommateur supprime aussi le clone privé puis reconstruit
tous les adaptateurs avec une seconde connexion SQL. Une qualification initiale
inconnue conserve alors la génération tracée, sans événement ni numéro nouveau
et sans CI individuelle. Un retrait commis ensuite sur la première connexion
reste un report nommé sur la seconde ; aucune ligne n'est recréée.

## Port de transport qualifié

Source de ce raccord : cadrage mesh-design **#69852 du 16 octobre à 03:12**,
persisté dans #1467. Aucun nouveau catalogue, formulaire de GO ou gel n'est
créé. Le fournisseur explicite lit les qualifications déjà acquises dans le
manifeste/journal du composeur ; une note ou une ref seule reste insuffisante.
Les résolutions de paire de `land.merge` ne sont pas ce contrat.

`Repository(..., transports=reader)` reçoit un lecteur de confiance appelé
avec `source`, `target`, `row`, `base`, `path` et `expected`. `base` est la
tête intermédiaire exacte ; `expected` est le reçu persisté lors d'une
revalidation, sinon `None`. `None` en retour signifie aucune remise disponible.
Le lecteur doit vérifier la qualification et son maintien depuis les preuves
existantes ; le consommateur vérifie ensuite les objets Git et le delta.
La forme `ameesh-train-transport/1` comprend :

- `source`, `target`, `admission` : l'identité complète de génération/gel,
  inchangée et séparée de la remise ;
- `base`, `head`, `tree` : commits et arbre complets ; la base doit être le
  premier parent de la remise, et le gel source doit rester son ancêtre ;
- `delta` : SHA-256 des enregistrements Git bruts de `base..head`, chemins
  triés et délimités par NUL, modes et identifiants complets des objets. Ils
  lient aussi les contenus binaires, sans dépendre des couleurs, préfixes,
  algorithmes de hunks ou de l'ordre local. Aucun renommage, outil externe,
  textconv ou remplacement d'objet ;
- `ref` : emplacement sous `refs/ameesh/resolutions/<lot>/`, utilisé pour
  récupérer l'objet absent dans le clone privé, jamais pour déduire un avis ;
- `qualification` : `kind=mechanical|adapted` et `references` structurées
  vers des jalons, courriers, événements existants (`kind` et `id`), ou un
  manifeste Git (`kind=manifest`, `commit`, `path`). Le lecteur résout ces
  références ; leur forme seule ne prouve pas leur autorité.

Le reçu est enregistré dans `plan.transports`, en dehors de `plan.proof`
qui conserve l'admission historique. L'empreinte du plan et l'autorité de
promotion couvrent ce champ. Le commit de paquet garde les transports dans
ses parents et lie leur empreinte dans son message. Les commits intermédiaires
propres ont des identités déterministes : une reconstruction peut revalider
la même base, même après perte du clone et changement d'horloge.

Le fournisseur est relu avant les effets. Une remise retirée ou remplacée
refuse la promotion ; sa présence dans un plan ne constitue pas une nouvelle
autorisation. Une remise mécanique conserve ses qualifications acquises,
sans re-gel ni demande de nouvel avis. Une remise adaptée conserve les
références qui qualifiaient déjà son contenu sur cette base.

Pendant la composition, une remise incompatible ou un conflit brut produit
un report nommé dans `state.selection` et `state.deferred`, avec ses dépendants.
La file d'admission n'est pas éjectée et aucun lot n'est déclaré fautif.
La composition des membres restants précède l'unique CI ; si tous sont reportés,
le résultat est un paquet vide avec ses motifs et aucun effet distant.

Le lecteur d'incidents accepte les parents supplémentaires vérifiés et utilise
le diff qualifié pour les fichiers d'un membre transporté, en conservant son
lot et son gel source. Il n'attribue pas une cause ou un auteur à la résolution.

`Passage(..., transports=reader)` lie le lecteur à la connexion fraîche du
passage (`reader(db, **context)`). Aucun lecteur réel n'est choisi par défaut.
Les transports courants restent chez impl01 ; le format de leur manifeste
est précisé par sa réponse du **16 octobre à 03:40** : aucun fichier de reçu
structuré dédié n'est versionné aujourd'hui. Les faits Git sont portés par
les commits à deux parents (base de chaîne, remise auteur), leurs messages et
les refs `refs/ameesh/resolutions/`. Le journal courant
`.qa/paquet-23h/MANIFESTE-chaine-paquet-23h-suivant.md` n'est pas versionné ;
ses lignes sont reprises à la coupure dans
`reviews/integration-paquet-h-<timestamp>.md`. Les références de relecture ne
figurent pas dans les commits ou les refs de transport.

### Durable journal reader

The pilot's lot event **30608, 16 October 2026 at 04:14 Paris** selects the
existing lot journal as the reconstructible index. `train_transport_journal`
implements this adapter without a new table or catalogue:

```python
train_transport_journal.import_ref(
    db, project, private_repository, published_ref,
    lot=lot_id, source_head=source_frozen_sha,
    qualification={"kind": "mechanical", "references": original_references},
    actor=importer,
)
reader = train_transport_journal.Recorded(
    project, resolve=train_transport_journal.Normalized()
)
passage = Passage(..., transports=reader)
```

Import fetches the published ref, reads its two parents, message, tree and
exact delta, and resolves the original event/milestone/mail IDs or versioned
manifest. The index retains source references, authors and identities; it
does not duplicate private mail contents. `work.note(..., once=True)` serializes identical imports on the
existing lot row; ordinary journal notes retain their existing behavior.
The index contains no admission generation or freeze ID. Only the receipt
selected into `plan.transports` binds the current generation and freeze.

At every read the adapter resolves the original sources again, checks their
original authors and unchanged facts, and checks the published ref. The
explicit `resolve` port receives these sources and complete lot event and
milestone histories on the current connection. It must qualify their actual
authority, exact scope and continued applicability, returning the same facts
and original references with `active=True`. A projection, ref name or free
GO prose alone cannot supply this answer. There is no default interpreter or
authority assigned to the importer. Existing mechanical qualifications are
preserved without new votes or freezes.

Missing/changed originals, missing resolvers, ambiguous index entries and
stale contexts produce named member deferrals. During CI, withdrawal refuses
promotion and preserves the existing plan and single CI attempt.

Admission proof failures also name their exact queue identity. While composing,
the engine defers that member and its open dependents, then qualifies the
independent remainder before publication. An unknown author, missing review or
stale freeze does not grant admission and does not eject the queue entry.
After publication the same failure drains the original immutable packet;
it cannot shrink its members or launch another CI. The consumer checks this
boundary with real Git/SQL and an explicit CI/provenance fixture.

The pilot supplied `ameesh-transport-qualification/1` on **16 October 2026,
05:10 Paris** (lot events 30879/30881/30884). `Normalized` recognizes only
that exact mechanical format: `schema`, `work_item_id`, `frozen_sha`, `base`,
`head`, `kind`, `state`, `source_reference`. The original qualification and
its normalization must belong to the same lot and database author. The last
applicable structured state, ordered by database time and event ID, maintains
or withdraws the exact frozen SHA/base/head. A historical freeze cannot be
rebound to the current one. Unknown fields, duplicate JSON keys and foreign
authors or scopes supply no authority. Adapted qualifications still require
their separate explicit resolver. No live Passage or profile is installed.

## Validation du port

Seven normalization boundary checks cover the original database author and
lot, exact bounds, latest maintained/withdrawn state, historical freezes,
unrecognized prose/JSON, duplicate keys and strict reference IDs. A dedicated
Passage check uses this resolver through import, persisted plan, lost clone
and a fresh SQL connection; CI and provenance remain explicit fixtures.

The source adapter adds nine targeted checks, green with both PostgreSQL
drivers, plus the 19 executor contract checks. They cover import to persisted
plan, restart with a fresh connection and lost clone, withdrawal, ref movement,
missing originals/resolver, changed authors, ambiguous sources, current
generation snapshots, stable mail delivery metadata, concurrent note deduplication
and preservation of ordinary notes and session fencing. Git and SQL are real;
the semantic qualification resolver, CI and provenance remain explicit fixtures.

Les vérifications ciblées couvrent 27 témoins moteur/Git existants, 7 lecteurs
d'incidents et 15 témoins de transport/sélection. Les nouveaux parcours couvrent
aussi un préfixe propre avant le transport, l'indépendance de l'empreinte aux
réglages locaux de diff et le report nommé d'une lecture d'objet impossible.
Le témoin d'affichage a été rouge avant la canonicalisation du delta, puis vert.

Les 13 témoins Runtime SQL et le nouveau parcours `Passage` sont verts avec
psycopg. Le nouveau parcours est également vert avec psql : plan persistant,
connexion fraîche, clone perdu, même tête promue, un seul essai et conservation
du gel source. Les commandes SQL passent par `ameesh heavy`.

Git et SQL sont réels ; CI, provenance et qualification du composeur sont des
fixtures explicites. Ces preuves ne constituent pas une coupure réelle sans
agent, une qualification des fournisseurs Nexlink ou une activation.
