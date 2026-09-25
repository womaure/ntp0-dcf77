#!/usr/bin/env python3
"""
ntp-0 Wochenübersicht
=====================

Fasst die regelmaessigen Pruefungen in einer kurzen Uebersicht zusammen,
fuer die Home Assistant keine Zeitreihe liefert:

  1. Tatsaechliche Abweichung zu UTC (DCF77 gegen die Internet-Server)
  2. Empfangsqualitaet: uebernommene und verlorene Minuten, Stoerungen
     und zu welchen Tageszeiten sie auftreten
  3. Nachjustierungen durch den Autotune-Regler
  4. Driftbericht aus den chrony-Logs
  5. Hardware-Uhr, Backup, Temperaturen, Drosselung, Journal

Aufruf (root noetig fuer Journal, Hardware-Uhr und chrony-Logs):
    sudo python3 /opt/ntp0_wochenbericht.py
    sudo python3 /opt/ntp0_wochenbericht.py --days 14
"""

import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
from collections import Counter

BACKUP_STATUS = "/var/lib/ntp0-backup/status.json"
CONFIG_FILE = "/etc/ntp-0.conf"
DRIFT_REPORT = "/opt/dcf77_drift_report.py"


def run(cmd, timeout=60):
    """Fuehrt einen Befehl aus und liefert stdout, bei Fehler None."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def titel(text):
    print()
    print(text)
    print("-" * len(text))


def journal(unit, days):
    """Journalzeilen einer Unit mit ISO-Zeitstempel."""
    out = run(["journalctl", "-u", unit, "--since", f"{days} days ago",
               "--no-pager", "-o", "short-iso"], timeout=120)
    return out.splitlines() if out else []


def zeitstempel(zeile):
    """Zeitstempel am Zeilenanfang (short-iso) lesen."""
    try:
        return datetime.datetime.strptime(zeile[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


# ---------------------------------------------------------------------
# 1. Abweichung zu UTC
# ---------------------------------------------------------------------

def offset_parsen(text):
    m = re.match(r"^([+-]?[\d.]+)(ns|us|ms|s)$", text.strip())
    if not m:
        return None
    return float(m.group(1)) * {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}[m.group(2)]


def abschnitt_utc():
    titel("1. Zeitquelle und Abweichung zu UTC")

    tracking = run(["chronyc", "tracking"])
    if tracking:
        felder = {}
        for zeile in tracking.splitlines():
            if ":" in zeile:
                k, _, v = zeile.partition(":")
                felder[k.strip()] = v.strip()
        print(f"  Quelle:     {felder.get('Reference ID', '?')}")
        print(f"  Stratum:    {felder.get('Stratum', '?')}")
        rms = felder.get("RMS offset", "")
        print(f"  RMS offset: {rms}")

    quellen = run(["chronyc", "sources", "-v"])
    if not quellen:
        print("  chronyc sources nicht abrufbar")
        return

    muster = re.compile(
        r"^(?P<mode>[#^=])(?P<state>[*+\-?x~])\s+(?P<name>\S+)\s+\d+\s+\d+\s+"
        r"(?P<reach>\d+)\s+\S+\s+[+-][\d.]+(?:ns|us|ms|s)"
        r"\[\s*(?P<gemessen>[+-][\d.]+(?:ns|us|ms|s))\s*\]")

    dcf = None
    internet = []
    for zeile in quellen.splitlines():
        m = muster.match(zeile.strip())
        if not m:
            continue
        wert = offset_parsen(m.group("gemessen"))
        if wert is None:
            continue
        if m.group("mode") == "#":
            dcf = wert
        elif m.group("mode") == "^" and m.group("state") in "*+-":
            internet.append(wert)

    if dcf is None or len(internet) < 2:
        print("  Zu wenige Quellen fuer einen Vergleich")
        return

    median = statistics.median(internet)
    abweichung = dcf - median
    print(f"  DCF77 gegen Internet-Median: {abweichung * 1000:+.1f} ms "
          f"(aus {len(internet)} Servern)")
    print("  Hinweis: 'System time' in chronyc tracking misst nur gegen die")
    print("  gewaehlte Quelle. Die Zeile hier ist die ehrliche Abweichung.")
    if abs(abweichung) <= 0.010:
        print("  Bewertung: unauffaellig (innerhalb der 10-ms-Totzone)")
    elif abs(abweichung) <= 0.050:
        print("  Bewertung: der Autotune-Regler sollte nachjustieren")
    else:
        print("  Bewertung: AUFFAELLIG - pruefen, ob der Regler arbeitet")


# ---------------------------------------------------------------------
# 2. Empfangsqualitaet
# ---------------------------------------------------------------------

def abschnitt_empfang(days):
    titel(f"2. DCF77-Empfang der letzten {days} Tage")

    zeilen = journal("dcf77-decoder.service", days)
    if not zeilen:
        print("  Keine Journaleintraege gefunden (sudo vergessen?)")
        return

    uebernommen = 0
    verloren = 0
    ignoriert = 0
    ungueltig = 0
    ursachen = Counter()
    verlust_je_stunde = Counter()
    neustarts = 0

    for z in zeilen:
        if "an SHM uebergeben" in z:
            uebernommen += 1
            m = re.search(r"(\d+) Stoerimpulse ignoriert", z)
            if m:
                ignoriert += int(m.group(1))
            continue

        ts = zeitstempel(z)
        if "Dekodierung fehlgeschlagen" in z or "Kette im Aufbau" in z \
                or "Verworfen:" in z:
            verloren += 1
            if ts:
                verlust_je_stunde[ts.hour] += 1
            if "Paritaetsfehler" in z:
                ursachen["Paritaetsfehler"] += 1
            elif "zu wenige Bits" in z:
                ursachen["Frame unvollstaendig"] += 1
            elif "Bit 0" in z or "Bit 20" in z or "Zeitzonenbits" in z:
                ursachen["Konstante Bits falsch"] += 1
            elif "Kette im Aufbau" in z:
                ursachen["Plausibilitaetskette neu aufgebaut"] += 1
        if "Ungueltige Pulslaenge" in z:
            ungueltig += 1
        if "DCF77-Dekoder gestartet" in z:
            neustarts += 1

    gesamt = uebernommen + verloren
    if gesamt == 0:
        print("  Keine auswertbaren Minuten")
        return

    quote = uebernommen * 100 / gesamt
    print(f"  Uebernommene Minuten:   {uebernommen}")
    print(f"  Verlorene Minuten:      {verloren}")
    print(f"  Quote:                  {quote:.1f} %")
    print(f"  Ignorierte Stoerimpulse (Minute gerettet): {ignoriert}")
    print(f"  Ungueltige Pulslaengen:                    {ungueltig}")
    if neustarts:
        print(f"  Decoder-Neustarts:                         {neustarts}")

    if ursachen:
        print()
        print("  Ursachen verlorener Minuten:")
        for ursache, anzahl in ursachen.most_common():
            print(f"    {anzahl:5}  {ursache}")

    if verlust_je_stunde:
        print()
        print("  Stunden mit den meisten Verlusten (Hinweis auf Stoerquellen):")
        for stunde, anzahl in verlust_je_stunde.most_common(4):
            balken = "#" * min(anzahl, 40)
            print(f"    {stunde:02d}:00-{stunde:02d}:59  {anzahl:4}  {balken}")


# ---------------------------------------------------------------------
# 3. Autotune
# ---------------------------------------------------------------------

def abschnitt_autotune(days):
    titel(f"3. Nachjustierung durch den Autotune-Regler")

    zeilen = journal("dcf77-autotune.service", days)
    korrekturen = []
    totzone = 0
    uebersprungen = 0
    for z in zeilen:
        m = re.search(r"Korrektur: ([\d.]+)s -> ([\d.]+)s", z)
        if m:
            korrekturen.append((zeitstempel(z), float(m.group(1)), float(m.group(2))))
        elif "Totzone" in z:
            totzone += 1
        elif "uebersprungen" in z:
            uebersprungen += 1

    print(f"  Laeufe in der Totzone (nichts zu tun): {totzone}")
    print(f"  Uebersprungen (zu wenig Empfang):      {uebersprungen}")
    print(f"  Korrekturen:                            {len(korrekturen)}")

    if korrekturen:
        erster = korrekturen[0][1]
        letzter = korrekturen[-1][2]
        print(f"  receiver_delay: {erster:.4f}s -> {letzter:.4f}s "
              f"(Summe {(letzter - erster) * 1000:+.1f} ms)")
        for ts, von, nach in korrekturen[-5:]:
            wann = ts.strftime("%d.%m. %H:%M") if ts else "?"
            print(f"    {wann}  {von:.4f} -> {nach:.4f}  "
                  f"({(nach - von) * 1000:+.1f} ms)")

    try:
        with open(CONFIG_FILE) as f:
            for zeile in f:
                if zeile.strip().startswith("receiver_delay"):
                    print(f"  Aktueller Wert: {zeile.split('=', 1)[1].strip()} s")
    except OSError:
        pass


# ---------------------------------------------------------------------
# 4. Driftbericht
# ---------------------------------------------------------------------

def abschnitt_drift(days):
    titel("4. Driftbericht (chrony refclocks.log)")
    if not os.path.exists(DRIFT_REPORT):
        print(f"  {DRIFT_REPORT} nicht vorhanden")
        return
    out = run(["python3", DRIFT_REPORT, "--days", str(days)], timeout=180)
    if not out:
        print("  Driftbericht nicht erzeugbar")
        return
    for zeile in out.splitlines():
        print(f"  {zeile}")


# ---------------------------------------------------------------------
# 5. System
# ---------------------------------------------------------------------

def temperatur(name):
    try:
        for e in os.listdir("/sys/class/hwmon"):
            try:
                with open(f"/sys/class/hwmon/{e}/name") as f:
                    if f.read().strip() != name:
                        continue
                with open(f"/sys/class/hwmon/{e}/temp1_input") as f:
                    return int(f.read().strip()) / 1000
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return None


def abschnitt_system():
    titel("5. Hardware-Uhr, Backup und System")

    out = run(["hwclock", "-r"])
    if out:
        try:
            rtc = datetime.datetime.fromisoformat(out.strip())
            diff = (rtc - datetime.datetime.now(rtc.tzinfo)).total_seconds()
            print(f"  Hardware-Uhr:  Abweichung {diff:+.2f} s")
        except ValueError:
            print(f"  Hardware-Uhr:  nicht auswertbar ({out.strip()})")
    else:
        print("  Hardware-Uhr:  antwortet nicht")

    t_rtc = temperatur("ds3231")
    t_cpu = temperatur("cpu_thermal")
    if t_rtc is not None or t_cpu is not None:
        teile = []
        if t_cpu is not None:
            teile.append(f"CPU {t_cpu:.1f} Grad")
        if t_rtc is not None:
            teile.append(f"RTC {t_rtc:.1f} Grad")
        print(f"  Temperatur:    {', '.join(teile)}")

    thr = run(["vcgencmd", "get_throttled"])
    if thr:
        wert = thr.strip().split("=")[-1]
        hinweis = "keine Drosselung" if wert == "0x0" else "AUFFAELLIG"
        print(f"  Drosselung:    {wert} ({hinweis})")

    try:
        with open(BACKUP_STATUS) as f:
            b = json.load(f)
        erfolg = b.get("letzter_erfolg")
        if erfolg:
            alter = (datetime.datetime.now().astimezone()
                     - datetime.datetime.fromisoformat(erfolg)).days
            print(f"  Backup:        letzter Erfolg vor {alter} Tagen "
                  f"({b.get('pfade')} Pfade, {b.get('groesse_kb')} KB)")
        if not b.get("letzter_versuch_erfolgreich", True):
            print(f"                 LETZTER VERSUCH FEHLGESCHLAGEN: {b.get('fehler')}")
    except (OSError, ValueError):
        print("  Backup:        keine Statusdatei")

    j = run(["journalctl", "--disk-usage"])
    if j:
        m = re.search(r"take up ([\d.]+\S+)", j)
        if m:
            print(f"  Journal:       {m.group(1)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    jetzt = datetime.datetime.now()
    print("=" * 64)
    print(f"  ntp-0 Wochenuebersicht - {jetzt:%d.%m.%Y %H:%M}")
    print(f"  Zeitraum: letzte {args.days} Tage")
    print("=" * 64)

    if os.geteuid() != 0:
        print()
        print("  Hinweis: ohne root fehlen Journal, Hardware-Uhr und Driftbericht.")
        print("  Aufruf: sudo python3 /opt/ntp0_wochenbericht.py")

    abschnitt_utc()
    abschnitt_empfang(args.days)
    abschnitt_autotune(args.days)
    abschnitt_drift(args.days)
    abschnitt_system()
    print()


if __name__ == "__main__":
    main()
