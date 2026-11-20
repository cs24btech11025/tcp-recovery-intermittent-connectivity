#!/usr/bin/env bash
# Usage: sudo ./setup_link.sh [BUF_KB=125] [BASE_LOSS_PCT=0]
#   BUF_KB        tbf byte limit = bottleneck buffer (125 ~ 1 BDP, 300 = large)
#   BASE_LOSS_PCT random loss on the data direction (0, 0.21, 1.35)
# Re-running tears down and rebuilds, so it is safe to call between configs.
set -euo pipefail
BUF_KB="${1:-125}"
LOSS="${2:-0}"

ip netns del srv 2>/dev/null || true
ip netns del cli 2>/dev/null || true

ip netns add srv; ip netns add cli
ip link add veth-s type veth peer name veth-c
ip link set veth-s netns srv; ip link set veth-c netns cli
ip netns exec srv ip addr add 10.0.0.1/24 dev veth-s
ip netns exec cli ip addr add 10.0.0.2/24 dev veth-c
ip netns exec srv ip link set veth-s up
ip netns exec cli ip link set veth-c up
ip netns exec srv ip link set lo up
ip netns exec cli ip link set lo up

# offloads off so qdiscs see real packets
ip netns exec srv ethtool -K veth-s tso off gso off gro off
ip netns exec cli ethtool -K veth-c tso off gso off gro off

LOSS_ARG=""
if [ "$LOSS" != "0" ]; then LOSS_ARG="loss ${LOSS}%"; fi

# server -> client: 50 ms delay (netem, big limit), then 10 Mbit/s tbf whose byte limit is the buffer
ip netns exec srv tc qdisc add dev veth-s root handle 1: netem delay 50ms limit 1000 $LOSS_ARG
ip netns exec srv tc qdisc add dev veth-s parent 1:1 handle 10: tbf rate 10mbit burst 15kb limit "${BUF_KB}kb"
# client -> server (ACKs): 50 ms delay only
ip netns exec cli tc qdisc add dev veth-c root netem delay 50ms limit 1000

echo "== link ready: buffer=${BUF_KB}KB, base loss=${LOSS}% =="
ip netns exec srv tc qdisc show dev veth-s
ip netns exec cli tc qdisc show dev veth-c
echo "== sanity: ping RTT should be ~100 ms idle; ~200 ms with a full 125 KB buffer under iperf3 =="
