#!/usr/bin/env python3
"""
DCF77-Drift aus den chrony-Logs auswerten
==========================================

Liest /var/log/chrony/refclocks.log (und die rotierten Vorgänger) und
fasst zusammen, wie die DCF77-Messwerte über die Zeit wandern.

Damit lässt sich die Frage beantworten, die uns bisher nur indirekt
zugänglich war: Wie schnell driftet die Empfängerverzögerung wirklich,
und wie stark streuen die Einzelmessungen?

Aufruf:
    python3 /opt/dcf77_drift_report.py                # letzte 7 Tage
    python3 /opt/dcf77_drift_report.py --days 30      # letzte 30 Tage
    python3 /opt/dcf77_drift_report.py --hours 12     # letzte 12 Stunden
    python3 /opt/dcf77_drift_report.py --csv          # Rohdaten als CSV

Muss mit sudo laufen: /var/log/chrony ist nur fuer root und den
Benutzer _chrony lesbar.

Hinweis: chrony schreibt die Zeitstempel in UTC. Die Ausgabe des
Skripts uebernimmt das, die Uhrzeiten liegen also im Sommer zwei
Stunden hinter der lokalen Zeit.
"""

import argparse
import datetime
import glob
import gzip
import os
import statistics
import sys

LOG_GLOB = "/var/log/chrony/refclocks.log*"


def open_maybe_gz(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    return open(path, "r", errors="replace")


def parse_logs(since):
    """Liest alle refclocks-Logs und liefert (zeitpunkt, roh, gefiltert).

    Das Format der Datei wird von chrony selbst im Kopf beschrieben -
    ein Banner mit den Spaltennamen wird periodisch mitgeschrieben.
    Wir werten die ersten beiden Spalten als Zeitstempel und suchen die
    beiden Offset-Spalten anhand ihrer Position hinter dem Refid.
    """
    samples = []
    files = sorted(glob.glob(LOG_GLOB))
    if not files:
        print(f"Keine Logdateien gefunden unter {LOG_GLOB}", file=sys.stderr)
        print("Ist 'log ... refclocks' in der chrony.conf aktiviert und "
              "chrony neu gestartet?", file=sys.stderr)
        sys.exit(1)

    for path in files:
        try:
            with open_maybe_gz(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("="):
                        continue
                    # Banner-Zeilen beginnen mit dem Datumsformat-Hinweis
                    if line.startswith("Date") or line.startswith("#"):
                        continue

                    parts = line.split()
                    if len(parts) < 8:
                        continue
                    # Das echte Format enthaelt Mikrosekunden im Zeitstempel
                    # ("13:45:00.001616"), deshalb zuerst mit %f versuchen.
                    ts = None
                    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                        try:
                            ts = datetime.datetime.strptime(
                                f"{parts[0]} {parts[1]}", fmt)
                            break
                        except ValueError:
                            continue
                    if ts is None:
                        continue
                    # chrony schreibt die Zeiten in UTC (siehe Banner in der
                    # Logdatei), deshalb auch so kennzeichnen - sonst wuerde
                    # der Vergleich mit dem Startzeitpunkt um den lokalen
                    # Zeitzonenversatz danebenliegen.
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
                    if ts < since:
                        continue

                    # Spaltenaufbau laut Banner der Logdatei:
                    #   Date Time Refid DP L P RawOffset CookedOffset Disp.
                    # Beispielzeile:
                    #   2026-09-18 13:45:00.001616 DCF 33 N 0 \
                    #       -1.617000e-03 -1.616998e-03 1.000e-02
                    try:
                        raw = float(parts[6])
                        cooked = float(parts[7])
                    except (ValueError, IndexError):
                        continue

                    samples.append((ts, raw, cooked))
        except OSError as e:
            print(f"WARNUNG: {path} nicht lesbar: {e}", file=sys.stderr)

    samples.sort(key=lambda s: s[0])
    return samples


# Werte jenseits dieser Grenze sind keine Messabweichung mehr, sondern
# ein falsch dekodierter Frame. Am 24.09.2026 kam einer mit einem um zwei
# Tage verschobenen Datum durch (172.800.000 ms). Die Standardabweichung
# des ganzen Tages stieg dadurch auf 5.715.716 ms - die Tageszeile war
# unbrauchbar, obwohl alle uebrigen 913 Messwerte in Ordnung waren.
AUSREISSER_GRENZE = 1.0   # Sekunden


def robuste_streuung(werte):
    """Streuung, die einzelne Ausreisser nicht mitreissen.

    Berechnet wird die mittlere absolute Abweichung vom Median, skaliert
    mit 1,4826. Bei normalverteilten Werten liefert das naeherungsweise
    dasselbe wie die Standardabweichung, reagiert aber kaum auf einzelne
    Ausreisser.
    """
    if len(werte) < 2:
        return 0.0
    med = statistics.median(werte)
    return 1.4826 * statistics.median([abs(w - med) for w in werte])


def report(samples, since):
    if not samples:
        print("Keine Messwerte im gewählten Zeitraum.")
        return

    print(f"DCF77-Messwerte seit {since:%Y-%m-%d %H:%M}")
    print(f"Zeitraum: {samples[0][0]:%Y-%m-%d %H:%M} bis "
          f"{samples[-1][0]:%Y-%m-%d %H:%M}")
    print(f"Anzahl Messwerte: {len(samples)}")
    print()

    by_day = {}
    for ts, raw, cooked in samples:
        by_day.setdefault(ts.date(), []).append(raw)

    print(f"{'Tag':12} {'Anzahl':>7} {'Median':>10} {'Streuung':>10} "
          f"{'Min':>10} {'Max':>10} {'Ausreisser':>11}")
    print("-" * 75)
    ausreisser_gesamt = []
    for day in sorted(by_day):
        alle = by_day[day]
        sauber = [w for w in alle if abs(w) <= AUSREISSER_GRENZE]
        ausreisser = [w for w in alle if abs(w) > AUSREISSER_GRENZE]
        ausreisser_gesamt += [(day, w) for w in ausreisser]
        if not sauber:
            print(f"{day!s:12} {len(alle):7}   nur Ausreisser")
            continue
        med = statistics.median(sauber)
        print(f"{day!s:12} {len(alle):7} "
              f"{med*1000:9.1f}ms {robuste_streuung(sauber)*1000:9.1f}ms "
              f"{min(sauber)*1000:9.1f}ms {max(sauber)*1000:9.1f}ms "
              f"{len(ausreisser):11}")

    print()
    print("Median, Streuung, Min und Max beruhen auf Werten unter "
          f"{AUSREISSER_GRENZE:.0f}s.")
    print("Die Spalte Ausreisser zaehlt alles darueber - das sind keine")
    print("Messabweichungen, sondern falsch dekodierte Frames.")

    if ausreisser_gesamt:
        print()
        print("Ausreisser im Zeitraum:")
        for day, w in ausreisser_gesamt[:10]:
            print(f"  {day}  {w/3600:+.2f} h  ({w:+.0f} s)")
        if len(ausreisser_gesamt) > 10:
            print(f"  ... und {len(ausreisser_gesamt)-10} weitere")

    print()
    print("Hinweis zur Aussagekraft: Diese Werte vergleichen DCF77 mit der")
    print("Systemuhr - und die folgt DCF77. Der Median liegt deshalb")
    print("zwangslaeufig nahe null und sagt NICHTS ueber die Abweichung zu")
    print("UTC oder ueber eine Drift des Empfaengers aus. Aussagekraeftig")
    print("sind hier nur Streuung (Rauschen der Einzelwerte) und die Zahl")
    print("der Ausreisser. Die tatsaechliche Abweichung zu UTC steht in")
    print("Abschnitt 1 des Wochenberichts.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--hours", type=float)
    ap.add_argument("--csv", action="store_true",
                    help="Rohdaten als CSV ausgeben statt Zusammenfassung")
    args = ap.parse_args()

    delta = (datetime.timedelta(hours=args.hours) if args.hours
             else datetime.timedelta(days=args.days))
    since = datetime.datetime.now(datetime.timezone.utc) - delta

    samples = parse_logs(since)

    if args.csv:
        print("zeitpunkt,roh_sekunden,gefiltert_sekunden")
        for ts, raw, cooked in samples:
            print(f"{ts.isoformat()},{raw},{cooked}")
    else:
        report(samples, since)


if __name__ == "__main__":
    main()
