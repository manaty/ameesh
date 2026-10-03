# V1 — autorité du propriétaire, mesh list, work_items (points 3 à 5)

Suite de [`V1-MAILBOX-RUNNER.md`](V1-MAILBOX-RUNNER.md) (points 1 et 2 : boîte
aux lettres Postgres et exécuteur par machine). Toujours en banc : aucun agent
réel n'est branché, la bascule est décrite dans [`BASCULE.md`](BASCULE.md).

---

## 1. Autorité du propriétaire (R4, spec §5 point 3)

### Le modèle

**L'autorité ne se déduit jamais d'un texte.** Un message ou une approbation ne
porte l'autorité du propriétaire que si trois conditions sont réunies :

1. **clé connue** — l'expéditeur a une clé *publique* enregistrée dans
   `agent_registry.public_key`, et cette clé n'est pas révoquée
   (`key_revoked_at IS NULL`). La clé privée ne touche jamais la base ;
2. **contenu couvert** — la signature Ed25519 vérifie sur un payload canonique
   qui *est exactement* le contenu stocké :
   `agent-mesh/v1|message|{"expires":…,"from":…,"kind":"message","nonce":…,"text":…,"to":…,"ts":…,"v":1}`
   (JSON trié, sans espaces). Le vérificateur reconstruit ce payload depuis la
   ligne et exige l'égalité avec `signed_payload`, puis vérifie la signature :
   modifier le corps, le destinataire, l'horodatage ou le nonce invalide la
   preuve ;
3. **échéance** — `signature_expires_at` est dans le futur. Une signature
   expirée est signalée comme telle, jamais ignorée.

Une signature invalide **ne retombe pas** dans « message d'agent ordinaire » :
elle est rendue visible (`[⚠ signature NON valide : …]`). Un message non signé
reste exactement ce qu'il était en v0 : un message d'agent, sans autorité.

Les approbations (`mesh_approvals`) suivent le même modèle avec, en plus, une
`action`, une empreinte d'artefact (`sha256` hex), une décision
(`approved`/`rejected`) et une **consommation unique** (`consumed_at`,
`consumed_by`) : une approbation ne peut pas servir deux fois.

### Ed25519 : trois implémentations, une seule interface

`signing.py` expose :

| backend | disponibilité | usage |
|---|---|---|
| `pure` | toujours (stdlib `hashlib` seulement) | défaut, vérification sans aucune dépendance |
| `cryptography` | si importable | accélération, contre-vérification croisée |
| `nacl` | si importable | idem |

`AGENT_MESH_SIGNING_BACKEND=auto|pure|cryptography|nacl` choisit. L'implémentation
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
  la cérémonie de clé est un acte du propriétaire, aucun agent ne génère ni ne
  détient sa clé privée (voir `BASCULE.md`, étape 3) ;
* la clé privée est écrite en `0600`, refusée à la lecture si ses permissions
  sont trop ouvertes, jamais journalisée, jamais en base, jamais dans un message ;
* `agent-mesh key register <agent> --public-key FICHIER` n'accepte qu'une clé
  publique (une clé privée est rejetée) ; `key revoke` invalide immédiatement
  toutes les signatures de cet agent.

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

Machine à états reprise de l'IMA :

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
  signature ;
* approbations : cycle complet, consommation unique, expiration, hash non
  couvert, décision `rejected`, clé non enregistrée refusée ;
* `mesh list`/`show` (texte et JSON), cycle de vie des clés par la CLI ;
* `work_items` : cycle, transitions refusées, boucle bornée, journal,
  déplacement concurrent (un seul gagnant), NOTIFY ;
* import/export v0 (horodatages, fichiers déplacés, registre repris).

---

## 6. Limites assumées

* **L'enregistrement d'une clé est un acte de confiance** : la CLI ne peut pas
  prouver qui a lancé `key register`. La protection est organisationnelle
  (cérémonie, machine, permissions) — R4 prouve la *signature*, pas l'identité
  de l'opérateur.
* **L'horloge est supposée à peu près juste** : les échéances sont comparées à
  l'heure locale/base. Une dérive d'horloge décale les expirations.
* **Pas encore de porte** : `verify --consume` existe, mais aucun merge ou
  déploiement ne l'appelle automatiquement ; c'est le branchement IMA
  (`governance-gate.js`) qui reste à faire.
* **Pas de clé matérielle** et pas de rotation automatique : `key revoke`
  puis `key register` sont manuels.
* La vérification des messages signés se fait à chaque hook (~28 ms en pur
  Python, moins avec `cryptography`) ; pas de cache pour l'instant.
