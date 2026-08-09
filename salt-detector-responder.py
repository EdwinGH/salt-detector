#!/usr/bin/env python3
"""
salt-detector-responder.py — reply to provisioning the instant it arrives.

Every previous attempt published to device/feedback/<id> asynchronously,
whenever the probe script got around to it. The device publishes
device/provisioning at QoS 1 and then waits, so it is plausible the firmware
only accepts a reply inside a short window after its own publish and discards
anything that arrives cold.

This subscribes, and the millisecond a provisioning message appears, fires a
candidate reply. One candidate per provisioning cycle (~60s). It then watches
for the two things that would indicate success:

  * provisioning stops recurring
  * any message appears under device/hardware/* — the namespace the device
    uses to acknowledge commands it understood (confirmed with admin/reset)

Requires paho-mqtt for the low-latency path:
    pip install paho-mqtt        (or: sudo dnf install python3-paho-mqtt)

  ./salt-detector-responder.py             # walk the candidate list
  ./salt-detector-responder.py --listen    # observe only, no replies
  ./salt-detector-responder.py --payload '1'   # hammer one candidate
"""

import argparse
import os
import json
import sys
import time
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("needs paho-mqtt:\n"
             "  sudo dnf install python3-paho-mqtt\n"
             "  or: pip install paho-mqtt")

BROKER = "127.0.0.1"
PORT = 1883
DEV = os.environ.get("SALTD_DEV", "xxxx-xxxx-xxxx")
FEEDBACK = f"device/feedback/{DEV}"

# One candidate per provisioning cycle. Scalars first: commands on this device
# take bare scalars (`1` on device/admin/reset triggered a real factory reset),
# so the JSON objects we spent most of the search on may always have been the
# wrong shape for this topic too.
CANDIDATES = [
    "1",
    "0",
    "true",
    "ok",
    DEV,
    "{}",
    '{"status":"ok"}',
    f'{{"device_id":"{DEV}"}}',
    f'{{"device_id":"{DEV}","status":"ok"}}',
    f'{{"device_id":"{DEV}","status":1}}',
    f'{{"device_id":"{DEV}","registered":true}}',
    f'{{"device_id":"{DEV}","timestamp":"{datetime.now():%Y-%m-%d %H:%M:%S}"}}',
]


def log(msg):
    print(f"{datetime.now():%H:%M:%S.%f}"[:-3] + f"  {msg}", flush=True)


class Responder:
    def __init__(self, candidates, listen_only=False):
        self.candidates = candidates
        self.listen_only = listen_only
        self.idx = 0
        self.last_sent = None
        self.last_sent_at = 0.0
        self.provisioning_seen = 0
        self.hardware_seen = []

    def on_connect(self, client, userdata, flags, rc, properties=None):
        log(f"connected to {BROKER}:{PORT} (rc={rc})")
        client.subscribe("device/#", qos=0)

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", "replace")

        # ignore our own feedback publishes echoing back
        if topic == FEEDBACK:
            return

        if topic.startswith("device/hardware/"):
            log(f"*** {topic}  {payload}")
            self.hardware_seen.append((topic, payload, self.last_sent))
            log(f"*** the device acknowledged something. last sent: "
                f"{self.last_sent!r}")
            return

        if topic == "device/provisioning":
            self.provisioning_seen += 1
            log(f"provisioning #{self.provisioning_seen}")

            # did the previous candidate work? provisioning recurring says no
            if self.last_sent is not None:
                gap = time.monotonic() - self.last_sent_at
                log(f"    -> previous candidate {self.last_sent!r} rejected "
                    f"(provisioning returned after {gap:.1f}s)")

            if self.listen_only:
                return
            if self.idx >= len(self.candidates):
                log("candidate list exhausted")
                client.disconnect()
                return

            cand = self.candidates[self.idx]
            self.idx += 1
            # fire immediately — the whole point of this script
            client.publish(FEEDBACK, cand, qos=0)
            self.last_sent = cand
            self.last_sent_at = time.monotonic()
            log(f"    <- [{self.idx}/{len(self.candidates)}] {cand}")
            return

        if topic in ("device/telemetry", "device/info"):
            return                              # routine, not interesting here
        log(f"    {topic}  {payload[:120]}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--listen", action="store_true",
                    help="observe provisioning timing without replying")
    ap.add_argument("--payload", help="send this one payload every cycle")
    ap.add_argument("--host", default=BROKER)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    cands = [args.payload] * 20 if args.payload else CANDIDATES
    r = Responder(cands, listen_only=args.listen)

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()              # paho 1.x
    client.on_connect = r.on_connect
    client.on_message = r.on_message

    log("waiting for device/provisioning — it arrives roughly every 60s")
    if args.listen:
        log("listen-only: no replies will be sent")
    else:
        log(f"{len(cands)} candidates, so allow ~{len(cands)} minutes")

    client.connect(args.host, args.port, 60)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        log("stopped")

    if r.hardware_seen:
        print()
        log("device/hardware/* messages seen during this run:")
        for t, p, sent in r.hardware_seen:
            log(f"  {t}  {p}   (after sending {sent!r})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

