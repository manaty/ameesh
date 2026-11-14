# Relais d'attach (D2, lot #7120)

Transport de l'attach distant : un **service de relais** apparie le poste
d'un humain et la **connexion sortante** de l'hôte de l'agent, puis relaie
des trames chiffrées de bout en bout autour d'un PTY. D2 (#7120) livre le
service, la connexion de l'hôte, le PTY, les trames, l'E2E, les quotas et
les journaux ; D3 (#7121) branche la session d'attach elle-même : bail,
attente de tour, reprise, journal A0, copie enfant et coupure.

Conception : `docs/design/5322-avatar-humain.md` §5.3 et §6 (D2).
Décision owner : **Q3 #213938, option a**, signée par `human:smichea` le
12/11/2026 (approbation #345) — le relais est **réservé au réseau du mesh**
(postes enrôlés), **sans exposition publique** ; une exposition publique
demanderait une décision séparée. Le grant d'attach vient de D1 (#7119).

## Architecture

```
poste (station)  ──TLS──►  relais  ◄──TLS──  hôte de l'agent (connexion sortante)
      │                      │                        │
      └──── E2E X25519/AES-GCM (le relais ne lit pas) ┴── PTY ── harnais
```

- **Relais** : écoute une adresse de boucle locale ou du mesh, refuse tout
  pair hors du mesh, apparie par `attach_id`, route les trames par
  `session_id`. Il **vérifie la signature WebAuthn** du reçu en lecture dans
  le registre (sans consommer le nonce) avant tout appariement, et ne
  conserve rien après la session.
- **Hôte** : ouvre **une** connexion sortante vers le relais, s'authentifie
  par un jeton, reçoit les `PAIR`, vérifie le grant avec D1, ouvre un PTY
  pour la commande interactive et pont les octets. Aucun port entrant.
- **Poste** : parle le protocole de `ameesh.attach_transport` (KEY, REQ,
  DATA, WINSIZE, SIGNAL, ACK, CLOSE). En D2, la station est le client de ce
  module ; D3 fournira la commande `ameesh attach` distante.

## Décisions de confiance

- Le relais **vérifie le reçu** (`ameesh-receipt/1` de D1) : forme, nature
  `attach`, liaison agent/hôte/tenant, `iat`/`exp` cohérents, validité
  ≤ 300 s, digest canonique, puis **signature WebAuthn** de la clé
  d'approbation, avec un authentificateur encore actif. Il ne consomme pas
  le nonce : `grant_checker` est injecté par la CLI (base + politique
  `AMEESH_APPROVE_RP_ID` / `AMEESH_APPROVE_ORIGINS`). Le canon de l'agent est
  résolu comme chez l'hôte et la façade est forcée à WebAuthn : un reçu
  signé par la seule clé `device-es256` est refusé. Sans vérificateur, le
  service refuse tout `OPEN` (`grant_unverified`, fail closed).
- La **porte faisant foi est l'hôte** : à la réception du `REQ`, il appelle
  `attach_grant.redeem` (D1) avec la politique, la preuve de la clé
  d'appareil et le canon de l'agent. Le grant est à usage unique ; un refus
  ne crée pas de PTY et n'ouvre aucune session.
- L'E2E est **authentifiée aux deux bouts**, pas seulement chiffrée :
  - l'hôte possède une clé Ed25519 ; sa clé publique est **épinglée dans le
    canon** de l'hôte (`attach_host_key`, 32 octets en base64url ou hex) et
    le poste la passe dans `Context.host_key`. L'hôte **signe son `KEY`**
    (domaine `ameesh-attach-host/1`) sur le hash de la transcription ; un
    `KEY` non signé, signé par une autre clé ou sans clé épinglée est refusé
    (`host_unauthenticated`, `host_signature`) ;
  - la preuve d'appareil signe `"ameesh-attach-device/2\0" ‖ challenge du
    reçu ‖ hash du transcript` : elle est liée à CETTE session chiffrée. Le
    `REQ` porte ce hash et l'hôte le compare à celui de son canal avant
    toute vérification.
  Un relais qui terminerait deux canaux (poste↔relais, relais↔hôte) obtient
  deux transcriptions différentes : il ne peut ni transposer la preuve
  d'appareil, ni signer le `KEY` de l'hôte. Le contenu reste protégé
  **même contre un relais actif**.
- L'E2E protège aussi le contenu en transit : clés X25519 éphémères,
  HKDF-SHA-256 salé par la transcription (protocole, session, agent, hôte,
  tenant, `attach_id`, digest du reçu, clé d'hôte épinglée, clés publiques),
  AES-256-GCM par direction, nonce = séquence, AAD liant type, séquence et
  transcription. Une trame rejouée, réordonnée, altérée ou d'un autre
  contexte est refusée.

## Protocole

Trame sur le fil : `longueur u32 | session 16 octets | type 1 octet | charge`.
La longueur est bornée avant lecture (`MAX_FRAME` = 64 Kio) ; les charges de
session sont bornées par type (`MAX_DATA` = 16 Kio). Une trame commencée mais
incomplète est bornée (30 s) puis ferme la connexion : jamais de
resynchronisation au milieu du flux. Une session de contrôle avant appariement
porte l'identifiant nul.

1. Le poste envoie `OPEN` (agent, hôte, tenant, reçu) ; l'hôte envoie
   `HELLO` (hôte, tenant, jeton). Le relais vérifie le jeton d'hôte en
   temps constant, vérifie le reçu (dont la signature), puis envoie `PAIR` à
   l'hôte et `PAIRED` au poste avec le `session_id`, l'`attach_id` et
   l'empreinte du reçu.
2. Les deux extrémités échangent `KEY` puis `CONFIRM` dans la session ;
   le `KEY` de l'hôte porte sa signature Ed25519, que le poste vérifie
   contre la clé épinglée. Toute trame de session avant la confirmation est
   refusée.
3. Le poste envoie `REQ` (reçu, signature de l'appareil, hash du transcript,
   clé d'appareil, taille de fenêtre, `TERM`). L'hôte compare le hash à son
   propre canal, vérifie, ouvre le PTY et répond `OK` (ou `NO` avec un code
   stable). Les `DATA` circulent ensuite dans les deux sens, avec `WINSIZE`,
   `SIGNAL`, `ACK` et `CLOSE`.
4. Une déconnexion est un échec de session : l'hôte ferme le PTY, le relais
   oublie la session, aucun état ne survit. Le poste ne reçoit ni session
   ni credential.

Signaux acceptés : HUP, INT, QUIT, TERM, CONT, TSTP. Jamais SIGKILL.

**Contre-pression** : l'émetteur cesse de lire le PTY quand la fenêtre
d'acquittement (1 Mio par défaut, ACK tous les 64 Kio) est pleine. Le débit
est aussi borné par le relais (octets/seconde et octets par session).

## Quotas et journaux

Défauts du service : 128 connexions, 64 sessions, 4 sessions par hôte,
8 par adresse, 32 appariements en attente, appariement 5 s, poignée de main
15 s, inactivité 15 min, session 2 h, 512 Mio par session, 4 Mio/s, file
d'écriture 256 trames, 120 connexions/min. L'hôte borne aussi ses sessions
et leur durée.

Les journaux ne portent que des **métadonnées** : événement, pair, hôte,
tenant, agent, `session`, `attach_id`, code, durée, octets, PID. Ni reçu, ni
nonce, ni clé, ni contenu du terminal : un secret traversant le PTY
n'apparaît dans aucun journal (test dédié).

## Exploitation

Le jeton d'hôte est un fichier **0600** (JSON `{"hôte": "jeton"}`), jamais
dans l'environnement ni dans un journal. La clé privée Ed25519 de l'hôte est
un fichier **0600** (32 octets bruts, hex ou base64url) ; sa clé publique est
épinglée dans la carte canon de l'hôte (`attach_host_key`). Le service refuse
une adresse d'écoute hors boucle locale/mesh et refuse le clair hors boucle
locale ; il exige `AMEESH_APPROVE_RP_ID` / `AMEESH_APPROVE_ORIGINS` et une
base joignable pour vérifier les reçus.

```sh
# service (sur un hôte du mesh, TLS obligatoire hors boucle locale)
ameesh attach-relay serve --listen 10.20.0.5:8790 --mesh-cidr 10.20.0.0/16 \
  --hosts-file /etc/ameesh/attach-hosts.json \
  --tls-cert /etc/ameesh/tls/relay.pem --tls-key /etc/ameesh/tls/relay-key.pem

# hôte de l'agent : une connexion sortante, la commande interactive en argv
ameesh attach-relay host --relay https://relay.interne:8790 \
  --host ameesh-agents-1 --token-file /etc/ameesh/attach-token \
  --host-key-file /etc/ameesh/attach-host-key \
  --ca-file /etc/ameesh/tls/ca.pem --agent avatar-alice \
  --command 'ameesh runner attach --agent avatar-alice'
```

`--command` est un argv découpé par `shlex.split`, **jamais** passé à un
shell. Une commande statique est liée à **exactement un** `--agent` : sans
cette contrainte, un grant valide pour un agent lancerait le harnais d'un
autre agent du même hôte (refus à la configuration). L'enfant reçoit
`AMEESH_ATTACH_REMOTE=1` et `TERM` du poste. Sur un hôte partagé, `--tenant`
et `--agent` restreignent ce que l'hôte sert.

Unité systemd utilisateur (l'application survit au tour ; jamais lancée
depuis le shell du tour) :

```ini
[Unit]
Description=ameesh attach relay host
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ameesh attach-relay host --relay https://relay.interne:8790 \
  --host ameesh-agents-1 --token-file %h/.config/ameesh/attach-token \
  --host-key-file %h/.config/ameesh/attach-host-key \
  --ca-file %h/.config/ameesh/tls/ca.pem --agent avatar-alice \
  --host-attach
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

En cas de coupure du relais, l'hôte retente avec un backoff borné ; les
sessions en cours échouent proprement (`relay_lost`, PTY fermé) et ne sont
pas rejouées. Le service s'arrête sur SIGTERM/SIGINT, ferme les sessions et
libère le port.

## Attach distant : session, bail, journal (D3, #7121)

`--host-attach` remplace la commande statique par le wrapper d'attach de
l'hôte (`ameesh attach --host-attach <agent>`). Le poste utilise la même
commande `ameesh attach` que pour un agent local ; le chemin distant est
choisi dès qu'une option distante est donnée (`--relay`, `--receipt`,
`--grant`, `--device-key-file`, `--host`) ou que l'agent est épinglé sur un
autre hôte.

```sh
# poste : reçu signé déjà écrit, clé d'appareil et clé d'hôte 0600
ameesh attach avatar-alice --host ameesh-agents-1 \
  --relay https://relay.interne:8790 --receipt ~/.config/ameesh/attach-receipt.json \
  --device-key-file ~/.config/ameesh/device.key \
  --host-key-file ~/.config/ameesh/attach-host.pub
```

- **Bail** : `attach:<tenant>:<appareil>` (conception §5.4), TTL court de
  60 s renouvelé tous les 20 s ; une coupure le laisse expirer et le
  résident reprend la session. Le bail local garde son TTL d'attach.
- **Attente de tour** : `REQ` porte `wait` (option `--wait`) ; sans elle un
  tour en cours est refusé (`turn_running`), avec elle l'hôte attend la fin
  du tour, bornée par `--max`. Un tour en cours n'est jamais interrompu.
- **Journal** : l'hôte imprime la dernière page du journal (A1) dans le PTY
  avant le harnais ; les échanges de la session sont écrits par les hooks A0
  (`UserPromptSubmit`/`Stop`) sous `AMEESH_ATTACH=1`, avec l'humain **vérifié
  du grant** comme auteur (`AMEESH_ATTACH_HUMAN`), jamais un nom fourni par
  le poste.
- **Copie enfant** : si l'agent attaché est un enfant, chaque message humain
  est recopié au journal du parent (design §4.2), seulement si parent et
  enfant partagent le même `responsible`, avec `meta={child, copied_from}`.
- **Bornes** : `--max` du poste est porté par la trame `REQ` jusqu'à l'argv
  du wrapper de l'hôte (et reste borné par la session du relais) ; un
  `SIGWINCH` relit la taille réelle du terminal avant chaque `WINSIZE`.
- **Coupure** : le wrapper installe SIGTERM/SIGHUP/SIGINT, arrête le groupe
  du harnais et rend le bail dans `finally` ; l'événement `agent.attach`
  (accepté ou refusé) ne porte ni reçu, ni nonce, ni signature.
- **Refus nommés** : grant absent/expiré/rejoué, autre humain, appareil
  différent, tour en cours sans `--wait`, agent inconnu — chacun journalisé,
  code stable côté poste.

## Tests

`tests/test_7120_attach_relay.py` couvre les bornes de trames (échéance
unique, y compris sous arrivée lente), la poignée de main et la signature
d'hôte, le rejeu, l'altération, le mauvais contexte, le PTY (echo, resize,
signal, fermeture), l'appariement et les refus du relais (dont la
vérification du reçu), les quotas et leur restitution, la reconnexion de
l'hôte, TLS et le CLI, et l'absence de contenu dans les journaux. Tout tourne
sur `127.0.0.1`, sans réseau externe. Les tests D1
(`tests/test_7119_attach_grant.py`) couvrent la preuve d'appareil liée au
transcript.
