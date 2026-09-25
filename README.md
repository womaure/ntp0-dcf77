# DCF77-Stratum-1-NTP-Server auf einem Raspberry Pi

Ein lokaler Zeitserver, der seine Zeit nicht aus dem Internet bezieht,
sondern aus dem Funksignal DCF77. Dekoder, Selbstkalibrierung, Backup und
Monitoring sind vollständig enthalten.

Entstanden für einen **Raspberry Pi 1 Model B+** (ein ARMv6-Kern,
700–800 MHz) unter Raspberry Pi OS Bookworm, 32 Bit. Auf neuerer Hardware
läuft alles unverändert – der alte Pi war die anspruchsvollere Umgebung.

## Was das System leistet

- **Stratum 1** über DCF77, angebunden an chrony per SHM-Refclock
- **Selbstkalibrierung**: Die Verarbeitungsverzögerung des Empfängers
  wandert über die Zeit. Ein Regler misst die Abweichung gegen mehrere
  Internet-Zeitserver und justiert stündlich nach.
- **Verfügbarkeit**: Fallen DCF77 und Internet gleichzeitig aus, liefert
  der Server seine eigene, zuletzt disziplinierte Uhr weiter aus
  (`local stratum 10`), statt zu verstummen.
- **Hardware-Uhr** (DS3231) für eine brauchbare Zeit unmittelbar nach
  dem Start
- **Monitoring** über MQTT in Home Assistant: zehn Sensoren von der
  Empfangsqualität bis zum Zustand der SD-Karte
- **Backup** der gesamten Konfiguration auf eine SMB-Freigabe, mit
  getestetem Wiederherstellungsweg

## Aufbau

```
DCF77-Empfänger ──GPIO──► dcf77_ntp_shm.py ──SHM──► chronyd ──NTP──► Netz
                                                       ▲
                            dcf77_autotune.py ─────────┘
                            (justiert die Empfängerverzögerung)

DS3231 ──I2C──► Kernel (stellt die Systemuhr beim Start)
```

## Verzeichnisse

| Pfad | Inhalt |
|---|---|
| `opt/` | Die Python-Programme, gehören nach `/opt` |
| `systemd/` | Unit-Dateien, gehören nach `/etc/systemd/system` |
| `etc/` | Konfigurationsvorlagen für chrony, nftables, fail2ban, logrotate |
| `tools/` | Systemanalyse (einmalig bzw. bei Bedarf) |
| `docs/` | Ausführliche Dokumentation |

## Die Programme

| Datei | Aufgabe |
|---|---|
| `dcf77_ntp_shm.py` | Dekodiert das Funksignal und übergibt jede Minute an chrony |
| `dcf77_autotune.py` | Justiert die Empfängerverzögerung nach (stündlich) |
| `dcf77_ha_status.py` | Veröffentlicht zehn Sensoren per MQTT |
| `dcf77_drift_report.py` | Wertet die chrony-Refclock-Logs aus |
| `ntp0_wochenbericht.py` | Fasst alle regelmäßigen Prüfungen zusammen |
| `ntp0_backup.py` | Sichert die Konfiguration auf eine SMB-Freigabe |
| `ntp0_restore.py` | Spielt ein Backup kontrolliert zurück |

## Einstieg

1. [Hardware und Verkabelung](docs/hardware.md)
2. [Installation](docs/installation.md)
3. [Betrieb und Überwachung](docs/betrieb.md)
4. [Home-Assistant-Anbindung](docs/home-assistant.md)
5. [Warum es so gebaut ist](docs/entscheidungen.md) – die
   Begründungen hinter den Entscheidungen, einschließlich der Irrwege

## Voraussetzungen

Raspberry Pi OS Bookworm (Debian 12) und damit Python 3.11. Die Programme
nutzen bewusst keine Sprachmerkmale neuerer Versionen.

```
chrony fail2ban nftables python3-systemd i2c-tools
python3-paho-mqtt python3-rpi.gpio smbclient cifs-utils vlan
```

## Beispieladressen

Sämtliche Adressen in den Konfigurationsvorlagen sind Platzhalter aus den
für Dokumentation reservierten Bereichen (192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24). Sie müssen vor dem Einsatz durch die eigenen ersetzt
werden.

## Genauigkeit – was realistisch ist

Erreicht werden einige Millisekunden Abweichung zu UTC. Das genügt für
ein Heimnetz oder ein kleines Firmennetz bei weitem.

DCF77 kann prinzipbedingt nicht mehr: Die Laufzeit des Funksignals und
die Verarbeitung im Empfänger lassen sich nicht exakt bestimmen, und die
Verzögerung wandert über die Zeit – genau deshalb gibt es die
Selbstkalibrierung. Wer Mikrosekunden braucht, nimmt GPS mit
PPS-Signal.

Dieser Server ist für den **internen** Gebrauch gedacht. Für einen
Beitrag zum öffentlichen NTP-Pool ist DCF77 als Quelle zu ungenau.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
