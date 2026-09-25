#!/usr/bin/env python3
"""
DCF77 Autotune - automatische Nachjustierung der Empfaengerverzoegerung
=======================================================================

Das DCF77-Empfangsmodul (Pollin WHP0027V03) hat eine Verarbeitungs-
verzoegerung, die nicht perfekt konstant ist: ueber Tage hinweg wandert
sie um einige zehn bis hundert Millisekunden (Signalstaerke, Temperatur,
elektrisches Umfeld). Bisher musste der Korrekturwert
(receiver_delay in /etc/ntp-0.conf) dann von Hand nachgezogen werden.

Dieses Skript uebernimmt das - vorsichtig und traege.

FUNKTIONSPRINZIP
----------------
Der naive Ansatz ("lies den DCF-Offset aus chronyc sources und korrigiere
ihn weg") hat ein Rueckkopplungsproblem: Sobald DCF77 die fuehrende
Quelle ist (#*), folgt die Systemuhr der DCF77-Zeit. Der gemessene
Offset geht dann gegen null - egal, wie falsch die Kalibrierung
tatsaechlich ist. Das Skript waere blind, sobald alles laeuft.

Deshalb vergleichen wir DCF77 nicht gegen die Systemuhr, sondern gegen
den MEDIAN der Internet-NTP-Quellen. Das ist eine von DCF77 unabhaengige
Referenz und funktioniert unabhaengig davon, welche Quelle chrony
gerade als fuehrend nutzt:

    fehler        = dcf_offset - median(internet_offsets)
    neuer_delay   = alter_delay - FAKTOR * fehler

Der FAKTOR (< 1) sorgt dafuer, dass nur ein Teil des Fehlers pro Lauf
ausgeglichen wird - wie ein traeges Thermostat. Das verhindert
Ueberschwingen und macht das System robust gegen einzelne Ausreisser.

SICHERHEITSMECHANISMEN
----------------------
1. Mindest-Reach: Nur messen, wenn DCF77 zuletzt zuverlaessig geliefert
   hat - sonst wuerde auf Basis einer unsicheren Messung korrigiert.
2. Genug Internet-Quellen: Ohne belastbare Referenz wird nicht getunt.
3. Konsistenz ueber mehrere Laeufe: Erst wenn N Messungen in Folge in
   dieselbe Richtung zeigen, wird ueberhaupt angepasst.
4. Totzone: Kleine Fehler (< DEADBAND) werden ignoriert - sonst wuerde
   das Skript ewig um den Nullpunkt herumzappeln.
5. Schrittbegrenzung: Pro Lauf hoechstens MAX_STEP Aenderung.
6. Harte Grenzen: receiver_delay bleibt immer in [MIN, MAX].
7. Alles wird protokolliert (journalctl -u dcf77-autotune.service).

Installation siehe dcf77-autotune.service / dcf77-autotune.timer.

Manueller Testlauf (aendert nichts, zeigt nur, was passieren wuerde):
    sudo python3 /opt/dcf77_autotune.py --dry-run
"""

import subprocess
import re
import sys
import json
import os
import datetime
import configparser
import statistics

CONFIG_FILE = "/etc/ntp-0.conf"
STATE_FILE = "/var/lib/dcf77-autotune/state.json"

# --- Regelparameter ---
CORRECTION_FACTOR = 0.6     # Anteil des Fehlers, der pro Lauf ausgeglichen wird
DEADBAND_SECONDS = 0.010    # Fehler unter 10ms gelten als "gut genug"
# Maximale Aenderung pro Korrektur.
#
# Urspruenglich 150 ms - sinnvoll fuer die Erstkalibrierung, als der Wert
# noch um fast 100 ms danebenlag. Im Dauerbetrieb war das viel zu gross:
# Im September pendelte receiver_delay innerhalb von zwei Tagen um rund
# +-90 ms hin und her (-56,6 ms, dann +55,9 ms). Der Regler hat nicht
# einer Drift gefolgt, sondern sich aufgeschaukelt.
#
# Die reale Drift des Empfaengers liegt bei einigen Millisekunden pro Tag.
# Mit 10 ms pro Schritt und der Beruhigungszeit unten sind bis zu rund
# 25 ms Korrektur am Tag moeglich - das reicht mit Abstand, und ein
# einzelner Ausreisser kann keinen grossen Sprung mehr ausloesen.
MAX_STEP_SECONDS = 0.010

# Nach jeder Korrektur so lange keine Messungen verwerten.
#
# Grund: Nach einer Korrektur liefert der Decoder sofort verschobene
# Zeitstempel, chrony zieht die Systemuhr aber nur allmaehlich nach. In
# dieser Phase vergleicht der Regler einen DCF-Messwert, der Minuten alt
# sein kann, mit frischen Internet-Messwerten, waehrend sich die Uhr
# dazwischen bewegt. Er sieht dann die Nachwirkung seiner eigenen
# Korrektur als neuen Fehler - und korrigiert in die Gegenrichtung.
# Die Messwerte werden in dieser Zeit weiter protokolliert, aber nicht
# verwertet. So laesst sich im Log beobachten, wie sich der Fehler nach
# einer Korrektur einschwingt.
SETTLE_SECONDS = 6 * 3600
REQUIRED_CONSISTENT = 3     # so viele Laeufe in Folge muessen zusammenpassen
CONSISTENCY_TOLERANCE = 0.060  # Messungen duerfen um max. 60ms streuen
MAX_MEASUREMENT_AGE = 6 * 3600  # Messungen aelter als 6h nicht mehr mitrechnen

# --- Sicherheitsgrenzen (muessen zu dcf77_ntp_shm.py passen) ---
DELAY_MIN = 0.30
DELAY_MAX = 1.50

# --- Qualitaetsanforderungen an die Messung ---
# Das Reach-Register ist ein Bitfeld der letzten 8 Abfragen. Bei diesem
# Setup ist es strukturell lueckenhaft: der Decoder schreibt einmal pro
# Minute, chrony fragt mit poll 6 alle 64s ab und verwirft dabei einen
# Teil der SHM-Samples. Real gemessene Werte im Normalbetrieb waren
# 0o42, 0o210, 0o104, 0o21, 0o44 - allesamt genau 2 von 8 erfolgreich.
# Die Schwelle muss also niedrig sein; sie soll nur "DCF77 liefert gar
# nichts" ausschliessen (0 oder 1 Treffer = gerade erst angelaufen).
# Die eigentliche Absicherung gegen Fehlmessungen leisten die
# Konsistenzpruefung ueber mehrere Laeufe, die Totzone und die
# Schrittbegrenzung weiter unten - nicht dieser Schwellenwert.
MIN_DCF_SUCCESSES = 2       # mind. 2 der letzten 8 Abfragen erfolgreich
MIN_INTERNET_SOURCES = 3    # mind. 3 brauchbare Internet-Quellen als Referenz


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def parse_offset(text):
    """Wandelt chrony-Offsetangaben wie '+100ms', '-1774us', '+0ns',
    '+1.2s' in Sekunden (float) um. Gibt None zurueck, wenn unparsbar."""
    text = text.strip()
    match = re.match(r"^([+-]?[\d.]+)(ns|us|ms|s)$", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    factor = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}[unit]
    return value * factor


def read_chrony_sources():
    """Ruft chronyc sources -v auf und liefert die Rohzeilen."""
    try:
        result = subprocess.run(
            ["chronyc", "sources", "-v"],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        log(f"FEHLER: chronyc nicht aufrufbar: {e}")
        return None

    if result.returncode != 0:
        log("FEHLER: chronyc sources lieferte einen Fehlercode")
        return None

    return result.stdout.splitlines()


def analyse_sources(lines):
    """Wertet die chronyc-Ausgabe aus.

    Rueckgabe: dict mit dcf_offset, dcf_reach, internet_offsets
    oder None, wenn die Ausgabe nicht brauchbar ist.
    """
    # Datenzeilen erkennen: beginnen mit Mode-Zeichen (#, ^, =) gefolgt
    # von einem Status-Zeichen. Kopf- und Legendenzeilen fallen weg.
    data_re = re.compile(
        r"^(?P<mode>[#^=])(?P<state>[*+\-?x~])\s+"
        r"(?P<name>\S+)\s+"
        r"(?P<stratum>\d+)\s+"
        r"(?P<poll>\d+)\s+"
        r"(?P<reach>\d+)\s+"
        r"(?P<lastrx>\S+)\s+"
        r"(?P<adjusted>[+-][\d.]+(?:ns|us|ms|s))"
        r"\[\s*(?P<measured>[+-][\d.]+(?:ns|us|ms|s))\s*\]"
    )

    dcf_offset = None
    dcf_reach = None
    internet_offsets = []

    for line in lines:
        m = data_re.match(line.strip())
        if not m:
            continue

        name = m.group("name")
        reach = int(m.group("reach"), 8)
        measured = parse_offset(m.group("measured"))
        if measured is None:
            continue

        if m.group("mode") == "#":
            # Lokale Referenzuhr - das ist unsere DCF77-Quelle
            dcf_offset = measured
            dcf_reach = reach
        elif m.group("mode") == "^":
            # Netzwerk-Server als unabhaengige Referenz.
            # Nur Quellen, die chrony selbst als brauchbar einstuft:
            #   '*' = aktuell beste, '+' = mit einbezogen, '-' = verfuegbar
            # Ausgeschlossen bleiben '?' (unusable), 'x' (fehlerhaft) und
            # '~' (zu sprunghaft). Wichtig u.a. wegen des Selbstverweis-
            # Eintrags ntp-0.example.lan, der per DHCP im Netz
            # bekanntgegeben wird, nie antwortet und mit '+0ns' sonst
            # den Median verfaelschen wuerde.
            if m.group("state") in "*+-" and reach > 0:
                internet_offsets.append((name, measured))

    if dcf_offset is None:
        log("Keine DCF-Zeile in chronyc sources gefunden - nichts zu tun.")
        return None

    return {
        "dcf_offset": dcf_offset,
        "dcf_reach": dcf_reach,
        "internet_offsets": internet_offsets,
    }


def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"measurements": []}


def save_state(state):
    """Speichert den Zustand. Der Zeitpunkt der letzten Korrektur bleibt
    dabei erhalten, auch wenn der Aufrufer nur die Messhistorie
    zuruecksetzt - sonst wuerde die Beruhigungszeit unbemerkt verloren
    gehen."""
    if "last_change" not in state:
        vorher = load_state()
        if "last_change" in vorher:
            state = dict(state)
            state["last_change"] = vorher["last_change"]
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"WARNUNG: Zustand nicht speicherbar: {e}")


def read_current_delay():
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_FILE):
        log(f"FEHLER: {CONFIG_FILE} nicht lesbar.")
        return None
    try:
        return parser.getfloat("dcf77", "receiver_delay")
    except Exception as e:
        log(f"FEHLER: receiver_delay nicht lesbar: {e}")
        return None


def write_new_delay(new_value):
    """Schreibt den neuen Wert zurueck - dabei bleiben alle anderen
    Abschnitte (insbesondere [mqtt] mit den Zugangsdaten) erhalten,
    weil configparser die Datei vollstaendig einliest und neu schreibt.
    Kommentare gehen dabei allerdings verloren, deshalb ersetzen wir
    stattdessen gezielt nur die eine Zeile im Dateitext."""
    try:
        with open(CONFIG_FILE, "r") as f:
            content = f.read()
    except Exception as e:
        log(f"FEHLER: {CONFIG_FILE} nicht lesbar: {e}")
        return False

    new_line = f"receiver_delay = {new_value:.4f}"
    updated, count = re.subn(
        r"^receiver_delay\s*=.*$", new_line, content, count=1, flags=re.MULTILINE
    )

    if count != 1:
        log("FEHLER: receiver_delay-Zeile nicht eindeutig gefunden - "
            "keine Aenderung vorgenommen.")
        return False

    try:
        with open(CONFIG_FILE, "w") as f:
            f.write(updated)
    except Exception as e:
        log(f"FEHLER: {CONFIG_FILE} nicht schreibbar: {e}")
        return False

    return True


def main():
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        log("DRY-RUN: es wird nichts geaendert, nur analysiert.")

    lines = read_chrony_sources()
    if lines is None:
        sys.exit(1)

    data = analyse_sources(lines)
    if data is None:
        sys.exit(0)

    dcf_offset = data["dcf_offset"]
    dcf_reach = data["dcf_reach"]
    internet = data["internet_offsets"]

    dcf_successes = bin(dcf_reach).count("1")

    log(f"DCF77: offset={dcf_offset*1000:+.1f}ms, reach={oct(dcf_reach)} "
        f"({dcf_successes}/8 erfolgreich), "
        f"Internet-Quellen: {len(internet)}")

    # --- Sicherheitscheck 1: liefert DCF77 zuverlaessig? ---
    if dcf_successes < MIN_DCF_SUCCESSES:
        # Lauf ueberspringen, aber die Historie NICHT loeschen: eine
        # unbrauchbare Messung sagt nichts darueber aus, ob die frueheren
        # Messungen richtig waren. Frueher wurde hier zurueckgesetzt -
        # dadurch konnte der Regler nie genug konsistente Messungen
        # ansammeln, wenn auch nur gelegentlich ein Lauf danebenging.
        log(f"Nur {dcf_successes}/8 erfolgreiche Abfragen "
            f"(mind. {MIN_DCF_SUCCESSES} noetig) - Messung nicht "
            f"belastbar, Lauf wird uebersprungen (Historie bleibt).")
        sys.exit(0)

    # --- Sicherheitscheck 2: genug unabhaengige Referenz? ---
    if len(internet) < MIN_INTERNET_SOURCES:
        log(f"Nur {len(internet)} brauchbare Internet-Quellen "
            f"(mind. {MIN_INTERNET_SOURCES} noetig) - kein Tuning.")
        sys.exit(0)

    # --- Fehler gegen unabhaengige Referenz bestimmen ---
    internet_median = statistics.median([off for _, off in internet])
    error = dcf_offset - internet_median

    log(f"Median Internet-Offset: {internet_median*1000:+.1f}ms "
        f"-> DCF77-Fehler: {error*1000:+.1f}ms")

    # --- Messung in die Historie aufnehmen ---
    state = load_state()
    now_ts = datetime.datetime.now().timestamp()

    # --- Sicherheitscheck: Beruhigungszeit nach der letzten Korrektur ---
    letzte = state.get("last_change", {}).get("time")
    if letzte:
        try:
            seit = now_ts - datetime.datetime.fromisoformat(letzte).timestamp()
        except ValueError:
            seit = None
        if seit is not None and seit < SETTLE_SECONDS:
            rest = (SETTLE_SECONDS - seit) / 3600
            log(f"Beruhigungszeit nach der Korrektur von {letzte[11:16]} Uhr: "
                f"noch {rest:.1f} h. Messung wird protokolliert, aber nicht "
                f"verwertet.")
            sys.exit(0)

    # Veraltete Messungen aussortieren. Noetig, seit die Historie bei
    # uebersprungenen Laeufen erhalten bleibt: sonst koennte eine Messung
    # von vorgestern mit einer von heute zu einer "konsistenten" Reihe
    # verrechnet werden, obwohl sich zwischenzeitlich alles geaendert hat.
    measurements = []
    for m in state.get("measurements", []):
        try:
            age = now_ts - datetime.datetime.fromisoformat(m["time"]).timestamp()
        except Exception:
            continue
        if age <= MAX_MEASUREMENT_AGE:
            measurements.append(m)

    measurements.append({
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "error": error,
    })
    # Nur die letzten N behalten
    measurements = measurements[-REQUIRED_CONSISTENT:]
    state["measurements"] = measurements

    # --- Sicherheitscheck 3: Totzone ---
    if abs(error) < DEADBAND_SECONDS:
        log(f"Fehler unter Totzone ({DEADBAND_SECONDS*1000:.0f}ms) - "
            f"alles im gruenen Bereich, kein Tuning noetig.")
        if not dry_run:
            save_state({"measurements": []})
        sys.exit(0)

    # --- Sicherheitscheck 4: genug konsistente Messungen? ---
    if len(measurements) < REQUIRED_CONSISTENT:
        log(f"Erst {len(measurements)} von {REQUIRED_CONSISTENT} noetigen "
            f"Messungen gesammelt - warte auf weitere Laeufe.")
        if not dry_run:
            save_state(state)
        sys.exit(0)

    errors = [m["error"] for m in measurements]
    spread = max(errors) - min(errors)
    same_sign = all(e > 0 for e in errors) or all(e < 0 for e in errors)

    if not same_sign or spread > CONSISTENCY_TOLERANCE:
        log(f"Messungen nicht konsistent (Streuung "
            f"{spread*1000:.0f}ms, gleiches Vorzeichen: {same_sign}) - "
            f"kein Tuning, Historie wird zurueckgesetzt.")
        if not dry_run:
            save_state({"measurements": []})
        sys.exit(0)

    # --- Korrektur berechnen ---
    current = read_current_delay()
    if current is None:
        sys.exit(1)

    # Median der Messreihe statt nur der letzten Messung: robuster
    stable_error = statistics.median(errors)
    step = -CORRECTION_FACTOR * stable_error

    # Schrittbegrenzung
    if abs(step) > MAX_STEP_SECONDS:
        step = MAX_STEP_SECONDS if step > 0 else -MAX_STEP_SECONDS
        log(f"Schritt auf +/-{MAX_STEP_SECONDS*1000:.0f}ms begrenzt.")

    new_delay = current + step

    # --- Sicherheitscheck 5: harte Grenzen ---
    if not (DELAY_MIN <= new_delay <= DELAY_MAX):
        log(f"FEHLER: Zielwert {new_delay:.4f}s liegt ausserhalb "
            f"[{DELAY_MIN}, {DELAY_MAX}] - keine Aenderung. "
            f"Bitte manuell pruefen, hier stimmt etwas grundsaetzlich nicht.")
        save_state({"measurements": []})
        sys.exit(1)

    log(f"Korrektur: {current:.4f}s -> {new_delay:.4f}s "
        f"(Schritt {step*1000:+.1f}ms, stabiler Fehler "
        f"{stable_error*1000:+.1f}ms)")

    if dry_run:
        log("DRY-RUN: keine Aenderung geschrieben.")
        sys.exit(0)

    if write_new_delay(new_delay):
        log("Neuer Wert geschrieben. Der Decoder uebernimmt ihn ab der "
            "naechsten Minute, ein Neustart ist nicht noetig.")
        # Historie leeren: die naechste Messreihe soll die Wirkung der
        # Korrektur bewerten, nicht die alten Messungen davor.
        save_state({"measurements": [], "last_change": {
            "time": datetime.datetime.now().isoformat(timespec="seconds"),
            "from": current,
            "to": new_delay,
        }})
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
