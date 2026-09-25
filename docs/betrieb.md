# Betrieb und Überwachung

## Wochenübersicht

Das wichtigste Werkzeug im laufenden Betrieb:

```bash
sudo python3 /opt/ntp0_wochenbericht.py
sudo python3 /opt/ntp0_wochenbericht.py --days 1
```

Die fünf Abschnitte:

1. **Abweichung zu UTC** – DCF77 gegen den Median mehrerer
   Internet-Zeitserver
2. **Empfang** – übernommene und verlorene Minuten, Ursachen und die
   Stunden mit den meisten Verlusten
3. **Autotune** – Zahl und Größe der Nachjustierungen
4. **Driftbericht** – Streuung der Einzelmessungen und Ausreißer
5. **System** – Hardware-Uhr, Temperaturen, Drosselung, Backup, Journal

### Die wichtigste Fehlinterpretation

`chronyc tracking` meldet unter `System time` oft Werte im
Mikrosekundenbereich. Das ist **nicht** die Genauigkeit gegenüber UTC,
sondern die Abweichung zur gewählten Quelle – und die ist DCF77 selbst.
Die Zahl sagt also nur, wie gut die Systemuhr dem Funksignal folgt.

Die ehrliche Zahl steht in Abschnitt 1 des Wochenberichts: der Vergleich
mit unabhängigen Internet-Servern.

Aus demselben Grund ist im Driftbericht die Spalte *Median* ohne
Aussagekraft – sie liegt zwangsläufig nahe null. Aussagekräftig sind dort
nur **Streuung** und die Zahl der **Ausreißer**.

## Was die Werte bedeuten

| Wert | Erwartung |
|---|---|
| Abweichung zu UTC | einige Millisekunden |
| Streuung der Einzelmessungen | 5 bis 10 ms bei gutem Empfang |
| Empfangsquote | über 95 Prozent |
| Autotune-Korrekturen | wenige pro Woche, jeweils unter 10 ms |
| Ausreißer über 1 s | selten; jeder einzelne ist ein Dekodierfehler |

Häufen sich Verluste zu bestimmten Tageszeiten, deutet das auf ein Gerät
im Haushalt hin. Abschnitt 2 des Berichts zeigt die Verteilung über die
Stunden – das ist der direkteste Weg zur Störquelle.

## Systemanalyse

Einmalig nach der Installation und bei Verdacht auf Last- oder
Jitter-Probleme:

```bash
sudo bash tools/ntp0_systemcheck.sh | tee /tmp/vorher.txt
```

Worauf zu achten ist:

- **Drosselung**: `throttled 0x0` bedeutet, dass es nie Unterspannung
  oder Überhitzung gab. Alles andere ist ein Befund.
- **CPU-Governor**: `ondemand` lässt die Taktrate wechseln, was
  Ausführungszeiten und damit die Erfassung der Signalflanken
  beeinflusst. `performance` ist konstanter.
- **Laufende Dienste**: Auf einem reinen Zeitserver sollte die Liste kurz
  sein. `wpa_supplicant`, `ModemManager`, `triggerhappy` und
  `avahi-daemon` laufen oft ohne Zweck mit.
- **Interrupts**: Bei Pi 1 und 2 hängt der Netzwerkanschluss am USB-Bus.
  Die dadurch entstehenden Interrupts sind bauartbedingt und die größte
  verbleibende Jitter-Quelle.

## Backup

Läuft wöchentlich, lässt sich aber jederzeit auslösen:

```bash
sudo systemctl start ntp0-backup.service
sudo python3 /opt/ntp0_backup.py --local-only   # nur lokal, kein Upload
```

Gesichert werden alle Programme, Konfigurationen, systemd-Units, die
SSH-Hostschlüssel und die Paketliste. Das Archiv enthält Passwörter im
Klartext und liegt deshalb mit Rechten 600.

Ein Lauf mit `--local-only` gilt bewusst **nicht** als erfolgreiches
Backup: Die Sicherung läge dann auf derselben Karte, gegen deren Ausfall
sie schützen soll.

## Wiederherstellung

```bash
sudo python3 /opt/ntp0_restore.py --archive <datei.tar.gz>            # Probelauf
sudo python3 /opt/ntp0_restore.py --archive <datei.tar.gz> --apply
```

Ohne `--apply` wird nichts geschrieben. Überschriebene Dateien bleiben
als `*.vor-restore` daneben liegen.

Betriebssystemnahe Dateien – `config.txt`, `sshd_config`, `hostname`,
`hosts` – werden nicht automatisch ersetzt, weil sie zur jeweiligen
Version des Betriebssystems gehören. Für die `config.txt` vergleicht das
Skript und zeigt die fehlenden Zeilen samt fertigem Befehl an.

Am Ende gibt das Skript die nötige Nacharbeit aus, mit Begründung je
Schritt.

### Der Test lohnt sich

Bei der ersten Erprobung mit einer neuen Karte kamen zutage: fehlende
SSH-Hostschlüssel im Backup, eine fehlende Journal-Begrenzung,
`fake-hwclock`, das wieder installiert war, sieben statt zwei zu
ergänzende Zeilen in der `config.txt` und drei fehlerhafte Befehle in der
Nacharbeitsliste. Nichts davon wäre ohne den Test aufgefallen – und im
Ernstfall steht man damit unter Zeitdruck.

## Regelmäßige Prüfungen

| Wann | Was |
|---|---|
| wöchentlich | `ntp0_wochenbericht.py` |
| monatlich | `tools/ntp0_systemcheck.sh` |
| halbjährlich | Wiederherstellung auf einer Reservekarte proben |

Den laufenden Zustand übernimmt Home Assistant, siehe
[home-assistant.md](home-assistant.md).

## Updates

Ein Zeitserver ist ein Dienst, den niemand vermisst, bis er fehlt.
Automatische Updates ohne Kontrolle sind hier keine gute Idee. Bewährt:
Sicherheitsupdates einspielen, aber zu einem selbst gewählten Zeitpunkt,
mit einem aktuellen Backup und einer Kontrolle danach:

```bash
sudo systemctl start ntp0-backup.service
sudo apt update && sudo apt full-upgrade
sudo reboot
systemctl --failed && chronyc tracking
```
