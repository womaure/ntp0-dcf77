# Warum es so gebaut ist

Diese Seite hält die Begründungen hinter den Entscheidungen fest –
einschließlich der Ansätze, die sich als falsch herausgestellt haben. Wer
das System anpasst, sollte wissen, welche naheliegenden Wege bereits
begangen und wieder verlassen wurden.

## chrony: `filter 1` bei der Refclock

```
refclock SHM 0 poll 6 filter 1 refid DCF prefer precision 1e-2
```

Ohne `filter 1` blieb das Reach-Register dauerhaft bei ein bis zwei von
acht. Grund: Der Medianfilter von chrony sammelt mehrere Messwerte pro
Abfrage – der Dekoder liefert aber nur einen pro Minute. Die Quelle galt
deshalb als weitgehend unerreichbar, obwohl der Empfang gut war.

Das hatte eine Folgewirkung, die lange unentdeckt blieb: Der
Autotune-Regler verlangt eine Mindestzahl erfolgreicher Abfragen. Durch
den niedrigen Reach wurde jeder Lauf verworfen – der Regler war
wochenlang funktionsunfähig, ohne dass es auffiel.

`precision 1e-2` ist ebenfalls bewusst gesetzt. Ohne die Angabe schätzt
chrony die Quelle zu optimistisch ein.

## Kurze Störimpulse ignorieren statt den Frame verwerfen

DCF77 kennt nur Pulse von etwa 100 und 200 ms. Ein Impuls von 20 bis
40 ms ist zweifelsfrei elektrisches Rauschen.

Anfangs verwarf der Dekoder bei jedem ungültigen Impuls den kompletten
Frame. Ein einzelner Glitch in Sekunde 45 vernichtete damit 45 bereits
korrekt dekodierte Bits. Jetzt werden Impulse unter 50 ms schlicht
übersprungen; erst ab sechs pro Minute gilt der Frame als zu gestört.

Das ist sicher: Zerhackt ein Glitch doch einmal ein echtes Bit, fehlt
dieses Bit am Ende, der Frame hat 58 statt 59 Bits und wird ohnehin
verworfen.

## Die RTC ist keine Fallback-Zeitquelle

Ein naheliegender Gedanke: Fällt DCF77 aus, soll die DS3231 einspringen.

Das funktioniert nicht, und zwar grundsätzlich. Die RTC wird vom Kernel
**aus der Systemzeit gestellt** (`rtcsync`, alle elf Minuten). Sie ist
keine unabhängige Referenz, sondern ein Spiegel dessen, was der Rechner
ohnehin weiß. Als laufende Quelle lieferte sie die eigene Zeit zurück –
bei einem Ausfall mitsamt dem Fehler. chrony kennt für RTCs folgerichtig
auch keinen Refclock-Treiber.

Ihr Nutzen liegt ausschließlich darin, nach einem Start sofort eine
brauchbare Zeit bereitzustellen.

Der Verfügbarkeits-Fallback ist stattdessen:

```
local stratum 10
```

Damit liefert der Server seine eigene, zuletzt disziplinierte Uhr weiter
aus, wenn alle Quellen fehlen. Die Clients sehen am hohen Stratum-Wert,
dass die Qualität gerade schlechter ist. Bewusster Kompromiss:
Verfügbarkeit vor nachweisbarer Genauigkeit – eine leicht driftende Zeit
ist besser als gar keine.

## Der Autotune-Regler: klein schreiten

Die erste Fassung erlaubte bis zu 150 ms Korrektur pro Lauf. Im
Dauerbetrieb pendelte der Wert dadurch um ±90 ms hin und her. Der Regler
folgte keiner Drift, sondern schaukelte sich auf.

Die Ursache war nicht der Regler, sondern die Messung: Ausgewertet wurde
jeweils ein **einzelner** DCF-Messwert, und die streuten damals um rund
33 ms. Drei verrauschte Einzelwerte sind keine belastbare Grundlage.

Zwei Änderungen haben das beruhigt:

- **10 ms maximale Schrittweite.** Die reale Drift beträgt wenige
  Millisekunden pro Tag; das reicht mit Abstand. Ein Ausreißer kann
  keinen großen Sprung mehr auslösen.
- **Sechs Stunden Beruhigungszeit** nach jeder Korrektur. Danach hat
  chrony die Systemuhr nachgezogen, und der Regler misst nicht mehr in
  seine eigene Nachwirkung hinein.

Eine Simulation über 30 Tage ergab: mittlerer Fehler 12,9 → 9,9 ms,
größter Ausschlag 48 → 34 ms. Mit einer gemittelten statt einer einzelnen
Messung wären 6,4 ms erreichbar – das wäre der nächste Schritt, falls die
Streuung wieder steigt.

## Plausibilitätsprüfung gegen die Systemzeit

Es kam ein Frame durch, dessen Datum um genau zwei Tage danebenlag.
Weder die Kettenprüfung (zwei Minuten im Abstand von 60 Sekunden) noch
die Paritätsbits konnten das fangen: Der Abstand stimmte, und zwei
Bitfehler im Datum hoben sich in der Parität gegenseitig auf.

Die Prüfung vergleicht deshalb jede dekodierte Zeit mit der Systemuhr.
Zwei Fallstricke waren dabei zu umschiffen:

- **Sie darf nicht sofort greifen.** Die erste Fassung prüfte ab der
  ersten Übergabe. Bei einem Kaltstart mit falscher Uhr wurde der erste
  Frame akzeptiert und jeder weitere verworfen – chrony bekam nie genug
  Messwerte, um die Uhr zu stellen. Der Dekoder blockierte sich selbst.
  Jetzt wird die Prüfung erst scharf, wenn einmal eine dekodierte Zeit
  nahe an der Systemzeit lag.
- **Sie braucht einen Ausweg.** Weicht 30 Minuten lang jeder Frame ab,
  liegt die Systemuhr falsch und nicht das Funksignal. Dann schaltet sich
  die Prüfung ab.

## Robuste Statistik im Driftbericht

Derselbe fehlerhafte Frame trieb die Standardabweichung eines Tages auf
5.715.716 ms – die Tageszeile war unbrauchbar, obwohl 913 von 914
Messwerten in Ordnung waren.

Der Bericht verwendet deshalb eine ausreißerunempfindliche Streuung
(mittlere absolute Abweichung vom Median, skaliert mit 1,4826) und zählt
Werte über einer Sekunde separat. Ein Ausreißer ist damit sichtbar, ohne
die Statistik zu zerstören.

## Systemlast: gemessen statt vermutet

Vermutet hatte ich, der Dekoder mit seiner Abfrage alle 10 ms sei der
größte Verbraucher. Gemessen war es der **Statusdienst** mit 7,2 Prozent
eines Kerns gegenüber 1,6 Prozent des Dekoders.

Der Grund war nicht die Auswertung, sondern das Starten externer
Prozesse: dreimal pro Durchlauf, alle 30 Sekunden. Auf einem
800-MHz-Einkerner ist jeder Prozessstart teuer. Intervall auf 60 Sekunden
und die Fail2ban-Abfrage nur jeden fünften Durchlauf – der Verbrauch fiel
auf 1,2 Prozent, ein Sechstel.

Lehre: Bei Last lohnt das Messen mehr als das Überlegen.

## Sensorwerte im Klartext

Die MQTT-Sensoren lieferten anfangs `green`, `orange` und `red`. Das sind
Farbnamen, keine Aussagen – und `red` stand für drei völlig verschiedene
Lagen: kein Empfang, Empfang mit abweichendem Zeitwert, zu unruhiges
Signal. Die erfordern unterschiedliche Maßnahmen.

Jetzt liefern die Sensoren ganze Sätze, die auch jemand versteht, der das
System nicht kennt. Die Ampelfarbe steckt im Attribut `ampel` und bleibt
für die farbliche Darstellung nutzbar.

Ebenso beim Reach: `chronyc` zeigt ihn oktal, `201` bedeutet zwei von acht
erfolgreichen Abfragen. Als Dezimalzahl 129 ausgegeben, wirkte er wie ein
hoher Wert. Die Sensoren zeigen „2/8 Abfragen".

## systemd-Härtung mit Augenmaß

Keiner der Dienste nimmt Verbindungen entgegen – der Schutz vor
Angreifern von außen ist also gering. Der reale Nutzen liegt darin,
Schäden durch Fehler in den eigenen Programmen zu begrenzen; immerhin
löschen zwei davon Dateien.

Deshalb nur wenige, wirkungsvolle Einstellungen statt einer langen Liste:
`NoNewPrivileges`, `ProtectSystem`, `ProtectHome`, `PrivateTmp` und
gezielte `ReadWritePaths`. Bewusst weggelassen wurden `SystemCallFilter`
und eigene Capability-Listen – sie brechen auf ARM gelegentlich subtil
Dinge, und der Zugewinn stünde dazu in keinem Verhältnis.

Nicht jeder Dienst verträgt dasselbe: Der Statusdienst braucht
`ProtectSystem=full` statt `strict`, weil er über Unix-Sockets mit
`chronyc` und `fail2ban-client` spricht. Der Backup-Dienst braucht
`ProtectHome=read-only`, sonst fehlte der SSH-Schlüssel im Archiv.

## Python 3.11

Die Programme laufen auf dem, was Bookworm mitbringt. Verschachtelte
f-Strings mit gleichen Anführungszeichen sind erst ab 3.12 erlaubt – ein
solcher Ausdruck hat den Statusdienst einmal beim Start abstürzen lassen,
weil er in der Entwicklungsumgebung übersetzbar war.

Wer Änderungen macht, prüft am besten gegen 3.11, nicht gegen die eigene
Version.
