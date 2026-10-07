# V1 — autorité du propriétaire, mesh list, work_items (points 3 à 5)

> **Avertissement — document du banc, dépassé pour l'autorité humaine.** Ce
> document décrit le modèle d'autorité du banc (clé Ed25519 « propriétaire »).
> La conception v1 l'a remplacé : l'autorité d'un humain est un **reçu**
> `ameesh-receipt/1` signé hors de portée des agents, sur son téléphone, par
> **ameesh-approve** ; la **cérémonie de clé Ed25519 sur le PC est abandonnée**
> ([décision 0012](design/decisions/0012-autorite-par-ameesh-approve.md)).
> Les clés Ed25519 restent pour la provenance des agents (R13) et les tests,
> jamais comme autorité humaine. Référence : [`docs/design/`](design/index.md) —
> [spécification](design/specification.md) §7 (porte), §8 (reçus), §9
> (ameesh-approve) — et la bascule réécrite, [`BASCULE.md`](BASCULE.md).
> Le reste (mesh list, work_items, import/export v0) demeure valable.

Suite de [`V1-MAILBOX-RUNNER.md`](V1-MAILBOX-RUNNER.md) (points 1 et 2 : boîte
aux lettres Postgres et exécuteur par machine). Toujours en banc : aucun agent
réel n'est branché, la bascule est décrite dans [`BASCULE.md`](BASCULE.md).

---

## 1. Autorité du propriétaire (R4, spec §5 point 3)

### Le modèle

**L'autorité ne se déduit jamais d'un texte.** Un message ou une approbation ne
porte l'autorité du propriétaire que si trois conditions sont réunies :

1. **clé propriétaire connue** — l'expéditeur a une clé *publique* enregistrée
   dans `agent_registry.public_key` avec `key_role = 'owner'`, et cette clé
   n'est pas révoquée (`key_revoked_at IS NULL`). La clé privée ne touche
   jamais la base ;
2. **contenu couvert** — la signature Ed25519 vérifie sur un payload canonique
   qui *est exactement* le contenu stocké :
   `agent-mesh/v1|message|{"expires":…,"from":…,"kind":"message","nonce":…,"text":…,"to":…,"ts":…,"v":1}`
   (JSON trié, sans espaces). Le vérificateur reconstruit ce payload depuis la
   ligne et exige l'égalité avec `signed_payload`, puis vérifie la signature :
   modifier le corps, le destinataire, l'horodatage ou le nonce invalide la
   preuve ;
3. **échéance** — `signature_expires_at` est dans le futur, **et c'est
   exactement l'échéance signée** : la colonne et le payload doivent coïncider
   (au microsecondes près). Modifier `expires_at` en base ne ressuscite donc
   pas une signature expirée ; il en va de même de `created_at` pour les
   approbations. Une signature expirée est signalée comme telle, jamais
   ignorée.

### Deux rôles de clés : autorité et provenance

`agent_registry.key_role` distingue deux choses qu'il ne faut pas confondre :

| rôle | ce que la clé prouve | ce qu'elle permet |
|---|---|---|
| `owner` | l'autorité du propriétaire (sa clé privée ne circule pas) | messages **et** approbations |
| `agent` (défaut) | la provenance : ce message est bien de cet agent, contenu et échéance couverts | rien de plus : **aucune** autorité propriétaire, **aucune** approbation |

Le défaut est `agent` (moindre privilège) : `key register --role owner` est un
acte explicite. C'est ce qui évite qu'enregistrer la clé d'un agent pour
signer son courrier lui donne, par effet de bord, l'autorité du propriétaire.

Les hooks distinguent donc **quatre états** :

* non signé → message d'agent, JSON identique à la v0 ;
* signé par une clé `agent` → `[signé par un agent, authentique, sans autorité propriétaire, expire …]` ;
* signé par une clé `owner` → `[autorité du propriétaire PROUVÉE, expire …]` ;
* signature invalide ou expirée → `[⚠ signature NON valide : …]`, jamais ignorée.

Les approbations (`mesh_approvals`) suivent le même modèle avec, en plus, une
`action`, une empreinte d'artefact (`sha256` hex) et une décision
(`approved`/`rejected`). La **consommation unique** n'est pas une colonne de la
ligne mais une table à part, `mesh_consumed_nonces`, clé primaire
`(approver, nonce)` : réinsérer le même reçu signé sous un nouvel id — ou
supprimer la ligne consommée puis la recréer — ne permet pas de le rejouer, et
un index unique sur `(approver, nonce)` bloque la copie directe. **À la mise à
niveau** d'un schéma antérieur, la migration `0007` rétro-remplit ce ledger
depuis les `consumed_at` historiques : une approbation consommée avant 0006 ne
redevient pas consommable après migration (0006 reste immuable). `verify_approval`
refuse toute clé qui n'est pas `owner`, y compris si la ligne a été insérée en
base à la main.

### Ed25519 : trois implémentations, une seule interface

`signing.py` expose :

| backend | disponibilité | usage |
|---|---|---|
| `pure` | toujours (stdlib `hashlib` seulement) | défaut, vérification sans aucune dépendance |
| `cryptography` | si importable | accélération, contre-vérification croisée |
| `nacl` | si importable | idem |

`AMEESH_SIGNING_BACKEND=auto|pure|cryptography|nacl` choisit. L'implémentation
pure est la RFC 8032 ; elle est validée par :

* les **vecteurs de test RFC 8032 §7.1** (dont le message de 1024 octets) ;
* un **croisement avec OpenSSL** : OpenSSL vérifie nos signatures et nous
  vérifions les siennes (test `test_croisement_openssl`) ;
* un **croisement entre backends** quand plusieurs sont installés.

Concrètement, un aller-retour signe en ~12 ms et vérifie en ~28 ms en pur
Python — négligeable devant un tour d'agent.

### Clés

Format PEM-like, lisible et sans ambiguïté (`AGENT-MESH PUBLIC/PRIVATE KEY`,
base64 du germe ou de la clé de 32 octets). Règles :

* `agent-mesh key generate` **refuse** de s'exécuter sans `--i-am-the-owner` :
  aucun agent ne génère ni ne détient une clé de rôle `owner`. (La cérémonie de
  clé du propriétaire prévue par l'ancienne bascule est abandonnée en v1 :
  décision 0012, voir l'avertissement en tête) ;
* la clé privée est écrite en `0600`, refusée à la lecture si ses permissions
  sont trop ouvertes, jamais journalisée, jamais en base, jamais dans un message ;
* `agent-mesh key register <agent> --public-key FICHIER [--role owner|agent]`
  n'accepte qu'une clé publique (une clé privée est rejetée) ; `key revoke`
  invalide immédiatement toutes les signatures de cet agent. Le rôle par défaut
  est `agent` ; passer `--role owner` délègue l'autorité du propriétaire et
  affiche un avertissement.

### Ce que ça donne dans un hook

Sans message signé, le JSON du hook est **identique à celui de la v0** (test
d'égalité avec `agent-mail.v0.py`). Dès qu'un message porte une preuve valide,
l'en-tête change et chaque ligne est marquée :

```
[agent-mail] 1 nouveau(x) message(s) — dont 1 avec l'autorité du propriétaire
PROUVÉE (signature Ed25519) ; les autres n'ont pas cette autorité :
— de proprietaire à 18:40 : Le lot est approuvé.  [autorité du propriétaire PROUVÉE, expire 06:40]
```

Et pour une falsification :

```
— de proprietaire à 18:41 : Falsifié.  [⚠ signature NON valide : la signature ne couvre pas ce contenu]
```

---

## 2. `mesh list` (spec §5 point 5)

`agent-mesh list` lit la vue `agent_mesh_overview` : nom, harnais, hôte, statut
(et son texte), bail avec temps restant, non-lus, budget consommé/prévu, clé
enregistrée, dernière présence. `agent-mesh show <agent>` détaille un agent
(session, bail, modèle, budget, clé, dossier, tours). `--json` existe sur les
deux pour les scripts et les autres agents.

## 3. `work_items` (spec §5 point 4)

Machine à états reprise d'un orchestrateur de tickets existant :

```
intake → build → qa → merged → promoted     (+ blocked, waiting_human)
```

* la boucle `qa → build` est **bornée à 2 retours** (`loops`) : au-delà, la CLI
  refuse et demande `blocked` ou `waiting_human` — une boucle non bornée coûte
  des tokens sans fin ;
* `promoted` est terminal, `closed_at` est posé ;
* chaque transition écrit une ligne dans `work_item_events` (état, note, acteur,
  horodatage) : c'est le ledger de reprise ;
* un trigger `pg_notify('work_item', …)` publie les changements d'état, comme
  la boîte aux lettres.

CLI : `agent-mesh work add|list|show|move|note` (+ `--json`).

## 4. Bascule v0 → v1 : import et export

`agent-mesh import-v0` lit la boîte fichier v0 (`~/.local/state/agent-mail`),
reprend le registre v0 (`agents/<nom>.json` : harnais, dossier, session) et
dépose les non-lus dans Postgres **en conservant leur horodatage**. Les fichiers
importés sont *déplacés* dans `inbox/<nom>/imported/`, jamais supprimés.

`agent-mesh export-v0` fait l'inverse : il réécrit les non-lus d'un agent dans
la boîte fichier v0 (format v0 strict) et les marque remis en base — un
déplacement, donc pas de doublon au retour. `--keep` copie sans marquer.

C'est ce couple qui rend chaque étape de `BASCULE.md` réversible.

---

## 5. Tests

110 tests `unittest`, exécutés deux fois (`scripts/test.sh` : pilote `psql`,
puis `psycopg` dans un venv avec `cryptography`). Couverture des points 3-5 :

* vecteurs RFC 8032 sur tous les backends disponibles, falsifications,
  signature non canonique, croisement inter-backends et **croisement OpenSSL** ;
* permissions des clés, refus d'écraser, clé privée lue comme publique rejetée ;
* messages : signé valide, non signé (y compris un texte qui se prétend du
  propriétaire), corps falsifié, destinataire modifié, signature bricolée,
  expirée, clé inconnue, empreinte inconnue, révocation puis ré-enregistrement ;
* hooks : preuve affichée, falsification visible, JSON identique à la v0 sans
  signature ; clé d'agent → « authentique, sans autorité propriétaire », jamais
  « PROUVÉE » ;
* rôles : une clé d'agent authentifie sans autorité, ne peut pas approuver, et
  une approbation insérée de force avec une clé d'agent est refusée ;
* approbations : cycle complet, consommation unique, expiration réelle, hash
  non couvert, décision `rejected`, clé non enregistrée refusée ;
* sondes codex3 sur le gel 345aca6, désormais couvertes : `expires_at` ou
  `created_at` modifiés en base → refusés ; reçu consommé supprimé puis
  réinséré → « nonce déjà consommé » ; copie directe → index unique ;
* **mise à niveau** : schéma 0001–0004 avec une approbation déjà consommée,
  application réelle de 0005–0007, reçu supprimé puis réinséré → toujours
  refusé par le ledger rétro-rempli ;
* `mesh list`/`show` (texte et JSON), cycle de vie des clés par la CLI ;
* `work_items` : cycle, transitions refusées, boucle bornée, journal,
  déplacement concurrent (un seul gagnant), NOTIFY ;
* import/export v0 (horodatages, fichiers déplacés, registre repris).

---

## 6. Limites assumées

* **L'enregistrement d'une clé est un acte de confiance** : la CLI ne peut pas
  prouver qui a lancé `key register` ni qui a choisi `--role owner`. La
  protection est organisationnelle (machine, permissions) — R4 prouve la
  *signature* et son rôle, pas l'identité de l'opérateur. C'est cette limite
  (agents et clé sous le même utilisateur Unix) qui a conduit la v1 à placer
  l'autorité humaine dans ameesh-approve et le registre des authentificateurs
  dans le canon, modifiable seulement par PR revue (spec §8.2). Avec plusieurs
  canons (L44, décision 0031), ce registre est tenu **par canon** : une passkey
  ne vaut que pour les objets (actions) du canon qui la déclare, et n'est
  révoquée que par lui (voir EXPLOITATION.md, « Plusieurs canons »).
* **Un rôle par clé, pas encore de portées** : `owner`/`agent` est binaire ;
  des capacités plus fines (« approuver un merge mais pas la production »)
  viendront avec la porte de gouvernance.
* **L'horloge est supposée à peu près juste** : les échéances sont comparées à
  l'heure locale/base. Une dérive d'horloge décale les expirations.
* **Pas encore de porte** : `verify --consume` existe, mais aucun merge ou
  déploiement ne l'appelle automatiquement ; c'est le branchement de la porte de
  gouvernance externe qui reste à faire.
* **Pas de clé matérielle** et pas de rotation automatique : `key revoke`
  puis `key register` sont manuels.
* La vérification des messages signés se fait à chaque hook (~28 ms en pur
  Python, moins avec `cryptography`) ; pas de cache pour l'instant.
