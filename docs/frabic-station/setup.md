# Camera Trigger Station Setup Guide

## Table of Contents

1. [Introduction](#1-introduction)
2. [System Overview](#2-system-overview)
   - [2.1 System Architecture](#21-system-architecture)
   - [2.2 Camera Identification](#22-camera-identification)
   - [2.3 USB Topology](#23-usb-topology)
3. [Hardware Requirements](#3-hardware-requirements)
   - [3.1 Bill of Materials (BOM)](#31-bill-of-materials-bom)
   - [3.2 Hardware Specifications](#32-hardware-specifications)
4. [Hardware Assembly](#4-hardware-assembly)
   - [4.1 Encoder Assembly](#41-encoder-assembly)
   - [4.2 Camera Assembly](#42-camera-assembly)
   - [4.3 Controller Assembly](#43-controller-assembly)
   - [4.4 Pre-Wiring Inspection](#44-pre-wiring-inspection)
5. [Cable Connections](#5-cable-connections)
   - [5.1 Camera USB Connections](#51-camera-usb-connections)
   - [5.2 Camera Power Connections](#52-camera-power-connections)
   - [5.3 Arduino USB Connection](#53-arduino-usb-connection)
   - [5.4 Trigger Cable Connections](#54-trigger-cable-connections)
   - [5.5 Cable Connection Checklist](#55-cable-connection-checklist)
6. [Signal Wiring](#6-signal-wiring)
   - [6.1 Encoder Wiring](#61-encoder-wiring)
   - [6.2 Relay Wiring](#62-relay-wiring)
   - [6.3 Camera Trigger Wiring](#63-camera-trigger-wiring)
   - [6.4 5V and GND Distribution](#64-5v-and-gnd-distribution)
7. [Software Installation](#7-software-installation)
   - [7.1 Raspberry Pi Setup](#71-raspberry-pi-application-setup)
   - [7.2 MindVision Camera SDK Installation](#72-mindvision-camera-sdk-installation)
   - [7.3 Arduino Firmware Installation](#73-arduino-firmware-installation)
   - [7.4 Application Deployment](#74-application-deployment)
8. [Validation and Testing](#8-validation-and-testing)
   - [8.1 Hardware Validation](#81-hardware-validation)
   - [8.2 Software Validation](#82-software-validation)
   - [8.3 End-to-End Testing](#83-end-to-end-testing)
9. [Troubleshooting](#9-troubleshooting)
10. [Appendix](#10-appendix)
    - [10.1 Signal Definitions](#101-signal-definitions)
    - [10.2 Optional Development Features](#102-optional-development-features)
    - [10.3 Revision History](#103-revision-history)

---

# 1. Introduction

This document describes the hardware assembly, cable connections, signal wiring, validation, and troubleshooting procedures for the Camera Trigger Station.

The station consists of three industrial cameras connected to a Raspberry Pi 5. An Arduino Uno R3 receives encoder signals and generates synchronized trigger events for all cameras through a 4-channel relay module.

This guide is intended for assembly and deployment teams. Hardware appearance, connector style, screw terminal layout, and board shape may vary by supplier. Signal names and terminal labels are the source of truth.

---

# 2. System Overview

The Camera Trigger Station consists of the following major components:

- Raspberry Pi 5
- Arduino Uno R3
- Rotary Encoder
- Encoder Wheel
- 4-Channel Relay Module
- Three Industrial Cameras
- USB 3.0 Hub

The Raspberry Pi is responsible for:

- Image acquisition
- Camera management
- Application execution
- Communication with the Arduino

The Arduino is responsible for:

- Encoder processing
- Trigger generation
- Communication with the Raspberry Pi

The relay module provides trigger switching control for all connected cameras.

---

## 2.1 System Architecture

```text
── TRIGGER SIGNAL PATH ─────────────────────────────────────

  Encoder
     │  (quadrature A/B)
     ▼
  Arduino Uno
     │  (D9)
     ▼
  Relay Module
     ├── NO1 ──► Camera 1 TRIG-
     ├── NO2 ──► Camera 2 TRIG-
     └── NO3 ──► Camera 3 TRIG-

── DATA / COMMUNICATION PATH ───────────────────────────────

  Camera 1 ──┐
  Camera 2 ──┼──► USB 3.0 Hub ──► Raspberry Pi
  Camera 3 ──┘

  Arduino Uno ────────────────►  Raspberry Pi
                (USB Serial)
```

---

## 2.2 Camera Identification

The station contains three cameras.

Each camera shall be assigned a permanent identifier and physical position.

| Camera ID | Position      |
| --------- | ------------- |
| Camera 1  | Left Camera   |
| Camera 2  | Center Camera |
| Camera 3  | Right Camera  |

These identifiers shall be used consistently throughout:

- Hardware assembly
- Wiring documentation
- Software configuration
- Validation procedures
- Troubleshooting procedures

All camera-related cables should be labeled according to the associated camera.

### Recommended Cable Labels

| Camera   | USB Cable | Trigger Cable |
| -------- | --------- | ------------- |
| Camera 1 | USB_CAM1  | TRIG_CAM1     |
| Camera 2 | USB_CAM2  | TRIG_CAM2     |
| Camera 3 | USB_CAM3  | TRIG_CAM3     |

### Camera Layout

```text
+-----------+-----------+-----------+
| Camera 1  | Camera 2  | Camera 3  |
|   Left    |  Center   |   Right   |
+-----------+-----------+-----------+
```

This camera numbering shall be maintained throughout the lifetime of the station.

---

## 2.3 USB Topology

The station uses three industrial cameras and one Raspberry Pi 5.

Because the Raspberry Pi has only two USB 3.0 ports, a USB 3.0 hub is required.

```text
Camera 1
   │
   ▼
USB 3.0 Hub

Camera 2
   │
   ▼
USB 3.0 Hub

Camera 3
   │
   ▼
USB 3.0 Hub

USB 3.0 Hub
   │
   ▼
Raspberry Pi USB 3.0 Port
```

The Arduino Uno shall be connected directly to a Raspberry Pi USB port using a USB A-to-B cable.

```text
Arduino Uno
   │
   ▼
USB A-to-B Cable
   │
   ▼
Raspberry Pi USB Port
```

Recommended USB assignment:

| Device      | Connection                                      |
| ----------- | ----------------------------------------------- |
| Camera 1    | USB 3.0 Hub                                     |
| Camera 2    | USB 3.0 Hub                                     |
| Camera 3    | USB 3.0 Hub                                     |
| USB 3.0 Hub | Raspberry Pi USB 3.0 Port                       |
| Arduino Uno | Raspberry Pi USB 2.0 Port or available USB port |

---

# 3. Hardware Requirements

## 3.1 Bill of Materials (BOM)

The following hardware is required to assemble one Camera Trigger Station.

| Item                          |    Quantity | Notes                                          |
| ----------------------------- | ----------: | ---------------------------------------------- |
| Raspberry Pi 5                |           1 | Main image acquisition computer                |
| Raspberry Pi Power Supply     |           1 | Power supply for Raspberry Pi                  |
| Arduino Uno R3                |           1 | Encoder and trigger controller                 |
| USB A-to-B Cable              |           1 | Arduino to Raspberry Pi                        |
| Rotary Encoder                |           1 | Conveyor movement input                        |
| Encoder Wheel                 |           1 | Mounted to encoder shaft                       |
| Encoder Mounting Bracket      |           1 | Holds encoder and wheel in position            |
| 4-Channel Relay Module (5V)   |           1 | Trigger switching module                       |
| Industrial Camera             |           3 | Camera 1, Camera 2, Camera 3                   |
| Camera Trigger Cable          |           3 | One trigger cable per camera                   |
| USB 3.0 Camera Cable          |           3 | One USB3 cable per camera                      |
| USB 3.0 Hub                   |           1 | Required for three cameras                     |
| Camera Power Adapter          |           3 | One power adapter per camera                   |
| Hookup Wire                   | As required | Signal wiring                                  |
| Cable Labels                  | As required | Recommended for all USB and trigger cables     |
| Cable Ties / Cable Management | As required | Recommended for strain relief and organization |

---

## 3.2 Hardware Specifications

### Raspberry Pi

- Model: Raspberry Pi 5
- Role: Image acquisition and application host

### Controller

- Model: Arduino Uno R3
- Role: Encoder reading and trigger generation

### Encoder

- Type: Incremental rotary encoder
- Required signals: VCC, 0V, A, B

### Encoder Wheel

- Role: Transfers conveyor movement to the rotary encoder

### Relay Module

- Type: 5V 4-channel relay module
- Required terminals: DC+, DC-, IN1, IN2, IN3, COM1, COM2, COM3, NO1, NO2, NO3
- NC1, NC2, NC3, and Channel 4 are not used in this system

### Cameras

- Quantity: 3
- Required connections per camera:
  - USB 3.0 data cable
  - Camera power adapter
  - Trigger cable

### USB Hub

- Type: USB 3.0 hub
- Role: Connect three cameras to the Raspberry Pi

---

# 4. Hardware Assembly

This section describes the physical assembly of the Camera Trigger Station. Electrical wiring and software installation are covered in later sections.

---

## 4.1 Encoder Assembly

### Required Components

- Rotary Encoder
- Encoder Wheel
- Encoder Mounting Bracket

### Installation Procedure

1. Install the encoder wheel onto the encoder shaft.
2. Secure the encoder wheel to prevent slipping during operation.
3. Mount the encoder assembly to the machine frame using the encoder mounting bracket.
4. Ensure the encoder wheel maintains consistent contact with the conveyor or driven surface.
5. Verify that the encoder rotates smoothly throughout the full operating range.
6. Verify that the encoder mounting bracket is rigid and does not move during operation.

### Validation

- Encoder wheel rotates freely.
- Encoder wheel does not slip on the shaft.
- Encoder assembly is securely mounted.
- Encoder wheel maintains continuous contact with the conveyor.

---

## 4.2 Camera Assembly

### Required Components

- Industrial Camera × 3
- Camera Mounting Hardware

### Installation Procedure

1. Install Camera 1 in the left camera position.
2. Install Camera 2 in the center camera position.
3. Install Camera 3 in the right camera position.
4. Secure all cameras using appropriate mounting hardware.
5. Ensure all camera mounting hardware is fully tightened.
6. Verify that camera movement is not possible during normal operation.
7. Label each camera and its cables using the Camera 1, Camera 2, and Camera 3 identifiers.

### Validation

- Camera 1 is installed in the left position.
- Camera 2 is installed in the center position.
- Camera 3 is installed in the right position.
- All cameras are securely mounted.
- Cameras are aligned according to station requirements.
- Camera mounts do not move when touched.
- USB and trigger cables are labeled for each camera.

---

## 4.3 Controller Assembly

### Required Components

- Raspberry Pi 5
- Arduino Uno R3
- 4-Channel Relay Module
- USB 3.0 Hub

### Installation Procedure

1. Mount the Raspberry Pi in the control enclosure or designated mounting location.
2. Mount the Arduino Uno near the Raspberry Pi.
3. Mount the relay module near the Arduino.
4. Mount the USB 3.0 hub in a location accessible to all camera USB cables.
5. Ensure all components are secured and protected from accidental movement.
6. Ensure the Arduino, relay module, and wiring terminals remain accessible for maintenance.

### Validation

- Raspberry Pi is securely mounted.
- Arduino is securely mounted.
- Relay module is securely mounted.
- USB hub is securely mounted.
- All components are accessible for maintenance.

---

## 4.4 Pre-Wiring Inspection

Before proceeding to cable installation:

- Verify all hardware components are present.
- Verify all mounting hardware is secure.
- Verify all connectors are accessible.
- Verify sufficient cable routing space is available.
- Verify no components interfere with moving equipment.
- Verify all cameras and cables are labeled using CAM1, CAM2, and CAM3 identifiers.

Once all checks have passed, proceed to Section 5: Cable Connections.

---

# 5. Cable Connections

This section describes all physical cable connections required before signal wiring.

Signal-level wiring is covered in Section 6.

---

## 5.1 Camera USB Connections

Each camera must be connected to the USB 3.0 hub using a USB 3.0 camera cable.

### Camera 1

```text
Camera 1 USB Port
    ↓
USB 3.0 Camera Cable
    ↓
USB 3.0 Hub
```

### Camera 2

```text
Camera 2 USB Port
    ↓
USB 3.0 Camera Cable
    ↓
USB 3.0 Hub
```

### Camera 3

```text
Camera 3 USB Port
    ↓
USB 3.0 Camera Cable
    ↓
USB 3.0 Hub
```

### USB Hub Connection

```text
USB 3.0 Hub
    ↓
USB 3.0 Cable
    ↓
Raspberry Pi USB 3.0 Port
```

Important notes:

- The USB hub must be connected to a Raspberry Pi USB 3.0 port.
- The camera USB cables should be connected to the USB 3.0 hub.
- Label each camera USB cable as USB_CAM1, USB_CAM2, or USB_CAM3.

---

## 5.2 Camera Power Connections

Each camera requires an external power supply.

### Camera 1

```text
Camera 1 Power Connector
    ↓
Camera Power Adapter
    ↓
AC Outlet
```

### Camera 2

```text
Camera 2 Power Connector
    ↓
Camera Power Adapter
    ↓
AC Outlet
```

### Camera 3

```text
Camera 3 Power Connector
    ↓
Camera Power Adapter
    ↓
AC Outlet
```

Important notes:

- Do not power the cameras from the Arduino.
- Do not power the cameras from Raspberry Pi GPIO pins.
- Use the supplied or approved camera power adapter for each camera.

---

## 5.3 Arduino USB Connection

The Arduino is connected directly to the Raspberry Pi using a USB A-to-B cable.

```text
Arduino USB-B Port
    ↓
USB A-to-B Cable
    ↓
Raspberry Pi USB Port
```

Purpose:

- Arduino power
- Serial communication between Arduino and Raspberry Pi

Recommended connection:

| Device      | Connection                                      |
| ----------- | ----------------------------------------------- |
| Arduino Uno | Raspberry Pi USB 2.0 port or available USB port |

---

## 5.4 Trigger Cable Connections

Each camera requires one trigger cable.

### Camera 1

```text
Trigger Cable
    ↓
Camera 1 Trigger Port
```

### Camera 2

```text
Trigger Cable
    ↓
Camera 2 Trigger Port
```

### Camera 3

```text
Trigger Cable
    ↓
Camera 3 Trigger Port
```

Trigger cable signal wiring is described in Section 6.

Important notes:

- Label each trigger cable as TRIG_CAM1, TRIG_CAM2, or TRIG_CAM3.
- Only the required trigger signals shall be wired into the trigger circuit.
- Unused trigger cable wires shall be insulated and secured so they cannot touch other terminals.

---

## 5.5 Cable Connection Checklist

Verify the following before proceeding:

### Camera Connections

- [ ] Camera 1 USB cable connected to USB 3.0 hub
- [ ] Camera 2 USB cable connected to USB 3.0 hub
- [ ] Camera 3 USB cable connected to USB 3.0 hub
- [ ] USB 3.0 hub connected to Raspberry Pi USB 3.0 port

### Power Connections

- [ ] Camera 1 power connected
- [ ] Camera 2 power connected
- [ ] Camera 3 power connected
- [ ] Raspberry Pi power connected

### Controller Connections

- [ ] Arduino USB cable connected to Raspberry Pi
- [ ] Arduino is securely mounted
- [ ] Relay module is securely mounted

### Trigger Cable Connections

- [ ] Camera 1 trigger cable connected to Camera 1
- [ ] Camera 2 trigger cable connected to Camera 2
- [ ] Camera 3 trigger cable connected to Camera 3
- [ ] Trigger cables are labeled
- [ ] Unused trigger cable wires are insulated

After all connections have been verified, proceed to Section 6: Signal Wiring.

---

# 6. Signal Wiring

This section describes the signal-level wiring between the encoder, Arduino, relay module, and camera trigger cables.

Power off the system before making or changing any signal wiring.

Signal names are the source of truth. Wire colors are provided only as the current hardware reference and must be verified against the actual component labeling.

---

## 6.1 Encoder Wiring

Connect the rotary encoder to the Arduino Uno.

| Encoder Signal | Arduino Connection |
| -------------- | ------------------ |
| VCC            | Arduino 5V         |
| 0V             | Arduino GND        |
| A              | Arduino D2         |
| B              | Arduino D3         |

### Encoder Wiring Diagram

```text
Encoder VCC  ───────────── Arduino 5V
Encoder 0V   ───────────── Arduino GND
Encoder A    ───────────── Arduino D2
Encoder B    ───────────── Arduino D3
```

### Encoder Wiring Notes

- Verify the encoder label before wiring.
- Encoder A and Encoder B must be connected to Arduino D2 and D3 because these pins support interrupt-based encoder reading.

---

## 6.2 Relay Wiring

Connect the relay module input side to the Arduino Uno.

| Relay Input Terminal | Arduino Connection |
| -------------------- | ------------------ |
| DC+                  | Arduino 5V         |
| DC-                  | Arduino GND        |
| IN1                  | Arduino D9         |
| IN2                  | Arduino D9         |
| IN3                  | Arduino D9         |

IN1, IN2, and IN3 are all connected to the same Arduino D9 pin so all three relay channels fire simultaneously on every trigger event.

Connect the relay output side for camera trigger switching.

| Relay Output Terminal | Connection        |
| --------------------- | ----------------- |
| COM1                  | Arduino GND       |
| NO1                   | Camera 1 TRIG-    |
| COM2                  | Arduino GND       |
| NO2                   | Camera 2 TRIG-    |
| COM3                  | Arduino GND       |
| NO3                   | Camera 3 TRIG-    |
| NC1, NC2, NC3         | Not used          |

### Relay Wiring Diagram

```text
Arduino 5V   ───────────── Relay DC+
Arduino GND  ───────────── Relay DC-

Arduino D9   ─┬─────────── Relay IN1
              ├─────────── Relay IN2
              └─────────── Relay IN3

Arduino GND  ─┬─────────── Relay COM1
              ├─────────── Relay COM2
              └─────────── Relay COM3

Relay NO1    ───────────── Camera 1 TRIG-
Relay NO2    ───────────── Camera 2 TRIG-
Relay NO3    ───────────── Camera 3 TRIG-

Relay NC1, NC2, NC3  ───── Not used
```

### Relay Wiring Notes

- Channels 1, 2, and 3 are used — one channel per camera.
- IN1, IN2, and IN3 share a single wire from D9 so all three channels activate together.
- Each camera TRIG- connects to its dedicated NOx terminal. Do not combine multiple TRIG- wires onto one NO terminal.
- NC1, NC2, and NC3 are not used. Channel 4 is not used.
- The relay input trigger mode must match the Arduino firmware configuration. The target production configuration is D9 active = relay active.
- If the relay module has a high-level / low-level trigger jumper, configure all three channels according to the firmware setting before validation.

---

## 6.3 Camera Trigger Wiring

### Camera Trigger Cable Wire Identification

The trigger cable labels are printed in Chinese. Use wire color to identify each signal.

| Wire Color | Signal Name | Action |
| ---------- | ----------- | ------ |
| Red        | DC12V+      | **Do not connect** — camera 12V power line |
| White      | TRIG+       | Connect to Arduino 5V |
| Brown      | IO2+        | Leave disconnected — insulate the exposed end |
| Green      | STB-        | Leave disconnected — insulate the exposed end |
| Yellow     | TRIG-       | Connect to Relay NO1 / NO2 / NO3 (one per camera; also labeled GPO- on the cable) |
| Black      | GND / IO2-  | Leave disconnected — insulate the exposed end |

Only the **White** and **Yellow** wires are used. All other wires must be insulated so they cannot contact any terminal.

---

Each camera trigger cable provides trigger input signals. For the current camera cable, the required trigger wires are:

| Camera Trigger Signal | Wire Color | Connection                              |
| --------------------- | ---------- | --------------------------------------- |
| TRIG+                 | White      | Arduino 5V                              |
| TRIG- (Camera 1)      | Yellow     | Relay NO1                               |
| TRIG- (Camera 2)      | Yellow     | Relay NO2                               |
| TRIG- (Camera 3)      | Yellow     | Relay NO3                               |

Each camera TRIG- connects to its own dedicated relay channel. All three relay channels fire simultaneously because IN1, IN2, and IN3 share the same D9 signal.

### Camera Trigger Positive Wiring

Connect all camera TRIG+ signals to Arduino 5V.

```text
Arduino 5V
   ├──────────── Camera 1 TRIG+ / White
   ├──────────── Camera 2 TRIG+ / White
   └──────────── Camera 3 TRIG+ / White
```

### Camera Trigger Negative Wiring

Each camera TRIG- connects to its own relay channel NO terminal.

```text
Camera 1 TRIG- / Yellow ───── Relay NO1 ───── Relay COM1 ───── Arduino GND
Camera 2 TRIG- / Yellow ───── Relay NO2 ───── Relay COM2 ───── Arduino GND
Camera 3 TRIG- / Yellow ───── Relay NO3 ───── Relay COM3 ───── Arduino GND
```

### Camera Trigger Behavior

When the relays are inactive:

```text
NO1, NO2, NO3 are each disconnected from their COM.
Camera TRIG- signals are not connected to Arduino GND.
No trigger is sent.
```

When the relays are active:

```text
NO1 connects to COM1 → Camera 1 TRIG- connects to Arduino GND.
NO2 connects to COM2 → Camera 2 TRIG- connects to Arduino GND.
NO3 connects to COM3 → Camera 3 TRIG- connects to Arduino GND.
All three cameras see trigger voltage between TRIG+ and TRIG- simultaneously.
```

### Unused Camera Trigger Cable Wires

Only TRIG+ and TRIG- are required for the camera trigger circuit.

For the current camera cable, the following wires are not used for this trigger station wiring:

| Signal     | Current Wire Color | Action                                |
| ---------- | ------------------ | ------------------------------------- |
| IO2+       | Brown              | Leave disconnected and insulated      |
| STB+       | Green              | Leave disconnected and insulated      |
| GND / IO2- | Black              | Leave disconnected for trigger wiring |
| DC12V+     | Red                | Do not connect to Arduino             |

Important notes:

- Do not connect the camera red power wire to the Arduino.
- Do not connect the camera black ground wire to the Arduino for the trigger circuit unless instructed by engineering.
- Camera power is provided through the camera power adapter, not through the Arduino.
- Insulate unused wires so they cannot touch each other or any terminal.

---

## 6.4 5V and GND Distribution

The Arduino 5V and GND pins are used for low-current signal wiring only.

### Arduino 5V Distribution

Arduino 5V connects to:

| Destination    | Purpose                       |
| -------------- | ----------------------------- |
| Encoder VCC    | Encoder power                 |
| Relay DC+      | Relay module input-side power |
| Camera 1 TRIG+ | Camera trigger positive       |
| Camera 2 TRIG+ | Camera trigger positive       |
| Camera 3 TRIG+ | Camera trigger positive       |

Diagram:

```text
Arduino 5V
   ├──────────── Encoder VCC
   ├──────────── Relay DC+
   ├──────────── Camera 1 TRIG+
   ├──────────── Camera 2 TRIG+
   └──────────── Camera 3 TRIG+
```

### Arduino GND Distribution

Arduino GND connects to:

| Destination | Purpose                          |
| ----------- | -------------------------------- |
| Encoder 0V  | Encoder ground                   |
| Relay DC-   | Relay module input-side ground   |
| Relay COM1  | Channel 1 trigger return contact |
| Relay COM2  | Channel 2 trigger return contact |
| Relay COM3  | Channel 3 trigger return contact |

Diagram:

```text
Arduino GND
   ├──────────── Encoder 0V
   ├──────────── Relay DC-
   ├──────────── Relay COM1
   ├──────────── Relay COM2
   └──────────── Relay COM3
```

### Wiring Method Notes

- Do not use a breadboard in production wiring.
- Use secure wire connections such as screw terminals, terminal blocks, or suitable wire connectors.
- Multiple wires connected to one terminal must be mechanically secure.
- If a screw terminal cannot reliably clamp multiple wires, combine them externally using a terminal block or connector, then run a single jumper into the relay or Arduino terminal.
- After wiring, gently pull each wire to confirm it is mechanically secured.

---

# 7. Software Installation

This section covers software installation for the Camera Trigger Station.

---

## 7.1 Raspberry Pi Application Setup

This section covers deploying the visionX capture application on a Raspberry Pi that already has the OS installed and configured.

### Prerequisites

- Raspberry Pi OS (Bookworm or Bullseye) is installed and running.
- The Raspberry Pi has internet access.
- Git is available on the Raspberry Pi.

### Clone the Repository

```bash
git clone <repository-url>
cd visionx-rpi-capture
```

### Run the Setup Script

```bash
sudo bash scripts/setup.sh
```

The setup script installs all application dependencies and registers the service. For a MindVision camera station, run the following options in order:

| Step | Menu Option | Action |
| ---- | ----------- | ------ |
| 1 | Option 3 | Install MindVision SDK |
| 2 | Option 4 | Install Arduino IDE |

Follow the reboot prompt after each step.

### Verify the Application Service

After rebooting, verify the application service is running:

```bash
sudo systemctl status rpi-capture
```

To follow the live log:

```bash
journalctl -u rpi-capture -f
```

The application starts automatically on boot. No manual start is required after installation.

---

## 7.2 MindVision Camera SDK Installation

### Purpose

The MindVision SDK provides the camera driver, shared library, and udev rules required for the Raspberry Pi to detect and communicate with MindVision industrial cameras.

### Installation

Run the setup script:

```bash
sudo bash scripts/setup.sh
```

Select option **3 — Install MindVision**.

The script installs:

| Component | Installed Path |
| --------- | -------------- |
| Shared library | `/lib/libMVSDK.so` |
| Header files | `/usr/include/CameraApi.h`, `CameraDefine.h`, `CameraStatus.h` |
| udev rules | `/etc/udev/rules.d/88-mvusb.rules`, `99-mvusb.rules` |

The udev rules are reloaded automatically. A reboot is recommended before connecting cameras.

### Verify the Installation

Run the setup script and select option **1 — Check installation status**, then **2 — MindVision** to confirm all components are present.

Alternatively, check manually:

```bash
ls /lib/libMVSDK.so
ls /usr/include/CameraApi.h
ls /etc/udev/rules.d/88-mvusb.rules
```

### Verify Camera Detection

After rebooting, connect the cameras via the USB 3.0 hub and verify the Raspberry Pi detects them:

```bash
lsusb
```

MindVision cameras appear as USB devices in the output. If a camera is not listed, verify the USB cable and power adapter connections.

---

## 7.3 Arduino Firmware Installation

### Purpose

The Arduino firmware controls the encoder-based camera trigger system.

The Arduino is responsible for:

- Reading the rotary encoder signals.
- Counting encoder movement.
- Sending trigger pulses from Arduino D9 to Relay IN1, IN2, and IN3.
- Activating Relay Channels 1, 2, and 3 simultaneously.
- Sending trigger event logs through USB serial.

The Arduino must be programmed before the station goes into service.

---

### Arduino IDE Installation

Arduino IDE 2 does not support ARM Linux. The setup script installs the legacy Arduino IDE 1.8.19 directly on the Raspberry Pi.

Run the setup script on the Raspberry Pi:

```bash
sudo bash scripts/setup.sh
```

Select option **4 — Install Arduino IDE**.

The script will:

- Detect the ARM architecture and download the correct build.
- Extract the IDE to `/opt/arduino-1.8.19`.
- Create a launcher at `/usr/local/bin/arduino`.
- Add the current user to the `dialout` group for serial port access.

A reboot or re-login is required after installation for the `dialout` group change to take effect.

Verify the installation:

```bash
arduino --version
```

---

### Firmware File Location

The Arduino firmware is stored in the project repository under the `arduino/` folder.

Expected location from the repository root:

[`arduino/camera_trigger/camera_trigger.ino`](../../arduino/camera_trigger/camera_trigger.ino)

The firmware is an Arduino sketch folder. Do not open the `.ino` file in isolation — open the `camera_trigger` folder as a sketch in Arduino IDE.

Do not create a new Arduino sketch manually. Use the firmware provided in the repository.

---

### Required Connection for Firmware Upload

Connect the Arduino to the Raspberry Pi using the USB A-to-B cable.

```text
Arduino USB-B Port
    ↓
USB A-to-B Cable
    ↓
Raspberry Pi USB Port
```

The same connection is used for firmware upload and for serial communication during normal station operation.

---

### Upload Procedure Using Arduino IDE

1. Complete the Arduino IDE installation described above.
1. Connect the Arduino Uno R3 to the Raspberry Pi using the USB A-to-B cable.
1. Open Arduino IDE:

```bash
arduino
```

1. Open the firmware sketch folder:

```text
File → Open → arduino/camera_trigger/camera_trigger.ino
```

1. Select the Arduino board:

```text
Tools → Board → Arduino Uno (R3)
```

1. Select the correct serial port:

```text
Tools → Port → /dev/ttyACM0
```

If `/dev/ttyACM0` does not appear, verify:

- The USB A-to-B cable is connected.
- The current user is in the `dialout` group (re-login if just added).
- No other process is holding the serial port.

1. Click **Upload**.
1. Wait until the upload completes successfully.

---

### Relay Trigger Mode

The firmware uses the following D9 output logic:

```text
D9 LOW  = trigger active (camera fires)
D9 HIGH = idle
```

Therefore, Relay Channels 1, 2, and 3 must each be configured as:

```text
LOW-level trigger
```

If any relay channel activates continuously when the Arduino is idle, that channel's trigger mode jumper is set to HIGH-level. Switch all three channels to LOW-level to match the firmware.

---

### Firmware Validation After Upload

After uploading the firmware, open the Arduino IDE Serial Monitor.

Set baud rate to:

```text
115200
```

Expected startup message:

```json
{"type":"startup","msg":"Trigger controller started"}
```

Rotate the encoder wheel.

Expected trigger output:

```json
{"type":"trigger","source":"encoder","count":118,"trigger":1,"speed_cms":5.20}
{"type":"trigger","source":"encoder","count":236,"trigger":2,"speed_cms":5.20}
{"type":"trigger","source":"encoder","count":354,"trigger":3,"speed_cms":5.20}
```

Expected heartbeat output (every 500 ms while encoder is moving):

```json
{"type":"speed","speed_cms":5.20,"count":118,"trigger_enabled":true}
```

During each trigger event:

- Relay Channels 1, 2, and 3 should all activate briefly.
- All three cameras should receive a trigger signal.
- The serial monitor should show a trigger log message.

---

### Arduino Firmware Validation Checklist

- [ ] Arduino IDE installed via `scripts/setup.sh` option 4.
- [ ] Firmware uploads successfully.
- [ ] Arduino appears as `/dev/ttyACM0`.
- [ ] Serial monitor opens at 115200 baud.
- [ ] Startup JSON message appears.
- [ ] Encoder rotation produces trigger JSON messages with `speed_cms` field.
- [ ] Relay Channel 1 activates during trigger events.
- [ ] Cameras capture images when trigger events occur.

---

## 7.4 Application Deployment

This section covers deploying application updates and configuring the station after the initial install described in Section 7.1.

---

### Configuration File

The application reads its configuration from `configuration.toml` in the project root.

This file is not tracked by git. Each deployment keeps its own copy.

Key settings for a MindVision trigger station:

```toml
[server]
env = "prod"          # Use "prod" for structured JSON logs on a live station

[camera]
type = "mindvision"   # Must be set to mindvision for this station

[hw_trigger]
serial_port         = "/dev/ttyACM0"   # Arduino serial port
serial_baud         = 115200
destination_url     = "http://<server-ip>:<port>/upload"  # Image upload endpoint
destination_api_key = ""               # Bearer token if required by the server
save_local          = true             # Also save captured images locally
local_save_dir      = "data/hw_captures"
local_max_files     = 200
local_max_mb        = 500
```

Edit `configuration.toml` to match the station environment before starting the service.

---

### Verify the Service is Running

After installation or reboot, verify the application service:

```bash
sudo systemctl status rpi-capture
```

To follow the live log:

```bash
journalctl -u rpi-capture -f
```

The application is accessible on port **8080**:

```
http://<raspberry-pi-ip>:8080
```

---

### Applying Application Updates

To update the application to a newer version:

```bash
cd ~/visionx-rpi-capture
git pull
sudo systemctl restart rpi-capture
```

Verify the service restarted cleanly:

```bash
sudo systemctl status rpi-capture
```

No reboot is required for application-only updates. A reboot is only required after SDK or driver changes.

---

### Manual Service Control

| Action | Command |
| ------ | ------- |
| Start service | `sudo systemctl start rpi-capture` |
| Stop service | `sudo systemctl stop rpi-capture` |
| Restart service | `sudo systemctl restart rpi-capture` |
| View live log | `journalctl -u rpi-capture -f` |
| Check API health | `curl http://localhost:8080/api/health` |

---

### Decoder Auto-Start Behavior

When the application starts, it checks whether the Arduino serial port (`/dev/ttyACM0`) is present.

If the port is present, the application automatically:

1. Sets all cameras to hardware trigger mode.
2. Starts the serial decoder listener.

No manual action is required to start the decoder after a reboot, provided the Arduino is connected before the Raspberry Pi boots.

If the Arduino is connected after the application has already started, use the web portal or POST to `/api/decoder/detect` to start the decoder manually.

---

# 8. Validation and Testing

This section describes validation checks after hardware assembly and wiring.

---

## 8.1 Hardware Validation

### Pre-Power Inspection

Before powering the system:

- [ ] Encoder VCC is connected to Arduino 5V.
- [ ] Encoder 0V is connected to Arduino GND.
- [ ] Encoder A is connected to Arduino D2.
- [ ] Encoder B is connected to Arduino D3.
- [ ] Relay DC+ is connected to Arduino 5V.
- [ ] Relay DC- is connected to Arduino GND.
- [ ] Relay IN1 is connected to Arduino D9.
- [ ] Relay IN2 is connected to Arduino D9.
- [ ] Relay IN3 is connected to Arduino D9.
- [ ] Relay COM1 is connected to Arduino GND.
- [ ] Relay COM2 is connected to Arduino GND.
- [ ] Relay COM3 is connected to Arduino GND.
- [ ] Relay NO1 is connected to Camera 1 TRIG-.
- [ ] Relay NO2 is connected to Camera 2 TRIG-.
- [ ] Relay NO3 is connected to Camera 3 TRIG-.
- [ ] Camera 1, Camera 2, and Camera 3 TRIG+ are connected to Arduino 5V.
- [ ] Camera red power wires are not connected to Arduino.
- [ ] Unused trigger cable wires are insulated.
- [ ] No loose wires are touching other terminals.

### Power-On Inspection

After powering the system:

- [ ] Raspberry Pi powers on.
- [ ] Arduino powers on through USB.
- [ ] Relay module power indicator is on.
- [ ] Cameras power on through their power adapters.
- [ ] USB hub powers on.

### Relay Trigger Check

When the Arduino sends a trigger signal:

- [ ] Relay Channels 1, 2, and 3 all activate.
- [ ] All three relay channel indicator LEDs change state.
- [ ] Three relay clicks are heard, if using a mechanical relay module.
- [ ] All three cameras receive the trigger event.

### Encoder Mechanical Check

Rotate the encoder wheel by hand and verify:

- [ ] Encoder wheel rotates smoothly.
- [ ] Encoder wheel does not slip on the shaft.
- [ ] Encoder mount does not move.
- [ ] Encoder wheel maintains contact with the conveyor or driven surface.

### Camera Connection Check

Verify:

- [ ] Camera 1 USB cable is connected to the USB 3.0 hub.
- [ ] Camera 2 USB cable is connected to the USB 3.0 hub.
- [ ] Camera 3 USB cable is connected to the USB 3.0 hub.
- [ ] USB 3.0 hub is connected to the Raspberry Pi USB 3.0 port.
- [ ] Each camera has a power adapter connected.
- [ ] Each camera has a trigger cable connected.

---

## 8.2 Software Validation

### Application Service Check

Verify the application service is running:

```bash
sudo systemctl status rpi-capture
```

Expected: `Active: active (running)`

If the service is not running, check logs for errors:

```bash
journalctl -u rpi-capture -f
```

---

### API Health Check

Verify the application is responding:

```bash
curl http://localhost:8080/api/health
```

Expected response:

```json
{"status": "ok"}
```

---

### Camera Detection Check

Verify the Raspberry Pi detects all three cameras:

```bash
lsusb
```

All three MindVision cameras must appear as USB devices. If a camera is missing, verify its USB and power connections.

Open the web portal and verify all three cameras are listed:

```
http://<raspberry-pi-ip>:8080
```

---

### Decoder Status Check

Verify the Arduino decoder is running and connected:

```bash
curl http://localhost:8080/api/decoder/status
```

Expected fields in the response:

| Field | Expected Value |
| ----- | -------------- |
| `running` | `true` |
| `port_present` | `true` |
| `serial_connected` | `true` |

If `running` is `false`, the decoder did not auto-start. POST to `/api/decoder/detect` to start it, or verify the Arduino USB cable is connected.

---

### Software Validation Checklist

- [ ] `rpi-capture` service is active and running.
- [ ] `GET /api/health` returns `{"status": "ok"}`.
- [ ] All three cameras appear in `lsusb` output.
- [ ] All three cameras are listed in the web portal.
- [ ] `GET /api/decoder/status` shows `running: true` and `serial_connected: true`.
- [ ] No errors in `journalctl -u rpi-capture`.

---

## 8.3 End-to-End Testing

This procedure verifies the full signal path from encoder movement through to camera capture and image delivery.

Complete Sections 8.1 and 8.2 before proceeding.

---

### Pre-Test Setup

1. Verify the Arduino is connected to the Raspberry Pi USB port.
2. Verify all three cameras are connected and powered.
3. Verify the relay wiring is complete per Section 6.
4. Verify the application service is running and cameras are detected (Section 8.2).

---

### Step 1 — Verify Decoder is Running

Open the web portal:

```
http://<raspberry-pi-ip>:8080
```

Confirm the decoder status shows **Running** and **Serial connected**.

Alternatively, check via API:

```bash
curl http://localhost:8080/api/decoder/status
```

If the decoder is not running, POST to start it:

```bash
curl -X POST http://localhost:8080/api/decoder/detect
```

---

### Step 2 — Rotate the Encoder

Slowly rotate the encoder wheel by hand or move the conveyor through a short distance.

Observe the serial monitor or application log:

```bash
journalctl -u rpi-capture -f
```

Expected log output for each trigger event:

```json
{"type":"trigger","source":"encoder","count":118,"trigger":1,"speed_cms":5.20}
```

Verify that:

- [ ] Trigger log messages appear in the application log.
- [ ] The relay Channel 1 activates (LED changes or click is heard).
- [ ] The `trigger` count increments with each trigger event.

---

### Step 3 — Verify Camera Captures

After trigger events, verify that images were captured.

Check the local capture directory:

```bash
ls -lh data/hw_captures/
```

Verify:

- [ ] Image files appear after each trigger event.
- [ ] The number of new images matches the number of trigger events.
- [ ] Images can be opened and are not corrupted.

If `save_local` is enabled in `configuration.toml`, images are saved to `data/hw_captures/` in the project root.

---

### Step 4 — Verify Upload Delivery (if configured)

If a `destination_url` is configured in `configuration.toml`, verify that captured images are received by the destination server.

Check the application log for upload confirmation:

```bash
journalctl -u rpi-capture -f
```

Check the destination server for received images.

Verify:

- [ ] Upload log messages appear without errors.
- [ ] Destination server receives images for each trigger event.

---

### End-to-End Test Checklist

- [ ] Decoder is running and serial connected before test.
- [ ] Encoder rotation produces trigger log messages.
- [ ] Relay activates during each trigger event.
- [ ] Image files appear in `data/hw_captures/` after trigger events.
- [ ] Images are not corrupted and can be opened.
- [ ] Upload delivery confirmed (if `destination_url` is configured).
- [ ] No errors in `journalctl -u rpi-capture` during the test.

---

# 9. Troubleshooting

This section covers common hardware assembly and wiring issues.

---

## 9.1 Arduino Does Not Power On

Possible causes:

- USB A-to-B cable not connected.
- Raspberry Pi USB port not powered.
- Faulty USB cable.
- Arduino power issue.

Checks:

1. Verify the Arduino USB cable is connected to the Raspberry Pi.
2. Try a different Raspberry Pi USB port.
3. Try a different USB A-to-B cable.
4. Verify the Arduino power LED turns on.

---

## 9.2 Relay Does Not Activate

Possible causes:

- Relay DC+ not connected to Arduino 5V.
- Relay DC- not connected to Arduino GND.
- Relay IN1 not connected to Arduino D9.
- Relay trigger mode does not match firmware.
- Arduino firmware not running.

Checks:

1. Verify Relay DC+ is connected to Arduino 5V.
2. Verify Relay DC- is connected to Arduino GND.
3. Verify Relay IN1, IN2, and IN3 are all connected to Arduino D9.
4. Verify the relay Channel 1, 2, and 3 indicators all change during a trigger event.
5. If the relay supports high-level / low-level trigger selection, verify the jumper setting matches the firmware on all three channels.

---

## 9.3 Camera Does Not Trigger

Possible causes:

- Camera trigger mode not enabled in software.
- Camera TRIG+ not connected to Arduino 5V.
- Camera TRIG- not connected to its relay NOx terminal.
- Relay COMx not connected to Arduino GND.
- Camera trigger cable connected to wrong camera or wrong wire.
- Relay not activating.

Hardware checks:

1. Verify camera TRIG+ is connected to Arduino 5V.
2. Verify camera TRIG- is connected to its dedicated relay NO terminal (NO1 for Camera 1, NO2 for Camera 2, NO3 for Camera 3).
3. Verify the corresponding relay COM terminal is connected to Arduino GND.
4. Verify the relay activates when a trigger event occurs.
5. Verify the camera trigger cable is connected to the correct camera.
6. Verify only the required trigger wires are used.

Software checks:

1. Check the decoder is running:

```bash
curl http://localhost:8080/api/decoder/status
```

Verify `running` is `true` and `serial_connected` is `true`.

2. Check the camera trigger mode:

The response from `/api/decoder/status` includes a `camera_mode` field. It must be `hardware_trigger` during normal operation.

3. Verify the Arduino is detected on the correct serial port:

```bash
ls /dev/ttyACM0
```

If the port is absent, verify the Arduino USB cable.

4. Check the application log for trigger events:

```bash
journalctl -u rpi-capture -f
```

Rotate the encoder and verify trigger log messages appear.

5. Use the diagnostics endpoint to fire a software trigger and confirm camera response:

```bash
curl http://localhost:8080/api/decoder/diag
```

The response reports `sw_trigger_ok` for each camera. If `false`, the camera is not responding to trigger signals.

---

## 9.4 Only One Camera Triggers

Possible causes:

- One camera TRIG+ is not connected to Arduino 5V.
- One camera TRIG- is not connected to its relay NO terminal.
- The corresponding relay IN terminal is not connected to Arduino D9.
- The corresponding relay COM terminal is not connected to Arduino GND.
- Camera cable labels were swapped.
- One camera is not powered or not configured.

Checks:

1. Verify Camera 1, Camera 2, and Camera 3 TRIG+ are all connected to Arduino 5V.
2. Verify Camera 1 TRIG- is connected to Relay NO1, Camera 2 TRIG- to NO2, Camera 3 TRIG- to NO3.
3. Verify Relay IN1, IN2, and IN3 are all connected to Arduino D9.
4. Verify Relay COM1, COM2, and COM3 are all connected to Arduino GND.
5. Verify each camera power adapter is connected.
6. Verify each camera USB cable is connected.

---

## 9.5 Encoder Count Does Not Change

Possible causes:

- Encoder VCC not connected.
- Encoder 0V not connected.
- Encoder A or B not connected to correct Arduino pins.
- Encoder wheel not contacting conveyor.
- Encoder wheel slipping on shaft.
- Arduino firmware not running.

Checks:

1. Verify Encoder VCC is connected to Arduino 5V.
2. Verify Encoder 0V is connected to Arduino GND.
3. Verify Encoder A is connected to Arduino D2.
4. Verify Encoder B is connected to Arduino D3.
5. Rotate encoder wheel manually and observe software/serial output.
6. Verify encoder wheel is mechanically secured to the encoder shaft.

---

## 9.6 Camera Not Detected by Raspberry Pi

Possible causes:

- Camera USB cable not connected.
- USB hub not connected to Raspberry Pi USB 3.0 port.
- Camera power adapter not connected.
- USB hub issue.
- Camera SDK or driver issue.

Hardware checks:

1. Verify camera USB cable is connected to the USB 3.0 hub.
2. Verify USB 3.0 hub is connected to Raspberry Pi USB 3.0 port.
3. Verify camera power adapter is connected.
4. Try another USB 3.0 cable.
5. Try another USB hub port.

Software checks:

1. Verify the MindVision SDK is installed:

```bash
ls /lib/libMVSDK.so
ls /etc/udev/rules.d/88-mvusb.rules
```

If missing, run the setup script and select option **3 — Install MindVision**.

2. Verify the camera appears as a USB device:

```bash
lsusb
```

If a camera is not listed, verify its USB cable and power adapter. Try another USB hub port.

3. Check the application log for camera detection errors at startup:

```bash
journalctl -u rpi-capture | grep -i "camera"
```

Look for `mindvision_cameras_detected` with the expected count, or `mindvision_enumerate_failed` if the SDK cannot see the cameras.

4. Verify udev rules are loaded:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Disconnect and reconnect the camera USB cable after reloading rules.

5. If cameras are still not detected after the above steps, reboot the Raspberry Pi with the cameras connected and powered.

---

# 10. Appendix

## 10.1 Signal Definitions

| Signal    | Description                                       |
| --------- | ------------------------------------------------- |
| VCC       | Positive supply voltage for encoder or module     |
| 0V        | Ground / negative supply reference                |
| GND       | Ground / negative supply reference                |
| A         | Encoder quadrature channel A                      |
| B         | Encoder quadrature channel B                      |
| DC+       | Relay module positive supply input                |
| DC-       | Relay module negative supply input                |
| IN1       | Relay Channel 1 control input (connected to D9)   |
| IN2       | Relay Channel 2 control input (connected to D9)   |
| IN3       | Relay Channel 3 control input (connected to D9)   |
| COM1      | Relay Channel 1 common contact                    |
| COM2      | Relay Channel 2 common contact                    |
| COM3      | Relay Channel 3 common contact                    |
| NO1       | Relay Channel 1 normally open contact → Camera 1  |
| NO2       | Relay Channel 2 normally open contact → Camera 2  |
| NO3       | Relay Channel 3 normally open contact → Camera 3  |
| NC1       | Relay Channel 1 normally closed contact; not used |
| NC2       | Relay Channel 2 normally closed contact; not used |
| NC3       | Relay Channel 3 normally closed contact; not used |
| TRIG+     | Camera trigger input positive                     |
| TRIG-     | Camera trigger input negative                     |
| USB_CAM1  | USB cable for Camera 1                            |
| USB_CAM2  | USB cable for Camera 2                            |
| USB_CAM3  | USB cable for Camera 3                            |
| TRIG_CAM1 | Trigger cable for Camera 1                        |
| TRIG_CAM2 | Trigger cable for Camera 2                        |
| TRIG_CAM3 | Trigger cable for Camera 3                        |

---

## 10.2 Optional Development Features

The following features are useful during development and debugging but are not required for the production station.

### Manual Override Button

A manual override button can be connected to the Arduino for development testing.

| Button Signal     | Arduino Connection |
| ----------------- | ------------------ |
| Button Terminal 1 | Arduino D4         |
| Button Terminal 2 | Arduino GND        |

Purpose:

- Manually trigger the camera system during debugging
- Test relay operation
- Test camera trigger response without conveyor movement

This button is optional and should not be included in the standard production wiring unless specifically requested.

### Debug LED

A debug LED can be connected during development to indicate trigger output activity.

This LED is optional and should not be included in production wiring unless specifically requested.

---

## 10.3 Revision History

| Version | Date       | Author      | Notes                                                        |
| ------- | ---------- | ----------- | ------------------------------------------------------------ |
| 0.1     | 2026-06-03 | Chongju Mai | Initial draft — hardware assembly, wiring, firmware, software validation, troubleshooting |
