#!/usr/bin/env python3
"""
ntp-0 Konfiguration wiederherstellen
=====================================

Gegenstück zu ntp0_backup.py: Spielt ein Backup-Archiv auf ein frisch
aufgesetztes System zurück.

WICHTIG - was dieses Skript NICHT tut
--------------------------------------
Es stellt kein Betriebssystem wieder her. Das Archiv enthält nur
Konfiguration. Reihenfolge einer Wiederherstellung:

  1. Raspberry Pi OS auf die neue Karte schreiben. Welche Version und
     welche Architektur, steht im MANIFEST.txt des Archivs.
  2. Fehlende Pakete ermitteln und gezielt installieren:
         comm -13 <(dpkg --get-selections | awk '$2=="install"{print $1}' | sort) \
                  <(awk '$2=="install"{print $1}' installierte-pakete.txt | sort)
     Die Liste enthaelt auch alte Kernel und Firmware fuer PC-Hardware -
     nur Benoetigtes installieren. Beim Test vom 22.09.2026 war das:
         chrony fail2ban python3-systemd i2c-tools python3-paho-mqtt
         python3-rpi.gpio smbclient vlan
     (chrony, nftables, fail2ban und die Python-Module müssen vorhanden
     sein, bevor ihre Konfiguration zurückkommt)
  3. Dieses Skript ausführen.

SICHERHEITSKONZEPT
------------------
Ein Skript, das ungefragt in /etc schreibt, kann ein System unbrauchbar
machen. Deshalb:

- Standardmäßig wird NUR ANGEZEIGT, was passieren würde. Erst mit
  --apply wird tatsächlich geschrieben.
- Jede Datei, die überschrieben wird, landet vorher als .vor-restore
  daneben.
- Die Dateien sind in drei Klassen eingeteilt (siehe unten). Heikle
  Dateien werden nur mit --include-heikel angefasst.

WARUM DREI KLASSEN
------------------
Nicht jede Datei darf unbesehen von einem alten auf ein neues System:

  EIGEN    Vom Projekt selbst erzeugte Dateien. Gehören genau so zurück.
  HEIKEL   Dateien, die zum Betriebssystem gehören und sich zwischen
           Versionen unterscheiden können. /boot/firmware/config.txt
           eines alten Images auf ein neues zu kopieren kann den Start
           verhindern; eine sshd_config aus einer älteren OpenSSH-Version
           kann den Zugang kosten. Hier wird stattdessen angezeigt, was
           inhaltlich nachzutragen ist.
  RECHTE   Dateien, bei denen Eigentümer und Zugriffsrechte über die
           Funktion entscheiden. NetworkManager ignoriert Verbindungen,
           deren Datei für andere lesbar ist; SSH verweigert den
           Schlüssel bei zu offenen Rechten.

Aufruf:
    sudo python3 ntp0_restore.py --archive ntp-0_config_....tar.gz
    sudo python3 ntp0_restore.py --archive ... --apply
    sudo python3 ntp0_restore.py --archive ... --apply --include-heikel
"""

import argparse
import datetime
import grp
import os
import pwd
import shutil
import subprocess
import sys
import tarfile
import tempfile

# Benutzer, dessen SSH-Schluessel wiederhergestellt wird.
SSH_USER = "pi"

# --- Klasse EIGEN: gehören unverändert zurück ---
EIGEN = [
    "/opt/dcf77_ntp_shm.py",
    "/opt/dcf77_ha_status.py",
    "/opt/dcf77_autotune.py",
    "/opt/dcf77_drift_report.py",
    "/opt/ntp0_wochenbericht.py",
    "/opt/ntp0_backup.py",
    "/opt/ntp0_restore.py",
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
    "/var/lib/dcf77-autotune/state.json",
    "/var/lib/ntp0-backup/status.json",
]

# --- Klasse RECHTE: Eigentümer/Modus sind funktionsentscheidend ---
# Pfad -> (Modus, Eigentümer, Gruppe)  None = nicht ändern
RECHTE = {
    "/etc/ntp-0.conf": (0o600, "root", "root"),
    f"/home/{SSH_USER}/.ssh/authorized_keys": (0o600, SSH_USER, SSH_USER),
    # Hostschluessel: sshd verweigert private Schluessel, die fuer andere
    # lesbar sind. Die oeffentlichen Teile duerfen lesbar sein.
    "/etc/ssh/ssh_host_ecdsa_key": (0o600, "root", "root"),
    "/etc/ssh/ssh_host_ecdsa_key.pub": (0o644, "root", "root"),
    "/etc/ssh/ssh_host_ed25519_key": (0o600, "root", "root"),
    "/etc/ssh/ssh_host_ed25519_key.pub": (0o644, "root", "root"),
    "/etc/ssh/ssh_host_rsa_key": (0o600, "root", "root"),
    "/etc/ssh/ssh_host_rsa_key.pub": (0o644, "root", "root"),
}
# Verzeichnisse, deren Inhalt einheitliche Rechte braucht
RECHTE_DIRS = {
    "/etc/NetworkManager/system-connections": (0o600, "root", "root", 0o700),
}

# --- Klasse HEIKEL: nur auf ausdrücklichen Wunsch ---
HEIKEL = {
    "/boot/firmware/config.txt":
        "Enthält Boot- und Hardwareoptionen. Ein neues OS-Image bringt eine "
        "eigene Fassung mit, die zu dessen Kernel passt - deshalb nicht "
        "ersetzen, sondern die fehlenden Zeilen ergänzen. Welche das sind, "
        "zeigt das Skript unten automatisch an.",
    "/etc/ssh/sshd_config":
        "Gehört zur installierten OpenSSH-Version. Eine ältere Fassung "
        "einzuspielen kann den Zugang kosten. Statt zu ersetzen: "
        "'PasswordAuthentication no' und 'KbdInteractiveAuthentication no' "
        "in der vorhandenen Datei setzen - ERST nachdem der Schlüssel-Login "
        "nachweislich funktioniert.",
    "/etc/hostname":
        "Wird beim Aufsetzen des Images bereits gesetzt. Nur einspielen, "
        "wenn der Hostname dort nicht schon 'ntp-0' lautet.",
    "/etc/hosts":
        "Enthält den Hostnamen-Eintrag des alten Systems. Meist unnötig, "
        "da das Image eine passende Fassung anlegt.",
}

# Was nach dem Zurückspielen zu tun ist
# Reihenfolge und Inhalt stammen aus dem Wiederherstellungstest vom
# 22.09.2026. Drei Eintraege der ersten Fassung waren fehlerhaft:
#   - chrony und fail2ban laufen nach der Paketinstallation bereits;
#     "enable --now" haette die wiederhergestellte Konfiguration nicht
#     eingelesen. Es braucht "restart".
#   - Bei "nft -f ... && systemctl ..." fehlte vor dem zweiten Befehl sudo.
#   - "nmcli connection add" haette die bereits wiederhergestellte
#     VLAN-Verbindung doppelt angelegt.
# Jeder Eintrag ist jetzt ein einzelner Befehl, damit das vorangestellte
# sudo immer fuer den ganzen Befehl gilt.
NACHARBEIT = [
    ("systemctl restart ssh",
     "SSH mit den wiederhergestellten Hostschluesseln neu starten. Die "
     "laufende Sitzung bleibt bestehen - trotzdem offen lassen, bis eine "
     "neue Anmeldung aus einem zweiten Terminal ohne Warnung klappt"),
    ("systemctl daemon-reload",
     "systemd die wiederhergestellten Units bekannt machen"),
    ("systemctl enable --now dcf77-decoder.service dcf77_ha_status.service",
     "Die beiden Dauerdienste starten"),
    ("systemctl enable --now dcf77-autotune.timer ntp0-backup.timer",
     "Die beiden Timer aktivieren (rtc-sync.timer bleibt bewusst aus)"),
    ("systemctl enable chrony fail2ban",
     "chrony und fail2ban dauerhaft aktivieren"),
    ("systemctl restart chrony fail2ban",
     "Beide laufen seit der Paketinstallation mit Standardkonfiguration - "
     "erst der Neustart liest die wiederhergestellte ein"),
    ("nft -f /etc/nftables.conf",
     "Firewall laden. Danach sofort eine neue SSH-Anmeldung testen"),
    ("systemctl enable --now nftables",
     "Firewall dauerhaft aktivieren"),
    ("nmcli connection reload",
     "NetworkManager die wiederhergestellte VLAN-Verbindung einlesen "
     "lassen. Kontrolle mit 'nmcli connection show' - vlan2 muss an "
     "eth0.2 gebunden sein"),
    ("systemctl restart systemd-journald",
     "Journal-Begrenzung (journald.conf.d/ntp0.conf) wirksam machen"),
    ("systemctl disable --now wpa_supplicant ModemManager "
     "triggerhappy.service triggerhappy.socket avahi-daemon.service "
     "avahi-daemon.socket",
     "Ueberfluessige Dienste abschalten (Systemlast-Analyse Sept. 2026). "
     "Meldungen zu Diensten, die das Image nicht enthaelt, sind harmlos"),
    ("apt purge -y fake-hwclock",
     "Konkurriert beim Start mit der DS3231 und ueberschreibt sonst "
     "die RTC-Zeit mit einem gespeicherten Stand"),
    ("raspi-config nonint do_i2c 0",
     "I2C einschalten (fuer die DS3231)"),
    ("reboot",
     "Erst NACHDEM die config.txt wie oben angezeigt ergaenzt ist - die "
     "Zeilen fuer RTC und Takt greifen nur beim Start"),
]


def log(msg):
    print(msg)


def entpacken(archiv):
    tmp = tempfile.mkdtemp(prefix="ntp0-restore-")
    try:
        with tarfile.open(archiv, "r:gz") as tar:
            tar.extractall(tmp)
    except Exception as e:
        log(f"FEHLER: Archiv nicht lesbar: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)

    # Im Archiv liegt ein einzelnes Wurzelverzeichnis
    eintraege = [d for d in os.listdir(tmp)
                 if os.path.isdir(os.path.join(tmp, d))]
    if len(eintraege) != 1:
        log(f"FEHLER: Unerwarteter Archivaufbau ({eintraege})")
        shutil.rmtree(tmp, ignore_errors=True)
        sys.exit(1)
    return tmp, os.path.join(tmp, eintraege[0])


def manifest_zeigen(wurzel):
    pfad = os.path.join(wurzel, "MANIFEST.txt")
    if not os.path.exists(pfad):
        log("Hinweis: Das Archiv enthält kein MANIFEST.txt "
            "(vermutlich eine ältere Fassung).")
        return
    log("=" * 70)
    with open(pfad) as f:
        for line in f:
            if line.startswith("--- Enthaltene Pfade"):
                break
            print(line.rstrip())
    log("=" * 70)
    log("")


def rechte_setzen(ziel, modus, user, gruppe):
    try:
        if modus is not None:
            os.chmod(ziel, modus)
        if user and gruppe:
            os.chown(ziel,
                     pwd.getpwnam(user).pw_uid,
                     grp.getgrnam(gruppe).gr_gid)
        return True
    except (OSError, KeyError) as e:
        log(f"    WARNUNG: Rechte für {ziel} nicht setzbar: {e}")
        return False


def datei_zurueck(wurzel, pfad, apply):
    quelle = os.path.join(wurzel, pfad.lstrip("/"))
    if not os.path.exists(quelle):
        return "fehlt"

    vorhanden = os.path.exists(pfad)
    if not apply:
        return "ersetzt" if vorhanden else "neu"

    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    if vorhanden:
        shutil.copy2(pfad, pfad + ".vor-restore")
    shutil.copy2(quelle, pfad)
    return "ersetzt" if vorhanden else "neu"


def verzeichnis_zurueck(wurzel, pfad, apply, dateimodus, user, gruppe,
                        dirmodus):
    quelle = os.path.join(wurzel, pfad.lstrip("/"))
    if not os.path.isdir(quelle):
        return "fehlt", []

    dateien = sorted(os.listdir(quelle))
    if not apply:
        return "ersetzt" if os.path.isdir(pfad) else "neu", dateien

    os.makedirs(pfad, exist_ok=True)
    for name in dateien:
        q = os.path.join(quelle, name)
        z = os.path.join(pfad, name)
        if os.path.exists(z):
            shutil.copy2(z, z + ".vor-restore")
        shutil.copy2(q, z)
        rechte_setzen(z, dateimodus, user, gruppe)
    rechte_setzen(pfad, dirmodus, user, gruppe)
    return "ersetzt", dateien


def config_txt_differenz(wurzel):
    """Zeilen, die in der gesicherten config.txt stehen, in der aktuellen
    aber fehlen.

    Beim Test vom 22.09.2026 nannte der statische Hinweis nur zwei Zeilen,
    tatsaechlich fehlten sieben (Uebertaktung, UART, RTC). Deshalb wird
    der Unterschied jetzt berechnet statt behauptet. Auskommentierte
    Zeilen werden ignoriert; eine Zeile gilt als vorhanden, wenn sie
    aktiv in der aktuellen Datei steht.
    """
    # Archivpfad genauso bilden wie ueberall sonst im Skript: der
    # absolute Pfad ohne fuehrenden Schraegstrich unter der Archivwurzel.
    aktuell = "/boot/firmware/config.txt"
    gesichert = os.path.join(wurzel, aktuell.lstrip("/"))
    if not os.path.exists(gesichert) or not os.path.exists(aktuell):
        return None

    def aktive_zeilen(pfad):
        zeilen = []
        with open(pfad, errors="replace") as f:
            for z in f:
                z = z.strip()
                if z and not z.startswith("#"):
                    zeilen.append(z)
        return zeilen

    vorhanden = set(aktive_zeilen(aktuell))
    return [z for z in aktive_zeilen(gesichert) if z not in vorhanden]


def main():
    ap = argparse.ArgumentParser(
        description="Stellt eine gesicherte ntp-0-Konfiguration wieder her.")
    ap.add_argument("--archive", required=True,
                    help="Pfad zum Backup-Archiv (.tar.gz)")
    ap.add_argument("--apply", action="store_true",
                    help="Tatsächlich schreiben. Ohne diese Angabe wird nur "
                         "angezeigt, was passieren würde.")
    ap.add_argument("--include-heikel", action="store_true",
                    help="Auch Dateien einspielen, die zum Betriebssystem "
                         "gehören (config.txt, sshd_config, hosts, hostname). "
                         "Siehe Erläuterung im Skriptkopf.")
    args = ap.parse_args()

    if os.geteuid() != 0:
        log("FEHLER: Muss als root laufen.")
        sys.exit(1)

    if not os.path.exists(args.archive):
        log(f"FEHLER: {args.archive} nicht gefunden.")
        sys.exit(1)

    tmp, wurzel = entpacken(args.archive)

    try:
        manifest_zeigen(wurzel)

        if not args.apply:
            log("PROBELAUF - es wird nichts geschrieben.")
            log("Mit --apply wird die Wiederherstellung tatsächlich "
                "durchgeführt.")
            log("")

        log("--- Projektdateien ---")
        zaehler = {"neu": 0, "ersetzt": 0, "fehlt": 0}
        for pfad in EIGEN:
            status = datei_zurueck(wurzel, pfad, args.apply)
            zaehler[status] = zaehler.get(status, 0) + 1
            zeichen = {"neu": "+", "ersetzt": "~", "fehlt": "-"}[status]
            log(f"  {zeichen} {pfad}")

        log("")
        log("--- Dateien mit besonderen Rechten ---")
        for pfad, (modus, user, gruppe) in RECHTE.items():
            status = datei_zurueck(wurzel, pfad, args.apply)
            zaehler[status] = zaehler.get(status, 0) + 1
            zeichen = {"neu": "+", "ersetzt": "~", "fehlt": "-"}[status]
            log(f"  {zeichen} {pfad}  -> Modus {oct(modus)}, {user}:{gruppe}")
            if args.apply and status != "fehlt":
                rechte_setzen(pfad, modus, user, gruppe)
                # Elternverzeichnis von .ssh braucht 0700
                if pfad.endswith("/.ssh/authorized_keys"):
                    rechte_setzen(os.path.dirname(pfad), 0o700, user, gruppe)

        for pfad, (dmodus, user, gruppe, dirmodus) in RECHTE_DIRS.items():
            status, dateien = verzeichnis_zurueck(
                wurzel, pfad, args.apply, dmodus, user, gruppe, dirmodus)
            zeichen = {"neu": "+", "ersetzt": "~", "fehlt": "-"}[status]
            log(f"  {zeichen} {pfad}/  ({len(dateien)} Dateien, "
                f"Modus {oct(dmodus)}, {user}:{gruppe})")
            for d in dateien:
                log(f"      {d}")

        log("")
        log("--- Betriebssystemnahe Dateien ---")
        if args.include_heikel:
            log("  (--include-heikel gesetzt: werden eingespielt)")
            for pfad in HEIKEL:
                status = datei_zurueck(wurzel, pfad, args.apply)
                zeichen = {"neu": "+", "ersetzt": "~", "fehlt": "-"}[status]
                log(f"  {zeichen} {pfad}")
        else:
            log("  Werden NICHT eingespielt. Stattdessen von Hand nachtragen:")
            log("")
            for pfad, grund in HEIKEL.items():
                quelle = os.path.join(wurzel, pfad.lstrip("/"))
                vorhanden = "im Archiv" if os.path.exists(quelle) else "fehlt im Archiv"
                log(f"  {pfad}  ({vorhanden})")
                for zeile in grund.split(". "):
                    if zeile.strip():
                        log(f"      {zeile.strip().rstrip('.')}.")
                log("")

        fehlend = config_txt_differenz(wurzel)
        if fehlend:
            log("  In /boot/firmware/config.txt fehlen diese Zeilen:")
            for z in fehlend:
                log(f"      {z}")
            log("  Ergaenzen, z.B.:")
            log("      sudo tee -a /boot/firmware/config.txt << 'EOF'")
            for z in fehlend:
                if z.startswith("dtparam=i2c_arm"):
                    continue  # erledigt raspi-config in der Nacharbeit
                log(f"      {z}")
            log("      EOF")
        elif fehlend is not None:
            log("  /boot/firmware/config.txt: nichts zu ergaenzen")

        log("")
        log(f"Zusammenfassung: {zaehler.get('neu',0)} neu, "
            f"{zaehler.get('ersetzt',0)} ersetzt, "
            f"{zaehler.get('fehlt',0)} im Archiv nicht vorhanden")

        if args.apply:
            log("")
            log("Überschriebene Dateien liegen als *.vor-restore daneben.")
            log("")
            log("--- Noch auszuführen ---")
            for befehl, zweck in NACHARBEIT:
                log(f"  # {zweck}")
                log(f"  sudo {befehl}")
                log("")
            log("--- Danach prüfen ---")
            log("  systemctl --failed        # keine fehlgeschlagenen Dienste?")
            log("  sudo fail2ban-client status sshd   # Jail aktiv?")
            log("  ip addr show eth0.2       # VLAN-Adresse vorhanden?")
            log("  chronyc tracking          # Stratum und Zeitquelle")
            log("  chronyc sources -v        # steht DCF in der Liste?")
            log("  systemctl status dcf77-decoder.service")
            log("  sudo nft list ruleset     # Firewall aktiv?")
            log("  sudo hwclock -r           # wird die DS3231 erkannt?")
        else:
            log("")
            log("Nichts geschrieben. Zum Ausführen: --apply anhängen.")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
