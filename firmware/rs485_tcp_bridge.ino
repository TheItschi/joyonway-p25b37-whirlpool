/*
 * RS485 <-> TCP Bridge for Joyonway Spa Controllers (ESP8266 + MAX485)
 * ---------------------------------------------------------------------
 * Transparent, protocol-agnostic bridge: every byte received on the RS485
 * bus is forwarded to the connected TCP client, and every byte received
 * from the TCP client is forwarded to the RS485 bus. All Joyonway framing,
 * escaping and CRC logic lives in the Python backend, NOT here.
 *
 * Tested on: Wemos D1 mini + MAX485 breakout, Joyonway P25B37 / PB554.
 *
 * Wiring (UART0 is swapped, USE_SWAPPED_UART = true):
 *   D1 (GPIO5)   -> MAX485 DE + RE (tied together)  direction control
 *   D8 (GPIO15)  -> MAX485 DI                       UART TX (swapped)
 *   D7 (GPIO13)  <- MAX485 RO                       UART RX (swapped)
 *                   via divider: RO -1k- D7 -2k- GND (RO is 5 V, ESP is 3.3 V)
 *   D8 (GPIO15)  -> 1k -> GND   MANDATORY pull-down, see boot note below
 *   MAX485 A/B   -> controller CN23 (COM1) A/B
 *   MAX485 GND   -> controller GND (common ground)
 *   MAX485 VCC   -> 5 V (regulator fed from CN23 V+ = 12 V)
 *
 * Why the swap: GPIO1/GPIO3 are shared with the USB-serial chip and showed
 * unreliable behaviour. With Serial.swap() UART0 moves to GPIO15/GPIO13.
 *
 * BOOT NOTE: GPIO15 is a strap pin and MUST be LOW at reset, otherwise the
 * ESP8266 does not boot from flash (and cannot be flashed). Many MAX485
 * modules have a pull-up on DI. The 1k pull-down on D8 overrides it.
 * DE/RE (D1) is driven LOW in setup(); if DE/RE reads 5 V on a multimeter,
 * the ESP is not running (or D1 is not connected).
 *
 * For flashing: disconnect D8/D7/D1 from the MAX485 (or at least DI) and
 * power the ESP over USB only.
 *
 * DIAG_MODE: for the loopback test (D8 <-> D7 bridged, MAX485 detached).
 * MUST be false in normal operation: the diagnostic text lines would end up
 * in the byte stream to the backend.
 */

#include <ESP8266WiFi.h>
#include <ESP8266mDNS.h>

// ---- Configuration -----------------------------------------------------

const char *WIFI_SSID = "YOUR_WIFI_SSID";
const char *WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// Static IP is strongly recommended so the backend config never goes stale.
const bool USE_STATIC_IP = true;
IPAddress STATIC_IP(192, 168, 100, 210);
IPAddress GATEWAY(192, 168, 100, 1);
IPAddress SUBNET(255, 255, 255, 0);

const uint16_t TCP_PORT = 8899;      // matches ha-joyonway default
const uint32_t RS485_BAUD = 38400;   // fixed by the Joyonway protocol (8N1)
const uint8_t DE_RE_PIN = 5;         // D1 - MAX485 direction control

const bool USE_SWAPPED_UART = true;  // true: TX = D8/GPIO15, RX = D7/GPIO13
const bool DIAG_MODE = false;        // loopback test only, see header

const char *MDNS_HOSTNAME = "hottub-bridge";

// Only one client is expected (the Python backend); a second connection
// simply pre-empts the first, mirroring how the Elfin EW11 behaves.
WiFiServer tcpServer(TCP_PORT);
WiFiClient tcpClient;

// ---- Direction control ---------------------------------------------------

inline void rs485TransmitMode() { digitalWrite(DE_RE_PIN, HIGH); }
inline void rs485ReceiveMode() { digitalWrite(DE_RE_PIN, LOW); }

void setup() {
  pinMode(DE_RE_PIN, OUTPUT);
  rs485ReceiveMode();

  Serial.begin(RS485_BAUD, SERIAL_8N1);
  if (USE_SWAPPED_UART) {
    Serial.swap();   // UART0 -> GPIO15 (TX) / GPIO13 (RX)
  }

  WiFi.mode(WIFI_STA);
  WiFi.setSleepMode(WIFI_NONE_SLEEP);  // no modem sleep: lower latency, stable link
  if (USE_STATIC_IP) {
    WiFi.config(STATIC_IP, GATEWAY, SUBNET);
  }
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
  }

  MDNS.begin(MDNS_HOSTNAME);
  tcpServer.begin();
  tcpServer.setNoDelay(true);
}

void loop() {
  MDNS.update();

  // Accept a new client, replacing any existing one.
  if (tcpServer.hasClient()) {
    WiFiClient newClient = tcpServer.accept();
    if (tcpClient) {
      tcpClient.stop();
    }
    tcpClient = newClient;
    tcpClient.setNoDelay(true);
    if (DIAG_MODE) {
      tcpClient.print("HELLO v3 swap=");
      tcpClient.print(USE_SWAPPED_UART ? "1" : "0");
      tcpClient.print("\n");
    }
  }

  // RS485 -> TCP
  static uint8_t rxBuf[256];
  size_t rxLen = 0;
  while (Serial.available() && rxLen < sizeof(rxBuf)) {
    rxBuf[rxLen++] = Serial.read();
  }
  if (rxLen > 0 && tcpClient && tcpClient.connected()) {
    tcpClient.write(rxBuf, rxLen);
  }

  // TCP -> RS485
  if (tcpClient && tcpClient.connected() && tcpClient.available()) {
    static uint8_t txBuf[256];
    size_t txLen = 0;
    while (tcpClient.available() && txLen < sizeof(txBuf)) {
      txBuf[txLen++] = tcpClient.read();
    }
    if (txLen > 0) {
      rs485TransmitMode();
      Serial.write(txBuf, txLen);
      Serial.flush();            // wait until the UART FIFO has finished sending
      if (DIAG_MODE) {
        delay(20);               // let the loopback bytes arrive
      }
      rs485ReceiveMode();
      if (DIAG_MODE) {
        tcpClient.printf("[tcp_rx=%u uart_rx_pending=%d]\n",
                         (unsigned)txLen, Serial.available());
      }
    }
  }

  // Reconnect WiFi if it drops.
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(500);
  }
}
