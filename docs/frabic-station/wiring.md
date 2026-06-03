# Production Camera Trigger Station

## Required Components

- Raspberry Pi 5
- Arduino Uno R3
- Incremental Rotary Encoder
- 4-Channel Relay Module
- Industrial Camera(s)

---

# System Architecture

Encoder
→ Arduino Uno
→ Relay Module
→ Camera Trigger Input

Arduino Uno
→ USB
→ Raspberry Pi

---

# Signal Mapping

## Encoder

| Encoder Signal | Arduino Connection |
| -------------- | ------------------ |
| VCC            | Arduino 5V         |
| 0V             | Arduino GND        |
| A              | Arduino D2         |
| B              | Arduino D3         |

---

## Relay Module

| Relay Signal | Arduino Connection |
| ------------ | ------------------ |
| DC+          | Arduino 5V         |
| DC-          | Arduino GND        |
| IN1          | Arduino D9         |
| COM1         | Arduino GND        |
| NO1          | Camera TRIG-       |

---

## Camera Trigger

| Camera Signal | Connection |
| ------------- | ---------- |
| TRIG+         | Arduino 5V |
| TRIG-         | Relay NO1  |

For multiple cameras:

Arduino 5V
→ Camera 1 TRIG+
→ Camera 2 TRIG+

Relay NO1
→ Camera 1 TRIG-
→ Camera 2 TRIG-

---

# Arduino Pin Assignment

| Pin | Function                             |
| --- | ------------------------------------ |
| D2  | Encoder A                            |
| D3  | Encoder B                            |
| D9  | Camera Trigger Output                |
| 5V  | Encoder VCC, Relay DC+, Camera TRIG+ |
| GND | Encoder 0V, Relay DC-, Relay COM1    |

---

# Validation Checklist

- Encoder count changes when wheel rotates
- Relay activates when trigger occurs
- Camera 1 captures image
- Camera 2 captures image
- USB serial communication with Raspberry Pi is operational

---

# Optional Development Features

## Manual Override Button (Debug Only)

| Button Signal     | Arduino Connection |
| ----------------- | ------------------ |
| Button Terminal 1 | Arduino D4         |
| Button Terminal 2 | Arduino GND        |

This button is used only for development and troubleshooting. It is not required in production deployments.
