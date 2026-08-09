#!/usr/bin/env python3
"""
salt-detector-daemon.py — sample the salt:detector on a loop, store to MySQL.

Runs under systemd as a long-lived service. Alerting is deliberately not done
here; salt-detector-alert.py reads the database on a cron schedule instead.

  ./salt-detector-daemon.py                     # run the loop (systemd calls this)
  ./salt-detector-daemon.py --once              # single run, then exit
  ./salt-detector-daemon.py --calibrate         # read without storing
  ./salt-detector-daemon.py --init-db           # create the tables
  ./salt-detector-daemon.py --info              # query device info
  ./salt-detector-daemon.py -c /path/to.conf    # explicit config

Protocol, reverse engineered from the device. The vendor endpoint
(iot.thinkwater.com:30103) was retired after the Culligan acquisition, so the
device is redirected to a local mosquitto broker by a DNS override. Command
payloads are bare scalars unless noted:

    publish  device/update/telemetry/<id>   "1"
    reply    device/telemetry               {"device_id":..., "level":<mm>,
                                             "timestamp":"YYYY-MM-DD HH:MM:SS",
                                             "meas_type":0}
    publish  device/info/request/<id>       "1"    -> device/info
    publish  device/update/timestamp/<id>   {"timestamp":"YYYY-MM-DD HH:MM:SS"}
    publish  device/admin/reset/<id>        "1"    -> factory reset, wipes wifi

Gotchas:
  * `level` is a raw ToF distance in millimetres, not a percentage
  * 0 means "no valid target", not "empty" — never store it
  * the timestamp topic wants a JSON object; a bare string reboots the device
  * never publish retained messages to device/feedback/<id> — the device
    resubscribes on boot, receives it again, and crash-loops
"""

import argparse
import json
import re
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

CONFIG_PATHS = [
    Path("salt-detector.conf"),
    Path.home() / ".config" / "salt-detector.conf",
    Path("/etc/salt-detector.conf"),
]

_stop = False


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}", flush=True)


# ------------------------------------------------------------------ config --

def strip_json5(text):
    """
    Minimal JSON5 -> JSON fallback for when no json5 library is installed.
    Handles // and /* */ comments and trailing commas, while leaving string
    contents (such as URLs) untouched. Keys in the shipped config are quoted,
    so that is sufficient.
    """
    out = []
    i, n = 0, len(text)
    in_str = False
    quote = ""
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                in_str = False
            i += 1
            continue
        if c in "\"'":
            in_str, quote = True, c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_config(path=None):
    candidates = [Path(path)] if path else CONFIG_PATHS
    for p in candidates:
        if not p.is_file():
            continue
        text = p.read_text()
        for mod in ("json5", "pyjson5"):
            try:
                lib = __import__(mod)
                cfg = lib.loads(text)
                cfg["_path"] = str(p)
                return cfg
            except ImportError:
                continue
            except Exception as exc:
                raise SystemExit(f"{p}: could not parse: {exc}")
        try:
            cfg = json.loads(strip_json5(text))
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"{p}: parse error at line {exc.lineno}: {exc.msg}\n"
                "the built-in fallback handles comments and trailing commas "
                "but needs quoted keys. for full JSON5 support:\n"
                "  pip install json5"
            )
        cfg["_path"] = str(p)
        return cfg
    raise SystemExit(
        "no config found. looked in:\n  " +
        "\n  ".join(str(p) for p in candidates) +
        "\ncopy salt-detector-example.conf to salt-detector.conf and edit it."
    )


# -------------------------------------------------------------------- mqtt --

class Device:
    """Talks to the salt:detector over MQTT via the mosquitto client binaries."""

    def __init__(self, cfg):
        m, s = cfg["mqtt"], cfg["sensor"]
        self.host = m["host"]
        self.port = int(m["port"])
        self.device_id = m["device_id"]
        self.timeout = int(m.get("timeout", 15))
        self.user = m.get("username")
        self.password = m.get("password")

        self.full_mm = int(s["full_mm"])
        self.empty_mm = int(s["empty_mm"])
        self.min_mm = int(s.get("min_valid_mm", 40))
        self.max_mm = int(s.get("max_valid_mm", 700))
        self.n_samples = int(s.get("samples", 5))
        self.sample_gap = int(s.get("sample_gap", 3))

    def _auth(self):
        return ["-u", self.user, "-P", self.password or ""] if self.user else []

    def publish(self, topic, payload):
        subprocess.run(
            ["mosquitto_pub", "-h", self.host, "-p", str(self.port),
             *self._auth(), "-t", topic, "-m", payload],
            check=True, capture_output=True,
        )

    def request(self, pub_topic, payload, sub_topic):
        """Subscribe, publish, return the first reply as a dict (or None)."""
        sub = subprocess.Popen(
            ["mosquitto_sub", "-h", self.host, "-p", str(self.port),
             *self._auth(), "-t", sub_topic, "-C", "1", "-W", str(self.timeout)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        time.sleep(1)                       # let the subscription register
        try:
            self.publish(pub_topic, payload)
        except subprocess.CalledProcessError as exc:
            sub.kill()
            log(f"publish failed: {exc}")
            return None
        try:
            out, _ = sub.communicate(timeout=self.timeout + 5)
        except subprocess.TimeoutExpired:
            sub.kill()
            return None
        out = (out or "").strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            log(f"unparseable reply: {out[:120]}")
            return None

    def set_clock(self):
        """The device clock resets to 1970 on reboot; push the real time."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self.publish(f"device/update/timestamp/{self.device_id}",
                         json.dumps({"timestamp": now}))
            return True
        except subprocess.CalledProcessError:
            return False

    def info(self):
        return self.request(f"device/info/request/{self.device_id}", "1",
                            "device/info")

    def read_distance(self):
        """One reading in mm, or None if missing or out of range."""
        reply = self.request(f"device/update/telemetry/{self.device_id}", "1",
                             "device/telemetry")
        if not reply:
            return None
        mm = reply.get("level")
        if not isinstance(mm, (int, float)):
            return None
        mm = int(mm)
        if mm < self.min_mm or mm > self.max_mm:
            return None
        return mm

    def sample(self, n=None, gap=None):
        """Median of several valid readings. Returns (median, [samples])."""
        n = n or self.n_samples
        gap = gap if gap is not None else self.sample_gap
        vals = []
        for i in range(n):
            mm = self.read_distance()
            if mm is not None:
                vals.append(mm)
                log(f"  sample {i + 1}/{n}: {mm} mm")
            else:
                log(f"  sample {i + 1}/{n}: no valid reading")
            if i < n - 1:
                time.sleep(gap)
        if not vals:
            return None, []
        return int(statistics.median(vals)), vals

    def to_percent(self, mm):
        if self.empty_mm == self.full_mm:
            return None
        pct = 100.0 * (self.empty_mm - mm) / (self.empty_mm - self.full_mm)
        return round(max(0.0, min(100.0, pct)), 1)


# ---------------------------------------------------------------- database --

def db_connect(cfg):
    d = cfg["database"]
    params = dict(host=d["host"], port=int(d.get("port", 3306)),
                  user=d["user"], password=d["password"], database=d["database"])
    try:
        import mysql.connector
        return mysql.connector.connect(**params)
    except ImportError:
        pass
    try:
        import pymysql
        return pymysql.connect(**params)
    except ImportError:
        raise SystemExit(
            "no MySQL driver found. install one of:\n"
            "  sudo dnf install python3-PyMySQL\n"
            "  pip install mysql-connector-python"
        )


def init_db(cfg):
    d = cfg["database"]
    conn = db_connect(cfg)
    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {d['readings_table']} (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            ts          DATETIME     NOT NULL,
            device_id   VARCHAR(32)  NOT NULL,
            distance_mm INT          NOT NULL,
            percent     DECIMAL(5,1)     NULL,
            samples     VARCHAR(128)     NULL,
            INDEX (ts),
            INDEX (device_id, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS {d['alerts_table']} (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            ts          DATETIME     NOT NULL,
            device_id   VARCHAR(32)  NOT NULL,
            reason      VARCHAR(32)  NOT NULL,
            distance_mm INT              NULL,
            percent     DECIMAL(5,1)     NULL,
            INDEX (device_id, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    conn.commit()
    cur.close()
    conn.close()


def store_reading(cfg, mm, pct, vals):
    d = cfg["database"]
    conn = db_connect(cfg)
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {d['readings_table']} "
        "(ts, device_id, distance_mm, percent, samples) VALUES (%s,%s,%s,%s,%s)",
        (datetime.now(), cfg["mqtt"]["device_id"], mm, pct,
         ",".join(str(v) for v in vals)),
    )
    conn.commit()
    cur.close()
    conn.close()


# -------------------------------------------------------------------- main --

def handle_signal(signum, frame):
    global _stop
    _stop = True
    log(f"signal {signum} received, finishing current cycle")


def measure_once(cfg, dev, store=True):
    if cfg["daemon"].get("set_clock", True):
        if not dev.set_clock():
            log("warning: could not set the device clock")

    mm, vals = dev.sample()
    if mm is None:
        log("no valid reading — nothing stored")
        return False

    pct = dev.to_percent(mm)
    log(f"distance {mm} mm  ->  {pct}%")

    if not store:
        return True
    try:
        store_reading(cfg, mm, pct, vals)
        log("stored")
        return True
    except Exception as exc:
        log(f"database write failed: {exc}")
        return False


def calibrate(cfg, dev):
    log("calibration mode — nothing will be stored")
    log("run once with the container FULL, once with it EMPTY, then put those")
    log("two figures into sensor.full_mm and sensor.empty_mm")
    mm, vals = dev.sample(n=8, gap=2)
    if mm is None:
        log("no valid readings. is the device online, and is anything within")
        log(f"range ({dev.min_mm}-{dev.max_mm} mm)? sunlight defeats the sensor.")
        return 1
    log(f"median {mm} mm from {vals}")
    log(f"current calibration maps that to {dev.to_percent(mm)}%")
    return 0


def loop(cfg, dev):
    interval = int(cfg["daemon"].get("interval", 3600))
    delay = int(cfg["daemon"].get("startup_delay", 30))

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log(f"daemon starting: device={dev.device_id} "
        f"broker={dev.host}:{dev.port} interval={interval}s")
    log(f"calibration: full={dev.full_mm}mm empty={dev.empty_mm}mm")

    if delay and not _stop:
        log(f"waiting {delay}s for the network to settle")
        for _ in range(delay):
            if _stop:
                break
            time.sleep(1)

    while not _stop:
        started = time.monotonic()
        try:
            measure_once(cfg, dev)
        except Exception as exc:
            log(f"cycle failed: {exc}")

        remaining = max(1, interval - (time.monotonic() - started))
        while remaining > 0 and not _stop:
            time.sleep(min(5, remaining))
            remaining -= 5

    log("daemon stopped")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help="path to salt-detector.conf")
    ap.add_argument("--once", action="store_true", help="one measurement, then exit")
    ap.add_argument("--calibrate", action="store_true", help="read without storing")
    ap.add_argument("--init-db", action="store_true", help="create the tables")
    ap.add_argument("--info", action="store_true", help="query device info")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log(f"config: {cfg['_path']}")
    dev = Device(cfg)

    if args.init_db:
        init_db(cfg)
        log("tables ready")
        return 0
    if args.info:
        info = dev.info()
        log(info if info else "no reply from device")
        return 0 if info else 1
    if args.calibrate:
        return calibrate(cfg, dev)
    if args.once:
        return 0 if measure_once(cfg, dev) else 1
    return loop(cfg, dev)


if __name__ == "__main__":
    sys.exit(main())
