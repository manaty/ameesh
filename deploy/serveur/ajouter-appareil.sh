#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# Ajoute un appareil (poste d'un humain, autre serveur) au réseau WireGuard du
# mesh (lot L55). À lancer en root sur le serveur :
#
#   ajouter-appareil.sh <nom>        → affiche la configuration WireGuard de l'appareil
#   ajouter-appareil.sh --liste      → appareils déclarés
#
# La clé privée de l'appareil n'est affichée qu'une fois et n'est pas gardée.
set -euo pipefail
RACINE=${RACINE:-}  # tests seulement
CONF=$RACINE/etc/wireguard/wg0.conf
NET=10.77.0
if [[ ${1:-} == --liste ]]; then grep -E '^# appareil ' "$CONF" || echo "aucun appareil"; exit; fi
nom=${1:?nom de l\'appareil}
[[ $nom =~ ^[a-z0-9-]{1,32}$ ]] || { echo "nom invalide : $nom" >&2; exit 2; }
grep -q "^# appareil $nom " "$CONF" && { echo "$nom est déjà déclaré" >&2; exit 2; }
# #7002 : une adresse est prise si wg0.conf, wg0 ou une unité de pair hors de
# wg0.conf (X-Ameesh-Pair-WireGuard, ameesh-bg-peer.service) la porte.
actifs=$(wg show wg0 allowed-ips)  # wg0 illisible : arrêt, pas d'adresse au hasard
pris=$(cat "$CONF"; printf '%s\n' "$actifs"; cat "$RACINE"/etc/systemd/system/*.service 2>/dev/null || true)
for n in $(seq 2 254); do grep -qE "(^|[ =,	])${NET//./\\.}\\.$n/32\b" <<<"$pris" || break; done
cle=$(wg genkey); pub=$(echo "$cle" | wg pubkey)
cat >> "$CONF" <<CONF

# appareil $nom $NET.$n
[Peer]
PublicKey = $pub
AllowedIPs = $NET.$n/32
CONF
# Le seul pair nouveau : `wg syncconf` retirerait les pairs posés hors de
# wg0.conf (#7002).
wg set wg0 peer "$pub" allowed-ips "$NET.$n/32"
ip=$(curl -fsS --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
port=$(awk -F' *= *' '/^ListenPort/{print $2}' "$CONF")
cat <<OUT
# --- /etc/wireguard/ameesh.conf sur l'appareil « $nom » (clé privée : à garder secrète) ---
[Interface]
Address = $NET.$n/32
PrivateKey = $cle

[Peer]
PublicKey = $(cat "$RACINE"/etc/wireguard/serveur.pub)
Endpoint = $ip:$port
AllowedIPs = $NET.1/32
PersistentKeepalive = 25
# --- puis : wg-quick up ameesh ; base du mesh : postgresql://ameesh@$NET.1:5432/ameesh?sslmode=require
OUT
