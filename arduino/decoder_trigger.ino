/*
 * decoder_trigger.ino
 *
 * Purpose:
 *   Reads a quadrature rotary encoder attached to a conveyor or moving stage
 *   and fires a camera trigger pulse every time the target moves approximately
 *   1 cm (configurable via triggerInterval). A manual push-button can also
 *   fire the trigger immediately for testing or calibration.
 *
 * Wiring summary:
 *   D2  (INT0) — Encoder channel A (Green wire)
 *   D3  (INT1) — Encoder channel B (White wire)
 *   D4          — Manual trigger button (other end to GND)
 *   D9          — Camera trigger output + status LED
 *                 LOW  = trigger active (camera fires)
 *                 HIGH = idle
 *
 * Output (JSON lines at 115200 baud):
 *   Trigger:   {"type":"trigger","source":"encoder","count":118,"trigger":1,"speed_cms":5.20}
 *   Heartbeat: {"type":"speed","speed_cms":5.20,"count":118,"trigger_enabled":true}
 *   Config:    {"type":"config","trigger_enabled":true,"trigger_interval":118,...}
 *   ACK:       {"type":"ack","cmd":"set_trigger_interval","ok":true}
 *   Startup:   {"type":"startup","msg":"Trigger controller started"}
 *
 * Input (JSON lines from host):
 *   {"cmd":"get_config"}
 *   {"cmd":"reset_count"}
 *   {"cmd":"set_trigger_enabled","value":true}
 *   {"cmd":"set_trigger_interval","value":118}
 *   {"cmd":"set_counts_per_cm","value":118.0}
 *   {"cmd":"set_pulse_width_ms","value":20}
 *   {"cmd":"set_speed_report_interval_ms","value":500}
 *   {"cmd":"fire_trigger"}
 */

// ── Pin assignments ──────────────────────────────────────────────────────────
const int encoderA     = 2;   // Encoder channel A (Green wire) — INT0
const int encoderB     = 3;   // Encoder channel B (White wire) — INT1
const int manualButton = 4;   // Push-button to GND; LOW when pressed
const int triggerOut   = 9;   // Camera shutter trigger + LED

// ── Configurable parameters (defaults; overridable via serial commands) ──────
long          triggerInterval        = 118;    // Encoder counts between triggers (≈1 cm)
float         countsPerCm            = 118.0;  // Encoder counts per centimeter of travel
int           pulseWidthMs           = 20;     // Trigger pulse width in milliseconds
unsigned long speedReportIntervalMs  = 500;    // Heartbeat period in milliseconds
bool          triggerEnabled         = true;   // Whether distance-based triggering is active

// ── Speed tracking ───────────────────────────────────────────────────────────
unsigned long lastTriggerTimeMs   = 0;        // millis() at last trigger fire
float         lastSpeedCms        = 0.0;      // Speed at last trigger event

// Time without a trigger after which speed is declared 0 in heartbeat
const unsigned long SPEED_STALE_MS = 2000;

// ── Heartbeat timing ─────────────────────────────────────────────────────────
unsigned long lastSpeedReportMs = 0;

// ── Encoder state (shared with ISR) ─────────────────────────────────────────
volatile long encoderCount = 0;   // Running signed count; updated in ISR
volatile int  lastEncoded  = 0;   // Previous 2-bit AB state for transition lookup

// ── Trigger tracking ─────────────────────────────────────────────────────────
long          lastTriggerCount = 0;   // encoderCount value at the last trigger
unsigned long triggerNumber    = 0;   // Monotonically increasing trigger index

// ── Button debounce ──────────────────────────────────────────────────────────
bool          lastButtonState = HIGH;
unsigned long lastButtonTime  = 0;
const unsigned long debounceMs = 50;

// ── Trigger polarity ─────────────────────────────────────────────────────────
const int TRIGGER_IDLE   = HIGH;
const int TRIGGER_ACTIVE = LOW;

// ── Command input buffer ─────────────────────────────────────────────────────
char cmdBuf[160];
int  cmdLen = 0;


// ── ISR: quadrature decoder ───────────────────────────────────────────────────
void updateEncoder() {
  int A = digitalRead(encoderA);
  int B = digitalRead(encoderB);
  int encoded = (A << 1) | B;
  int sum = (lastEncoded << 2) | encoded;

  if (sum == 0b1101 || sum == 0b0100 || sum == 0b0010 || sum == 0b1011) encoderCount++;
  if (sum == 0b1110 || sum == 0b0111 || sum == 0b0001 || sum == 0b1000) encoderCount--;

  lastEncoded = encoded;
}


// ── Emit helpers ─────────────────────────────────────────────────────────────

void emitConfig() {
  Serial.print(F("{\"type\":\"config\""));
  Serial.print(F(",\"trigger_enabled\":")); Serial.print(triggerEnabled ? F("true") : F("false"));
  Serial.print(F(",\"trigger_interval\":")); Serial.print(triggerInterval);
  Serial.print(F(",\"counts_per_cm\":")); Serial.print(countsPerCm, 2);
  Serial.print(F(",\"pulse_width_ms\":")); Serial.print(pulseWidthMs);
  Serial.print(F(",\"speed_report_interval_ms\":")); Serial.print(speedReportIntervalMs);
  Serial.println(F("}"));
}

void emitAck(const char* cmd, bool ok) {
  Serial.print(F("{\"type\":\"ack\",\"cmd\":\""));
  Serial.print(cmd);
  Serial.print(F("\",\"ok\":"));
  Serial.print(ok ? F("true") : F("false"));
  Serial.println(F("}"));
}


// ── Helper: fire a single trigger pulse ──────────────────────────────────────
void fireTrigger(const char* source, long countValue) {
  unsigned long now = millis();
  float speedCms = 0.0;

  if (lastTriggerTimeMs > 0) {
    unsigned long dt = now - lastTriggerTimeMs;
    if (dt > 0) {
      float distanceCm = (float)triggerInterval / countsPerCm;
      speedCms = distanceCm / (dt / 1000.0f);
    }
  }
  lastTriggerTimeMs = now;
  lastSpeedCms = speedCms;

  digitalWrite(triggerOut, TRIGGER_ACTIVE);
  delay(pulseWidthMs);
  digitalWrite(triggerOut, TRIGGER_IDLE);
  triggerNumber++;

  Serial.print(F("{\"type\":\"trigger\",\"source\":\""));
  Serial.print(source);
  Serial.print(F("\",\"count\":"));
  Serial.print(countValue);
  Serial.print(F(",\"trigger\":"));
  Serial.print(triggerNumber);
  Serial.print(F(",\"speed_cms\":"));
  Serial.print(speedCms, 2);
  Serial.println(F("}"));
}


// ── Command handler ───────────────────────────────────────────────────────────
void handleCommand(const char* buf) {
  char* vp;

  if (strstr(buf, "\"get_config\"")) {
    emitConfig();
    return;
  }

  if (strstr(buf, "\"reset_count\"")) {
    noInterrupts();
    encoderCount = 0;
    interrupts();
    lastTriggerCount = 0;
    lastTriggerTimeMs = 0;
    lastSpeedCms = 0.0;
    emitAck("reset_count", true);
    return;
  }

  if (strstr(buf, "\"set_trigger_enabled\"")) {
    vp = strstr(buf, "\"value\":");
    if (vp) {
      // Skip whitespace after the colon
      vp += 8;
      while (*vp == ' ') vp++;
      triggerEnabled = (*vp != '0' && *vp != 'f' && *vp != 'F');
      // Re-sync lastTriggerCount so we don't burst on re-enable
      noInterrupts();
      lastTriggerCount = encoderCount;
      interrupts();
      emitAck("set_trigger_enabled", true);
      emitConfig();
    }
    return;
  }

  if (strstr(buf, "\"set_trigger_interval\"")) {
    vp = strstr(buf, "\"value\":");
    if (vp) {
      long val;
      if (sscanf(vp + 8, "%ld", &val) == 1 && val > 0) {
        triggerInterval = val;
        emitAck("set_trigger_interval", true);
        emitConfig();
      } else {
        emitAck("set_trigger_interval", false);
      }
    }
    return;
  }

  if (strstr(buf, "\"set_counts_per_cm\"")) {
    vp = strstr(buf, "\"value\":");
    if (vp) {
      float val;
      if (sscanf(vp + 8, "%f", &val) == 1 && val > 0) {
        countsPerCm = val;
        emitAck("set_counts_per_cm", true);
        emitConfig();
      } else {
        emitAck("set_counts_per_cm", false);
      }
    }
    return;
  }

  if (strstr(buf, "\"set_pulse_width_ms\"")) {
    vp = strstr(buf, "\"value\":");
    if (vp) {
      long val;
      if (sscanf(vp + 8, "%ld", &val) == 1 && val > 0 && val <= 500) {
        pulseWidthMs = (int)val;
        emitAck("set_pulse_width_ms", true);
        emitConfig();
      } else {
        emitAck("set_pulse_width_ms", false);
      }
    }
    return;
  }

  if (strstr(buf, "\"set_speed_report_interval_ms\"")) {
    vp = strstr(buf, "\"value\":");
    if (vp) {
      long val;
      if (sscanf(vp + 8, "%ld", &val) == 1 && val >= 100 && val <= 10000) {
        speedReportIntervalMs = (unsigned long)val;
        emitAck("set_speed_report_interval_ms", true);
        emitConfig();
      } else {
        emitAck("set_speed_report_interval_ms", false);
      }
    }
    return;
  }

  if (strstr(buf, "\"fire_trigger\"")) {
    noInterrupts();
    long manualCount = encoderCount;
    interrupts();
    emitAck("fire_trigger", true);
    fireTrigger("serial", manualCount);
    return;
  }
}


// ── setup ─────────────────────────────────────────────────────────────────────
void setup() {
  pinMode(encoderA,     INPUT_PULLUP);
  pinMode(encoderB,     INPUT_PULLUP);
  pinMode(manualButton, INPUT_PULLUP);

  pinMode(triggerOut, OUTPUT);
  digitalWrite(triggerOut, TRIGGER_IDLE);

  int A = digitalRead(encoderA);
  int B = digitalRead(encoderB);
  lastEncoded = (A << 1) | B;

  attachInterrupt(digitalPinToInterrupt(encoderA), updateEncoder, CHANGE);
  attachInterrupt(digitalPinToInterrupt(encoderB), updateEncoder, CHANGE);

  Serial.begin(115200);
  Serial.println(F("{\"type\":\"startup\",\"msg\":\"Trigger controller started\"}"));
  emitConfig();
}


// ── loop ──────────────────────────────────────────────────────────────────────
void loop() {
  // ── Read and process incoming serial commands ────────────────────────────
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmdLen > 0) {
        cmdBuf[cmdLen] = '\0';
        handleCommand(cmdBuf);
        cmdLen = 0;
      }
    } else if (cmdLen < 159) {
      cmdBuf[cmdLen++] = c;
    }
  }

  // ── Safely snapshot the volatile counter ────────────────────────────────
  noInterrupts();
  long countCopy = encoderCount;
  interrupts();

  unsigned long now = millis();

  // ── Distance-based trigger ───────────────────────────────────────────────
  if (triggerEnabled) {
    long moved = labs(countCopy - lastTriggerCount);
    if (moved >= triggerInterval) {
      lastTriggerCount = countCopy;
      fireTrigger("encoder", countCopy);
    }
  } else {
    // Keep lastTriggerCount in sync so we don't burst on re-enable
    lastTriggerCount = countCopy;
  }

  // ── Periodic speed heartbeat ─────────────────────────────────────────────
  if (now - lastSpeedReportMs >= speedReportIntervalMs) {
    lastSpeedReportMs = now;
    float speedToReport = lastSpeedCms;
    if (lastTriggerTimeMs == 0 || (now - lastTriggerTimeMs) > SPEED_STALE_MS) {
      speedToReport = 0.0;
    }
    Serial.print(F("{\"type\":\"speed\""));
    Serial.print(F(",\"speed_cms\":")); Serial.print(speedToReport, 2);
    Serial.print(F(",\"count\":")); Serial.print(countCopy);
    Serial.print(F(",\"trigger_enabled\":")); Serial.print(triggerEnabled ? F("true") : F("false"));
    Serial.println(F("}"));
  }

  // ── Manual button handling (with debounce) ───────────────────────────────
  bool buttonState = digitalRead(manualButton);

  if (buttonState != lastButtonState) {
    lastButtonTime  = millis();
    lastButtonState = buttonState;
  }

  if ((millis() - lastButtonTime) > debounceMs) {
    static bool alreadyPressed = false;

    if (buttonState == LOW && !alreadyPressed) {
      alreadyPressed = true;
      noInterrupts();
      long manualCount = encoderCount;
      interrupts();
      fireTrigger("manual", manualCount);
    }

    if (buttonState == HIGH) {
      alreadyPressed = false;
    }
  }
}
