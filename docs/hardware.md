# Hardware und Verkabelung

## Verwendete Teile

| Teil | Bemerkung |
|---|---|
| Raspberry Pi 1 Model B+ | Jedes neuere Modell geht auch |
| DCF77-Empfangsmodul mit Ferritantenne | Hier ein Pollin-Modul, Platinenaufdruck WHP0027V03 |
| DS3231-RTC-Modul mit CR2032 | Über I2C, meist zusammen mit einem AT24C32-EEPROM auf der Platine |
| Widerstand 100 Ω, ¼ W | für den Versorgungsfilter |
| Elektrolytkondensator 10 µF, ≥ 10 V | dito, Polung beachten |
| Keramikkondensator 100 nF | dito, ungepolt |
| SD-Karte mit hoher Schreibfestigkeit | „High Endurance" bzw. „Pro Endurance", 64 GB |

## Belegung am 40-Pin-Header

DCF77-Modul:

| Modul | Pi-Pin | Funktion |
|---|---|---|
| VCC | 1 | 3,3 V (über den Filter, siehe unten) |
| GND | 6 | Masse |
| PON | 9 | auf Masse = Empfangsteil dauerhaft eingeschaltet |
| TCO | 11 | Signalausgang, entspricht GPIO 17 |

DS3231:

| Modul | Pi-Pin |
|---|---|
| VCC | 17 (3,3 V) |
| GND | 20 |
| SDA | 3 |
| SCL | 5 |

Die GPIO-Nummer im Programm (`DCF_PIN = 17`) ist die
Broadcom-Nummerierung, nicht die Position am Header. Verwechslungen
damit sind die häufigste Fehlerquelle beim Aufbau.

## Filter in der Versorgungsleitung

Der 3,3-V-Ausgang des Pi stammt von einem Schaltregler. Dessen Reste auf
der Versorgungsleitung stören den Empfänger messbar.

```
Pi Pin 1 (3,3 V) ──[ R 100 Ω ]──┬──────── VCC am DCF-Modul
                                │
                          C1 ═══╪═══ C2
                       (10 µF)  │  (100 nF)
                                │
Pi Pin 6 (GND) ─────────────────┴──────── GND am DCF-Modul
```

Bei einer Stromaufnahme von höchstens 120 µA fallen am Widerstand nur
0,012 V ab. Die Grenzfrequenz liegt bei rund 160 Hz und dämpft damit
alles, was in der Nähe der 77,5 kHz stört.

Zwei Kondensatoren deshalb, weil der Elko niederfrequente Störungen
abfängt, hochfrequente wegen seiner Eigeninduktivität aber nicht mehr –
das übernimmt der Keramikkondensator.

**Beide Kondensatoren so nah wie möglich an die Anschlüsse des Moduls**,
nicht ans Pi-Ende der Leitung. Kurze Wege sind hier entscheidend.

### Was der Filter gebracht hat

Vorher traten in Wellen gehäufte Störimpulse von 20 bis 40 ms Länge auf,
meist morgens und abends – typisch für ein Gerät im Haushalt, das ein- und
ausgeschaltet wird. Die Streuung der Einzelmessungen lag bei 32 bis 43 ms.

Danach verschwanden die kurzen Störimpulse fast vollständig, die Streuung
fiel auf 5 bis 6 ms. Die Empfangsquote stieg von rund 96 auf über 99
Prozent in den ersten Tagen.

Das ist ein Erfahrungswert aus einem Aufbau, keine allgemeingültige
Zusage. Kommen die Störungen bei dir nicht über die Versorgung, sondern
werden direkt in die Ferritantenne eingestrahlt, hilft nur Abstand oder
eine andere Ausrichtung.

## Antenne aufstellen

Die Ferritantenne ist gerichtet. Sie steht quer zur Richtung des Senders
(Mainflingen bei Frankfurt) am besten – also nicht auf ihn zeigend.

Abstand halten zu: Schaltnetzteilen, Monitoren, LED-Leuchtmitteln,
Ladegeräten, WLAN-Routern und dem Pi selbst.

Nach dem Einschalten dauert es laut Datenblatt bis zu 20 Minuten, bis
sich das Modul auf das Signal eingestellt hat.

## Signalform prüfen

Das Modul dieses Aufbaus liefert **HIGH** während des aktiven Pulses
(etwa 100 ms für eine Null, 200 ms für eine Eins) und LOW dazwischen.
Andere Module invertieren das.

Vor der Inbetriebnahme lohnt eine Messung der rohen Pegelwechsel. Passt
die Polarität nicht, sind im Dekoder `BIT_0_*`/`BIT_1_*` und die
Erkennung der Minutenlücke entsprechend zu tauschen.

## SD-Karte

Dauerbetrieb mit ständigen Schreibzugriffen ist der Verschleißfall, für
den Karten der Endurance-Reihen gebaut sind. Eine größere Karte hält
länger, weil mehr Speicherzellen für die Verteilung der Schreibzugriffe
zur Verfügung stehen – 64 GB bei wenigen GB Bedarf ist also kein Unsinn.

Den Verschleiß selbst kann die Karte nicht melden: Das dafür vorgesehene
Herstellerkommando beantworten die gängigen Endurance-Modelle nicht.
`dcf77_ha_status.py` wertet deshalb aus, was das Betriebssystem weiß –
geschriebene Datenmenge, Dateisystemfehler, Ein-/Ausgabefehler und ob das
Dateisystem auf nur-lesen umgeschaltet hat.
