#!/usr/bin/env python3
"""
DCF77 -> NTP SHM Refclock
==========================

Baut auf dem funktionierenden Dekoder auf. Sobald eine Minute
erfolgreich dekodiert wurde, wird die Zeit in ein SysV-Shared-
Memory-Segment geschrieben, das chrony (oder ntpd) als lokale
Referenzuhr ("SHM Refclock", Unit 0) lesen kann.

Anschluss (zur Erinnerung):
  VCC -> Pin 1  (3.3V)
  GND -> Pin 6  (GND)
  PON -> Pin 9  (GND)
  TCO -> Pin 11 (GPIO17, BCM-Zaehlweise)

WICHTIG:
- Dieses Script muss als root laufen (oder das SHM-Segment muss
  vorher mit passenden Rechten angelegt sein), damit chrony
  (Benutzer "_chrony" bzw. "chrony") lesend zugreifen kann.
  Wir legen das Segment mit Modus 0666 an, das ist fuer den
  Hausgebrauch ausreichend (jeder lokale Prozess darf lesen/schreiben).
- Genauigkeit: Dieser Software-Decoder liefert typischerweise
  +/- 100-300ms Abweichung, keine Millisekunden-Praezision.
  Fuer einen internen Heimnetz-NTP-Server voellig ausreichend.

Passende chrony.conf-Zeile (siehe separate Datei):
  refclock SHM 0 poll 6 refid DCF prefer

Ausfuehren (als root, z.B. ueber systemd-Service):
  sudo python3 dcf77_ntp_shm.py
"""

import RPi.GPIO as GPIO
import ctypes
import ctypes.util
import time
import datetime
import sys
import configparser

DCF_PIN = 17  # physischer Pin 11

BIT_0_MIN, BIT_0_MAX = 0.05, 0.15   # ~100ms LOW-Puls = Bit 0
BIT_1_MIN, BIT_1_MAX = 0.15, 0.25   # ~200ms LOW-Puls = Bit 1
GAP_THRESHOLD = 1.5                  # HIGH > 1.5s = Minutenluecke

# Impulse unterhalb dieser Laenge sind eindeutig elektrisches Rauschen -
# DCF77 kennt nur 100ms und 200ms. Real beobachtet wurden Glitches von
# 20, 21, 31 und 41 ms. Sie werden ignoriert, statt den Frame zu
# verwerfen (siehe ausfuehrliche Begruendung in der Hauptschleife).
GLITCH_MAX_SECONDS = 0.05

# Haeufen sich die Glitches innerhalb einer Minute, ist der Empfang so
# gestoert, dass dem Frame nicht mehr zu trauen ist - dann doch verwerfen.
MAX_GLITCHES_PER_FRAME = 5

SHM_UNIT = 0
SHM_KEY = 0x4e545030 + SHM_UNIT  # "NTP0" + Unit-Nummer, Standard fuer ntpd/chrony SHM-Refclocks
IPC_CREAT = 0o1000

# Empirisch ermittelte Verarbeitungsverzoegerung des Empfaengermoduls
# (Pollin WHP0027V03). Der Wert steht NICHT mehr fest im Code, sondern
# in /etc/ntp-0.conf unter [dcf77] receiver_delay - so kann das
# Autotune-Skript ihn anpassen, ohne diese Datei zu veraendern, und ein
# Update dieses Skripts ueberschreibt den gelernten Wert nicht.
#
# Der Wert wird bei jeder erkannten Minutenluecke neu eingelesen (das
# ist ein billiger Dateizugriff einmal pro Minute), damit Aenderungen
# ohne Neustart des Dienstes wirksam werden - ein Neustart wuerde die
# Plausibilitaetskette unnoetig unterbrechen.
CONFIG_FILE = "/etc/ntp-0.conf"
RECEIVER_DELAY_FALLBACK = 0.80   # nur, falls die Config nicht lesbar ist
RECEIVER_DELAY_MIN = 0.30        # Sicherheitsgrenzen: unplausible Werte
RECEIVER_DELAY_MAX = 1.50        # werden ignoriert


def read_receiver_delay():
    """Liest [dcf77] receiver_delay aus der Config.
    Faellt bei Fehlern oder unplausiblen Werten auf den Fallback zurueck,
    statt mit einem kaputten Wert weiterzurechnen."""
    try:
        parser = configparser.ConfigParser()
        if not parser.read(CONFIG_FILE):
            return RECEIVER_DELAY_FALLBACK
        value = parser.getfloat("dcf77", "receiver_delay",
                                fallback=RECEIVER_DELAY_FALLBACK)
    except Exception:
        return RECEIVER_DELAY_FALLBACK

    if not (RECEIVER_DELAY_MIN <= value <= RECEIVER_DELAY_MAX):
        print(f"  WARNUNG: receiver_delay={value}s liegt ausserhalb "
              f"{RECEIVER_DELAY_MIN}-{RECEIVER_DELAY_MAX}s - nutze "
              f"Fallback {RECEIVER_DELAY_FALLBACK}s")
        return RECEIVER_DELAY_FALLBACK

    return value


# --- SysV-Shared-Memory Struktur, wie von ntpd/chrony erwartet ---
class ShmTime(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_int),
        ("count", ctypes.c_int),
        ("clockTimeStampSec", ctypes.c_long),
        ("clockTimeStampUSec", ctypes.c_int),
        ("receiveTimeStampSec", ctypes.c_long),
        ("receiveTimeStampUSec", ctypes.c_int),
        ("leap", ctypes.c_int),
        ("precision", ctypes.c_int),
        ("nsamples", ctypes.c_int),
        ("valid", ctypes.c_int),
        ("clockTimeStampNSec", ctypes.c_uint),
        ("receiveTimeStampNSec", ctypes.c_uint),
        ("dummy", ctypes.c_int * 8),
    ]


def attach_shm():
    libc_name = ctypes.util.find_library("c")
    libc = ctypes.CDLL(libc_name, use_errno=True)

    libc.shmget.restype = ctypes.c_int
    libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
    libc.shmat.restype = ctypes.c_void_p
    libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]

    shmid = libc.shmget(SHM_KEY, ctypes.sizeof(ShmTime), IPC_CREAT | 0o666)
    if shmid < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"shmget fehlgeschlagen: {os_strerror(err)}")

    addr = libc.shmat(shmid, None, 0)
    # shmat liefert bei Fehler (void*)-1
    if addr is None or ctypes.c_long(addr).value == -1:
        err = ctypes.get_errno()
        raise OSError(err, f"shmat fehlgeschlagen: {os_strerror(err)}")

    shm_struct = ctypes.cast(addr, ctypes.POINTER(ShmTime)).contents
    return shm_struct


def os_strerror(errno_val):
    import os
    try:
        return os.strerror(errno_val)
    except Exception:
        return f"errno {errno_val}"


def write_shm(shm, dt, receive_time):
    """Schreibt einen neuen Zeitstempel ins SHM-Segment.

    dt: zeitzonenbewusstes datetime der DCF77-Zeit (exakter Minutenbeginn).
        Der UTC-Offset stammt aus Bit 17/18 des Funksignals, nicht aus der
        Systemzeitzone - deshalb ist .timestamp() hier auch waehrend der
        Zeitumstellung eindeutig.
    receive_time: time.time()-Wert, zu dem dieser Minutenbeginn im System
        erkannt wurde
    """
    clock_ts = dt.timestamp()

    clock_sec = int(clock_ts)
    clock_usec = int((clock_ts - clock_sec) * 1_000_000)

    recv_sec = int(receive_time)
    recv_usec = int((receive_time - recv_sec) * 1_000_000)

    shm.mode = 1
    shm.clockTimeStampSec = clock_sec
    shm.clockTimeStampUSec = clock_usec
    shm.receiveTimeStampSec = recv_sec
    shm.receiveTimeStampUSec = recv_usec
    shm.leap = 0
    # Hinweis: chrony IGNORIERT die Felder precision und nsamples aus dem
    # SHM-Segment - es nutzt stattdessen die precision-Option der
    # refclock-Zeile in /etc/chrony/chrony.conf (dort auf 1e-2 gesetzt,
    # passend zur 10-ms-Aufloesung des Pollings hier).
    # Die Werte bleiben nur fuer den Fall gesetzt, dass jemand statt
    # chrony den klassischen ntpd verwendet - der wertet sie aus.
    shm.precision = -7   # 2^-7 s = ca. 7.8 ms
    shm.nsamples = 3
    shm.valid = 1
    shm.count += 1


def bcd_to_int(bits, weights):
    return sum(b * w for b, w in zip(bits, weights))


def binary_to_int(bits):
    return sum(b * (2 ** i) for i, b in enumerate(bits))


def parity_ok(bits):
    return (sum(bits) % 2) == 0


def decode_minute(bits):
    """Dekodiert einen 59-Bit-Frame.

    Rueckgabe: (datetime MIT Zeitzone, Umstellungsankuendigung, Fehlertext)
    Das datetime traegt den aus dem Funksignal selbst gelesenen
    UTC-Offset - nicht die Zeitzone des Systems.
    """
    if len(bits) < 59:
        return None, False, "zu wenige Bits (%d)" % len(bits)

    # --- Konstante Bits pruefen ---
    # Bit 0 (Minutenmarke) ist per Definition immer 0, Bit 20 (Start of
    # Time) immer 1. Stimmen sie nicht, ist der Frame verschoben oder
    # gestoert - dann sind alle folgenden Felder wertlos.
    #
    # Das ist mehr als Kosmetik: Die drei Paritaetspruefungen allein
    # lassen einen voellig zufaelligen Frame mit 1/8 Wahrscheinlichkeit
    # durch. Diese beiden Bits druecken das auf 1/32.
    if bits[0] != 0:
        return None, False, "Bit 0 (Minutenmarke) ist nicht 0 - Frame gestoert"
    if bits[20] != 1:
        return None, False, "Bit 20 (Start of Time) ist nicht 1 - Frame gestoert"

    # --- Zeitzone direkt aus dem Signal (Bit 17 = MESZ, Bit 18 = MEZ) ---
    # Die PTB sendet die gueltige Zone explizit mit. Die beiden Bits sind
    # zueinander komplementaer: genau eines ist gesetzt. Sind beide 0 oder
    # beide 1, ist der Frame kaputt - das ist zugleich eine zusaetzliche
    # Plausibilitaetspruefung, die uns nichts kostet.
    #
    # Frueher haben wir stattdessen die Zeitzone des Systems auf die
    # dekodierte Zeit angewendet. Das geht in der doppelt durchlaufenen
    # Stunde bei der Rueckstellung im Oktober schief, weil die lokale Zeit
    # dort mehrdeutig ist. Aus dem Signal gelesen ist sie eindeutig.
    is_cest = bits[17] == 1   # Z1: MESZ, UTC+2
    is_cet = bits[18] == 1    # Z2: MEZ,  UTC+1

    if is_cest == is_cet:
        return None, False, ("Zeitzonenbits unplausibel "
                             f"(Bit17={bits[17]}, Bit18={bits[18]}) - "
                             "beide muessen unterschiedlich sein")

    utc_offset_hours = 2 if is_cest else 1
    tz = datetime.timezone(datetime.timedelta(hours=utc_offset_hours))

    # Bit 16 (A1) kuendigt eine Zeitumstellung innerhalb der naechsten
    # Stunde an. Wir nutzen sie nur zur Protokollierung - die Umschaltung
    # selbst ergibt sich automatisch aus Bit 17/18 der jeweiligen Minute.
    dst_announced = bits[16] == 1

    minute_bits = bits[21:28]
    minute_parity = bits[28]
    if not parity_ok(minute_bits + [minute_parity]):
        return None, dst_announced, "Paritaetsfehler Minute"
    minute = bcd_to_int(minute_bits, [1, 2, 4, 8, 10, 20, 40])

    hour_bits = bits[29:35]
    hour_parity = bits[35]
    if not parity_ok(hour_bits + [hour_parity]):
        return None, dst_announced, "Paritaetsfehler Stunde"
    hour = bcd_to_int(hour_bits, [1, 2, 4, 8, 10, 20])

    day_bits = bits[36:42]
    weekday_bits = bits[42:45]
    month_bits = bits[45:50]
    year_bits = bits[50:58]
    date_parity = bits[58]
    date_all = day_bits + weekday_bits + month_bits + year_bits + [date_parity]
    if not parity_ok(date_all):
        return None, dst_announced, "Paritaetsfehler Datum"

    day = bcd_to_int(day_bits, [1, 2, 4, 8, 10, 20])
    month = bcd_to_int(month_bits, [1, 2, 4, 8, 10])
    year = 2000 + bcd_to_int(year_bits, [1, 2, 4, 8, 10, 20, 40, 80])

    try:
        dt = datetime.datetime(year, month, day, hour, minute, tzinfo=tz)
    except ValueError as e:
        return None, dst_announced, f"ungueltiges Datum: {e}"

    return dt, dst_announced, None


def main():
    print("Verbinde SHM-Segment (Unit 0) fuer chrony/ntpd...")
    try:
        shm = attach_shm()
    except OSError as e:
        print(f"FEHLER: Konnte SHM-Segment nicht anlegen: {e}")
        print("Laeuft das Script als root? (sudo)")
        sys.exit(1)
    print("SHM-Segment bereit.\n")

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(DCF_PIN, GPIO.IN, pull_up_down=GPIO.PUD_OFF)

    print("DCF77-Dekoder gestartet. Warte auf Minutenmarke (Luecke)...")

    last_state = GPIO.input(DCF_PIN)
    last_edge_time = time.time()
    last_valid_bit_edge_time = last_edge_time
    bits = []
    synced = False
    last_heartbeat = time.time()
    HEARTBEAT_INTERVAL = 30  # Sekunden - warnt, falls laenger gar keine Flanke ankommt

    # Selbstheilende Plausibilitaetspruefung: statt gegen einen fest
    # eingefrorenen "letzten guten Anker" zu pruefen (der bei einem
    # einzigen Fehlwert dauerhaft haengen bleiben kann), vergleichen wir
    # fortlaufend gegen den JEWEILS LETZTEN gesehenen Wert. Sobald zwei
    # aufeinanderfolgende Minuten zueinander passen (60s Abstand), gilt
    # die Kette als vertrauenswuerdig und wird an SHM uebergeben. Ein
    # einzelner Ausreisser reisst die Kette einmal ab, baut sich aber
    # aus den naechsten zwei passenden Minuten automatisch neu auf -
    # kein dauerhaftes Haengenbleiben mehr moeglich.
    chain_last_time = None
    chain_length = 0
    # Die Pruefung gegen die Systemzeit greift erst, wenn die Systemuhr
    # nachweislich zu unseren Werten passt - also nicht schon nach der
    # ersten Uebergabe.
    #
    # Der erste Entwurf prueft ab der ersten Uebergabe. Ein Test mit
    # falscher Startzeit zeigte, dass sich der Decoder damit selbst
    # blockiert: Der erste Frame kommt durch, alle weiteren werden
    # verworfen - und chrony bekommt nie genug Messwerte, um die Uhr zu
    # stellen. Deshalb wird die Pruefung erst scharf, wenn einmal eine
    # dekodierte Zeit nahe an der Systemzeit lag.
    systemzeit_vertrauenswuerdig = False
    verworfen_in_folge = 0
    dst_announce_logged = False  # verhindert, dass die Ankuendigung 60x geloggt wird
    glitch_count = 0             # ignorierte Stoerimpulse im aktuellen Frame
    PLAUSIBILITY_TOLERANCE = 0.3  # Sekunden Toleranz um 60s
    MIN_CHAIN_LENGTH = 2  # so viele aufeinanderfolgende passende Minuten noetig

    # Groesste Abweichung zwischen dekodierter Zeit und Systemzeit, die
    # noch akzeptiert wird - sobald der Decoder mindestens einmal
    # erfolgreich geliefert hat.
    #
    # Hintergrund: Am 24.09.2026 kam ein Frame durch, dessen Datum um
    # genau zwei Tage danebenlag (172.800.000 ms im Driftbericht).
    # Uhrzeit und Sekundenlage stimmten, deshalb passte der Abstand von
    # 60 Sekunden zur Kette, und die beiden Datums-Bitfehler hoben sich
    # in der Paritaet gegenseitig auf. Weder Kette noch Paritaet konnten
    # das fangen.
    #
    # Ist die Systemuhr erst einmal diszipliniert, ist bei einer solchen
    # Abweichung die dekodierte Zeit falsch und nicht die Systemuhr.
    # Vor der ersten erfolgreichen Uebergabe wird NICHT geprueft, sonst
    # koennte sich der Decoder nie von einer falschen Startzeit erholen -
    # etwa nach einem Kaltstart ohne Netz und mit leerer RTC.
    SANITY_MAX_ABWEICHUNG = 120.0   # Sekunden

    # Nach so vielen aufeinanderfolgend verworfenen Frames wird die
    # Pruefung wieder abgeschaltet. Dann liegt die Systemuhr falsch, nicht
    # das Funksignal - etwa weil sie jemand von Hand verstellt hat. Ohne
    # diesen Ausweg koennte sich der Decoder nicht mehr erholen.
    SANITY_RESET_NACH = 30

    try:
        while True:
            state = GPIO.input(DCF_PIN)
            now = time.time()

            # Heartbeat: warnt sichtbar, falls minutenlang GAR KEINE
            # Flanke mehr ankommt (z.B. Antenne verrutscht, Kabel lose).
            if now - last_edge_time > HEARTBEAT_INTERVAL and now - last_heartbeat > HEARTBEAT_INTERVAL:
                print(f"  WARNUNG: seit {now - last_edge_time:.0f}s keine Pegelaenderung "
                      f"mehr erkannt - Empfang/Verkabelung pruefen?")
                last_heartbeat = now

            if state != last_state:
                if last_state == 1:
                    # HIGH-Phase beendet: das war der aktive Puls
                    # (durch den Signaltest bestaetigt: kurze ~100-200ms
                    # Pulse sind HIGH, die langen Pausen dazwischen LOW).
                    high_duration = now - last_edge_time
                    if BIT_0_MIN <= high_duration <= BIT_0_MAX:
                        bits.append(0)
                        last_valid_bit_edge_time = now
                    elif BIT_1_MIN <= high_duration <= BIT_1_MAX:
                        bits.append(1)
                        last_valid_bit_edge_time = now
                    elif high_duration < GLITCH_MAX_SECONDS:
                        # Sehr kurzer Impuls (typisch 20-40ms). DCF77 sendet
                        # ausschliesslich 100ms- und 200ms-Pulse, das hier
                        # ist also eindeutig elektrisches Rauschen und kein
                        # verstuemmeltes Bit.
                        #
                        # Frueher wurde deshalb der KOMPLETTE Frame verworfen.
                        # Das war zu streng: ein einzelner Glitch in Sekunde 45
                        # hat 45 bereits korrekt dekodierte Bits vernichtet -
                        # daher die vielen "zu wenige Bits (44)"-Meldungen im
                        # Log. Jetzt wird der Glitch schlicht ignoriert.
                        #
                        # Das ist sicher: Zerhackt ein Glitch doch einmal ein
                        # echtes Bit, fehlt dieses Bit am Ende, der Frame hat
                        # 58 statt 59 Bits und wird ohnehin verworfen. Es kann
                        # also kein falscher Zeitwert entstehen.
                        glitch_count += 1
                        if glitch_count > MAX_GLITCHES_PER_FRAME:
                            print(f"  {glitch_count} Stoerimpulse in dieser "
                                  f"Minute - Frame verworfen (Empfang zu stark "
                                  f"gestoert)")
                            bits = []
                            glitch_count = 0
                    else:
                        # Zu lang fuer einen Glitch, aber keine gueltige
                        # Bitlaenge (z.B. die beobachteten 254ms): hier ist
                        # tatsaechlich etwas am Frame kaputt.
                        print(f"  Ungueltige Pulslaenge "
                              f"({high_duration*1000:.0f}ms), Frame verworfen")
                        bits = []
                        glitch_count = 0
                last_edge_time = now
                last_state = state
            else:
                if state == 0 and (now - last_edge_time) > GAP_THRESHOLD:
                    # LOW-Phase > 1.5s = Minutenluecke (Sek. 59 fehlt).
                    # Minutenbeginn liegt exakt 1,0s nach Ende des letzten
                    # gueltigen LOW-Pulses (= Ende von Bit 58).
                    receiver_delay = read_receiver_delay()
                    minute_start_time = last_valid_bit_edge_time + 1.0 + receiver_delay

                    gap_since_valid_bit = now - last_valid_bit_edge_time
                    gap_plausible = 1.3 <= gap_since_valid_bit <= 2.6

                    if synced and bits and gap_plausible:
                        dt, dst_announced, err = decode_minute(bits)
                        if dt:
                            if dst_announced and not dst_announce_logged:
                                print("  HINWEIS: DCF77 kuendigt eine "
                                      "Zeitumstellung innerhalb der naechsten "
                                      "Stunde an (Bit 16).")
                                dst_announce_logged = True
                            elif not dst_announced:
                                dst_announce_logged = False

                            # Gegenprobe gegen die Systemzeit
                            abweichung = dt.timestamp() - minute_start_time
                            passt_zur_systemzeit = abs(abweichung) <= SANITY_MAX_ABWEICHUNG

                            if passt_zur_systemzeit:
                                systemzeit_vertrauenswuerdig = True
                                verworfen_in_folge = 0
                            elif systemzeit_vertrauenswuerdig:
                                verworfen_in_folge += 1
                                print(f"  Dekodierte Zeit {dt:%d.%m.%Y %H:%M} weicht "
                                      f"{abweichung/3600:+.1f} h von der Systemzeit ab "
                                      f"- Frame verworfen "
                                      f"({verworfen_in_folge}/{SANITY_RESET_NACH})")
                                if verworfen_in_folge >= SANITY_RESET_NACH:
                                    print("  Seit "
                                          f"{SANITY_RESET_NACH} Minuten weicht jeder Frame "
                                          "ab - offenbar liegt die SYSTEMUHR falsch. "
                                          "Pruefung wird ausgesetzt, das Funksignal gilt "
                                          "wieder als Referenz.")
                                    systemzeit_vertrauenswuerdig = False
                                    verworfen_in_folge = 0
                                bits = []
                                glitch_count = 0
                                last_edge_time = now
                                continue

                            if chain_last_time is not None:
                                delta = minute_start_time - chain_last_time
                                matches_chain = abs(delta - 60.0) <= PLAUSIBILITY_TOLERANCE
                            else:
                                matches_chain = False  # erster Wert ueberhaupt

                            if matches_chain:
                                chain_length += 1
                            else:
                                chain_length = 1  # Kette neu beginnen (nicht: alles verwerfen)

                            chain_last_time = minute_start_time

                            if chain_length >= MIN_CHAIN_LENGTH:
                                write_shm(shm, dt, minute_start_time)
                                zone = "MESZ" if dt.utcoffset() == datetime.timedelta(hours=2) else "MEZ"
                                print(f"[{datetime.datetime.now():%H:%M:%S}] "
                                      f"DCF77: {dt:%d.%m.%Y %H:%M} {zone} "
                                      f"an SHM uebergeben "
                                      f"(count={shm.count}, Kette={chain_length}"
                                      f"{f', {glitch_count} Stoerimpulse ignoriert' if glitch_count else ''})")
                            else:
                                print(f"  Kette im Aufbau ({chain_length}/{MIN_CHAIN_LENGTH}) - "
                                      f"noch nicht uebernommen")
                        else:
                            print(f"  Dekodierung fehlgeschlagen: {err} "
                                  f"({len(bits)} Bits)")
                    elif synced and bits and not gap_plausible:
                        print(f"  Verworfen: Luecke war {gap_since_valid_bit:.3f}s "
                              f"(unplausibel, erwartet ~1.7-2.0s)")
                    bits = []
                    glitch_count = 0   # Zaehler gilt immer nur fuer eine Minute
                    synced = True
                    last_edge_time = now

            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\nBeendet durch Nutzer.")
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
