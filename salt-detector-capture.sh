#!/usr/bin/env bash
#
# salt-detector-capture.sh — capture EVERYTHING the device does.
#
# Every earlier capture was port-filtered (53/123/443/8883/30103), so any
# other protocol the device speaks would have been invisible. This captures
# all traffic to and from the device IP, all DNS on the LAN, and the MQTT
# stream, with synchronised timestamps.
#
# The white LED has only ever been seen shortly after a fresh BLE pairing from
# the app — and the app talks to cloud.thinkwater.com (still alive) before
# handing configuration to the device. So the interesting window is: start the
# capture, factory reset, re-pair through the app, and watch what happens as
# the LED goes white and then decays back to blinking.
#
#   sudo ./salt-detector-capture.sh start    # begin capturing
#   sudo ./salt-detector-capture.sh stop     # stop and summarise
#   sudo ./salt-detector-capture.sh report   # analyse what was captured
#
set -uo pipefail

IFACE=${IFACE:-eth1}
DEVIP=${DEVIP:-192.168.1.59}
DEVMAC=${DEVMAC:-xx:xx:xx:xx:xx:xx}
BROKER=${BROKER:-127.0.0.1}
BPORT=${BPORT:-1883}
OUT=${OUT:-/tmp/salt-detector-capture}

PCAP="$OUT/all-traffic.pcap"
MQTTLOG="$OUT/mqtt.log"
DNSLOG="$OUT/dns.log"
PIDFILE="$OUT/pids"

need() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }

start() {
    need tcpdump; need mosquitto_sub
    mkdir -p "$OUT"
    : > "$PIDFILE"

    # 1. everything to or from the device, no port filter at all.
    #    Matched on MAC as well as IP so we still see it if the IP changes
    #    or it falls back to DHCP/link-local after a reset.
    tcpdump -i "$IFACE" -n -s0 -w "$PCAP" \
        "host $DEVIP or ether host $DEVMAC" 2>/dev/null &
    echo $! >> "$PIDFILE"

    # 2. all DNS on the segment, so we see any new hostname it looks up
    tcpdump -i "$IFACE" -n -l "port 53" 2>/dev/null \
        | ts_prefix > "$DNSLOG" &
    echo $! >> "$PIDFILE"

    # 3. the MQTT side, timestamped to match
    mosquitto_sub -h "$BROKER" -p "$BPORT" -t '#' -F '%I %t %p' \
        > "$MQTTLOG" 2>/dev/null &
    echo $! >> "$PIDFILE"

    sleep 1
    echo "capturing to $OUT"
    echo "  $PCAP     all traffic (no port filter)"
    echo "  $DNSLOG   dns queries"
    echo "  $MQTTLOG  mqtt"
    echo
    echo "now, in this order:"
    echo "  1. note the current LED state"
    echo "  2. factory reset:  mosquitto_pub -h $BROKER -p $BPORT \\"
    echo "        -t device/admin/reset/<device-id> -m 1"
    echo "  3. re-pair through the app over Bluetooth"
    echo "  4. watch the LED. note the time it goes white."
    echo "  5. leave it running until it goes back to blinking, then:"
    echo "        sudo $0 stop"
    echo
    echo "write down the times of every LED change — they are the whole point."
}

ts_prefix() { while IFS= read -r l; do echo "$(date '+%H:%M:%S') $l"; done; }

stop() {
    if [ ! -f "$PIDFILE" ]; then echo "not running"; exit 1; fi
    while read -r p; do kill "$p" 2>/dev/null; done < "$PIDFILE"
    rm -f "$PIDFILE"
    sleep 1
    echo "stopped."
    report
}

report() {
    need tcpdump
    [ -f "$PCAP" ] || { echo "no capture at $PCAP"; exit 1; }

    echo
    echo "=== every remote endpoint the device talked to ==================="
    tcpdump -nr "$PCAP" 2>/dev/null \
        | grep -oE "> [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:" \
        | sort | uniq -c | sort -rn | head -30

    echo
    echo "=== protocols and ports seen ====================================="
    tcpdump -nr "$PCAP" 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/) print $i}' \
        | awk -F. '{print $5}' | sort -n | uniq -c | sort -rn | head -20

    echo
    echo "=== non-MQTT traffic (anything not on 30103) ====================="
    tcpdump -nr "$PCAP" 2>/dev/null 'not port 30103' | head -40

    echo
    echo "=== dns lookups by the device ===================================="
    grep -i "$DEVIP" "$DNSLOG" 2>/dev/null | grep -oE 'A\? [^ ]+' \
        | sort | uniq -c | sort -rn | head -20

    echo
    echo "=== any plaintext strings in the capture ========================="
    strings "$PCAP" 2>/dev/null \
        | grep -aiE 'http|thinkwater|culligan|token|auth|user|provision' \
        | sort -u | head -30

    echo
    echo "=== mqtt topics seen ============================================="
    awk '{print $2}' "$MQTTLOG" 2>/dev/null | sort | uniq -c | sort -rn | head -20

    echo
    echo "=== device-originated mqtt (the interesting half) ================"
    grep -E ' device/(telemetry|info|hardware|provisioning)' "$MQTTLOG" 2>/dev/null \
        | tail -25

    echo
    echo "full capture: $PCAP"
    echo "open in wireshark, or:  tcpdump -nr $PCAP -A | less"
}

case "${1:-start}" in
    start)  start ;;
    stop)   stop ;;
    report) report ;;
    *) echo "usage: $0 [start|stop|report]"; exit 1 ;;
esac

