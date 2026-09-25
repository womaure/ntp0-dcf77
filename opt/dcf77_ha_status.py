#!/usr/bin/env python3
"""
DCF77 -> Home Assistant Status-Ampel (MQTT)
=============================================

Liest periodisch den chrony-Status der DCF77-Quelle aus und
veroeffentlicht eine Ampel-Farbe (green/orange/red) per MQTT.
Nutzt MQTT Discovery, damit Home Assistant den Sensor automatisch
findet - keine manuelle configuration.yaml noetig.

VORAUSSETZUNG: paho-mqtt muss installiert sein:
    pip3 install paho-mqtt --break-system-packages

WICHTIG - vor dem Start:
    /etc/ntp-0.conf muss existieren und die MQTT-Zugangsdaten enthalten
    (Broker-IP, Port, User, Passwort). Siehe ntp-0.conf als Vorlage.

Ausfuehren (zum Testen):
    python3 dcf77_ha_status.py

Fuer Dauerbetrieb: als systemd-Service einrichten (siehe
dcf77-ha-status.service), analog zum Decoder-Dienst.
"""

import subprocess
import re
import time
import json
import sys
import datetime
import configparser
import signal
import os

import paho.mqtt.client as mqtt

CONFIG_FILE = "/etc/ntp-0.conf"


def load_mqtt_config():
    """Liest die MQTT-Zugangsdaten aus /etc/ntp-0.conf.
    Bricht mit klarer Fehlermeldung ab, falls die Datei fehlt oder
    Pflichtfelder leer sind - besser als ein kryptischer Verbindungsfehler."""
    parser = configparser.ConfigParser()
    read_files = parser.read(CONFIG_FILE)
    if not read_files:
        print(f"FEHLER: Konfigurationsdatei {CONFIG_FILE} nicht gefunden.")
        sys.exit(1)

    if "mqtt" not in parser:
        print(f"FEHLER: Abschnitt [mqtt] fehlt in {CONFIG_FILE}.")
        sys.exit(1)

    section = parser["mqtt"]
    broker = section.get("broker", "").strip()
    port = section.getint("port", fallback=1883)
    user = section.get("user", "").strip()
    password = section.get("password", "").strip()

    if not broker:
        print(f"FEHLER: 'broker' ist in {CONFIG_FILE} nicht gesetzt.")
        sys.exit(1)
    if not user or not password:
        print(f"FEHLER: 'user' und/oder 'password' sind in {CONFIG_FILE} "
              f"noch nicht eingetragen. Bitte ausfuellen.")
        sys.exit(1)

    return broker, port, user, password


MQTT_BROKER, MQTT_PORT, MQTT_USER, MQTT_PASSWORD = load_mqtt_config()

# Abstand zwischen zwei Statusdurchlaeufen.
#
# Urspruenglich 30 Sekunden. Die Systemanalyse hat gezeigt, dass dieser
# Dienst mit 7,2 Prozent eines Kerns der groesste Einzelverbraucher auf
# dem Pi war - mehr als das Vierfache des Dekoders. Ursache ist nicht die
# Auswertung selbst, sondern das Starten externer Prozesse: Auf einem
# ARMv6-Einkerner mit 800 MHz ist jeder Prozessstart teuer, und es waren
# drei pro Durchlauf.
#
# 60 Sekunden reichen voellig: Der Dekoder liefert ohnehin nur einen Wert
# pro Minute, haeufigeres Abfragen liefert also keine neue Information.
CHECK_INTERVAL = 60

# Fail2ban wird nur jeden n-ten Durchlauf abgefragt. Sperren aendern sich
# selten - seit der Einrichtung gab es ausser den Testsperren keine
# einzige. Eine Verzoegerung von wenigen Minuten bis zur Anzeige einer
# neuen Sperre ist unerheblich, spart aber jeden fuenften Prozessstart.
FAIL2BAN_EVERY_N = 5

# Die SD-Pruefungen, die externe Prozesse starten (Kernel-Log durchsuchen,
# TRIM-Status), laufen nur alle zehn Minuten. Ein Kartendefekt kuendigt
# sich ueber Stunden an; minuetlich zu pruefen braechte nichts ausser Last.
SD_TEUER_EVERY_N = 10

DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_status/config"
STATE_TOPIC = "homeassistant/sensor/dcf77_status/state"
ATTR_TOPIC = "homeassistant/sensor/dcf77_status/attributes"

CLIENTS_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp_clients/config"
CLIENTS_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp_clients/state"
CLIENTS_ATTR_TOPIC = "homeassistant/sensor/dcf77_ntp_clients/attributes"

BOOT_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_last_boot/config"
BOOT_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_last_boot/state"

# Alter, jetzt entfernter Zahlen-Sensor - Topic bleibt hier als Konstante
# erhalten, nur um ihn einmalig sauber aus Home Assistant zu entfernen
# (siehe Aufraeum-Hinweis unten bei main()).
F2B_DISCOVERY_TOPIC_OLD = "homeassistant/sensor/dcf77_ntp0_fail2ban/config"

F2B_IPS_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_fail2ban_ips/config"
F2B_IPS_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_fail2ban_ips/state"
F2B_IPS_ATTR_TOPIC = "homeassistant/sensor/dcf77_ntp0_fail2ban_ips/attributes"

# Gemeinsames Verfuegbarkeits-Topic fuer alle Sensoren dieses Geraets.
# Ohne das behaelt Home Assistant bei einem Ausfall wegen retain=True
# einfach den letzten Wert bei - die Ampel stuende dann dauerhaft auf
# "green", obwohl der Server tot ist. Mit Last-Will meldet der Broker
# automatisch "offline", sobald die Verbindung abreisst, und HA zeigt
# die Sensoren als "nicht verfuegbar" an.
SOURCE_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_zeitquelle/config"
SOURCE_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_zeitquelle/state"
SOURCE_ATTR_TOPIC = "homeassistant/sensor/dcf77_ntp0_zeitquelle/attributes"

RTC_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_rtc/config"
RTC_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_rtc/state"
RTC_ATTR_TOPIC = "homeassistant/sensor/dcf77_ntp0_rtc/attributes"

RTCTEMP_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_rtc_temp/config"
RTCTEMP_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_rtc_temp/state"

CPUTEMP_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_cpu_temp/config"
CPUTEMP_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_cpu_temp/state"

BACKUP_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_backup/config"
BACKUP_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_backup/state"
BACKUP_ATTR_TOPIC = "homeassistant/sensor/dcf77_ntp0_backup/attributes"

SD_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_sd/config"
SD_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_sd/state"
SD_ATTR_TOPIC = "homeassistant/sensor/dcf77_ntp0_sd/attributes"

SDW_DISCOVERY_TOPIC = "homeassistant/sensor/dcf77_ntp0_sd_written/config"
SDW_STATE_TOPIC = "homeassistant/sensor/dcf77_ntp0_sd_written/state"

AVAILABILITY_TOPIC = "homeassistant/sensor/dcf77_ntp0/availability"
PAYLOAD_ONLINE = "online"
PAYLOAD_OFFLINE = "offline"

DISCOVERY_PAYLOAD = {
    "name": "DCF77 Status",
    "unique_id": "dcf77_status_ntp0",
    "state_topic": STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "json_attributes_topic": ATTR_TOPIC,
    "icon": "mdi:radio-tower",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

CLIENTS_DISCOVERY_PAYLOAD = {
    "name": "DCF77 NTP Clients",
    "unique_id": "dcf77_ntp_clients_ntp0",
    "state_topic": CLIENTS_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "json_attributes_topic": CLIENTS_ATTR_TOPIC,
    "icon": "mdi:lan-connect",
    "unit_of_measurement": "Clients",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

BOOT_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 Letzter Neustart",
    "unique_id": "dcf77_ntp0_last_boot",
    "state_topic": BOOT_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "device_class": "timestamp",
    "icon": "mdi:restart",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

SOURCE_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 Zeitquelle",
    "unique_id": "dcf77_ntp0_zeitquelle",
    "state_topic": SOURCE_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "json_attributes_topic": SOURCE_ATTR_TOPIC,
    "icon": "mdi:source-branch",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

SD_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 SD-Karte",
    "unique_id": "dcf77_ntp0_sd",
    "state_topic": SD_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "json_attributes_topic": SD_ATTR_TOPIC,
    "icon": "mdi:micro-sd",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

SDW_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 SD-Karte geschrieben",
    "unique_id": "dcf77_ntp0_sd_written",
    "state_topic": SDW_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "device_class": "data_size",
    "unit_of_measurement": "GiB",
    "state_class": "total_increasing",
    "icon": "mdi:content-save-edit-outline",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

BACKUP_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 Letztes Backup",
    "unique_id": "dcf77_ntp0_backup",
    "state_topic": BACKUP_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "json_attributes_topic": BACKUP_ATTR_TOPIC,
    "device_class": "timestamp",
    "icon": "mdi:content-save-check-outline",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

RTC_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 Hardware-Uhr",
    "unique_id": "dcf77_ntp0_rtc",
    "state_topic": RTC_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "json_attributes_topic": RTC_ATTR_TOPIC,
    "icon": "mdi:clock-check-outline",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

RTCTEMP_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 Temperatur Hardware-Uhr",
    "unique_id": "dcf77_ntp0_rtc_temp",
    "state_topic": RTCTEMP_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "device_class": "temperature",
    "unit_of_measurement": "°C",
    "state_class": "measurement",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

CPUTEMP_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 Temperatur CPU",
    "unique_id": "dcf77_ntp0_cpu_temp",
    "state_topic": CPUTEMP_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "device_class": "temperature",
    "unit_of_measurement": "°C",
    "state_class": "measurement",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}

F2B_IPS_DISCOVERY_PAYLOAD = {
    "name": "ntp-0 Fail2ban gesperrte IPs",
    "unique_id": "dcf77_ntp0_fail2ban_ips",
    "state_topic": F2B_IPS_STATE_TOPIC,
    "availability_topic": AVAILABILITY_TOPIC,
    "payload_available": PAYLOAD_ONLINE,
    "payload_not_available": PAYLOAD_OFFLINE,
    "json_attributes_topic": F2B_IPS_ATTR_TOPIC,
    "icon": "mdi:ip-network",
    "device": {
        "identifiers": ["ntp0_dcf77"],
        "name": "DCF77 NTP-Server (ntp-0)",
        "model": "Raspberry Pi 1 Model B+ - Pollin DCF77 + DS3231 RTC",
        "manufacturer": "DIY",
    },
}


# Klartext-Beschreibung der Zustandszeichen aus "chronyc sources".
# Der Sensorwert soll auch für jemanden verständlich sein, der das
# Projekt nicht kennt - "green" oder "x" sagen nichts aus.
#
# Wichtig: Früher wurden '?', 'x' und '~' alle zu "red" zusammengefasst.
# Das verschleierte einen wesentlichen Unterschied: Bei '?' kommt gar
# nichts an, bei 'x' kommt sehr wohl ein Signal, es weicht nur von den
# übrigen Quellen ab. Das sind völlig verschiedene Fehlerbilder und
# erfordern unterschiedliche Maßnahmen.
DCF_STATE_TEXT = {
    "*": ("Synchronisiert", "green"),
    "+": ("Empfang gut, andere Quelle führt", "orange"),
    "-": ("Empfang gut, wird nicht verwendet", "orange"),
    "x": ("Empfang da, Zeitwert weicht ab", "red"),
    "?": ("Kein Empfang", "red"),
    "~": ("Signal zu unruhig", "red"),
}


def get_dcf_status():
    """Fragt chronyc sources ab und wertet die DCF77-Zeile aus.

    Rueckgabe: (klartext, detail_dict)
    Der Klartext ist der Sensorwert in Home Assistant. Die Ampelfarbe
    steckt zusaetzlich im Attribut "ampel", damit eine farbliche
    Darstellung weiterhin moeglich ist.
    """
    try:
        result = subprocess.run(
            ["chronyc", "sources", "-v"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout
    except Exception as e:
        return "Status nicht abrufbar", {
            "ampel": "red",
            "error": f"chronyc nicht erreichbar: {e}"}

    dcf_line = None
    for line in output.splitlines():
        if " DCF " in line or line.strip().startswith(("#", "^", "=")) and "DCF" in line:
            dcf_line = line
            break

    if dcf_line is None:
        return "DCF77-Quelle nicht eingerichtet", {
            "ampel": "red",
            "error": "Keine DCF-Zeile in chronyc sources gefunden"}

    # Beispielzeile: "#*  DCF   0  6  42  209  -27ms[-2230us] +/- 2278us"
    # Erste beiden Zeichen: Mode ('#') und State ('*','+','-','?','x','~')
    state_char = dcf_line.strip()[1] if len(dcf_line.strip()) > 1 else "?"

    reach_match = re.search(r"DCF\s+\d+\s+\d+\s+(\d+)", dcf_line)
    reach_octal = reach_match.group(1) if reach_match else "0"
    try:
        reach_value = int(reach_octal, 8)
    except ValueError:
        reach_value = 0

    # Reach ist ein Bitfeld der letzten 8 Abfragen. Die Dezimalzahl ist
    # zum Ablesen unbrauchbar (129 klingt nach "viel", bedeutet aber nur
    # 2 von 8 Treffern). Deshalb zusaetzlich die Trefferzahl mitgeben.
    reach_successes = bin(reach_value).count("1")

    klartext, ampel = DCF_STATE_TEXT.get(
        state_char, ("Zustand unbekannt", "red"))

    # Sonderfall: chrony meldet die Quelle als brauchbar, es ist aber noch
    # keine einzige Abfrage angekommen (direkt nach einem Neustart).
    if state_char in ("+", "-") and reach_value == 0:
        klartext, ampel = "Wartet auf ersten Empfang", "red"

    detail = {
        "raw_line": dcf_line.strip(),
        "state_char": state_char,
        "ampel": ampel,
        "reach_octal": reach_octal,
        "reach_decimal": reach_value,
        "reach_successes": reach_successes,
        "reach_display": f"{reach_successes}/8 Abfragen",
    }

    return klartext, detail


def get_clients_summary():
    """Fragt chronyc clients ab und wertet die Client-Liste aus.
    Muss als root laufen (unser systemd-Dienst tut das bereits).
    Rueckgabe: (anzahl_aktiv, detail_dict)
    """
    try:
        result = subprocess.run(
            ["chronyc", "clients"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout
    except Exception as e:
        return 0, {"error": f"chronyc clients nicht erreichbar: {e}"}

    lines = output.splitlines()
    # Erste zwei Zeilen sind Header + Trennlinie ("===...")
    data_lines = [l for l in lines if l.strip() and not l.startswith("Hostname")
                  and not l.strip().startswith("=")]

    clients = []
    for line in data_lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        hostname = parts[0]
        try:
            ntp_requests = int(parts[1])
        except ValueError:
            ntp_requests = 0
        clients.append({"hostname": hostname, "ntp_requests": ntp_requests})

    active_clients = [c for c in clients if c["ntp_requests"] > 0]

    detail = {
        "total_known": len(clients),
        "active": len(active_clients),
        "clients": clients,
    }

    return len(active_clients), detail


def get_tracking_status():
    """Wertet 'chronyc tracking' aus und bestimmt, woher die Zeit kommt.

    Besonders wichtig ist der Notlauf: Fallen DCF77 und die Internet-
    Quellen gleichzeitig aus, liefert chrony dank der 'local'-Direktive
    weiter Zeit aus - erkennbar an der Referenz-ID 7F7F0101 (das ist die
    klassische LOCAL-Kennung 127.127.1.1) und dem leeren Quellennamen.
    Ohne diesen Sensor wäre von außen nicht zu sehen, dass der Server
    gerade nur noch auf seiner eigenen Uhr läuft.

    Rueckgabe: (quelle_als_text, detail_dict)
    """
    try:
        result = subprocess.run(
            ["chronyc", "tracking"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout
    except Exception as e:
        return "Status nicht abrufbar", {
            "error": f"chronyc tracking nicht erreichbar: {e}"}

    if result.returncode != 0:
        return "Status nicht abrufbar", {
            "error": "chronyc tracking fehlgeschlagen"}

    fields = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()

    # "Reference ID" sieht so aus: "44434600 (DCF)" bzw. "7F7F0101 ()"
    ref_raw = fields.get("Reference ID", "")
    ref_id = ref_raw.split()[0] if ref_raw else ""
    ref_name = ""
    if "(" in ref_raw and ")" in ref_raw:
        ref_name = ref_raw[ref_raw.index("(") + 1:ref_raw.rindex(")")].strip()

    def num(key):
        """Zahl aus Feldern wie '0.000140355 seconds slow of NTP time'."""
        raw = fields.get(key, "")
        for token in raw.split():
            try:
                return float(token)
            except ValueError:
                continue
        return None

    stratum = None
    try:
        stratum = int(fields.get("Stratum", ""))
    except ValueError:
        pass

    # Klartext, der ohne Projektkenntnis verständlich ist.
    if ref_id.upper().startswith("7F7F"):
        # 7F7F0101 ist die klassische LOCAL-Kennung (127.127.1.1): chronyd
        # liefert die eigene Systemuhr aus, weil keine Quelle verfügbar ist.
        quelle = "Notlauf: eigene Uhr, keine externe Quelle"
    elif ref_name == "DCF":
        quelle = "DCF77-Funksignal"
    elif ref_name:
        quelle = f"Internet-Zeitserver: {ref_name}"
    elif not ref_id or ref_id == "00000000":
        # Direkt nach dem Start, bevor eine Quelle ausgewählt wurde.
        quelle = "Noch keine Synchronisation"
    else:
        quelle = f"Unbekannte Quelle ({ref_id})"

    detail = {
        "reference_id": ref_id,
        "reference_name": ref_name,
        "stratum": stratum,
        "system_time_offset_s": num("System time"),
        "last_offset_s": num("Last offset"),
        "rms_offset_s": num("RMS offset"),
        "frequency_ppm": num("Frequency"),
        "skew_ppm": num("Skew"),
        "root_delay_s": num("Root delay"),
        "root_dispersion_s": num("Root dispersion"),
        "update_interval_s": num("Update interval"),
        "leap_status": fields.get("Leap status", ""),
    }

    return quelle, detail


def hwmon_pfad_finden(gesuchter_name):
    """Findet den hwmon-Eintrag mit dem gesuchten Namen.

    Die Nummerierung unter /sys/class/hwmon ist nicht stabil - je nach
    Reihenfolge der Treiberinitialisierung kann die DS3231 mal hwmon1,
    mal hwmon2 sein. Deshalb wird nach dem Namen gesucht statt eine
    Nummer fest zu verdrahten.
    """
    try:
        for eintrag in os.listdir("/sys/class/hwmon"):
            namensdatei = f"/sys/class/hwmon/{eintrag}/name"
            try:
                with open(namensdatei) as f:
                    if f.read().strip() == gesuchter_name:
                        return f"/sys/class/hwmon/{eintrag}"
            except OSError:
                continue
    except OSError:
        pass
    return None


def temperatur_lesen(hwmon_name):
    """Liest temp1_input des benannten hwmon-Geraets in Grad Celsius."""
    basis = hwmon_pfad_finden(hwmon_name)
    if not basis:
        return None
    try:
        with open(f"{basis}/temp1_input") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


PROC_MOUNTS = "/proc/mounts"
SYS_BLOCK = "/sys/block"
SYS_EXT4 = "/sys/fs/ext4"
SD_SAMPLES_FILE = "/var/lib/ntp0-status/sd_samples.json"

# Hersteller-IDs aus dem CID-Register der Karte
SD_HERSTELLER = {
    "0x000003": "SanDisk", "0x00001b": "Samsung", "0x000074": "Transcend",
    "0x000002": "Kioxia/Toshiba", "0x000027": "Phison", "0x000028": "Lexar",
    "0x000041": "Kingston", "0x00009f": "Kingston",
}


def _lesen(pfad):
    try:
        with open(pfad) as f:
            return f.read().strip()
    except OSError:
        return None


def _wurzelgeraet():
    """Blockgeraet und Optionen des Wurzeldateisystems aus /proc/mounts.
    Wird ermittelt statt fest verdrahtet, damit der Sensor auch nach
    einem Kartenwechsel oder anderer Partitionierung stimmt."""
    try:
        with open(PROC_MOUNTS) as f:
            for zeile in f:
                teile = zeile.split()
                if len(teile) >= 4 and teile[1] == "/":
                    return os.path.basename(teile[0]), teile[3].split(",")
    except OSError:
        pass
    return None, []


def _schreibrate(kbytes):
    """Fuehrt eine stuendliche Messreihe des ext4-Schreibzaehlers und
    berechnet daraus die Schreibmenge der letzten 24 Stunden und den
    Tagesdurchschnitt der letzten sieben Tage.

    Die Reihe liegt unter /var/lib/ntp0-status und wird bewusst NICHT
    gesichert: Nach einer Wiederherstellung auf eine neue Karte gehoert
    der Zaehler zu einem anderen Dateisystem, alte Werte waeren sinnlos.
    Sinkt der Zaehler (neue Karte, neues Dateisystem), beginnt die Reihe
    von vorn.
    """
    jetzt = time.time()
    try:
        with open(SD_SAMPLES_FILE) as f:
            reihe = json.load(f)
    except (OSError, ValueError):
        reihe = []

    if reihe and kbytes < reihe[-1][1]:
        reihe = []
    if not reihe or jetzt - reihe[-1][0] >= 3600:
        reihe.append([jetzt, kbytes])
        reihe = [r for r in reihe if jetzt - r[0] <= 8 * 86400]
        try:
            os.makedirs(os.path.dirname(SD_SAMPLES_FILE), exist_ok=True)
            with open(SD_SAMPLES_FILE, "w") as f:
                json.dump(reihe, f)
        except OSError:
            pass

    def rate(zeitraum):
        # aeltester Messpunkt, der mindestens den halben Zeitraum zurueckliegt
        kandidaten = [r for r in reihe if jetzt - r[0] >= zeitraum / 2]
        if not kandidaten:
            return None
        start = min(kandidaten, key=lambda r: abs((jetzt - r[0]) - zeitraum))
        dauer = jetzt - start[0]
        if dauer <= 0:
            return None
        return (kbytes - start[1]) / 1024 / (dauer / 86400)   # MB pro Tag

    r24 = rate(86400)
    r7 = rate(7 * 86400)
    return (round(r24, 1) if r24 is not None else None,
            round(r7, 1) if r7 is not None else None)


def get_sd_status(mit_teuren_pruefungen, letzte_teure):
    """Zustand der SD-Karte.

    Die Karte selbst meldet keinen Verschleiss - SanDisk High Endurance
    und Samsung Pro Endurance beantworten das dafuer vorgesehene
    Herstellerkommando nicht. Ausgewertet wird deshalb, was Linux weiss,
    und das sind genau die Anzeichen, die einem Ausfall vorausgehen:
    Dateisystemfehler, Lese-/Schreibfehler der Karte und ein Wechsel auf
    schreibgeschuetzt.

    Die Kernelmeldungen und der TRIM-Status erfordern externe Prozesse
    und werden nur bei mit_teuren_pruefungen neu erhoben; dazwischen
    gilt letzte_teure.

    Rueckgabe: (klartext, gesamt_gib, detail, teure_ergebnisse)
    """
    partition, optionen = _wurzelgeraet()
    if not partition:
        return "Nicht ermittelbar", None, {"error": "Wurzeldateisystem unbekannt"}, letzte_teure

    karte = re.sub(r"p\d+$", "", partition)          # mmcblk0p2 -> mmcblk0
    geraet = f"{SYS_BLOCK}/{karte}/device"
    manfid = _lesen(f"{geraet}/manfid")

    kb = _lesen(f"{SYS_EXT4}/{partition}/lifetime_write_kbytes")
    fehler = _lesen(f"{SYS_EXT4}/{partition}/errors_count")
    kb = int(kb) if kb and kb.isdigit() else None
    fehler = int(fehler) if fehler and fehler.isdigit() else None

    try:
        # Berechnung wie bei df: belegt im Verhaeltnis zu belegt plus
        # verfuegbar. Die fuer root reservierten Bloecke (bei ext4 meist
        # 5 %) zaehlen weder als belegt noch als verfuegbar.
        st = os.statvfs("/")
        benutzt = st.f_blocks - st.f_bfree
        belegt = round(100 * benutzt / (benutzt + st.f_bavail), 1)
    except OSError:
        belegt = None

    discard = _lesen(f"{SYS_BLOCK}/{karte}/queue/discard_max_bytes")
    schreibgeschuetzt = "ro" in optionen

    if mit_teuren_pruefungen:
        teure = {}
        out = None
        try:
            r = subprocess.run(["journalctl", "-k", "-b", "--no-pager", "-o", "cat"],
                               capture_output=True, text=True, timeout=30)
            out = r.stdout if r.returncode == 0 else None
        except Exception:
            pass
        if out is not None:
            muster = re.compile(
                rf"(I/O error.*{karte}|{karte}.*I/O error|mmc\d+: .*(timeout|error))",
                re.IGNORECASE)
            teure["io_fehler_seit_start"] = sum(1 for z in out.splitlines() if muster.search(z))
        try:
            r = subprocess.run(["systemctl", "show", "fstrim.service",
                                "-p", "ExecMainExitTimestamp", "-p", "ExecMainStatus"],
                               capture_output=True, text=True, timeout=10)
            werte = dict(z.split("=", 1) for z in r.stdout.splitlines() if "=" in z)
            teure["trim_letzter_lauf"] = werte.get("ExecMainExitTimestamp") or None
            teure["trim_erfolgreich"] = werte.get("ExecMainStatus") == "0" \
                if werte.get("ExecMainExitTimestamp") else None
        except Exception:
            pass
        letzte_teure = teure

    io_fehler = letzte_teure.get("io_fehler_seit_start")
    r24, r7 = _schreibrate(kb) if kb is not None else (None, None)

    detail = {
        "karte": _lesen(f"{geraet}/name"),
        "hersteller": SD_HERSTELLER.get(manfid, manfid),
        "hergestellt": _lesen(f"{geraet}/date"),
        "partition": partition,
        "geschrieben_gib": round(kb / 1048576, 2) if kb is not None else None,
        "geschrieben_mb_24h": r24,
        "geschrieben_mb_pro_tag_7d": r7,
        "dateisystemfehler": fehler,
        "io_fehler_seit_start": io_fehler,
        "schreibgeschuetzt": schreibgeschuetzt,
        "belegt_prozent": belegt,
        "trim_unterstuetzt": (discard is not None and discard.isdigit() and int(discard) > 0),
        "trim_letzter_lauf": letzte_teure.get("trim_letzter_lauf"),
        "trim_erfolgreich": letzte_teure.get("trim_erfolgreich"),
    }

    # Bewertung, schwerwiegendstes zuerst
    if schreibgeschuetzt:
        klartext = "Schreibgeschuetzt eingehaengt - Karte pruefen!"
    elif fehler:
        klartext = f"Dateisystemfehler ({fehler})"
    elif io_fehler:
        klartext = f"Lese-/Schreibfehler seit Start ({io_fehler})"
    elif belegt is not None and belegt >= 90:
        klartext = f"Fast voll ({belegt:.0f} %)"
    else:
        klartext = "In Ordnung"

    gesamt = detail["geschrieben_gib"]
    return klartext, gesamt, detail, letzte_teure


BACKUP_STATUS_FILE = "/var/lib/ntp0-backup/status.json"


def get_backup_status():
    """Liest die Statusdatei, die das Backup-Skript nach jedem Lauf schreibt.

    Der Sensorwert ist der Zeitpunkt des letzten ERFOLGREICHEN Backups.
    Schlaegt ein Lauf fehl oder faellt ganz aus, altert dieser Wert - und
    Home Assistant kann allein daran erkennen, dass seit Tagen nichts
    gesichert wurde. Ob das Skript gescheitert ist oder gar nicht lief,
    spielt dafuer keine Rolle.

    Rueckgabe: (zeitstempel_oder_None, detail_dict)
    """
    try:
        with open(BACKUP_STATUS_FILE) as f:
            status = json.load(f)
    except FileNotFoundError:
        return None, {"hinweis": "Noch kein Backup gelaufen "
                                 "(Statusdatei fehlt)"}
    except (OSError, ValueError) as e:
        return None, {"error": f"Statusdatei nicht lesbar: {e}"}

    letzter_erfolg = status.get("letzter_erfolg")

    detail = dict(status)
    if letzter_erfolg:
        try:
            alter = (datetime.datetime.now().astimezone()
                     - datetime.datetime.fromisoformat(letzter_erfolg))
            detail["alter_tage"] = round(alter.total_seconds() / 86400, 1)
        except ValueError:
            pass

    return letzter_erfolg, detail


def get_rtc_status():
    """Prueft die Hardware-Uhr (DS3231).

    Die DS3231 kann ihre Batteriespannung nicht melden - dafuer hat der
    Chip kein Register. Der aussagekraeftige Indikator ist stattdessen
    die Abweichung zur Systemzeit:

      - Im Normalbetrieb stellt der Kernel die RTC alle 11 Minuten
        (rtcsync), die Abweichung bleibt im Bereich von Sekundenbruch-
        teilen.
      - Ist die Knopfzelle leer, verliert die RTC bei stromloser Phase
        ihre Zeit. Das zeigt sich nach dem naechsten Start als voellig
        abwegiges Datum - so wie am 1. September, als die frisch
        eingebaute, noch nie gestellte RTC "2000-01-01" meldete.

    Rueckgabe: (klartext, detail_dict)
    """
    geraet_da = os.path.exists("/dev/rtc0")

    try:
        result = subprocess.run(
            ["hwclock", "-r"],
            capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return "hwclock nicht vorhanden", {"geraet_vorhanden": geraet_da}
    except Exception as e:
        return "Nicht abrufbar", {"geraet_vorhanden": geraet_da,
                                  "error": str(e)}

    if result.returncode != 0:
        # Haeufigster Fall: fehlende Rechte, oder die RTC antwortet nicht
        fehler = result.stderr.strip() or "unbekannter Fehler"
        if "denied" in fehler.lower() or "permitted" in fehler.lower():
            return "Keine Berechtigung zum Lesen", {
                "geraet_vorhanden": geraet_da, "error": fehler}
        return "Antwortet nicht", {"geraet_vorhanden": geraet_da,
                                   "error": fehler}

    roh = result.stdout.strip()
    try:
        rtc_zeit = datetime.datetime.fromisoformat(roh)
    except ValueError:
        return "Zeit nicht lesbar", {"geraet_vorhanden": geraet_da,
                                     "rohwert": roh}

    jetzt = datetime.datetime.now(rtc_zeit.tzinfo)
    abweichung = (rtc_zeit - jetzt).total_seconds()

    detail = {
        "geraet_vorhanden": geraet_da,
        "rtc_zeit": rtc_zeit.isoformat(timespec="seconds"),
        "abweichung_sekunden": round(abweichung, 3),
        "temperatur_c": temperatur_lesen("ds3231"),
    }

    # Bewertung. Die Grenzen orientieren sich am beobachteten Verhalten:
    # Mit rtcsync liegt die Abweichung normalerweise deutlich unter einer
    # Sekunde.
    betrag = abs(abweichung)
    if betrag > 86400:
        klartext = "Zeit voellig abwegig - Batterie pruefen"
    elif betrag > 60:
        klartext = f"Weicht stark ab ({abweichung:+.0f}s)"
    elif betrag > 2:
        klartext = f"Weicht ab ({abweichung:+.1f}s)"
    else:
        klartext = "Laeuft synchron"

    return klartext, detail


def get_fail2ban_status():
    """Fragt fail2ban-client status sshd ab und wertet aktive Sperren aus.
    Muss als root laufen (unser systemd-Dienst tut das bereits).
    Rueckgabe: (anzahl_gesperrt, detail_dict)
    """
    try:
        result = subprocess.run(
            ["fail2ban-client", "status", "sshd"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout
    except Exception as e:
        return 0, {"error": f"fail2ban-client nicht erreichbar: {e}"}

    if result.returncode != 0:
        return 0, {"error": "fail2ban-client Aufruf fehlgeschlagen "
                             "(laeuft der Dienst?)"}

    currently_banned = 0
    banned_ips = []

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("`- Currently banned:") or line.startswith("|- Currently banned:"):
            try:
                currently_banned = int(line.split(":")[1].strip())
            except (ValueError, IndexError):
                pass
        elif line.startswith("`- Banned IP list:") or line.startswith("|- Banned IP list:"):
            ip_part = line.split(":", 1)[1].strip()
            if ip_part:
                banned_ips = ip_part.split()

    detail = {
        "currently_banned": currently_banned,
        "banned_ips": banned_ips,
    }

    return currently_banned, detail


def get_last_boot_time():
    """Liest /proc/uptime aus und berechnet den Zeitpunkt des letzten
    Boots als ISO-8601-Zeitstempel (UTC), wie ihn der Home Assistant
    'timestamp'-device_class erwartet.

    Gibt None zurueck, wenn seit dem Boot weniger als 2 Minuten
    vergangen sind: in diesem kurzen Fenster koennte die Systemuhr
    noch nicht von der RTC korrigiert worden sein (die wird laut
    dmesg typischerweise erst nach ca. 90s ausgelesen), und eine in
    dieser Phase berechnete Boot-Zeit waere unzuverlaessig. Nach
    Ablauf dieses Fensters ist die Systemuhr garantiert korrekt,
    unabhaengig davon, wie lange der Pi tatsaechlich schon laeuft -
    ein rein kalenderbasierter Plausibilitaetscheck wuerde dagegen
    faelschlich auch echte, lange Laufzeiten verwerfen.
    """
    try:
        with open("/proc/uptime", "r") as f:
            uptime_seconds = float(f.readline().split()[0])
    except Exception:
        return None

    if uptime_seconds < 120:
        return None

    now = datetime.datetime.now(datetime.timezone.utc)
    boot_time = now - datetime.timedelta(seconds=uptime_seconds)
    # Auf ganze Sekunden runden, Mikrosekunden-Praezision ist hier unnoetig
    boot_time = boot_time.replace(microsecond=0)
    return boot_time.isoformat()


def publish_discovery(client):
    """Veroeffentlicht alle Discovery-Configs und meldet uns als online.

    Wird nicht nur beim Start aufgerufen, sondern bei JEDER erfolgreichen
    Verbindung (siehe on_connect). Das ist wichtig: Nach einem Broker-
    Neustart oder einer Verbindungsunterbrechung hat der Broker unser
    Last-Will "offline" gesetzt - ohne erneutes "online" blieben die
    Sensoren in Home Assistant dauerhaft auf "nicht verfuegbar" stehen.
    """
    client.publish(DISCOVERY_TOPIC, json.dumps(DISCOVERY_PAYLOAD), retain=True)
    client.publish(CLIENTS_DISCOVERY_TOPIC, json.dumps(CLIENTS_DISCOVERY_PAYLOAD), retain=True)
    client.publish(BOOT_DISCOVERY_TOPIC, json.dumps(BOOT_DISCOVERY_PAYLOAD), retain=True)
    client.publish(F2B_IPS_DISCOVERY_TOPIC, json.dumps(F2B_IPS_DISCOVERY_PAYLOAD), retain=True)
    client.publish(SOURCE_DISCOVERY_TOPIC, json.dumps(SOURCE_DISCOVERY_PAYLOAD), retain=True)
    client.publish(RTC_DISCOVERY_TOPIC, json.dumps(RTC_DISCOVERY_PAYLOAD), retain=True)
    client.publish(BACKUP_DISCOVERY_TOPIC, json.dumps(BACKUP_DISCOVERY_PAYLOAD), retain=True)
    client.publish(SD_DISCOVERY_TOPIC, json.dumps(SD_DISCOVERY_PAYLOAD), retain=True)
    client.publish(SDW_DISCOVERY_TOPIC, json.dumps(SDW_DISCOVERY_PAYLOAD), retain=True)
    client.publish(RTCTEMP_DISCOVERY_TOPIC, json.dumps(RTCTEMP_DISCOVERY_PAYLOAD), retain=True)
    client.publish(CPUTEMP_DISCOVERY_TOPIC, json.dumps(CPUTEMP_DISCOVERY_PAYLOAD), retain=True)

    # Alten, jetzt redundanten Zahlen-Sensor "ntp-0 Fail2ban Sperren"
    # einmalig aus Home Assistant entfernen: eine leere retained
    # Nachricht auf seinem Discovery-Topic loescht die Entitaet.
    client.publish(F2B_DISCOVERY_TOPIC_OLD, "", retain=True)

    # Erst NACH der Discovery online melden, damit Home Assistant die
    # Entitaeten schon kennt, wenn die Verfuegbarkeit eintrifft.
    client.publish(AVAILABILITY_TOPIC, PAYLOAD_ONLINE, retain=True)


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("MQTT verbunden - Discovery und Verfuegbarkeit werden gesendet.")
        publish_discovery(client)
    else:
        print(f"MQTT-Verbindung fehlgeschlagen (Code {rc})")


def on_disconnect(client, userdata, rc):
    if rc != 0:
        print(f"MQTT-Verbindung unerwartet getrennt (Code {rc}) - "
              f"paho versucht automatisch neu zu verbinden.")


def main():
    # Sauberes Beenden: systemd schickt beim Stoppen ein SIGTERM. Ohne
    # Handler wuerde Python sofort abbrechen, der finally-Block liefe
    # nicht, und wir koennten uns nicht ordentlich abmelden - Home
    # Assistant haette dann bis zum Keepalive-Timeout eine Falschanzeige.
    def handle_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, handle_sigterm)

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    # Last-Will MUSS vor connect() gesetzt werden: Der Broker merkt sich
    # diese Nachricht und veroeffentlicht sie selbsttaetig, sobald die
    # Verbindung abreisst (Absturz, Stromausfall, Netzwerkproblem).
    client.will_set(AVAILABILITY_TOPIC, PAYLOAD_OFFLINE, retain=True)

    print(f"Verbinde zu MQTT-Broker {MQTT_BROKER}:{MQTT_PORT}...")
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()

    print("MQTT Discovery veroeffentlicht. Home Assistant sollte die Sensoren "
          "'DCF77 Status', 'DCF77 NTP Clients', 'ntp-0 Letzter Neustart' und "
          "'ntp-0 Fail2ban gesperrte IPs', 'ntp-0 Zeitquelle', "
          "'ntp-0 Hardware-Uhr' und die beiden Temperatursensoren "
          "automatisch finden.")

    durchlauf = 0
    # Letztes Fail2ban-Ergebnis merken, damit zwischen den Abfragen
    # weiterhin ein gueltiger Wert veroeffentlicht wird.
    letzte_f2b = (0, {"currently_banned": 0, "banned_ips": []})
    # Ergebnisse der teuren SD-Pruefungen (Kernelmeldungen, TRIM) - werden
    # nur alle SD_TEUER_EVERY_N Durchlaeufe neu erhoben.
    letzte_sd_teure = {}

    try:
        while True:
            durchlauf += 1
            dcf_text, detail = get_dcf_status()
            client.publish(STATE_TOPIC, dcf_text, retain=True)
            client.publish(ATTR_TOPIC, json.dumps(detail), retain=True)

            active_count, clients_detail = get_clients_summary()
            client.publish(CLIENTS_STATE_TOPIC, str(active_count), retain=True)
            client.publish(CLIENTS_ATTR_TOPIC, json.dumps(clients_detail), retain=True)

            last_boot = get_last_boot_time()
            if last_boot:
                client.publish(BOOT_STATE_TOPIC, last_boot, retain=True)

            quelle, tracking_detail = get_tracking_status()
            client.publish(SOURCE_STATE_TOPIC, quelle, retain=True)
            client.publish(SOURCE_ATTR_TOPIC, json.dumps(tracking_detail), retain=True)

            sd_text, sd_gesamt, sd_detail, letzte_sd_teure = get_sd_status(
                durchlauf == 1 or durchlauf % SD_TEUER_EVERY_N == 0,
                letzte_sd_teure)
            client.publish(SD_STATE_TOPIC, sd_text, retain=True)
            client.publish(SD_ATTR_TOPIC, json.dumps(sd_detail), retain=True)
            if sd_gesamt is not None:
                client.publish(SDW_STATE_TOPIC, str(sd_gesamt), retain=True)

            backup_zeit, backup_detail = get_backup_status()
            if backup_zeit:
                client.publish(BACKUP_STATE_TOPIC, backup_zeit, retain=True)
            client.publish(BACKUP_ATTR_TOPIC, json.dumps(backup_detail), retain=True)

            rtc_text, rtc_detail = get_rtc_status()
            client.publish(RTC_STATE_TOPIC, rtc_text, retain=True)
            client.publish(RTC_ATTR_TOPIC, json.dumps(rtc_detail), retain=True)

            rtc_temp = rtc_detail.get("temperatur_c")
            if rtc_temp is not None:
                client.publish(RTCTEMP_STATE_TOPIC, str(rtc_temp), retain=True)

            cpu_temp = temperatur_lesen("cpu_thermal")
            if cpu_temp is not None:
                client.publish(CPUTEMP_STATE_TOPIC, str(cpu_temp), retain=True)

            if durchlauf == 1 or durchlauf % FAIL2BAN_EVERY_N == 0:
                letzte_f2b = get_fail2ban_status()
            banned_count, f2b_detail = letzte_f2b
            ips_text = ", ".join(f2b_detail.get("banned_ips", [])) or "keine"
            client.publish(F2B_IPS_STATE_TOPIC, ips_text, retain=True)
            client.publish(F2B_IPS_ATTR_TOPIC, json.dumps(f2b_detail), retain=True)

            # Verbindungsstatus mit ausgeben. Ohne diesen Hinweis wuerde
            # das Log auch bei getrennter Verbindung munter "Status: green"
            # melden - publish() schlaegt dann still fehl, und man haelt
            # ein totes Monitoring faelschlich fuer funktionierend.
            if client.is_connected():
                conn_note = ""
            else:
                conn_note = "  [!! KEINE MQTT-VERBINDUNG - Werte gehen ins Leere !!]"

            # Textbausteine vorab zusammensetzen. Frueher steckten sie als
            # verschachtelte f-Strings direkt im print - das ist erst ab
            # Python 3.12 zulaessig und scheiterte auf dem Pi (3.11).
            rtc_temp_text = f", {rtc_temp} Grad" if rtc_temp is not None else ""
            cpu_temp_text = f" | CPU {cpu_temp} Grad" if cpu_temp is not None else ""
            alter = backup_detail.get("alter_tage")
            backup_text = (f" | Backup vor {alter}d" if alter is not None
                           else " | Backup: unbekannt")
            sd_text_log = f" | SD: {sd_text}"

            print(f"[{time.strftime('%H:%M:%S')}] DCF77: {dcf_text} "
                  f"[{detail.get('ampel')}] "
                  f"(reach {detail.get('reach_display')}) | "
                  f"Clients bekannt: {active_count}/{clients_detail.get('total_known')} | "
                  f"Letzter Boot: {last_boot} | "
                  f"Fail2ban gesperrt: {ips_text} | "
                  f"Quelle: {quelle} (Stratum {tracking_detail.get('stratum')}) | "
                  f"RTC: {rtc_text}"
                  f"{rtc_temp_text}"
                  f"{cpu_temp_text}"
                  f"{backup_text}"
                  f"{sd_text_log}"
                  f"{conn_note}")
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        print("\nBeendet - melde mich bei Home Assistant ab.")
    finally:
        # Aktiv abmelden, statt auf den Keepalive-Timeout zu warten.
        # Bei einem geplanten Stopp (systemctl stop, Neustart) steht der
        # Sensor damit sofort auf "nicht verfuegbar" statt erst nach
        # bis zu 90 Sekunden.
        try:
            info = client.publish(AVAILABILITY_TOPIC, PAYLOAD_OFFLINE, retain=True)
            info.wait_for_publish(timeout=5)
        except Exception:
            pass
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
