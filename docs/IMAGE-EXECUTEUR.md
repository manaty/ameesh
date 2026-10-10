# Image `ameesh-executor` : l'interface figée

Interface `ameesh-executor/1`, figée par le lot L114a (2026-10-10). Elle dit
ce que le lanceur d'une VM d'appareil prêté (Nexlink Compute, contrat
`compute-agent-turn` v0.2) doit fournir à l'image, et ce qu'il en reçoit.
Contrat de l'API : `docs/EXECUTEUR-MEDIEE.md`. Étude :
`docs/design/etudes/executeur-mediee.md`.

Une modification incompatible ouvre `ameesh-executor/2`. Elle ne retouche
jamais la v1 en silence. Brouillons : `deploy/image-executeur/Containerfile`
et `deploy/image-executeur/ameesh-executor` (aucune image construite ni
publiée).

## 1. Point d'entrée

```
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/ameesh-executor"]
CMD        ["run"]
```

| Commande | Effet |
|---|---|
| `ameesh-executor run` (défaut) | contrôles, enrôlement au premier démarrage, puis `exec ameesh run` en mode médié |
| `ameesh-executor health` | sonde (section 8) |
| `ameesh-executor show` | état de l'appareil en JSON (`ameesh-device-show/1`), sans secret |

Les arguments qui suivent `run` vont tels quels à `ameesh run`. Le lanceur
n'en passe aucun.

## 2. Variables d'environnement

**Posées par le lanceur :**

| Variable | Obligatoire | Valeur |
|---|---|---|
| `AMEESH_EXEC_URL` | oui | `https://<mesh.server_host>`, sans chemin ni `/` final ; le port 443 est implicite |
| `AMEESH_EXEC_MESH` | non, conseillée | `mesh.id` de la recette. Elle est comparée au mesh enregistré à l'enrôlement ; une différence donne le code 5 |
| `AMEESH_EXEC_HOST` | non | le nom de la fiche `Host`, comparé de la même façon. Sans elle, l'hôte est celui que le code d'enrôlement a fixé |
| `AMEESH_EXEC_ATTEST` | non | `1` : demander l'attestation d'appareil (section 3) |
| `AMEESH_EXEC_LABEL` | non | un libellé lisible de l'appareil, affiché par `ameesh host show` |
| `HTTPS_PROXY` | non | le mandataire CONNECT de l'hôte, si l'egress n'est pas transparent (section 6) |

Le mesh et l'hôte ne sont jamais *choisis* par la VM. Le code d'enrôlement
les lie côté serveur. Les variables ne servent qu'à refuser un volume
monté pour un autre mesh.

**Le code d'enrôlement ne passe jamais par l'environnement.** Il arrive par
un fichier (section 3).

**Fixées par l'image, à ne pas changer :** `AMEESH_BACKEND=mediated`,
`AMEESH_EXEC_HOME=/var/lib/ameesh-exec`, `AMEESH_STATE`, `DSH_HOME`,
`AMEESH_DSH_BIN`, `AMEESH_EXEC_SIGNER=cryptography`, `AMEESH_HOST_GATE=file`,
`AMEESH_HOST_GATE_PATH`, `AMEESH_HOST_GATE_ACK`,
`AMEESH_HOST_GATE_FALLBACK=stopped` et `AMEESH_HOST_GATE_DRAIN=90`.

Aucun fichier de configuration n'est à monter : l'image porte toute sa
configuration dans ces variables. Ne posez jamais `AMEESH_DSN`,
`AMEESH_EXEC_TOKEN` ni une clé de fournisseur.

## 3. Chemins

| Chemin (dans le conteneur) | Montage | Contenu |
|---|---|---|
| `/var/lib/ameesh-exec/` | **volume persistant**, `rw`, uid 10001, `0700` | tout l'état de l'exécuteur (ci-dessous) |
| `/var/lib/ameesh-exec/key.pem` | (volume) | la clé P-256 de l'exécuteur, PKCS#8 PEM, `0600`. Elle est créée à l'enrôlement ; une clé plus ouverte est refusée |
| `/var/lib/ameesh-exec/executor.json` | (volume) | `ameesh-exec-device/1` : `executor_id`, `mesh`, `host`, `server_url`. Aucun secret |
| `/var/lib/ameesh-exec/work/<agent>/` | (volume) | dossier de travail, `cwd` du tour : l'archive du commit rendue par le serveur (L113), chemin stable d'un bail à l'autre (dsh lie sa session au dossier). **Effacé** à la fin du bail, au drainage, à la révocation, et en entier à chaque démarrage |
| `/var/lib/ameesh-exec/harness/` | (volume) | `DSH_HOME` : la session du harnais, effacée avec le dossier de travail |
| `/var/lib/ameesh-exec/state/` | (volume) | `AMEESH_STATE` : l'état local d'ameesh, sans secret |
| `/run/ameesh-enroll/code` | `:ro`, **propre au job**, premier démarrage seulement | le code d'enrôlement : une ligne, 24 caractères de Crockford, avec ou sans tirets |
| `/run/ameesh-enroll/attestation.json` | (même montage) | facultatif : `ameesh-device-attestation/1` (Q1) |
| `/run/ameesh-gate/state/state.json` | `:ro` | `ameesh-host-state/1`, écrit par le runner (fichier temporaire puis `rename`) |
| `/run/ameesh-gate/ack/ack.json` | `rw` | `ameesh-host-ack/1`, écrit par l'exécuteur de la même façon |
| `/run/ameesh-gate/ack/attest-challenge.txt` | (même dossier) | le défi à faire signer, présent seulement si `AMEESH_EXEC_ATTEST=1` |

**Enrôlement, pas à pas.** Au premier démarrage, `executor.json` est absent.

1. L'entrée vérifie que `/run/ameesh-enroll/code` existe, sans le lire.
2. Elle lance `ameesh device enroll --code-file /run/ameesh-enroll/code`,
   qui lit le code lui-même. Le code ne paraît jamais dans une ligne de
   commande (`/proc/<pid>/cmdline`), dans l'environnement ni dans un
   journal ; `tests/test_l114_image_executeur.py` le vérifie.
3. Le code est usé côté serveur, qu'il soit accepté ou non.

La VM ne peut pas effacer ce fichier, puisque le montage est `:ro`. Le
lanceur l'efface dès le premier `ack.json`, que l'exécuteur n'écrit
qu'après un enrôlement réussi, et au plus tard à la fin du job.

**Attestation, si `AMEESH_EXEC_ATTEST=1`.**

1. L'entrée écrit `attest-challenge.txt`.
2. Elle attend jusqu'à 120 s le fichier `/run/ameesh-enroll/attestation.json`,
   que l'application de bureau dépose côté hôte.
3. Sans réponse, elle s'enrôle sans attestation.

**Le volume persistant.** Il y en a un par couple (appareil, `mesh.id`). Il
survit d'une fenêtre à l'autre et appartient à l'uid 10001. Le lanceur
l'efface quand le propriétaire retire le mesh, quand l'offre est révoquée,
ou après le code de sortie 6.

**Ce qui ne va jamais dans le volume :**

- le code d'enrôlement ;
- un jeton `amx1.…` ou `ams1.…` (ils restent en mémoire) ;
- une clé de fournisseur de modèle ou un identifiant de forge ;
- la clé d'appareil Nexlink ;
- les fichiers de la porte ;
- `window.json` ou tout autre artefact `out/` ;
- les données d'un autre mesh.

Réciproquement, `key.pem` ne va jamais dans `out/`, dans le dossier de la
porte ni sur le disque de l'hôte hors de ce volume.

## 4. Utilisateur

L'exécuteur tourne sous l'uid et le gid **10001** (`ameesh`), jamais root :
l'entrée refuse l'uid 0 (code 2). Il n'a besoin d'aucune capacité Linux,
d'aucun port en écoute, ni de privilège. La racine du conteneur peut être en
lecture seule. Seuls le volume et `/tmp` (un `tmpfs` suffit) sont inscrits.

## 5. Signaux et arrêt

- **Voie normale.** Le runner écrit `draining` dans la porte. L'exécuteur
  cesse de réclamer, finit chaque tour au point sûr, puis rend ses baux et
  efface ses dossiers. Il écrit enfin `ack.json` avec `drained: true` et un
  `seq` supérieur ou égal à celui du drainage. Le runner peut alors arrêter
  le conteneur.
  - `drain_deadline_ts` vaut 90 s au plus.
  - Au-delà, le tour est interrompu comme une préemption : la consigne est
    remise en attente.
- **SIGTERM (ou SIGINT).** C'est le même drainage, borné à **90 s**
  (`AMEESH_HOST_GATE_DRAIN`), puis la sortie avec le code 0 dès que plus
  aucun bail n'est détenu (L114b : `GateController.terminate`, la porte ne
  peut plus rouvrir la réclamation, un `stopped` de sa part reste
  appliqué). Un second signal arrête tout de suite. Le délai d'arrêt du
  lanceur doit donc être d'au moins **100 s** (`TimeoutStopSec=100`, ou
  `stop -t 100`).
- **SIGKILL ou coupure.** Rien n'est écrit. Le bail ameesh échoit en 90 s au
  plus. Le courrier réservé est remis à nouveau et signalé comme doublon.
  Les dossiers restés sur le volume sont effacés au démarrage suivant.
- **`stopped` dans la porte, ou porte illisible.** C'est un arrêt immédiat
  des tours, sans réclamation. Le processus reste vivant et repart quand la
  porte revient à `available`.

L'acquittement porte `seq`, `state`, `in_turn` (les agents encore en tour),
`held` (les baux encore détenus), `drained` et `ts`. Le nom du champ est
`in_turn`, et non `agents_in_turn` : à corriger au §6 du contrat Nexlink.

## 6. Réseau

- **Sortant.** Seul `<mesh.server_host>:443` est joint, en TLS, et il porte
  tout le trafic :
  - l'API `/api/exec/v1` ;
  - le flux d'événements SSE, dont une connexion reste ouverte en
    permanence, avec un battement toutes les 20 s ;
  - le relais de modèle `/api/exec/v1/llm/…` (L111) ;
  - le paquet git de travail `/api/exec/v1/work/…` (L113).

  L'image n'a besoin d'aucun registre, d'aucun fournisseur de modèle ni
  d'aucune forge.
- **Mandataire.** L'egress peut être transparent : la VM résout le nom et
  nftables n'ouvre que l'adresse du serveur, sur le port 443. Il peut aussi
  passer par un mandataire CONNECT (`HTTPS_PROXY`), qui ne doit admettre que
  ce nom et ce port. Le client de l'exécuteur le respecte (L114b : tunnel
  CONNECT, `NO_PROXY`, `Proxy-Authorization` tiré de `user:mot@`), comme
  l'enrôlement (`urllib`). `dsh` (Node) ne le suit que si la VM pose aussi
  `NODE_USE_ENV_PROXY=1` (Node 22.21 et plus) : point à vérifier sur
  l'image réelle.
- **Entrant.** Aucun.
- **Horloge.** L'heure de la VM doit être juste à ±30 s près : les assertions
  ES256 vivent 60 s.

## 7. Codes de sortie

| Code | Sens | Ce que fait le lanceur |
|---|---|---|
| 0 | arrêt propre (signal drainé) | rien |
| 1 | erreur passagère : serveur injoignable, `GET /host` illisible | relancer avec une attente croissante (30 s, puis jusqu'à 10 min) |
| 2 | configuration invalide : URL absente ou non https, root, volume absent ou non inscriptible | ne pas relancer ; corriger le lancement |
| 3 | appareil non enrôlé et aucun code | demander un code au propriétaire |
| 4 | enrôlement refusé : code faux, échu ou usé, ou attestation fausse | demander un nouveau code ; ne pas relancer avec le même |
| 5 | le volume appartient à un autre serveur, mesh ou hôte | ne pas relancer ; signaler, ne pas effacer seul |
| 6 | exécuteur révoqué par le serveur (en marche, ou dès `GET /host` au démarrage) | effacer le volume ; ne pas relancer sans nouveau code |

## 8. Santé et journaux

- **Sonde.** `ameesh-executor health` rend 0 si l'appareil est enrôlé et si
  le processus `ameesh run` est vivant, 1 sinon. L'image la déclare en
  `HEALTHCHECK` toutes les 30 s, après un démarrage de 60 s.
  - La vivacité réelle d'un appareil, c'est son bail : le serveur la voit
    (`ameesh host show`), et le runner la voit par `ack.json`.
  - La sonde ne parle pas au réseau.
- **Journaux.** Ils vont sur stdout et stderr, une ligne par événement, sans
  tampon (`PYTHONUNBUFFERED=1`). Ils ne contiennent jamais de jeton, de code,
  de clé ni de contenu de modèle. Ils peuvent nommer les agents, l'hôte et
  le mesh : ils restent sur l'appareil et ne remontent pas au hub.

## 9. Signature des releases de l'image

- **Clé.** La clé de release ameesh est une clé ECDSA P-256 (ES256), unique,
  propre à l'image `ameesh-executor`.
  - **La clé privée est détenue par le propriétaire d'ameesh**, hors dépôt,
    chiffrée par une phrase. Elle n'est jamais en CI, sur le serveur d'un
    mesh, ni dans une image.
  - **La clé publique** est en SPKI DER, encodée en base64url sans
    remplissage. Elle sera publiée ici à sa création ; Nexlink la configure
    en `AMEESH_RELEASE_PUBLIC_KEY`.
- **Octets signés.** Ils sont identiques à `agentImageSignedBytes` côté
  Nexlink :

  ```
  UTF-8( JSON.stringify(["ameesh/release/v1/image", image]) )
  ```

  - `image` vaut exactement `<registre>/ameesh-executor@sha256:<64 hex
    minuscules>`, la valeur de `recipe.image`, sans étiquette.
  - Le JSON est **compact, sans espace** :
    `["ameesh/release/v1/image","registre.exemple.org/ameesh-executor@sha256:2d71…"]`.
  - En Python, l'équivalent est
    `json.dumps([D, image], separators=(",", ":"))`.
- **Signature.** C'est ECDSA P-256 sur SHA-256, au format IEEE P1363 :
  `r‖s`, 64 octets, en base64url sans remplissage, soit **86 caractères**.
  C'est la valeur de `recipe.image_signature`.
  - Vérification croisée faite le 2026-10-10 : une signature de
    `signer-release.py` est acceptée par le code de vérification du hub
    (Node, `dsaEncoding: 'ieee-p1363'`), et refusée pour une autre image.
- **Procédure de release.**
  1. La CI construit l'image et la pousse. Elle relève le digest.
  2. Le propriétaire signe sur son poste :
     `deploy/image-executeur/signer-release.py sign --key <clé> <image@digest>`.
     La commande rend `ameesh-release-signature/1` : `{image,
     image_signature, public_key}`.
  3. Il vérifie : `signer-release.py verify --pub … <image> <signature>`.
  4. Le triplet est publié dans les notes de release.
  5. Nexlink épingle `image` et `image_signature` dans la recette.
- **Création de la clé**, une seule fois, par le propriétaire :

  ```sh
  openssl ecparam -name prime256v1 -genkey -noout | openssl pkcs8 -topk8 -out ameesh-release.pem
  signer-release.py public --key ameesh-release.pem   # → AMEESH_RELEASE_PUBLIC_KEY
  ```

- **Rotation et compromission.** On crée une nouvelle clé, on re-signe les
  images encore admises, et Nexlink remplace `AMEESH_RELEASE_PUBLIC_KEY`. Les
  recettes signées par l'ancienne clé sont alors refusées à l'enqueue. Une
  liste de clés acceptées sera, au besoin, une v2 de cette section.

## 10. Réponses au contrat Nexlink (`compute-agent-turn` v0.2)

- **Q1, format de `device_attestation`. Tranchée par L110.** Ce n'est pas du
  JCS : c'est un texte ASCII, une ligne par champ, avec un `\n` final. Le
  préfixe de domaine est la première ligne, `ameesh-device-attestation/1` ;
  `nexlink/compute/v1/ameesh-attest` n'est pas employé.

  ```
  ameesh-device-attestation/1
  server_url=https://mesh.exemple.org
  code_sha256=<hex du SHA-256 du code normalisé>
  executor_thumbprint=<empreinte RFC 7638 de la clé de l'exécuteur>
  ```

  - L'appareil n'a pas à recomposer ce texte : `ameesh device challenge` le
    produit, et l'image le dépose dans `attest-challenge.txt`.
  - La clé d'appareil Nexlink signe ces octets tels quels, en ES256.
  - L'application de bureau dépose
    `{"schema": "ameesh-device-attestation/1", "device_public_key":
    <SPKI DER base64url>, "signature": <r‖s base64url>}` dans
    `/run/ameesh-enroll/attestation.json`.
  - Une attestation fausse fait refuser l'enrôlement. Une attestation
    absente n'empêche rien.
- **Q2, signature de l'image.** Accord avec la décision. La section 9 donne
  le format, qui est compatible octet pour octet avec le code 94a déjà
  écrit. Le runner revérifie le digest avant de lancer le conteneur.
- **Q3, `lease_ttl` d'un hôte volatil.** C'est 90 s, avec un renouvellement
  toutes les 30 s, imposés par `GET /api/exec/v1/host`. La valeur sera
  mesurée en L115. Le bail Compute de 120 s / 40 s de la classe `agent` est
  compatible.
- **Q4 et Q5.** Elles relèvent du propriétaire ; ameesh n'a rien à
  trancher. Seule contrainte côté ameesh : le code d'enrôlement est émis par
  un humain habilité, jamais par un agent (`ameesh host enroll`, L110).
- **Écarts relevés dans le contrat v0.2, à corriger côté Nexlink :**
  - §4 : la commande est `ameesh host enroll` côté serveur et
    `ameesh device enroll` côté VM, et non `ameesh executor …` ;
  - §6 : le champ d'acquittement est `in_turn` (et `held`), et non
    `agents_in_turn` ;
  - §4 : le format de l'attestation est celui de Q1.

## 11. Dépendances D1 à D3 et lots ameesh

| Dépendance (contrat v0.1 §6) | Ce qu'elle devient | Lots ameesh |
|---|---|---|
| **D1** API d'exécuteur médiée | `/api/exec/v1` sur le serveur du mesh : le contrat est figé (62 opérations, version 1.1), le serveur, le client, le relais de modèle et le dépôt de travail | L107 (contrat, fait), L108 (serveur), L109 (client), L111 (relais), L113 (dépôt de travail) |
| **D2** émetteur de jetons serveur à serveur | **abandonné en v0.2**. Le hub ne voit, ne détient ni ne relaie aucun jeton. L'exécuteur obtient lui-même ses jetons (`amx1` de 10 min, `ams1` liés au bail), par sa clé enrôlée | L110 (enrôlement et identité) |
| **D3** fiche Host et admission | la fiche `Host` au canon (`policy.volatile`, `occupants`), l'admission des agents sur l'hôte, et le code d'enrôlement lié à l'hôte et à une liste d'agents. **Sans fiche, aucun code n'est émis** | L110 (codes, révocation), L112 (porte d'hôte), canon du mesh (PR revue par un mainteneur) |
| image et empaquetage | cette interface ; les brouillons du `Containerfile` et de l'entrée | **L114** |
| essai de bout en bout | un mesh jetable et une VM : enrôlement, tour relayé, drainage, VM tuée, révocation, tentatives interdites | L115 |

**Reste à câbler en L114b** (aucun changement d'interface) :

- dans `ameesh run` en mode médié :
  - SIGTERM vaut un drainage local borné à 90 s ; aujourd'hui, le tour est
    arrêté en 3 s ;
  - le code de sortie est 6 sur `executor_revoked` ; aujourd'hui, c'est 0 ;
  - `HttpTokenSource` (L110) est la source de jetons du transport (L109) ;
- l'option `--code-file FICHIER` (ou `-` pour stdin) de `ameesh device
  enroll` et `ameesh device challenge`, à la place de `--code`, dans
  `host_cli.py` (L110), que l'entrée appelle déjà ;
- `HTTPS_PROXY` (CONNECT) dans le transport `http.client` de L109, à
  vérifier aussi pour `dsh` en L115.
