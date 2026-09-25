#!/bin/bash
#
# ntp-0 Systemanalyse: Last und Jitter-Quellen
# =============================================
#
# Erhebt den Ist-Zustand, bevor irgendetwas verändert wird. Die Ausgabe
# ist so aufgebaut, dass sie sich später erneut erzeugen und vergleichen
# lässt - jede Optimierung muss sich an diesen Zahlen messen lassen.
#
# Aufruf:
#     sudo bash ntp0_systemcheck.sh
#     sudo bash ntp0_systemcheck.sh > /tmp/vorher.txt
#
# Läuft auch ohne root, dann fehlen einzelne Werte.

abschnitt() {
    echo
    echo "==============================================================="
    echo "  $1"
    echo "==============================================================="
}

echo "ntp-0 Systemanalyse - $(date '+%Y-%m-%d %H:%M:%S')"
echo "Host: $(hostname)   Kernel: $(uname -r)   $(uname -m)"

# ---------------------------------------------------------------------
abschnitt "1. Grundlast"
# ---------------------------------------------------------------------
uptime
echo
echo "Arbeitsspeicher:"
free -h | sed 's/^/  /'
echo
echo "Load-Durchschnitt pro Kern (Kerne: $(nproc)):"
awk -v k="$(nproc)" '{printf "  1min: %.3f  5min: %.3f  15min: %.3f  (pro Kern)\n", $1/k, $2/k, $3/k}' /proc/loadavg

# ---------------------------------------------------------------------
abschnitt "2. CPU-Frequenz und Powermanagement"
# ---------------------------------------------------------------------
# Frequenzskalierung ist eine klassische Jitter-Quelle: Wechselt die
# Taktrate, ändern sich Ausführungszeiten und damit die Latenz beim
# Erfassen von GPIO-Flanken.
if [ -d /sys/devices/system/cpu/cpu0/cpufreq ]; then
    echo "Governor:      $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
    echo "Aktuell:       $(( $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null) / 1000 )) MHz"
    echo "Minimum:       $(( $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq 2>/dev/null) / 1000 )) MHz"
    echo "Maximum:       $(( $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null) / 1000 )) MHz"
    echo "Verfügbar:     $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null)"
    echo
    echo "Frequenz je Kern:"
    for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq; do
        kern=$(echo "$c" | grep -o 'cpu[0-9]*' | head -1)
        echo "  $kern: $(( $(cat "$c" 2>/dev/null) / 1000 )) MHz"
    done
else
    echo "Keine cpufreq-Schnittstelle vorhanden."
fi

# ---------------------------------------------------------------------
abschnitt "3. Temperatur und Drosselung"
# ---------------------------------------------------------------------
# Drosselung wegen Hitze oder Unterspannung verändert die Taktrate
# abrupt - und Unterspannung ist zugleich ein Verdächtiger für
# Empfangsstörungen am DCF77-Modul.
if command -v vcgencmd >/dev/null 2>&1; then
    echo "Temperatur:    $(vcgencmd measure_temp 2>/dev/null)"
    echo "Kernspannung:  $(vcgencmd measure_volts core 2>/dev/null)"
    T=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
    echo "Throttled:     $T"
    if [ "$T" = "0x0" ]; then
        echo "               -> keinerlei Drosselung seit dem Start"
    else
        echo "               -> ACHTUNG, Bits bedeuten:"
        echo "                  0x1 Unterspannung jetzt, 0x2 Takt gedrosselt jetzt"
        echo "                  0x4 gedrosselt jetzt,    0x8 Temperaturgrenze jetzt"
        echo "                  0x10000 Unterspannung aufgetreten"
        echo "                  0x40000 Drosselung aufgetreten"
        echo "                  0x80000 Temperaturgrenze aufgetreten"
    fi
else
    echo "vcgencmd nicht verfügbar."
    [ -r /sys/class/thermal/thermal_zone0/temp ] && \
      echo "Temperatur: $(( $(cat /sys/class/thermal/thermal_zone0/temp) / 1000 )) Grad"
fi

# ---------------------------------------------------------------------
abschnitt "4. Laufende Dienste"
# ---------------------------------------------------------------------
# Jeder zusätzliche Dienst ist potenziell Last, Angriffsfläche und
# Jitter-Quelle. Auf einem reinen Zeitserver sollte die Liste kurz sein.
ANZ=$(systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null | wc -l)
echo "Anzahl laufender Dienste: $ANZ"
echo
systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null \
  | awk '{print "  " $1}'

# ---------------------------------------------------------------------
abschnitt "5. CPU-Verbrauch der Projektdienste"
# ---------------------------------------------------------------------
# Aufschlussreich ist das Verhältnis von CPU-Zeit zu Laufzeit: Ein Wert
# nahe der Laufzeit bedeutet Dauerbeschäftigung.
for d in dcf77-decoder dcf77_ha_status chrony fail2ban; do
    if systemctl is-active --quiet "$d" 2>/dev/null; then
        CPU=$(systemctl show "$d" -p CPUUsageNSec --value 2>/dev/null)
        SINCE=$(systemctl show "$d" -p ActiveEnterTimestamp --value 2>/dev/null)
        if [ -n "$CPU" ] && [ "$CPU" != "[not set]" ]; then
            SEK=$(( CPU / 1000000000 ))
            LAUF=$(( $(date +%s) - $(date -d "$SINCE" +%s 2>/dev/null || echo "$(date +%s)") ))
            if [ "$LAUF" -gt 0 ]; then
                PROZ=$(awk -v c="$SEK" -v l="$LAUF" 'BEGIN{printf "%.1f", c*100/l}')
                printf "  %-20s CPU %6ds bei %7ds Laufzeit  = %5s%% eines Kerns\n" \
                       "$d" "$SEK" "$LAUF" "$PROZ"
            fi
        fi
    fi
done

# ---------------------------------------------------------------------
abschnitt "6. Top-Prozesse nach CPU"
# ---------------------------------------------------------------------
ps -eo pcpu,pmem,comm --sort=-pcpu --no-headers 2>/dev/null | head -8 \
  | awk '{printf "  %5s%% CPU  %5s%% RAM  %s\n", $1, $2, $3}'

# ---------------------------------------------------------------------
abschnitt "7. Kontextwechsel und Interrupts"
# ---------------------------------------------------------------------
# Hohe Werte bedeuten, dass der Scheduler häufig umschaltet. Genau das
# erzeugt Latenzschwankungen beim Erfassen von GPIO-Flanken.
if command -v vmstat >/dev/null 2>&1; then
    echo "Messung über 5 Sekunden:"
    vmstat 5 2 | tail -1 | awk '{
        printf "  Interrupts/s:      %s\n", $11
        printf "  Kontextwechsel/s:  %s\n", $12
        printf "  CPU: user %s%%  system %s%%  idle %s%%  iowait %s%%\n", $13, $14, $15, $16
    }'
else
    echo "vmstat nicht verfügbar (Paket: procps)"
fi

# ---------------------------------------------------------------------
abschnitt "8. Interrupt-Quellen"
# ---------------------------------------------------------------------
echo "Die aktivsten Interruptquellen:"
awk 'NR>1 {s=0; for(i=2;i<=NF-2;i++) if($i ~ /^[0-9]+$/) s+=$i;
     if(s>1000) printf "  %12d  %s\n", s, substr($0, index($0,$(NF-1)))}' /proc/interrupts \
  | sort -rn | head -8

# ---------------------------------------------------------------------
abschnitt "9. Kernel-Parameter mit Zeitbezug"
# ---------------------------------------------------------------------
echo "Boot-Parameter:"
cat /proc/cmdline 2>/dev/null | tr ' ' '\n' | sed 's/^/  /'
echo
echo "Taktquelle (clocksource):"
echo "  aktuell:   $(cat /sys/devices/system/clocksource/clocksource0/current_clocksource 2>/dev/null)"
echo "  verfügbar: $(cat /sys/devices/system/clocksource/clocksource0/available_clocksource 2>/dev/null)"
echo
echo "Relevante sysctl-Werte:"
for p in kernel.sched_rt_runtime_us vm.swappiness vm.dirty_ratio \
         vm.dirty_background_ratio vm.dirty_writeback_centisecs; do
    echo "  $p = $(sysctl -n $p 2>/dev/null)"
done

# ---------------------------------------------------------------------
abschnitt "10. Schreiblast auf der SD-Karte"
# ---------------------------------------------------------------------
# Schreibzugriffe sind Verschleiß und können den Prozess blockieren.
if [ -r /proc/diskstats ]; then
    awk '$3 ~ /^mmcblk0$/ {
        printf "  Gelesen:     %.1f MB\n", $6*512/1048576
        printf "  Geschrieben: %.1f MB\n", $10*512/1048576
        printf "  (seit dem Start des Systems)\n"
    }' /proc/diskstats
fi
echo
echo "Größe der chrony-Logs:"
du -sh /var/log/chrony 2>/dev/null | sed 's/^/  /' || echo "  nicht lesbar"
echo "Größe des Journals:"
journalctl --disk-usage 2>/dev/null | sed 's/^/  /'

echo
echo "==============================================================="
echo "  Ende der Analyse"
echo "==============================================================="
