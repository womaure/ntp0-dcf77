#!/usr/bin/env python3
"""
ntp-0 Konfigurations-Backup
============================

Sichert alle projektrelevanten Dateien des DCF77-NTP-Servers in ein
komprimiertes Archiv und legt dieses auf der NAS-Freigabe ab.

HINTERGRUND
-----------
Saemtliche Konfiguration - Decoder, Autotune, Status-Dienst, chrony,
nftables, fail2ban, systemd-Units, RTC- und VLAN-Einrichtung - liegt
ausschliesslich auf der SD-Karte des Pi. SD-Karten sind bei Dauerbetrieb
mit staendigen Schreibzugriffen (Logs) eine bekannte Verschleissstelle.
Ohne Backup ist bei einem Kartendefekt die gesamte Einrichtung verloren.

WAS GESICHERT WIRD
------------------
- Die drei Python-Dienste unter /opt
- /etc/ntp-0.conf (enthaelt Zugangsdaten - siehe Sicherheitshinweis)
- chrony-Konfiguration und Driftfile
- nftables-Regelwerk
- fail2ban jail.local
- Alle systemd-Units des Projekts
- SSH-Konfiguration und autorisierte Schluessel
- Boot-Konfiguration (RTC-Overlay, I2C)
- NetworkManager-Verbindungen (enthaelt die VLAN2-Einrichtung)
- Autotune-Zustand
- Installierte Paketliste (fuer eine Wiederherstellung von Null)

SICHERHEITSHINWEIS
------------------
Das Archiv enthaelt Passwoerter im Klartext (MQTT, Backup) sowie
SSH-Schluessel. Es wird deshalb mit Rechten 600 erzeugt. Auf dem NAS
liegt es allerdings so, wie die Freigabe es zulaesst - die Freigabe
sollte entsprechend nicht fuer jeden im Netz lesbar sein.
Wer das nicht moechte, kann in /etc/ntp-0.conf unter [backup]
gpg_recipient setzen; dann wird das Archiv vor dem Hochladen
verschluesselt.

KONFIGURATION
-------------
Alles Noetige steht in /etc/ntp-0.conf im Abschnitt [backup].

Manueller Testlauf ohne Upload:
    sudo python3 /opt/ntp0_backup.py --local-only
"""

import configparser
import datetime
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

# Benutzer, dessen SSH-Schluessel mitgesichert wird. Bei einer
# Standardinstallation von Raspberry Pi OS ist das der beim Aufsetzen
# angelegte Benutzer.
SSH_USER = "pi"

CONFIG_FILE = "/etc/ntp-0.conf"
WORK_DIR = "/var/tmp/ntp0-backup"

# Ergebnis jedes Laufs wird hier hinterlegt. Der Status-Dienst liest die
# Datei und macht sie als Sensor in Home Assistant sichtbar.
#
# Warum eine Datei und nicht direkt MQTT: Dieses Skript laeuft nur einmal
# pro Woche. Ein hier veroeffentlichter Wert waere zwar retained, aber die
# Datei ueberlebt auch einen Neustart und - wichtiger - sie bleibt alt
# stehen, wenn das Backup gar nicht erst startet. Genau das soll auffallen.
STATUS_FILE = "/var/lib/ntp0-backup/status.json"

# Werden waehrend des Laufs gefuellt und am Ende in die Statusdatei
# uebernommen.
_letzte_pfadzahl = None
_letzte_generationen = None

# Was gesichert wird. Nicht vorhandene Pfade werden uebersprungen und
# am Ende aufgelistet - so faellt auf, wenn sich etwas verschoben hat.
BACKUP_PATHS = [
    "/opt/dcf77_ntp_shm.py",
    "/opt/dcf77_ha_status.py",
    "/opt/dcf77_autotune.py",
    "/opt/dcf77_drift_report.py",
    "/opt/ntp0_wochenbericht.py",
    "/opt/ntp0_backup.py",
    "/opt/ntp0_restore.py",
    "/etc/ntp-0.conf",
    "/etc/chrony/chrony.conf",
    "/var/lib/chrony/chrony.drift",
    "/etc/nftables.conf",
    "/etc/fail2ban/jail.local",
    "/etc/systemd/system/dcf77-decoder.service",
    "/etc/systemd/system/dcf77_ha_status.service",
    "/etc/systemd/system/dcf77-autotune.service",
    "/etc/systemd/system/dcf77-autotune.timer",
    "/etc/systemd/system/rtc-sync.service",
    "/etc/systemd/system/rtc-sync.timer",
    "/etc/systemd/system/ntp0-backup.service",
    "/etc/systemd/system/ntp0-backup.timer",
    "/etc/logrotate.d/chrony",
    "/etc/systemd/journald.conf.d/ntp0.conf",
    "/etc/ssh/sshd_config",
    # SSH-Hostschluessel: Ohne sie erzeugt ein neu aufgesetztes System
    # eigene, und jeder Client meldet beim naechsten Verbinden "REMOTE HOST
    # IDENTIFICATION HAS CHANGED" - bei einem echten Ausfall nicht von einem
    # Angriff zu unterscheiden. Die privaten Schluessel sind sensibel; das
    # Archiv wird deshalb ohnehin mit Rechten 600 angelegt.
    "/etc/ssh/ssh_host_ecdsa_key",
    "/etc/ssh/ssh_host_ecdsa_key.pub",
    "/etc/ssh/ssh_host_ed25519_key",
    "/etc/ssh/ssh_host_ed25519_key.pub",
    "/etc/ssh/ssh_host_rsa_key",
    "/etc/ssh/ssh_host_rsa_key.pub",
    f"/home/{SSH_USER}/.ssh/authorized_keys",
    "/boot/firmware/config.txt",
    "/etc/NetworkManager/system-connections",
    "/var/lib/dcf77-autotune/state.json",
    "/var/lib/ntp0-backup/status.json",
    "/etc/hostname",
    "/etc/hosts",
]


def status_schreiben(erfolg, archiv=None, pfade=None, groesse_kb=None,
                     fehler=None, generationen=None):
    """Haelt das Ergebnis des Laufs fest.

    Der Zeitstempel des letzten ERFOLGS bleibt bei einem Fehlschlag
    unveraendert stehen. Dadurch altert er, und Home Assistant kann
    allein daran erkennen, dass seit Tagen kein Backup mehr durchkam -
    unabhaengig davon, ob das Skript gescheitert ist oder gar nicht lief.
    """
    jetzt = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    bisher = {}
    try:
        with open(STATUS_FILE) as f:
            bisher = json.load(f)
    except (OSError, ValueError):
        pass

    status = {
        "letzter_versuch": jetzt,
        "letzter_versuch_erfolgreich": bool(erfolg),
        "letzter_erfolg": jetzt if erfolg else bisher.get("letzter_erfolg"),
        "fehler": None if erfolg else (fehler or "unbekannt"),
        "archiv": archiv if erfolg else bisher.get("archiv"),
        "pfade": pfade if erfolg else bisher.get("pfade"),
        "groesse_kb": groesse_kb if erfolg else bisher.get("groesse_kb"),
        "generationen_auf_nas": generationen if erfolg
                                else bisher.get("generationen_auf_nas"),
    }

    try:
        os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
        with open(STATUS_FILE, "w") as f:
            json.dump(status, f, indent=2)
    except OSError as e:
        log(f"WARNUNG: Statusdatei nicht schreibbar: {e}")


def log(msg):
    print(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}")


def load_backup_config():
    parser = configparser.ConfigParser()
    if not parser.read(CONFIG_FILE):
        log(f"FEHLER: {CONFIG_FILE} nicht lesbar.")
        sys.exit(1)
    if "backup" not in parser:
        log(f"FEHLER: Abschnitt [backup] fehlt in {CONFIG_FILE}.")
        sys.exit(1)

    s = parser["backup"]
    cfg = {
        "server": s.get("server", "").strip(),
        "share": s.get("share", "").strip(),
        "subdir": s.get("subdir", "").strip(),
        "user": s.get("user", "").strip(),
        "password": s.get("password", "").strip(),
        "workgroup": s.get("workgroup", "").strip(),
        "keep": s.getint("keep", fallback=8),
        "gpg_recipient": s.get("gpg_recipient", "").strip(),
    }
    for key in ("server", "share", "user", "password"):
        if not cfg[key]:
            log(f"FEHLER: '{key}' ist in {CONFIG_FILE} unter [backup] nicht gesetzt.")
            sys.exit(1)
    return cfg


def collect_package_list(target_dir):
    """Paketliste mitsichern - erleichtert den Wiederaufbau von Null."""
    try:
        out = subprocess.run(["dpkg", "--get-selections"],
                             capture_output=True, text=True, timeout=60)
        with open(os.path.join(target_dir, "installierte-pakete.txt"), "w") as f:
            f.write(out.stdout)
        return True
    except Exception as e:
        log(f"WARNUNG: Paketliste nicht erstellbar: {e}")
        return False


def read_first_line(path):
    try:
        with open(path) as f:
            return f.read().strip().replace("\x00", "")
    except Exception:
        return None


def write_manifest(target_dir, included, missing):
    """Uebersicht ins Archiv legen - wer es Monate spaeter oeffnet, sieht
    sofort, was drin ist, was gefehlt hat und auf welches Betriebssystem
    es gehoert.

    Die Angaben zu OS und Hardware sind nicht schmueckendes Beiwerk: Ohne
    sie laesst sich bei einer Wiederherstellung nicht entscheiden, welches
    Raspberry-Pi-OS-Image zu waehlen ist. Ein 64-Bit-Image auf einem
    Pi 1 Model B+ waere zum Beispiel schlicht nicht lauffaehig - das
    Geraet hat einen ARMv6-Kern und braucht ein 32-Bit-Image.
    """
    path = os.path.join(target_dir, "MANIFEST.txt")
    with open(path, "w") as f:
        f.write("Backup des DCF77-NTP-Servers ntp-0\n")
        f.write(f"Erstellt: {datetime.datetime.now():%Y-%m-%d %H:%M:%S %Z}\n")
        f.write("\n")

        f.write("--- System, auf das dieses Backup gehoert ---\n")
        try:
            u = os.uname()
            f.write(f"Hostname:      {u.nodename}\n")
            f.write(f"Kernel:        {u.release}\n")
            f.write(f"Architektur:   {u.machine}\n")
        except Exception:
            pass

        # Betriebssystem-Version: entscheidend fuer die Imagewahl
        try:
            osrel = {}
            with open("/etc/os-release") as osf:
                for line in osf:
                    if "=" in line:
                        k, _, v = line.partition("=")
                        osrel[k.strip()] = v.strip().strip('"')
            f.write(f"Betriebssystem: {osrel.get('PRETTY_NAME', '?')}\n")
            if osrel.get("VERSION_CODENAME"):
                f.write(f"Codename:      {osrel['VERSION_CODENAME']}\n")
        except Exception:
            f.write("Betriebssystem: (nicht ermittelbar)\n")

        # Hardware-Modell des Pi
        model = read_first_line("/proc/device-tree/model")
        if model:
            f.write(f"Hardware:      {model}\n")

        f.write("\n--- Enthaltene Pfade ---\n")
        for p in included:
            f.write(f"  {p}\n")
        if missing:
            f.write("\n--- NICHT gefunden (pruefen, ob verschoben oder entfallen) ---\n")
            for p in missing:
                f.write(f"  {p}\n")

        f.write("\n--- Wiederherstellung ---\n")
        f.write("Am einfachsten mit dem beiliegenden Skript:\n")
        f.write("  1. Neues Raspberry Pi OS passend zu den Angaben oben\n")
        f.write("     auf die Karte schreiben und starten\n")
        f.write("  2. Archiv auf das neue System kopieren\n")
        f.write("  3. Fehlende Pakete gezielt installieren (Befehl und\n")
        f.write("     Paketliste stehen im Kopf von opt/ntp0_restore.py)\n")
        f.write("  4. sudo python3 ntp0_restore.py --archive <datei.tar.gz>\n")
        f.write("     (zeigt zunaechst nur an, was passieren wuerde;\n")
        f.write("      mit --apply wird tatsaechlich geschrieben)\n")


def build_archive(cfg):
    """Sammelt alle Dateien und packt sie in ein tar.gz."""
    os.makedirs(WORK_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")
    staging = tempfile.mkdtemp(prefix="staging-", dir=WORK_DIR)
    included, missing = [], []

    try:
        for src in BACKUP_PATHS:
            if not os.path.exists(src):
                missing.append(src)
                continue
            # Verzeichnisstruktur im Archiv beibehalten, damit beim
            # Wiederherstellen klar ist, wohin die Datei gehoert.
            dst = os.path.join(staging, src.lstrip("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
            included.append(src)

        collect_package_list(staging)
        write_manifest(staging, included, missing)

        archive = os.path.join(WORK_DIR, f"ntp-0_config_{stamp}.tar.gz")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname=f"ntp-0_config_{stamp}")
        os.chmod(archive, 0o600)

        global _letzte_pfadzahl
        _letzte_pfadzahl = len(included)
        log(f"Archiv erstellt: {archive} "
            f"({os.path.getsize(archive)/1024:.0f} KB, "
            f"{len(included)} Pfade)")
        if missing:
            log(f"Nicht gefunden ({len(missing)}): {', '.join(missing)}")

        return archive
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def encrypt_archive(archive, recipient):
    """Optionale GPG-Verschluesselung vor dem Hochladen."""
    encrypted = archive + ".gpg"
    try:
        subprocess.run(
            ["gpg", "--batch", "--yes", "--trust-model", "always",
             "--recipient", recipient, "--output", encrypted,
             "--encrypt", archive],
            check=True, capture_output=True, timeout=300)
    except Exception as e:
        log(f"FEHLER: Verschluesselung fehlgeschlagen: {e}")
        return None
    os.chmod(encrypted, 0o600)
    os.remove(archive)
    log(f"Archiv verschluesselt fuer {recipient}")
    return encrypted


def upload(archive, cfg):
    """Haengt die NAS-Freigabe kurzzeitig ein und kopiert das Archiv."""
    mountpoint = tempfile.mkdtemp(prefix="nasmount-", dir=WORK_DIR)

    # Zugangsdaten ueber eine temporaere Datei uebergeben, nicht ueber die
    # Kommandozeile: Kommandozeilen sind fuer jeden Nutzer via /proc
    # sichtbar, das Passwort waere sonst systemweit lesbar.
    credfile = tempfile.NamedTemporaryFile(mode="w", delete=False,
                                           dir=WORK_DIR, prefix="cred-")
    try:
        credfile.write(f"username={cfg['user']}\npassword={cfg['password']}\n")
        if cfg["workgroup"]:
            credfile.write(f"domain={cfg['workgroup']}\n")
        credfile.close()
        os.chmod(credfile.name, 0o600)

        unc = f"//{cfg['server']}/{cfg['share']}"
        log(f"Hänge {unc} ein...")
        try:
            subprocess.run(
                ["mount", "-t", "cifs", unc, mountpoint,
                 "-o", f"credentials={credfile.name},vers=3.0,"
                       f"uid=0,gid=0,file_mode=0600,dir_mode=0700"],
                check=True, capture_output=True, text=True, timeout=60)
        except subprocess.CalledProcessError as e:
            log(f"FEHLER beim Einhängen: {e.stderr.strip()}")
            log("Prüfen: Ist der Freigabename korrekt? Ist cifs-utils "
                "installiert? Ist SMB auf der FritzBox aktiviert?")
            return False
        except FileNotFoundError:
            log("FEHLER: 'mount' bzw. cifs-Unterstützung fehlt. "
                "Installieren mit: sudo apt install cifs-utils")
            return False

        try:
            target_dir = os.path.join(mountpoint, cfg["subdir"]) if cfg["subdir"] else mountpoint
            os.makedirs(target_dir, exist_ok=True)

            dest = os.path.join(target_dir, os.path.basename(archive))
            shutil.copy2(archive, dest)
            log(f"Hochgeladen: {dest}")

            rotate(target_dir, cfg["keep"])
            return True
        finally:
            subprocess.run(["umount", mountpoint],
                           capture_output=True, timeout=60)
    finally:
        try:
            os.remove(credfile.name)
        except OSError:
            pass
        shutil.rmtree(mountpoint, ignore_errors=True)


def rotate(target_dir, keep):
    """Alte Generationen entfernen, damit die Freigabe nicht volllaeuft."""
    try:
        archives = sorted(
            f for f in os.listdir(target_dir)
            if f.startswith("ntp-0_config_") and ".tar.gz" in f
        )
    except OSError as e:
        log(f"WARNUNG: Rotation nicht möglich: {e}")
        return

    surplus = archives[:-keep] if len(archives) > keep else []
    for f in surplus:
        try:
            os.remove(os.path.join(target_dir, f))
            log(f"Alte Generation entfernt: {f}")
        except OSError as e:
            log(f"WARNUNG: {f} nicht löschbar: {e}")
    global _letzte_generationen
    _letzte_generationen = min(len(archives), keep)
    log(f"{_letzte_generationen} Generationen auf dem NAS "
        f"(Vorgabe: {keep})")


def cleanup_local(keep_local=2):
    """Lokale Kopien begrenzen - die SD-Karte soll nicht volllaufen."""
    try:
        archives = sorted(
            f for f in os.listdir(WORK_DIR)
            if f.startswith("ntp-0_config_") and ".tar.gz" in f
        )
    except OSError:
        return
    for f in archives[:-keep_local] if len(archives) > keep_local else []:
        try:
            os.remove(os.path.join(WORK_DIR, f))
        except OSError:
            pass


def main():
    local_only = "--local-only" in sys.argv

    if os.geteuid() != 0:
        log("FEHLER: Muss als root laufen (liest /etc/ntp-0.conf und "
            "SSH-Schlüssel, hängt die Freigabe ein).")
        sys.exit(1)

    # build_archive meldet die Kennzahlen zurueck, damit sie in die
    # Statusdatei koennen. Frueher standen sie nur im Log.
    try:
        cfg = load_backup_config()
    except SystemExit:
        status_schreiben(False, fehler="Konfiguration nicht lesbar")
        raise

    try:
        archive = build_archive(cfg)
    except Exception as e:
        status_schreiben(False, fehler=f"Archiv nicht erstellbar: {e}")
        log(f"FEHLER: Archiv nicht erstellbar: {e}")
        sys.exit(1)

    pfade = _letzte_pfadzahl
    groesse_kb = round(os.path.getsize(archive) / 1024) if os.path.exists(archive) else None

    if cfg["gpg_recipient"]:
        archive = encrypt_archive(archive, cfg["gpg_recipient"])
        if archive is None:
            status_schreiben(False, fehler="Verschlüsselung fehlgeschlagen")
            sys.exit(1)

    if local_only:
        log(f"--local-only: kein Upload. Archiv liegt unter {archive}")
        # Ein Probelauf ohne Upload gilt NICHT als erfolgreiches Backup -
        # die Sicherung liegt ja nur lokal auf derselben SD-Karte, gegen
        # deren Ausfall sie schuetzen soll.
        sys.exit(0)

    ok = upload(archive, cfg)
    cleanup_local()

    if not ok:
        status_schreiben(False, fehler="Upload auf das NAS fehlgeschlagen")
        log("Backup NICHT auf dem NAS abgelegt - lokale Kopie bleibt "
            f"unter {archive} erhalten.")
        sys.exit(1)

    status_schreiben(True, archiv=os.path.basename(archive), pfade=pfade,
                     groesse_kb=groesse_kb,
                     generationen=_letzte_generationen)
    log("Backup abgeschlossen.")


if __name__ == "__main__":
    main()
