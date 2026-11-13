# Relais d'attach (D2, lot #7120)

Transport de l'attach distant : un **service de relais** apparie le poste
d'un humain et la **connexion sortante** de l'hôte de l'agent, puis relaie
des trames chiffrées de bout en bout autour d'un PTY. Ce lot livre le
service, la connexion de l'hôte, le PTY, les trames, l'E2E, les quotas et
les journaux. Il ne livre pas `ameesh attach` distant : le bail, l'attente
de tour, la reprise et le journal A0 restent à D3 (#7121).

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
  `session_id`. Il ne se connecte à aucune base et ne conserve rien après la
  session.
- **Hôte** : ouvre **une** connexion sortante vers le relais, s'authentifie
  par un jeton, reçoit les `PAIR`, vérifie le grant avec D1, ouvre un PTY
  pour la commande interactive et pont les octets. Aucun port entrant.
- **Poste** : parle le protocole de `ameesh.attach_transport` (KEY, REQ,
  DATA, WINSIZE, SIGNAL, ACK, CLOSE). En D2, la station est le client de ce
  module ; D3 fournira la commande `ameesh attach` distante.

## Décisions de confiance

- Le relais **pré-contrôle** le reçu (`ameesh-receipt/1` de D1) : forme,
  nature `attach`, liaison agent/hôte/tenant, `iat`/`exp` cohérents,
  validité ≤ 300 s, non expiré, digest canonique. Il **ne vérifie pas** la
  signature WebAuthn et n'a pas de base.
- La **porte faisant foi est l'hôte** : à la réception du `REQ`, il appelle
  `attach_grant.redeem` (D1) avec la politique `AMEESH_APPROVE_RP_ID` /
  `AMEESH_APPROVE_ORIGINS`, la signature de la clé d'appareil et le canon de
  l'agent. Le grant est à usage unique ; un refus ne crée pas de PTY et
  n'ouvre aucune session.
- L'E2E protège le contenu **contre le relais** : clés X25519 éphémères,
  HKDF-SHA-256 salé par la transcription (protocole, session, agent, hôte,
  tenant, `attach_id`, digest du reçu, clés publiques), AES-256-GCM par
  direction, nonce = séquence, AAD liant type, séquence et transcription.
  Une trame rejouée, réordonnée, altérée ou d'un autre contexte est refusée.
- Un relais **activement malveillant** (qui altère le routage ou monte un
  faux hôte) n'est pas couvert par l'E2E seule ; le modèle D2 est celui d'un
  relais **interne au mesh, cru pour le routage**, avec l'hôte comme seule
  autorité d'ouverture. Une exposition publique devrait ajouter la
  vérification du reçu côté relais et une décision owner.

## Protocole

Trame sur le fil : `longueur u32 | session 16 octets | type 1 octet | charge`.
La longueur est bornée avant lecture (`MAX_FRAME` = 64 Kio) ; les charges de
session sont bornées par type (`MAX_DATA` = 16 Kio). Une trame commencée mais
incomplète est bornée (30 s) puis ferme la connexion : jamais de
resynchronisation au milieu du flux. Une session de contrôle avant appariement
porte l'identifiant nul.

1. Le poste envoie `OPEN` (agent, hôte, tenant, reçu) ; l'hôte envoie
   `HELLO` (hôte, tenant, jeton). Le relais vérifie le jeton d'hôte en
   temps constant, pré-contrôle le reçu, puis envoie `PAIR` à l'hôte et
   `PAIRED` au poste avec le `session_id`, l'`attach_id` et l'empreinte du
   reçu.
2. Les deux extrémités échangent `KEY` puis `CONFIRM` dans la session ;
   toute trame de session avant la confirmation est refusée.
3. Le poste envoie `REQ` (reçu, signature de l'appareil, clé d'appareil,
   taille de fenêtre, `TERM`). L'hôte vérifie, ouvre le PTY et répond `OK`
   (ou `NO` avec un code stable). Les `DATA` circulent ensuite dans les deux
   sens, avec `WINSIZE`, `SIGNAL`, `ACK` et `CLOSE`.
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
dans l'environnement ni dans un journal. Le service refuse une adresse
d'écoute hors boucle locale/mesh et refuse le clair hors boucle locale.

```sh
# service (sur un hôte du mesh, TLS obligatoire hors boucle locale)
ameesh attach-relay serve --listen 10.20.0.5:8790 --mesh-cidr 10.20.0.0/16 \
  --hosts-file /etc/ameesh/attach-hosts.json \
  --tls-cert /etc/ameesh/tls/relay.pem --tls-key /etc/ameesh/tls/relay-key.pem

# hôte de l'agent : une connexion sortante, la commande interactive en argv
ameesh attach-relay host --relay https://relay.interne:8790 \
  --host ameesh-agents-1 --token-file /etc/ameesh/attach-token \
  --ca-file /etc/ameesh/tls/ca.pem \
  --command 'ameesh runner attach --agent avatar-alice'
```

`--command` est un argv découpé par `shlex.split`, **jamais** passé à un
shell. L'enfant reçoit `AMEESH_ATTACH_REMOTE=1` et `TERM` du poste. Sur un
hôte partagé, `--tenant` et `--agent` restreignent ce que l'hôte sert.

Unité systemd utilisateur (l'application survit au tour ; jamais lancée
depuis le shell du tour) :

```ini
[Unit]
Description=ameesh attach relay host
After=network-online.target

[Service]
ExecStart=/usr/local/bin/ameesh attach-relay host --relay https://relay.interne:8790 \
  --host ameesh-agents-1 --token-file %h/.config/ameesh/attach-token \
  --ca-file %h/.config/ameesh/tls/ca.pem --command 'ameesh runner attach --agent avatar-alice'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

En cas de coupure du relais, l'hôte retente avec un backoff borné ; les
sessions en cours échouent proprement (`relay_lost`, PTY fermé) et ne sont
pas rejouées. Le service s'arrête sur SIGTERM/SIGINT, ferme les sessions et
libère le port.

## Tests

`tests/test_7120_attach_relay.py` couvre les bornes de trames, la poignée de
main, le rejeu, l'altération, le mauvais contexte, le PTY (echo, resize,
signal, fermeture), l'appariement et les refus du relais, les quotas, la
reconnexion de l'hôte, TLS et le CLI, et l'absence de contenu dans les
journaux. Tout tourne sur `127.0.0.1`, sans base ni réseau externe.
