set -u
apt-get -qq update >/dev/null && apt-get -qq install -y iptables >/dev/null 2>&1; iptables --version
install -m 755 /t/wg /usr/local/bin/wg; sed 's|PUB_FILE=/etc/ameesh-bg-peer/bureau-goal.pub|PUB_FILE=/t/pub|' /t/ameesh-bg-peer.sh > /s.sh
iptables -P FORWARD DROP; iptables -N DOCKER-USER; iptables -A DOCKER-USER -j RETURN; iptables -A FORWARD -j DOCKER-USER
echo "== start 1"; bash /s.sh start; echo rc=$?
echo "== start 2 (idempotence)"; bash /s.sh start; echo rc=$?
iptables -S | grep -E "10.77.0.6|BG-IN"; cat /tmp/wgstate
echo "== stop"; bash /s.sh stop; echo rc=$?
echo "== après stop"; iptables -S | grep -E "10.77.0.6|BG-IN" || echo "(aucune règle .6 / BG-IN)"; iptables -S DOCKER-USER; cat /tmp/wgstate; echo "(fin wg)"
echo "== stop sans DOCKER-USER"; bash /s.sh start >/dev/null; iptables -D DOCKER-USER -s 10.77.0.6/32 -i wg0 -j DROP; iptables -D FORWARD -j DOCKER-USER; iptables -F DOCKER-USER; iptables -X DOCKER-USER; bash /s.sh stop; echo rc=$?; iptables -S | grep -E "10.77.0.6|BG-IN" || echo "(aucune règle .6 / BG-IN)"
