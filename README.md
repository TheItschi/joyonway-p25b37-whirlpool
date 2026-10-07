# Whirlpool-Steuerung für Joyonway P25B37 mit Domoticz-Integration

Eine lokale, cloud-unabhängige Web-App zur Steuerung eines Whirlpools mit
Joyonway-P25B37-Steuerung (getestet mit Touchpad PB554, Board-/Panel-Version
1.8). Kommunikation läuft komplett im lokalen Netz über RS485 — keine
Cloud, kein Internet, keine Joyonway-App nötig.

## Architektur

```
┌─────────────┐   RS485 (38400 8N1)   ┌──────────────────┐   TCP    ┌──────────────┐   HTTP/WS   ┌─────────┐
│ P25B37      │◄─────────────────────►│ ESP8266 + MAX485 │◄────────►│ Python       │◄───────────►│ Browser │
│ Steuerbox   │   (CN23/CN24-Port)    │ RS485↔TCP-Bridge │  :8899   │ Backend      │   :8000     │ (Web-UI)│
└─────────────┘                       └──────────────────┘          └──────────────┘             └─────────┘
```

- **ESP8266 + MAX485** (`firmware/`): transparente RS485↔TCP-Bridge, kennt
  das Joyonway-Protokoll nicht — leitet nur Bytes weiter. Ersetzt ein
  kommerzielles Bridge-Modul wie den Elfin EW11.
- **Python-Backend** (`backend/`): enthält die gesamte Protokoll-Logik
  (Framing, Escape-Dekodierung, CRC-32, Byte-Map für P25B37/P25B85),
  portiert aus dem MIT-lizenzierten Projekt
  [`alexbde/ha-joyonway`](https://github.com/alexbde/ha-joyonway) und von
  Home-Assistant-Abhängigkeiten befreit. Läuft am besten auf deinem
  bestehenden Windows-Heim-Server (dort, wo auch Domoticz läuft).
- **Web-Frontend** (`frontend/`): einfaches Dashboard (HTML/CSS/JS,
  fetch/WebSocket), wird direkt vom Backend ausgeliefert.

## ⚠️ Sicherheitshinweis

> Physische Eingriffe in die Steuerbox können Whirlpool, Steuerung oder
> andere Elektrik beschädigen. Diese Anleitung dient nur der
> Community-Referenz — auf eigenes Risiko. **Vor dem Öffnen der
> Steuerbox immer die Hauptsicherung des Whirlpools abschalten.**

## 1. Hardware: RS485-Tap einrichten

### Benötigte Teile

- ESP8266-Board (Wemos D1 Mini)
- MAX485-Modul (TTL↔RS485, mit getrennten DE/RE/DI/RO-Pins, **kein**
  Auto-Direction-Modul)
- 5-V-Regler (z. B. Festspannungsregler/Step-down), gespeist aus CN23 V+
- Widerstände: 1 kΩ (2×) und 2 kΩ (1×)
- 4-poliges Anschlusskabel für CN23 (COM1..3, was frei ist)

### Anschluss

1. Whirlpool-Hauptsicherung ausschalten, Steuerbox öffnen.
2. COM-Port **CN23** (COM1..3, was frei ist) suchen. Pinbelegung (4-polig): **V+**, **B**,
   **A**, **GND** (laut Deckelschema der P25B85, gemessen: V+ = 12 V).
3. Verkabelung:

   | Quelle | Ziel | Hinweis |
   |---|---|---|
   | CN23 A | MAX485 A | |
   | CN23 B | MAX485 B | bei Stille/CRC-Müll A/B tauschen |
   | CN23 GND | MAX485 GND + ESP GND | gemeinsame Masse |
   | CN23 V+ (12 V) | 5-V-Regler Eingang | Regler speist ESP **und** MAX485 VCC |
   | D1 (GPIO5) | MAX485 DE **und** RE (gebrückt) | Richtungssteuerung |
   | D8 (GPIO15) | MAX485 DI | UART TX (getauscht) |
   | D8 (GPIO15) | 1 kΩ nach GND | **Pflicht**, siehe Boot-Hinweis |
   | MAX485 RO | 1 kΩ → D7 (GPIO13) → 2 kΩ → GND | Spannungsteiler, RO liefert 5 V, ESP verträgt 3,3 V |

   ```
   CN23 V+ ──► 5V-Regler ──► ESP 5V-Pin + MAX485 VCC
   CN23 A  ──► MAX485 A        D1 ──► DE+RE
   CN23 B  ──► MAX485 B        D8 ──┬─► DI
   CN23 GND ─► GND (alle)           └─ 1k ─ GND
                               D7 ◄──┬─ 1k ─ RO
                                     └─ 2k ─ GND
   ```

### Wichtige Hardware-Hinweise (aus der Inbetriebnahme)

- **Warum D8/D7 statt TX/RX?** Die Standard-UART-Pins GPIO1/GPIO3 hingen am
  USB-Seriell-Chip und lieferten im Loopback-Test keine Daten.
  `Serial.swap()` legt UART0 auf GPIO15 (TX) / GPIO13 (RX). Das ist beim
  ESP8266 die einzige Alternative für den Hardware-UART.
- **GPIO15 (D8) ist ein Boot-Strap-Pin** und muss beim Reset **LOW** sein.
  Viele MAX485-Module haben einen Pull-up am DI-Eingang. Der ESP bootet dann
  nicht mehr und lässt sich auch nicht flashen (`Failed to connect: Timed out
  waiting for packet header`). Abhilfe: 1 kΩ von D8 nach GND (siehe Tabelle).
- **DE/RE im Leerlauf muss ca. 0 V haben.** Zeigt DE/RE 5 V, läuft der ESP
  nicht (oder D1 ist nicht angeschlossen). Dann sendet der MAX485 dauerhaft
  und stört den Bus: **sofort A/B abklemmen** und die Ursache beheben.
- MAX485 braucht **5 V** (nicht 3,3 V). Die V+-Leitung der Steuerung (12 V)
  nur über einen Regler verwenden, nie direkt am ESP/MAX485.
- Bei angeschlossenem USB-Kabel ist die Bridge ohne Einschränkung nutzbar,
  da die UART jetzt auf D8/D7 liegt. Zum **Flashen** trotzdem D8 (DI) und D7
  (RO) abziehen und den 5-V-Regler trennen.

## 2. Firmware flashen

1. Arduino IDE öffnen, ESP8266-Board-Paket installieren (Board:
   „LOLIN(WEMOS) D1 R2 & mini“).
2. `firmware/rs485_tcp_bridge.ino` öffnen.
3. `WIFI_SSID`, `WIFI_PASSWORD` anpassen. Static IP ist auf
   `192.168.100.210` gesetzt (bei Bedarf ändern, dann auch
   `HOTTUB_BRIDGE_HOST` im Backend anpassen).
4. Vor dem Flashen D8/D7 vom MAX485 trennen (siehe Hinweise oben), flashen.
5. Verdrahten und über den Regler versorgen. Die Bridge ist unter
   `192.168.100.210:8899` per TCP erreichbar.

### Test ohne Bus (Loopback)

Prüft ESP, UART und TCP-Pfad unabhängig vom Controller:

1. In der Firmware `DIAG_MODE = true`, flashen.
2. MAX485 abziehen, **D8 mit D7 brücken**, Backend-Container stoppen
   (`docker compose stop`).
3. Test:
   ```
   docker run --rm python:3.12-slim python -c "import socket,time;s=socket.create_connection(('192.168.100.210',8899));s.settimeout(1);print(s.recv(64));s.send(b'ABC');time.sleep(.5);print(s.recv(64))"
   ```
   Erwartet: `HELLO v3 swap=1`, dann `[tcp_rx=3 uart_rx_pending=3]` und `ABC`.
4. **Danach `DIAG_MODE = false` setzen und neu flashen.**

### Fehlersuche

| Symptom | Ursache / Maßnahme |
|---|---|
| Backend „verbunden“, aber `data: null`, alle `rx_frame_stats` = 0 | DE/RE messen (soll ca. 0 V), RO im Leerlauf (soll High sein), A/B tauschen, S1/A1 und CN23-Belegung gegen Deckelschema prüfen |
| ESP bootet nicht, flasht nicht | GPIO15 beim Reset zu hoch: 1 kΩ D8→GND, DI beim Flashen abziehen |
| esptool: `Timed out waiting for packet header` | TX/RX-Brücke noch gesteckt, USB-Kabel nur Laden, Regler gleichzeitig angeschlossen |
| Echo kommt nicht zurück | Backend-Container hält die einzige TCP-Verbindung: `docker compose stop` |
| `crc_error` zählt hoch | A/B vertauscht oder Pegel/Leitung gestört |
| PowerShell blockt `$s.Write([byte[]]…)` | Virenscanner-Fehlalarm, stattdessen Docker-Variante verwenden |

## 3. Backend einrichten

Auf deinem Windows-Heim-Server (dort, wo Domoticz läuft) oder auf jedem
anderen dauerhaft laufenden Rechner im selben Netz:

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt

copy .env.example .env
# .env anpassen: HOTTUB_BRIDGE_HOST=192.168.100.210 (deine ESP8266-IP)

python run.py
```

Das Backend läuft danach auf `http://<server-ip>:8000` und liefert dort
auch gleich das Web-Dashboard aus.

> **Hinweis zur Modellwahl:** `HOTTUB_MODEL=P25B37` ist der Default in
> `.env.example`. Solltest du stattdessen einen P25B85 haben, einfach auf
> `P25B85` ändern — beide Adapter sind bereits enthalten.

### Als Windows-Dienst / Autostart

Am einfachsten über den Windows Taskplaner: Aufgabe "Bei Anmeldung"
mit Aktion `python.exe run.py` und Arbeitsverzeichnis `backend/`.

### Alternative: Docker Compose

Backend + Frontend lassen sich auch als ein Container starten (die
ESP8266-Firmware läuft weiterhin separat auf dem ESP8266, nicht im
Container):

```bash
docker compose up -d --build
```

`docker-compose.yml` mappt Host-Port **8050** auf den internen
Container-Port 8000 und liest Bridge-Host/-Port/Modell aus
Umgebungsvariablen:

```yaml
services:
  hottub:
    build: .
    container_name: hottub-web-app
    restart: unless-stopped
    ports:
      - "8050:8000"
    environment:
      HOTTUB_BRIDGE_HOST: "192.168.100.210"   # IP deiner ESP8266-Bridge
      HOTTUB_BRIDGE_PORT: "8899"
      HOTTUB_MODEL: "P25B37"
```

Vor dem Start `HOTTUB_BRIDGE_HOST` auf die tatsächliche IP deiner
ESP8266-Bridge anpassen. Dashboard danach unter
`http://<docker-host>:8050` erreichbar.


## 4. Web-Dashboard nutzen

Im Browser `http://<server-ip>:8050` (Docker) bzw. `:8000` (ohne Docker)
öffnen. 

- Hero-Karte: aktuelle Temperatur, Sollwert, Status (Aus/Bereit/Umwälzung/Heizt), Düsen,
  darunter Zustands-Chips „Ozon auto/manuell“ und „Licht“ (Punkt farbig = an, grau = aus;
  der Licht-Punkt zeigt die eingestellte Lichtfarbe). Bubbles-Deko oben rechts; mit
  `BUBBLES_ANIMATED = true` in `frontend/app.js` steigen sie auf, solange die Düsen laufen.
- Heizung ein/aus, Licht ein/aus (Buttons sind gesperrt, solange keine Bus-Daten ankommen)
- Massagedüsen: Aus / Niedrig / Hoch (P25: zweistufige Pumpe)
- Lichtfarbe (8 Presets), Ozon: Auto/Manuell
- Temperaturverlauf (1 Wert/min, 24 h, im Speicher des Backends; geht beim Container-Neustart verloren)
- Solltemperatur: Slider bzw. −/+, dann „übernehmen“ (ein Bus-Befehl)
- Diagnose (aufklappbar): Modell, Bridge, Spa-Uhr, Frame-Zähler, CRC-Fehler
- Fehlerleiste bei „Bridge nicht erreichbar“ bzw. „Keine Daten vom Bus“

Auf dem iPhone: Seite in Safari öffnen → Teilen → „Zum Home-Bildschirm“.

REST: `GET /api/status`, `GET /api/history`,
`POST /api/heater|jets|light|temperature|ozone/mode|ozone/manual`,
WebSocket `/ws`.

### Düsen und Lichtfarbe: Verhalten
- Die Düsen laufen wie am Original-Panel zyklisch: Aus → Niedrig → Hoch → Aus. Ein Klick auf eine
  Stufe, die nicht direkt folgt, wird als Folge bestätigter Einzelschritte ausgeführt
  (z. B. Hoch → Niedrig = Hoch → Aus → Niedrig). Der letzte Klick gewinnt.
- Standard: `HOTTUB_JETS_DIRECT=1` sendet den Ziel-Frame direkt (am Pool verifiziert). `0` erzwingt die Sequenz Aus → Niedrig → Hoch.
- Jeder Schritt wird gegen die Broadcast-Daten geprüft und bis zu 6× wiederholt (jeweils mit anderer Verzögerung nach dem Sync-Frame, die erfolgreiche steht im Log); Farbwechsel ebenso.
- Pulsierender Button = Befehl läuft noch. Nach 20 s ohne Bestätigung erscheint ein Hinweis.
- Betrieb: Der Umschalter „Zeitgesteuert / Manuell“ steht als erstes Element im Panel „Steuerung“. Im zeitgesteuerten Betrieb entscheidet der Controller selbst (Zeitpläne); „Heizung ein/aus“ ist dann deaktiviert und bleibt auch beim Heizen auf „Heizung ein“ (das Backend lehnt den Befehl mit HTTP 409 ab). Erst im manuellen Betrieb ist die Heizung schaltbar. Es gibt kein automatisches Umschalten mehr.
- Zeitpläne: Karte „Zeitpläne“ speichert benannte Voreinstellungen (2 Heizzeiten, 2 Filterzeiten, Solltemperatur; bis 20 Stück) in `./data/modes.json` (Docker-Volume). Die Zeiteinstellung erscheint nur beim Anlegen/Bearbeiten eines Zeitplans. „Aktivieren“ schreibt Heizzeiten, Filterzeiten und Sollwert nacheinander in den Controller und prüft jeden Schritt; der passende Zeitplan wird als „aktiv“ markiert. Der Controller selbst kennt nur einen aktiven Satz (2+2 Zeitfenster), weitere Zeitpläne sind reine Backend-Daten.
- Diagnose → „Uhrzeit synchronisieren“: setzt die Controller-Uhr auf die Uhrzeit des Browsers (`POST /api/time/sync`), bestätigt anhand der Spa-Uhr (Toleranz 5 s).
- Automatischer Uhrenabgleich: Das Backend prüft alle 30 s die Spa-Uhr und stellt sie bei mehr als 30 s Abweichung auf die Ortszeit (`HOTTUB_TZ`, Standard Europe/Berlin; `HOTTUB_AUTO_TIME_SYNC=0` schaltet ihn ab). Nach einem Versuch wartet er 10 Minuten.
- Diagnose: `docker logs hottub-web-app --tail 100` zeigt je Versuch „confirmed" / „not confirmed".

### Domoticz-Anbindung
Das Backend schickt alle 60 s (`interval_s`) Status und Messwerte per HTTP GET (`/json.htm?...`) an virtuelle Geräte in Domoticz. Konfiguration: `./data/domoticz.json` (wird beim ersten Start als Vorlage angelegt, wird vor jedem Durchlauf neu gelesen, kein Neustart nötig). Ist `url` leer, bleibt die Funktion aus. Ein Wert wird nur übertragen, wenn sein `idx` eingetragen ist und der Wert selbst nicht leer ist. Optional `username`/`password` (Basic Auth).

| Schlüssel in `idx` | Inhalt | Virtuelles Gerät in Domoticz | Aufruf |
|---|---|---|---|
| `temperature` | Wassertemperatur °C | Temperatur | `udevice` svalue |
| `setpoint` | Solltemperatur °C | Temperatur (oder Thermostat-Setpoint) | `udevice` svalue |
| `heater` | Heizung freigegeben | Schalter (An/Aus) | `switchlight` On/Off |
| `heating` | Heizelement läuft | Schalter | `switchlight` |
| `light` | Licht | Schalter | `switchlight` |
| `ozone` | Ozon läuft | Schalter | `switchlight` |
| `jets` | Düsen Aus/Niedrig/Hoch | Wahlschalter (Selector): Aus=0, Niedrig=10, Hoch=20 | `switchlight` Set Level |
| `light_color` | Lichtfarbe (Name) | Text | `udevice` svalue |
| `status` | Aus / Bereit / Umwälzung / Heizt | Text | `udevice` svalue |
| `heater_mode` | auto / manual | Text | `udevice` svalue |
| `ozone_mode` | auto / manual | Text | `udevice` svalue |

Der letzte Durchlauf steht in der Weboberfläche unter Diagnose („Domoticz“), Fehler im Container-Log.

## Lizenz & Attribution

Die Protokoll-Implementierung (`backend/app/protocol.py`,
`backend/app/adapters/`) ist portiert aus
[`alexbde/ha-joyonway`](https://github.com/alexbde/ha-joyonway)
(MIT-Lizenz, siehe `LICENSE-ha-joyonway`). Dank an `@KDy` (Baudrate- und
CRC-Reverse-Engineering), `@KnapTheBuilder` und alle weiteren
Mitwirkenden dieses Projekts — ohne deren Vorarbeit wäre dieses Projekt
ungleich aufwändiger gewesen.

Der übrige Code (ESP8266-Firmware, FastAPI-Backend-Anpassung,
Web-Frontend) steht unter der MIT-Lizenz, siehe `LICENSE`.

Haftungsausschluss: Inoffizielles Hobbyprojekt, nicht mit Joyonway
verbunden. Nutzung auf eigene Gefahr, insbesondere bei Eingriffen in die
Steuerung eines Geräts mit Netzspannung und Wasser.
