# Camera Mounting Guide

This guide covers how to mount the cameras and set the correct position before calibration.

---

## Before You Start

Make sure the following are ready before mounting:

- The inspection machine is powered off.
- The garment roll is loaded into the machine.
- The VisionX system (Raspberry Pi) is set up and ready to power on.

---

## Step 1: Mount the Cameras

Mount each camera so it points **straight down** at the fabric surface.

**What "straight down" means:**
Look at the camera from the side. The lens must face directly downward — not tilted forward, backward, or to either side.

**How to check:**
Hold a small spirit level against the side of the camera body. The bubble must sit in the centre. Adjust the mount angle until it does.

**Height:**
All three cameras must be at the same height from the fabric surface. Use a measuring tape to confirm the distance from each camera lens down to the top of the roll is equal for Camera 1, Camera 2, and Camera 3.

> **Do not fully tighten the mounting bolts yet.** You will need to slide the cameras in the next step.

### Pre-Power Checklist

- [ ] Camera 1 points straight down (level confirmed)
- [ ] Camera 2 points straight down (level confirmed)
- [ ] Camera 3 points straight down (level confirmed)
- [ ] All three cameras are at the same height above the roll surface
- [ ] Mounting bolts are hand-tight only — not fully locked

---

## Step 2: Power On and Open the Live Feed

1. Power on the VisionX system.
2. Open a web browser and go to the VisionX portal:
   ```
   http://<raspberry-pi-ip>:8080
   ```
3. Click **Stitch** in the top navigation bar.
4. You should see a live feed from all three cameras side by side.

> If the camera images are black, go to the Home page and turn off the **HW Trigger** switch. Then return to the Stitch tab.

---

## Step 3: Adjust Camera Positions Using the Live Feed

Use the live feed to check and adjust how much the cameras overlap each other.

**What is overlap?**
Overlap is the strip of fabric that two neighbouring cameras can both see at the same time. A small amount of overlap is needed so the system can join the images together. Too much overlap wastes coverage and makes joining harder to tune.

**What you are aiming for:**
Each pair of neighbouring cameras should overlap just enough to cover the full width of the roll — with no gap between them and no large overlap zone.

**How to adjust:**

1. Loosen the mounting bolt on the camera you want to move.
2. Slide the camera left or right while watching the live feed on screen.
3. Stop when the overlap between cameras is as small as possible while still covering the edge of the roll surface.
4. Fully tighten the mounting bolt.
5. Repeat for all camera pairs.

**Check the full roll width is covered:**

- The left edge of Camera 1's image must reach the left edge of the roll.
- The right edge of Camera 3's image must reach the right edge of the roll.
- There must be no uncovered strip between any two cameras.

### Final Mounting Checklist

- [ ] Camera 1 covers the left edge of the roll
- [ ] Camera 3 covers the right edge of the roll
- [ ] Camera 1 and Camera 2 overlap just enough — no gap, no large overlap
- [ ] Camera 2 and Camera 3 overlap just enough — no gap, no large overlap
- [ ] All mounting bolts are fully tightened
- [ ] Camera positions do not move when touched or when the machine vibrates

---

## After Mounting

Proceed to the calibration guide:

[Camera Calibration Guide](calibration.md)

> **Important:** Do not move or adjust the cameras after this point. If a camera is moved, the full calibration must be repeated.
