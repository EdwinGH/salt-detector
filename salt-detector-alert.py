#!/usr/bin/env python3
"""
salt-detector-alert.py — check the stored salt level and email if it is low.

Reads the database only; it never talks to the device. Intended for cron:

    0 20 * * *  /opt/salt-detector/salt-detector-alert.py \
                  -c /etc/salt-detector.conf \
                  >> /var/log/salt-detector-alert.log 2>&1

  ./salt-detector-alert.py                    # normal check
  ./salt-detector-alert.py --dry-run          # decide, but do not send
  ./salt-detector-alert.py --force            # ignore threshold and cooldown
  ./salt-detector-alert.py --status           # print recent readings and exit
  ./salt-detector-alert.py -c /path/to.conf   # explicit config

Deliberately independent of the daemon: if the daemon dies, this notices the
stale data and tells you about the monitoring rather than the salt.
"""

import argparse
import json
import re
import smtplib
import socket
import subprocess
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

CONFIG_PATHS = [
    Path("salt-detector.conf"),
    Path.home() / ".config" / "salt-detector.conf",
    Path("/etc/salt-detector.conf"),
]


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


def latest_reading(cfg):
    d = cfg["database"]
    conn = db_connect(cfg)
    cur = conn.cursor()
    cur.execute(
        f"SELECT ts, distance_mm, percent, samples FROM {d['readings_table']} "
        "WHERE device_id = %s ORDER BY ts DESC LIMIT 1",
        (cfg["mqtt"]["device_id"],),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def recent_readings(cfg, days=30):
    d = cfg["database"]
    conn = db_connect(cfg)
    cur = conn.cursor()
    cur.execute(
        f"SELECT ts, distance_mm, percent FROM {d['readings_table']} "
        "WHERE device_id = %s AND ts > %s ORDER BY ts",
        (cfg["mqtt"]["device_id"], datetime.now() - timedelta(days=days)),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def last_alert(cfg):
    d = cfg["database"]
    conn = db_connect(cfg)
    cur = conn.cursor()
    cur.execute(
        f"SELECT ts, reason FROM {d['alerts_table']} "
        "WHERE device_id = %s ORDER BY ts DESC LIMIT 1",
        (cfg["mqtt"]["device_id"],),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def record_alert(cfg, reason, mm, pct):
    d = cfg["database"]
    conn = db_connect(cfg)
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO {d['alerts_table']} "
        "(ts, device_id, reason, distance_mm, percent) VALUES (%s,%s,%s,%s,%s)",
        (datetime.now(), cfg["mqtt"]["device_id"], reason, mm, pct),
    )
    conn.commit()
    cur.close()
    conn.close()


# -------------------------------------------------------------------- mail --

def send_mail(cfg, subject, body):
    m = cfg["alert"]["mail"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = m["from"]
    msg["To"] = m["to"]
    msg.set_content(body)

    if m.get("smtp_host"):
        with smtplib.SMTP(m["smtp_host"], int(m.get("smtp_port", 25)),
                          timeout=30) as s:
            if m.get("smtp_tls"):
                s.starttls()
            if m.get("smtp_user"):
                s.login(m["smtp_user"], m.get("smtp_password") or "")
            s.send_message(msg)
    else:
        subprocess.run(["/usr/sbin/sendmail", "-t", "-oi"],
                       input=msg.as_bytes(), check=True)
    log(f"mail sent to {m['to']}")


def consumption_note(cfg, mm):
    """Rough trend line, if there is enough history to say anything useful."""
    try:
        rows = recent_readings(cfg, 30)
    except Exception:
        return ""
    if len(rows) < 2:
        return ""
    first_ts, first_mm, _ = rows[0]
    days = max(1, (datetime.now() - first_ts).days)
    drop = mm - first_mm
    if drop <= 0:
        return ""
    per_day = drop / days
    note = (f"\nOver the last {days} days the distance grew by {drop} mm "
            f"({per_day:.1f} mm/day), so the salt is going down.\n")
    to_empty = int(cfg["sensor"]["empty_mm"]) - mm
    if per_day > 0 and to_empty > 0:
        note += (f"At that rate it reaches empty in roughly "
                 f"{int(to_empty / per_day)} days.\n")
    return note


def low_body(cfg, ts, mm, pct, samples):
    a, s = cfg["alert"], cfg["sensor"]
    return f"""The water softener is running low on salt.

  Level        : {pct}%
  Distance     : {mm} mm from the sensor
  Threshold    : {a['threshold_percent']}%
  Measured at  : {ts:%Y-%m-%d %H:%M:%S}
  Raw samples  : {samples or 'n/a'}
{consumption_note(cfg, mm)}
Calibration in use: full = {s['full_mm']} mm, empty = {s['empty_mm']} mm.

When refilling, wipe the sensor lens — salt crust on the window corrupts
readings. Soft dry cloth, no liquids or sprays.

-- salt-detector-alert
"""


# -------------------------------------------------------------- diagnostics --
#
# Run only when a reading is stale. Read-only, best-effort: every check is
# wrapped so a permission error or missing tool degrades to a note in the
# output rather than crashing the alert. Nothing here talks to the device
# itself — this diagnoses the monitoring, not the salt.

def check_broker_reachable(cfg, timeout=3):
    """Plain TCP connect to the local plaintext listener. No MQTT handshake,
    no side effects — just answers "is anything listening at all?"."""
    host = cfg["mqtt"]["host"]
    port = int(cfg["mqtt"]["port"])
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} accepts connections"
    except OSError as exc:
        return False, f"{host}:{port} refused/unreachable: {exc}"


def service_status(name):
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, text=True, timeout=5)
        state = (r.stdout or r.stderr or "unknown").strip()
        state = state.splitlines()[0] if state else "unknown"
        return state
    except Exception as exc:
        return f"unknown ({exc})"


def journal_lines(unit, hours):
    """Best-effort journalctl read. Returns (lines, error_note)."""
    try:
        r = subprocess.run(
            ["journalctl", "-u", unit, "--since", f"{hours} hours ago",
             "--no-pager"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0 and not r.stdout:
            return [], (r.stderr or "journalctl returned no output — "
                       "permission denied, or the unit name is wrong?")
        return r.stdout.splitlines(), None
    except FileNotFoundError:
        return [], "journalctl not found"
    except Exception as exc:
        return [], str(exc)


def daemon_journal_summary(unit, hours):
    """Count the recognisable outcome lines the daemon logs each cycle."""
    lines, err = journal_lines(unit, hours)
    if err:
        return None, err
    patterns = [
        ("stored (successful reading)", "  stored"),
        ("no reply from device", "no reply from device"),
        ("out-of-range reading", "out of range:"),
        ("reply missing 'level' field", "reply had no usable 'level' field"),
        ("database write failed", "database write failed"),
        ("retained-feedback warning (auto-cleared)",
         "retained message is sitting on device/feedback"),
    ]
    counts = {label: sum(1 for l in lines if needle in l)
             for label, needle in patterns}
    return counts, None


def device_seen_on_broker(cfg, mosquitto_unit, hours):
    """
    Has the device (re)connected to mosquitto recently? Matched on the last
    hex group of the device id (e.g. 'bd34' from 'e0e2-e66c-bd34') rather
    than an exact client-id string, since the firmware's client id casing is
    inconsistent (observed as e.g. 'ESP32_6cBD34').
    """
    lines, err = journal_lines(mosquitto_unit, hours)
    if err:
        return None, None, err
    tail = cfg["mqtt"]["device_id"].split("-")[-1].lower()
    connects = [l for l in lines
               if "new client connected" in l.lower() and tail in l.lower()]
    return len(connects) > 0, len(connects), None


def ping_device(ip, count=3, timeout=2):
    """Optional: only runs if diagnostics.device_ip is set in the config."""
    try:
        r = subprocess.run(
            ["ping", "-c", str(count), "-W", str(timeout), ip],
            capture_output=True, text=True, timeout=count * timeout + 5,
        )
        m = re.search(r"(\d+)% packet loss", r.stdout)
        loss = m.group(1) + "%" if m else "unknown"
        return loss, r.stdout
    except Exception as exc:
        return None, str(exc)


def diagnose_stale(cfg, hours):
    """Assemble a human-readable diagnostic report for a stale reading."""
    d = cfg.get("diagnostics", {})
    daemon_unit = d.get("daemon_service", "salt-detector")
    broker_unit = d.get("broker_service", "mosquitto")
    device_ip = d.get("device_ip")

    lines = []

    reachable, detail = check_broker_reachable(cfg)
    lines.append(f"Broker ({daemon_unit}'s MQTT host)   : "
                f"{'reachable' if reachable else 'UNREACHABLE'} — {detail}")

    state = service_status(daemon_unit)
    flag = "" if state == "active" else "  <-- not running"
    lines.append(f"Daemon service ({daemon_unit})        : {state}{flag}")

    seen, n, err = device_seen_on_broker(cfg, broker_unit, hours)
    if err:
        lines.append(f"Device seen on broker (last {hours}h)  : unknown — {err}")
    elif seen:
        loop_flag = "  <-- reconnecting repeatedly, possible bootloop" if n > 5 else ""
        lines.append(f"Device seen on broker (last {hours}h)  : "
                    f"yes, {n} connection(s){loop_flag}")
    else:
        lines.append(f"Device seen on broker (last {hours}h)  : "
                    f"NO — device has not reconnected at all")

    counts, err = daemon_journal_summary(daemon_unit, hours)
    if err:
        lines.append(f"\nDaemon log summary (last {hours}h): unavailable — {err}")
    else:
        lines.append(f"\nDaemon log summary (last {hours}h):")
        for label, n in counts.items():
            if n:
                lines.append(f"  {n:>3}  {label}")
        if not any(counts.values()):
            lines.append("  (no recognisable log lines at all in this window)")

    if device_ip:
        loss, out = ping_device(device_ip)
        if loss is None:
            lines.append(f"\nPing {device_ip}: could not run ping — {out}")
        else:
            lines.append(f"\nPing {device_ip}: {loss} packet loss")

    return "\n".join(lines)


def stale_body(cfg, ts, age_hours):
    diagnosis = diagnose_stale(cfg, int(age_hours) + 2)
    return f"""No fresh reading from the salt:detector.

  Last reading : {ts:%Y-%m-%d %H:%M:%S} ({age_hours:.1f} hours ago)
  Allowed age  : {cfg['alert']['max_reading_age_hours']} hours

Automatic diagnosis:

{diagnosis}

The salt level itself may be fine — this is about the monitoring. If the
above doesn't point at an obvious cause, worth checking manually:

  systemctl status salt-detector
  journalctl -u salt-detector -n 50
  mosquitto_sub -h {cfg['mqtt']['host']} -t 'device/#' -v

If the device dropped off the network, its LED says where it is: flashing
white means it lost its wifi credentials and needs re-pairing through the
app over Bluetooth. If it's reconnecting many times an hour, it may be
bootlooping — see readme.md, "The provisioning reply: root cause found".

-- salt-detector-alert
"""


# -------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help="path to salt-detector.conf")
    ap.add_argument("--dry-run", action="store_true", help="decide but do not send")
    ap.add_argument("--force", action="store_true",
                    help="send regardless of threshold and cooldown")
    ap.add_argument("--status", action="store_true", help="print recent readings")
    ap.add_argument("--diagnose", action="store_true",
                    help="run the stale-data diagnostics now, regardless of "
                         "whether a reading is actually stale, and print the "
                         "report without sending mail")
    args = ap.parse_args()

    cfg = load_config(args.config)
    a = cfg["alert"]

    if args.diagnose:
        print(diagnose_stale(cfg, int(a.get("max_reading_age_hours", 26))))
        return 0

    if args.status:
        rows = recent_readings(cfg, 14)
        if not rows:
            log("no readings in the last 14 days")
            return 1
        for ts, mm, pct in rows[-20:]:
            print(f"  {ts:%Y-%m-%d %H:%M}  {mm:>4} mm  {pct:>5}%")
        la = last_alert(cfg)
        if la:
            print(f"\n  last alert: {la[0]:%Y-%m-%d %H:%M} ({la[1]})")
        return 0

    row = latest_reading(cfg)
    if not row:
        log("no readings in the database at all — has the daemon run?")
        return 1

    ts, mm, pct, samples = row
    pct = float(pct) if pct is not None else None
    age_hours = (datetime.now() - ts).total_seconds() / 3600.0
    log(f"latest: {mm} mm / {pct}% at {ts:%Y-%m-%d %H:%M} ({age_hours:.1f}h ago)")

    # stale data means the monitoring is broken, which is its own problem
    if age_hours > float(a.get("max_reading_age_hours", 26)):
        if not a.get("alert_on_stale", True):
            log("reading is stale; alert_on_stale is off, doing nothing")
            return 0
        prev = last_alert(cfg)
        if prev and prev[1] == "stale" and \
                (datetime.now() - prev[0]).total_seconds() / 3600.0 \
                < float(a["cooldown_hours"]):
            log("stale, but a stale alert was sent inside the cooldown")
            return 0
        log("reading is stale — alerting about the monitoring, not the salt")
        if args.dry_run:
            log("dry run, not sending")
            return 0
        send_mail(cfg, "Salt detector: no recent readings",
                  stale_body(cfg, ts, age_hours))
        record_alert(cfg, "stale", mm, pct)
        return 0

    threshold = float(a["threshold_percent"])
    if not args.force and (pct is None or pct >= threshold):
        log(f"level {pct}% is at or above the {threshold}% threshold — nothing to do")
        return 0

    prev = last_alert(cfg)
    if prev and not args.force:
        hours = (datetime.now() - prev[0]).total_seconds() / 3600.0
        if hours < float(a["cooldown_hours"]):
            log(f"below threshold, but the last alert was {hours:.1f}h ago "
                f"(cooldown {a['cooldown_hours']}h) — not sending")
            return 0

    if args.force and pct is not None and pct >= threshold:
        log(f"level {pct}% is above the {threshold}% threshold, "
            "but --force was given — sending anyway")
    else:
        log(f"level {pct}% is below {threshold}% — sending alert")
    if args.dry_run:
        log("dry run, not sending")
        return 0

    send_mail(cfg, a["mail"]["subject"], low_body(cfg, ts, mm, pct, samples))
    record_alert(cfg, "low", mm, pct)
    return 0


if __name__ == "__main__":
    sys.exit(main())