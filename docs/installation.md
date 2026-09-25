# Installation

Voraussetzung ist ein frisch aufgesetztes Raspberry Pi OS Lite (Bookworm,
32 Bit für ARMv6-Modelle), erreichbar per SSH, sowie die
[Verkabelung](hardware.md).

## 1. Pakete

```bash
sudo apt update
sudo apt install chrony fail2ban python3-systemd i2c-tools \
                 python3-paho-mqtt python3-rpi.gpio smbclient cifs-utils vlan
```

`apt` entfernt dabei `systemd-timesyncd` – richtig so, zwei Zeitdienste
vertragen sich nicht.

Falls `fake-hwclock` installiert ist, muss es weg. Es überschreibt beim
Start die Zeit der DS3231 mit einem gespeicherten Wert:

```bash
sudo apt purge fake-hwclock
```

## 2. I2C und Hardware-Uhr

```bash
sudo raspi-config nonint do_i2c 0
```

In `/boot/firmware/config.txt` ergänzen:

```
dtparam=i2c_arm=on
dtoverlay=i2c-rtc,ds3231
```

Nach einem Neustart prüfen:

```bash
sudo hwclock -r
```

Meldet der Befehl ein Datum weit in der Vergangenheit, ist die Uhr noch
nie gestellt worden – das erledigt der Kernel selbsttätig, sobald die
Systemzeit steht (`rtcsync` in der chrony-Konfiguration, alle elf
Minuten).

**Ein eigener Dienst für den Abgleich ist nicht nötig.** Die beiliegenden
`rtc-sync.*`-Units sind ein Überbleibsel und bleiben bewusst deaktiviert;
sie täten nur, was der Kernel ohnehin macht.

## 3. Programme und Units

```bash
sudo cp opt/*.py /opt/
sudo cp systemd/* /etc/systemd/system/
sudo cp etc/chrony.conf /etc/chrony/chrony.conf
sudo cp etc/nftables.conf /etc/nftables.conf
sudo cp etc/jail.local /etc/fail2ban/jail.local
sudo cp etc/logrotate-chrony /etc/logrotate.d/chrony
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp etc/journald-ntp0.conf /etc/systemd/journald.conf.d/ntp0.conf
```

In `chrony.conf` und `nftables.conf` die **eigenen Netze** eintragen –
die enthaltenen Adressen sind Platzhalter.

Der Benutzer, dessen SSH-Schlüssel gesichert wird, steht als `SSH_USER`
oben in `ntp0_backup.py` und `ntp0_restore.py`.

## 4. Zentrale Konfiguration

```bash
sudo cp etc/ntp-0.conf.example /etc/ntp-0.conf
sudo chmod 600 /etc/ntp-0.conf
sudo nano /etc/ntp-0.conf
```

Dort stehen MQTT-Zugang, die Empfängerverzögerung und das Backup-Ziel.
Die Datei enthält Passwörter im Klartext – daher `chmod 600`.

Den Freigabenamen des Backup-Ziels nicht raten, sondern abfragen:

```bash
smbclient -L //192.0.2.50 -U backup-user
```

## 5. Dienste starten

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dcf77-decoder.service dcf77_ha_status.service
sudo systemctl enable --now dcf77-autotune.timer ntp0-backup.timer
sudo systemctl restart chrony fail2ban systemd-journald
sudo nft -f /etc/nftables.conf
sudo systemctl enable --now nftables
```

**Nach dem Laden der Firewall sofort eine zweite SSH-Verbindung testen**,
solange die erste noch offen ist.

## 6. Erste Kalibrierung

Die Empfängerverzögerung ist geräteabhängig. Der Startwert in der
Vorlage passt zu einem bestimmten Modul und ist bei dir mit hoher
Wahrscheinlichkeit falsch.

Vorgehen: einige Stunden laufen lassen, dann

```bash
sudo python3 /opt/ntp0_wochenbericht.py --days 1
```

Abschnitt 1 zeigt die Abweichung gegenüber den Internet-Zeitservern.
Liegt sie über 100 ms, trägt man die Differenz einmalig von Hand in
`/etc/ntp-0.conf` ein:

```
receiver_delay = <alter Wert> + <Abweichung in Sekunden>
```

Der Dekoder liest den Wert bei jeder Minutenlücke neu ein, ein Neustart
ist nicht nötig. Ab da übernimmt der Regler; er arbeitet mit höchstens
10 ms pro Schritt und braucht für große Sprünge zu lange.

## 7. Kontrolle

```bash
systemctl --failed
chronyc tracking
chronyc sources -v
journalctl -u dcf77-decoder.service -f
```

Nach spätestens zwanzig Minuten sollte `chronyc tracking` **Stratum 1**
und eine Referenz namens `DCF` zeigen.

In `chronyc sources -v` steht das Reach-Register in **oktaler**
Schreibweise: `377` sind acht von acht erfolgreichen Abfragen, `201`
dagegen nur zwei. Die MQTT-Sensoren rechnen das in „N/8 Abfragen" um.

## 8. Optional: zweites Netz per VLAN

```bash
sudo nmcli connection add type vlan con-name vlan2 dev eth0 id 2 \
     ip4 198.51.100.8/24
```
