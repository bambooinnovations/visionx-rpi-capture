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
 * Output:
 *   Each trigger event prints a JSON line to Serial at 115200 baud, e.g.:
 *     {"type":"trigger","source":"encoder","count":118,"trigger":1}
 *   "source" is either "encoder" (distance-based) or "manual" (button press).
 *
 * Encoder logic:
 *   Both channels are read on every edge (CHANGE interrupt) and decoded with
 *   a 4-bit state machine so the count is direction-aware.
 */

// ── Pin assignments ──────────────────────────────────────────────────────────
const int encoderA    = 2;   // Encoder channel A (Green wire) — INT0
const int encoderB    = 3;   // Encoder channel B (White wire) — INT1
const int manualButton = 4;  // Push-button to GND; LOW when pressed
const int triggerOut  = 9;   // Camera shutter trigger + LED

// ── Trigger spacing ──────────────────────────────────────────────────────────
// Number of encoder counts between consecutive camera triggers.
// 118 counts ≈ 1 cm of travel with the current encoder/gear ratio.
const long triggerInterval = 118;

// ── Encoder state (shared with ISR) ─────────────────────────────────────────
volatile long encoderCount = 0;   // Running signed count; updated in ISR
volatile int  lastEncoded  = 0;   // Previous 2-bit AB state for transition lookup

// ── Trigger tracking ─────────────────────────────────────────────────────────
long          lastTriggerCount = 0;   // encoderCount value at the last trigger
unsigned long triggerNumber    = 0;   // Monotonically increasing trigger index

// ── Button debounce ──────────────────────────────────────────────────────────
bool          lastButtonState = HIGH;
unsigned long lastButtonTime  = 0;
const unsigned long debounceMs = 50;  // Ignore transitions shorter than 50 ms

// ── Trigger polarity ─────────────────────────────────────────────────────────
// D9 is wired as a low-side switch: pull LOW to activate the camera opto-input.
const int TRIGGER_IDLE   = HIGH;
const int TRIGGER_ACTIVE = LOW;

// ── ISR: quadrature decoder ───────────────────────────────────────────────────
// Called on any edge of either encoder channel.
// Builds a 4-bit word from the previous and current AB states and uses a
// lookup table encoded in two if-statements to determine direction.
void updateEncoder() {
  int A = digitalRead(encoderA);
  int B = digitalRead(encoderB);

  int encoded = (A << 1) | B;            // Current 2-bit AB state
  int sum = (lastEncoded << 2) | encoded; // 4-bit: [prev_A, prev_B, cur_A, cur_B]

  // Valid forward (CW) transitions
  if (sum == 0b1101 || sum == 0b0100 || sum == 0b0010 || sum == 0b1011) {
    encoderCount++;
  }

  // Valid backward (CCW) transitions
  if (sum == 0b1110 || sum == 0b0111 || sum == 0b0001 || sum == 0b1000) {
    encoderCount--;
  }

  lastEncoded = encoded;
}

// ── Helper: fire a single trigger pulse ──────────────────────────────────────
// Pulls D9 LOW for 20 ms then restores it HIGH, then logs a JSON event.
void fireTrigger(const char* source, long countValue) {
  digitalWrite(triggerOut, TRIGGER_ACTIVE);
  delay(20);                              // 20 ms pulse width for camera input
  digitalWrite(triggerOut, TRIGGER_IDLE);

  triggerNumber++;

  // Emit a machine-readable JSON event so the host can correlate frames
  Serial.print("{\"type\":\"trigger\",\"source\":\"");
  Serial.print(source);
  Serial.print("\",\"count\":");
  Serial.print(countValue);
  Serial.print(",\"trigger\":");
  Serial.print(triggerNumber);
  Serial.println("}");
}

// ── setup ─────────────────────────────────────────────────────────────────────
void setup() {
  // Encoder inputs with internal pull-ups (encoder common connected to GND)
  pinMode(encoderA,     INPUT_PULLUP);
  pinMode(encoderB,     INPUT_PULLUP);
  pinMode(manualButton, INPUT_PULLUP);

  // Trigger output starts idle (HIGH)
  pinMode(triggerOut, OUTPUT);
  digitalWrite(triggerOut, TRIGGER_IDLE);

  // Seed the decoder state so the first interrupt is interpreted correctly
  int A = digitalRead(encoderA);
  int B = digitalRead(encoderB);
  lastEncoded = (A << 1) | B;

  // Attach interrupts to both channels for full quadrature resolution (4× counts)
  attachInterrupt(digitalPinToInterrupt(encoderA), updateEncoder, CHANGE);
  attachInterrupt(digitalPinToInterrupt(encoderB), updateEncoder, CHANGE);

  Serial.begin(115200);
  Serial.println("Trigger controller started");
}

// ── loop ──────────────────────────────────────────────────────────────────────
void loop() {
  // Safely snapshot the volatile counter (interrupts could update it mid-read)
  noInterrupts();
  long countCopy = encoderCount;
  interrupts();

  // Distance-based trigger: fire whenever travel since last trigger >= interval
  long moved = labs(countCopy - lastTriggerCount);
  if (moved >= triggerInterval) {
    lastTriggerCount = countCopy;
    fireTrigger("encoder", countCopy);
  }

  // ── Manual button handling (with debounce) ───────────────────────────────
  bool buttonState = digitalRead(manualButton);

  // Record the time of any state change to start the debounce window
  if (buttonState != lastButtonState) {
    lastButtonTime  = millis();
    lastButtonState = buttonState;
  }

  // Act only after the signal has been stable for debounceMs
  if ((millis() - lastButtonTime) > debounceMs) {
    static bool alreadyPressed = false;

    if (buttonState == LOW && !alreadyPressed) {
      // Button just pressed — fire one manual trigger
      alreadyPressed = true;

      noInterrupts();
      long manualCount = encoderCount;
      interrupts();

      fireTrigger("manual", manualCount);
    }

    if (buttonState == HIGH) {
      // Button released — allow the next press to trigger again
      alreadyPressed = false;
    }
  }
}
