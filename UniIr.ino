/*
 * Protocol over Bluetooth (lines, '\n'):
 *   PING -> PONG          HB -> (silent, resets controller timer)
 *   CAP  -> CAPTURING / CODE.. / STATE.. / RAW.. / TIMEOUT
 *   SEND CODE <proto> <bits> <valHex>     -> SENT
 *   SEND STATE <proto> <nbytes> <b..>     -> SENT
 *   LOADRAW <len> / D <us..> / FIRE       -> ACK / ACK / SENT   (manual raw)
 *   LEN? -> LEN <n>
 *   AUTOCFG <timeoutSec>                  -> ACK   (clear lists, set timeout)
 *   AUTOACT  <spec>                       -> ACK   (append activity command)
 *   AUTOIDLE <spec>                       -> ACK   (append idle command)
 *   AUTOSAVE -> ACK     AUTOCLEAR -> ACK     AUTOSHOW -> CFG.. / ACT.. / IDLE..
 *   (async) PIR 1 / PIR 0
 * <spec> = "CODE <proto> <bits> <valHex>" or "STATE <proto> <nbytes> <b..>".
 */

#include <Arduino.h>
#include "BluetoothSerial.h"
#include <Preferences.h>
#include <IRremoteESP8266.h>
#include <IRsend.h>
#include <IRrecv.h>
#include <IRutils.h>

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED)
#error Bluetooth is not enabled. Select a classic ESP32 (WROOM) board.
#endif
#if !defined(CONFIG_BT_SPP_ENABLED)
#error Serial Bluetooth (SPP) not available. Select a classic ESP32 (WROOM) board.
#endif

const uint16_t kRecvPin = 35;
const uint16_t kSendPin = 32;
const uint16_t kPirPin  = 34;

const uint16_t kCaptureBufferSize = 1024;
const uint8_t  kTimeout           = 50;
const uint16_t kMinUnknownSize    = 12;
const uint8_t  kFrequency         = 38;
const unsigned long CAPTURE_TIMEOUT_MS    = 20000UL;
const unsigned long CONTROLLER_TIMEOUT_MS = 12000UL;

BluetoothSerial SerialBT;
IRrecv  irrecv(kRecvPin, kCaptureBufferSize, kTimeout, true);
IRsend  irsend(kSendPin);
Preferences prefs;
decode_results results;

// ---- autonomous command lists (persisted in NVS) ----------------------
#define MAX_ACT  4
#define MAX_IDLE 4
String   actList[MAX_ACT];
String   idleList[MAX_IDLE];
int      nAct = 0, nIdle = 0;
uint32_t autoTimeoutMs = 600000UL;
bool     autoEnabled   = false;       // standalone allowed? toggled from the app, persisted

// ---- runtime state -----------------------------------------------------
bool          pirState   = false;
unsigned long lastMotion = 0;
bool          autoOn     = false;     // assumed device state while standalone
unsigned long lastHB     = 0;
bool          hbSeen      = false;
bool          wasPresent  = true;     // for present->absent edge detection

// ---- raw manual-send buffer -------------------------------------------
#define RAW_MAX 600
uint16_t rawBuf[RAW_MAX];
uint16_t rawIdx = 0;

// ---- line reader -------------------------------------------------------
char     lineBuf[300];
uint16_t linePos = 0;

bool readLineBT() {
  while (SerialBT.available()) {
    char c = SerialBT.read();
    if (c == '\r') continue;
    if (c == '\n') { lineBuf[linePos] = '\0'; linePos = 0; return true; }
    if (linePos < sizeof(lineBuf) - 1) lineBuf[linePos++] = c;
    else linePos = 0;
  }
  return false;
}

long ti(char *s) { return s ? atol(s) : 0; }

// ---- NVS ---------------------------------------------------------------
void loadConfig() {
  prefs.begin("irauto", true);
  autoTimeoutMs = (uint32_t) prefs.getUInt("to", 600) * 1000UL;
  autoEnabled = prefs.getBool("en", false);
  nAct  = prefs.getInt("na", 0); if (nAct  > MAX_ACT)  nAct  = MAX_ACT;
  nIdle = prefs.getInt("ni", 0); if (nIdle > MAX_IDLE) nIdle = MAX_IDLE;
  char k[6];
  for (int i = 0; i < nAct;  i++) { snprintf(k, sizeof(k), "a%d", i); actList[i]  = prefs.getString(k, ""); }
  for (int i = 0; i < nIdle; i++) { snprintf(k, sizeof(k), "i%d", i); idleList[i] = prefs.getString(k, ""); }
  prefs.end();
}

void saveConfig() {
  prefs.begin("irauto", false);
  prefs.clear();
  prefs.putUInt("to", autoTimeoutMs / 1000UL);
  prefs.putBool("en", autoEnabled);
  prefs.putInt("na", nAct);
  prefs.putInt("ni", nIdle);
  char k[6];
  for (int i = 0; i < nAct;  i++) { snprintf(k, sizeof(k), "a%d", i); prefs.putString(k, actList[i]); }
  for (int i = 0; i < nIdle; i++) { snprintf(k, sizeof(k), "i%d", i); prefs.putString(k, idleList[i]); }
  prefs.end();
}

// ---- IR send from a text spec -----------------------------------------
void fireSpec(char *spec) {
  char *t = strtok(spec, " ");
  if (!t) return;
  if (!strcmp(t, "CODE")) {
    decode_type_t p   = (decode_type_t) ti(strtok(NULL, " "));
    uint16_t      bit = ti(strtok(NULL, " "));
    char         *vh  = strtok(NULL, " ");
    uint64_t      val = vh ? strtoull(vh, NULL, 16) : 0;
    irsend.send(p, val, bit);
  } else if (!strcmp(t, "STATE")) {
    decode_type_t p  = (decode_type_t) ti(strtok(NULL, " "));
    uint16_t      nb = ti(strtok(NULL, " "));
    uint8_t st[64]; uint16_t n = 0; char *b;
    while (n < nb && n < 64 && (b = strtok(NULL, " "))) st[n++] = (uint8_t) strtoul(b, NULL, 16);
    irsend.send(p, st, n);
  }
}

void fireList(String *list, int n, const char *tag) {
  Serial.print(F("AUTO: firing ")); Serial.print(tag);
  Serial.print(F(" (")); Serial.print(n); Serial.println(F(" cmd)"));
  char buf[280];
  for (int i = 0; i < n; i++) {
    list[i].toCharArray(buf, sizeof(buf));
    fireSpec(buf);
    delay(60);
  }
}

// ---- capture -----------------------------------------------------------
void doCapture() {
  SerialBT.println(F("CAPTURING"));
  irrecv.resume();
  unsigned long start = millis();
  while (millis() - start < CAPTURE_TIMEOUT_MS) {
    if (!irrecv.decode(&results)) { delay(1); continue; }
    decode_type_t proto = results.decode_type;
    if (proto == UNKNOWN) {
      uint16_t len  = getCorrectedRawLength(&results);
      uint16_t *raw = resultToRawArray(&results);
      SerialBT.print(F("RAW ")); SerialBT.print(len);
      for (uint16_t i = 0; i < len; i++) { SerialBT.print(' '); SerialBT.print(raw[i]); }
      SerialBT.println();
      delete[] raw;
    } else if (hasACState(proto)) {
      uint16_t nbytes = results.bits / 8;
      SerialBT.print(F("STATE ")); SerialBT.print((int)proto); SerialBT.print(' ');
      SerialBT.print(typeToString(proto)); SerialBT.print(' '); SerialBT.print(nbytes);
      for (uint16_t i = 0; i < nbytes; i++) {
        SerialBT.print(' ');
        if (results.state[i] < 0x10) SerialBT.print('0');
        SerialBT.print(results.state[i], HEX);
      }
      SerialBT.println();
    } else {
      SerialBT.print(F("CODE ")); SerialBT.print((int)proto); SerialBT.print(' ');
      SerialBT.print(typeToString(proto)); SerialBT.print(' ');
      SerialBT.print(results.bits); SerialBT.print(' ');
      SerialBT.println(uint64ToString(results.value, 16));
    }
    irrecv.resume();
    return;
  }
  SerialBT.println(F("TIMEOUT"));
}

// ---- command dispatch --------------------------------------------------
void handleCommand() {
  if (!strcmp(lineBuf, "PING")) { SerialBT.println(F("PONG")); return; }
  if (!strcmp(lineBuf, "HB"))   { lastHB = millis(); hbSeen = true; SerialBT.println(F("HBOK")); return; }
  if (!strcmp(lineBuf, "CAP"))  { doCapture(); return; }

  if (!strncmp(lineBuf, "SEND ", 5)) { fireSpec(lineBuf + 5); SerialBT.println(F("SENT")); return; }

  if (!strcmp(lineBuf, "LEN?"))        { SerialBT.print(F("LEN ")); SerialBT.println(rawIdx); return; }
  if (!strncmp(lineBuf, "LOADRAW", 7)) { rawIdx = 0; SerialBT.println(F("ACK")); return; }
  if (lineBuf[0] == 'D' && lineBuf[1] == ' ') {
    char *v = strtok(lineBuf + 2, " ");
    while (v) { if (rawIdx < RAW_MAX) rawBuf[rawIdx++] = (uint16_t) atol(v); v = strtok(NULL, " "); }
    SerialBT.println(F("ACK")); return;
  }
  if (!strcmp(lineBuf, "FIRE")) {
    if (rawIdx == 0) { SerialBT.println(F("ERR_EMPTY")); return; }
    irsend.sendRaw(rawBuf, rawIdx, kFrequency);
    SerialBT.println(F("SENT")); return;
  }

  // provisioning
  if (!strncmp(lineBuf, "AUTOCFG ", 8)) {
    autoTimeoutMs = (uint32_t) ti(lineBuf + 8) * 1000UL;
    nAct = 0; nIdle = 0;
    SerialBT.println(F("ACK")); return;
  }
  if (!strncmp(lineBuf, "AUTOACT ", 8)) {
    if (nAct < MAX_ACT) actList[nAct++] = String(lineBuf + 8);
    SerialBT.println(F("ACK")); return;
  }
  if (!strncmp(lineBuf, "AUTOIDLE ", 9)) {
    if (nIdle < MAX_IDLE) idleList[nIdle++] = String(lineBuf + 9);
    SerialBT.println(F("ACK")); return;
  }
  if (!strncmp(lineBuf, "AUTOEN ", 7)) {
    autoEnabled = ti(lineBuf + 7) != 0;
    if (!autoEnabled) autoOn = false;
    saveConfig();
    Serial.print(F("AUTO: standalone ")); Serial.println(autoEnabled ? F("ENABLED") : F("DISABLED"));
    SerialBT.println(F("ACK")); return;
  }
  if (!strcmp(lineBuf, "AUTOSAVE"))  { saveConfig(); SerialBT.println(F("ACK")); return; }
  if (!strcmp(lineBuf, "AUTOCLEAR")) { nAct = 0; nIdle = 0; autoOn = false; saveConfig(); SerialBT.println(F("ACK")); return; }
  if (!strcmp(lineBuf, "AUTOSHOW")) {
    SerialBT.print(F("CFG ")); SerialBT.print(autoTimeoutMs / 1000UL);
    SerialBT.print(' '); SerialBT.print(nAct); SerialBT.print(' '); SerialBT.println(nIdle);
    return;
  }

  SerialBT.println(F("ERR"));
}

// ---- PIR + autonomous --------------------------------------------------
void updatePir() {
  bool m = digitalRead(kPirPin);
  if (m != pirState) {
    pirState = m;
    SerialBT.print(F("PIR ")); SerialBT.println(m ? 1 : 0);
  }
  if (m) lastMotion = millis();
}

void autonomousTick() {
  if (!autoEnabled || (nAct == 0 && nIdle == 0)) return;
  unsigned long now = millis();
  if (!autoOn && pirState) {
    Serial.println(F("AUTO: motion detected"));
    fireList(actList, nAct, "activity");
    autoOn = true;
  } else if (autoOn && (now - lastMotion > autoTimeoutMs)) {
    Serial.println(F("AUTO: idle timeout"));
    fireList(idleList, nIdle, "idle");
    autoOn = false;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(kPirPin, INPUT);
  SerialBT.begin("UniIr");
  irrecv.setUnknownThreshold(kMinUnknownSize);
  irrecv.enableIRIn();
  irsend.begin();
  loadConfig();
  Serial.print(F("Ready. activity cmds=")); Serial.print(nAct);
  Serial.print(F(" idle cmds=")); Serial.print(nIdle);
  Serial.print(F(" timeout(s)=")); Serial.println(autoTimeoutMs / 1000UL);
  SerialBT.println(F("BOOT"));
}

void loop() {
  updatePir();
  if (readLineBT()) handleCommand();

  bool present = hbSeen && (millis() - lastHB < CONTROLLER_TIMEOUT_MS);

  // present -> absent edge: the PC's heartbeat stopped.
  if (wasPresent && !present) {
    // Force any lingering Bluetooth client off. macOS often won't tear down the
    // old RFCOMM session itself, so the ESP32 must, or the next app launch
    // reconnects onto a dead channel (looks connected, no data) until re-pair.
    if (SerialBT.hasClient()) {
      SerialBT.disconnect();
      Serial.println(F("BT: heartbeat lost, dropped client for a clean reconnect"));
    }
    if (autoEnabled) {
      autoOn = true;
      lastMotion = millis();
      Serial.println(F("AUTO: standalone start - assuming ON, idle countdown"));
    }
  }
  wasPresent = present;

  if (!present) autonomousTick();
}