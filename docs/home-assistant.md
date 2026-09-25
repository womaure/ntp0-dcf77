# Home-Assistant-Anbindung

`dcf77_ha_status.py` veröffentlicht die Sensoren per MQTT Discovery. Sie
erscheinen automatisch als Gerät, eine YAML-Konfiguration ist nicht
nötig. Zugangsdaten und Broker stehen in `/etc/ntp-0.conf`.

Alle Sensoren teilen ein Verfügbarkeits-Topic mit Last-Will: Fällt der
Server aus, zeigt Home Assistant „nicht verfügbar" statt eines
eingefrorenen Werts.

Der Rest dieser Seite beschreibt die Sensoren und sinnvolle
Benachrichtigungen.

## Ausgangslage

Auf dem Raspberry Pi (Beispieladresse 192.0.2.10) läuft ein DCF77-gesteuerter
NTP-Server, der die Clients im Netz mit Zeit versorgt. Er meldet
seinen Zustand per MQTT an Home Assistant.

**MQTT-Broker:** 192.0.2.60:1883
**Gerät in Home Assistant:** „DCF77 NTP-Server (ntp-0)"
**Integration:** MQTT Discovery — die Entitäten sind bereits vorhanden und
brauchen keine YAML-Konfiguration.

Alle Sensoren teilen sich ein gemeinsames Verfügbarkeits-Topic mit
Last-Will. Fällt der Pi aus, gehen sie automatisch auf „nicht verfügbar".

---

## Vorhandene Entitäten

Die genauen Entity-IDs bitte in Home Assistant nachschlagen
(Entwicklerwerkzeuge → Zustände, nach `dcf77` oder `ntp` filtern). Die
Namen lauten:

| Anzeigename | Zustandswerte | Wichtige Attribute |
|---|---|---|
| **DCF77 Status** | Klartext, siehe unten | `ampel` (green/orange/red), `reach_successes` (0–8), `state_char` |
| **ntp-0 Zeitquelle** | Klartext, siehe unten | `stratum`, `rms_offset_s`, `skew_ppm`, `reference_name` |
| **ntp-0 Hardware-Uhr** | Klartext, siehe unten | `abweichung_sekunden`, `temperatur_c`, `geraet_vorhanden` |
| **ntp-0 Letztes Backup** | Zeitstempel (device_class: timestamp) | `letzter_versuch_erfolgreich`, `fehler`, `alter_tage`, `pfade` |
| **ntp-0 Fail2ban gesperrte IPs** | `keine` oder IP-Liste als Text | `currently_banned`, `banned_ips` |
| **DCF77 NTP Clients** | Anzahl | `clients` |
| **ntp-0 Temperatur CPU** | Grad Celsius | — |
| **ntp-0 Temperatur Hardware-Uhr** | Grad Celsius | — |
| **ntp-0 Letzter Neustart** | Zeitstempel | — |
| **ntp-0 SD-Karte** | Klartext, siehe unten | `geschrieben_mb_24h`, `dateisystemfehler`, `io_fehler_seit_start`, `schreibgeschuetzt`, `belegt_prozent`, `trim_erfolgreich` |
| **ntp-0 SD-Karte geschrieben** | GiB gesamt (total_increasing) | — |

### Mögliche Zustandswerte im Klartext

**DCF77 Status:**
- `Synchronisiert` — Normalzustand
- `Empfang gut, andere Quelle führt`
- `Empfang gut, wird nicht verwendet`
- `Empfang da, Zeitwert weicht ab`
- `Kein Empfang`
- `Signal zu unruhig`
- `Wartet auf ersten Empfang`
- `Status nicht abrufbar`

**ntp-0 Zeitquelle:**
- `DCF77-Funksignal` — Normalzustand, Stratum 1
- `Internet-Zeitserver: <name>` — Rückfall auf NTP-Pool
- `Notlauf: eigene Uhr, keine externe Quelle` — kritisch
- `Noch keine Synchronisation`

**ntp-0 Hardware-Uhr:**
- `Laeuft synchron` — Normalzustand
- `Weicht ab (+X.Xs)`
- `Weicht stark ab (+Xs)`
- `Zeit voellig abwegig - Batterie pruefen`
- `Antwortet nicht`

**ntp-0 SD-Karte:**
- `In Ordnung` — Normalzustand
- `Fast voll (X %)`
- `Lese-/Schreibfehler seit Start (N)`
- `Dateisystemfehler (N)`
- `Schreibgeschuetzt eingehaengt - Karte pruefen!` — kritisch

---

## Die Automationen

Die Verzögerungen (`for:`) sind entscheidend. Ohne sie gäbe es
Fehlalarme, weil der DCF77-Empfang naturgemäß schwankt und nach einem
Neustart rund zehn Minuten bis zur Synchronisation braucht. Automationen,
die zu oft auslösen, werden abgeschaltet und nützen dann gar nichts.

### 1. Server nicht erreichbar — kritisch

- **Auslöser:** Zustand von *ntp-0 Zeitquelle* wird `unavailable`
- **Dauer:** 5 Minuten
- **Meldung:** „NTP-Server ntp-0 antwortet seit 5 Minuten nicht. Prüfen, ob
  der Pi läuft und im Netz erreichbar ist."
- **Begründung der Dauer:** Kurze Netzaussetzer und Dienstneustarts sollen
  nicht melden.

### 2. Notlauf aktiv — kritisch

- **Auslöser:** *ntp-0 Zeitquelle* ist `Notlauf: eigene Uhr, keine externe Quelle`
- **Dauer:** 15 Minuten
- **Meldung:** „ntp-0 läuft im Notlauf — weder DCF77 noch Internet
  verfügbar. Die Zeit stammt nur noch aus der eigenen Systemuhr."
- **Hinweis:** Dieser Zustand tritt nach jedem Neustart kurz auf, deshalb
  die 15 Minuten.

### 3. DCF77 dauerhaft gestört — Warnung

- **Auslöser:** *DCF77 Status* ist **nicht** `Synchronisiert`
- **Dauer:** 60 Minuten
- **Meldung:** „DCF77-Empfang seit einer Stunde gestört. Aktueller Zustand:
  {{ states('sensor.<dcf77_status>') }}"
- **Begründung:** Störphasen von einigen Minuten sind normal und wurden
  mehrfach beobachtet. Eine Stunde ist auffällig.
- **Nützlich:** Der Zustandstext unterscheidet die Ursachen — `Kein Empfang`
  deutet auf Antenne oder Verkabelung, `Empfang da, Zeitwert weicht ab` auf
  ein Kalibrierungsproblem, `Signal zu unruhig` auf Störquellen.

### 4. Hardware-Uhr auffällig — Warnung

- **Auslöser:** *ntp-0 Hardware-Uhr* ist nicht `Laeuft synchron`
- **Dauer:** 30 Minuten
- **Meldung:** „Hardware-Uhr der ntp-0 meldet: {{ states(...) }}. Bei
  ‚Batterie pruefen' ist die CR2032 auf dem DS3231-Modul zu tauschen."
- **Begründung:** Die RTC stellt nach einem Neustart sofort die Systemzeit
  bereit. Fällt sie aus, merkt man es sonst erst beim nächsten Stromausfall.

### 5. Backup veraltet — Warnung

- **Auslöser:** Zeitstempel von *ntp-0 Letztes Backup* älter als 8 Tage
- **Prüfung:** einmal täglich, etwa um 09:00
- **Meldung:** „Letztes erfolgreiches Backup der ntp-0 ist {{ ... }} Tage
  alt. Das Backup läuft sonntags um 03:30 auf die FritzBox-Freigabe."
- **Begründung der 8 Tage:** Das Backup läuft wöchentlich; acht Tage lassen
  einen Lauf ausfallen, ohne sofort zu melden.
- **Umsetzungshinweis:** Das Attribut `alter_tage` liefert den Wert direkt,
  alternativ über den Zeitstempel rechnen.

### 6. Backup fehlgeschlagen — Warnung

- **Auslöser:** Attribut `letzter_versuch_erfolgreich` von *ntp-0 Letztes
  Backup* wechselt auf `false`
- **Dauer:** sofort
- **Meldung:** „Backup der ntp-0 fehlgeschlagen: {{ state_attr(...,
  'fehler') }}"
- **Begründung:** Meldet den Fehlschlag sofort, während Automation 5 den
  Fall abdeckt, dass das Backup gar nicht erst startet.

### 7. SD-Karte auffällig — Warnung, bei „Schreibgeschuetzt" kritisch

- **Auslöser:** *ntp-0 SD-Karte* ist nicht `In Ordnung`
- **Dauer:** 10 Minuten
- **Meldung:** „SD-Karte der ntp-0 meldet: {{ states(...) }}"
- **Begründung:** Die Karte kann ihren Verschleiß nicht selbst melden
  (weder SanDisk High Endurance noch Samsung Pro Endurance beantworten
  das Herstellerkommando dafür). Ausgewertet werden deshalb die
  Anzeichen, die einem Ausfall vorausgehen. `Schreibgeschuetzt` bedeutet,
  dass ext4 wegen schwerer Fehler auf nur-lesen geschaltet hat — dann
  zeitnah auf die Reservekarte wechseln und aus dem Backup
  wiederherstellen.
- **Hinweis:** Die Prüfung der Kernelmeldungen läuft nur alle zehn
  Minuten; kürzere Dauern bringen nichts.

### 8. Ungewöhnlich hohe Schreiblast — Hinweis

- **Auslöser:** Attribut `geschrieben_mb_24h` von *ntp-0 SD-Karte* über
  einem Schwellwert
- **Schwellwert:** Erst nach einer Woche festlegen, wenn der Normalwert
  bekannt ist — etwa das Dreifache des üblichen Tageswerts
- **Begründung:** Fängt ab, wenn plötzlich etwas die Karte zuschreibt,
  wie früher das auf 200 MB angewachsene Journal. Das verkürzt die
  Lebensdauer, lange bevor ein Fehler auftritt.

### Optional: Fail2ban-Sperre

- **Auslöser:** *ntp-0 Fail2ban gesperrte IPs* wechselt von `keine` auf
  etwas anderes
- **Meldung:** „Fail2ban hat auf ntp-0 gesperrt: {{ states(...) }}"
- **Einschätzung:** Seit der Einrichtung gab es außer Testsperren keine.
  Da SSH ausschließlich per Schlüssel läuft und nur aus bekannten Subnetzen
  erreichbar ist, wäre eine Sperre ein ungewöhnliches Ereignis — und
  deshalb einen Hinweis wert.
- **Hinweis:** Dieser Sensor wird nur alle fünf Minuten aktualisiert.

---

## Umsetzungshinweise

**Benachrichtigungsweg:** Push über die Home-Assistant-App, Telegram oder
E-Mail — je nachdem, was eingerichtet ist.

**Gruppierung:** Kritische Meldungen (1 und 2) sollten sich von Warnungen
unterscheiden, etwa durch unterschiedliche Priorität oder Kanäle. Ein
ausgefallener Zeitserver ist etwas anderes als ein verpasstes Backup.

**Wiederholung vermeiden:** Automationen sollten nicht im Minutentakt
erneut melden, solange ein Zustand anhält. Der `for:`-Mechanismus löst
ohnehin nur beim Zustandswechsel aus.

**Entwarnung:** Erwägenswert wäre eine Meldung, wenn sich ein Zustand
wieder normalisiert — zumindest für die beiden kritischen Fälle.

**Testen:** Die Automationen lassen sich ohne echten Ausfall prüfen, indem
man die Entitätszustände unter Entwicklerwerkzeuge → Zustände von Hand
setzt. Das überschreibt den Wert bis zur nächsten MQTT-Nachricht — beim
DCF77-Status nach spätestens einer Minute, bei Fail2ban nach fünf.

---

## Hintergrund zu den Schwellwerten

Die Zahlen stammen aus dem beobachteten Verhalten über mehrere Wochen:

- Nach einem Neustart dauert es rund **neun Minuten**, bis DCF77 als Quelle
  übernimmt. In dieser Zeit läuft der Server über die RTC und
  Internet-NTP — er ist verfügbar, aber nicht auf Stratum 1.
- Der Empfang schwankt tageszeitabhängig. Beobachtet wurden Störphasen von etwa **einer Stunde** mit gehäuften Störimpulsen,
  vermutlich durch ein Gerät im Haushalt.
- Das Reach-Register erreicht im guten Fall 8 von 8, fällt aber
  regelmäßig auf 2 bis 5 zurück, ohne dass das ein Problem wäre.
- Das Backup läuft wöchentlich und dauert wenige Sekunden.
