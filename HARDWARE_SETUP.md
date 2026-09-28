# EggSort+ ESP32 hardware setup

EggSort+ now uses one ESP32 for the HX711 load cell, the PCA9685 servo board,
the load-cell release gate, and all four actuated size gates. The ESP32 talks
to the Flask application over its USB serial connection at 115200 baud.

The firmware is:

`esp32/eggsort_controller/eggsort_controller.ino`

## Required parts

- ESP32 development board (the firmware targets the common ESP32 Dev Module)
- HX711 load-cell amplifier and load cell
- PCA9685 16-channel PWM servo driver
- Five servos: one load-cell gate plus Small, Medium, Large, and Extra Large
- Regulated 5-6 V servo power supply sized for the combined servo stall current
- USB data cable for the ESP32
- Common ground wiring and suitable terminal blocks/connectors

Do not power the servos from the ESP32 3.3 V pin or USB 5 V pin. Servo current
can reset or damage the controller. Use the external servo supply on PCA9685
`V+`, and connect all grounds together.

## Wiring

### ESP32 to HX711

| ESP32 | HX711 | Purpose |
|---|---|---|
| `3V3` | `VCC` | HX711 logic/power |
| `GND` | `GND` | Common ground |
| GPIO `19` | `DT` / `DOUT` | Load-cell data |
| GPIO `18` | `SCK` / `CLK` | Load-cell clock |

Connect the load cell to `E+`, `E-`, `A+`, and `A-` on the HX711 according to
the load-cell manufacturer's diagram. Wire colors are not standardized.

### ESP32 to PCA9685 logic

| ESP32 | PCA9685 | Purpose |
|---|---|---|
| `3V3` | `VCC` | PCA9685 logic voltage |
| `GND` | `GND` | Common ground |
| GPIO `21` | `SDA` | I2C data |
| GPIO `22` | `SCL` | I2C clock |

Leave the PCA9685 at its default I2C address `0x40`. If the board exposes
`OE`, connect it to ground or leave it in the module's enabled default state.

### Servo power and channels

Connect the external regulated supply positive output to PCA9685 `V+` and its
negative output to PCA9685 `GND`. Connect that same ground to ESP32 `GND`.

| PCA9685 channel | Gate |
|---:|---|
| 1 | Load-cell release gate |
| 5 | Small gate (Peewee also uses this physical chute) |
| 2 | Medium gate |
| 4 | Large gate |
| 3 | Extra Large gate |
| none | Jumbo continues straight to the final chute |

On a standard servo connector, brown/black is normally ground, red is servo
power, and orange/yellow/white is signal. Confirm the markings for each servo.

## ESP32 firmware setup and upload

1. Install Arduino IDE 2.x.
2. In Boards Manager, install **esp32 by Espressif Systems**.
3. In Library Manager, install:
   - **Adafruit PWM Servo Driver Library**
   - **Adafruit BusIO**
   - **HX711 Arduino Library**
4. Open `esp32/eggsort_controller/eggsort_controller.ino`.
5. Select **ESP32 Dev Module** (or the exact ESP32 board being used).
6. Select its COM port and upload.
7. Open Serial Monitor at **115200 baud**. With the scale empty, reset the
   ESP32 and confirm that `Egg Sorting Ready` appears.
8. Close Serial Monitor before starting Flask. Only one program can own the
   COM port at a time.

If no port appears, use a known USB data cable and install the driver for the
board's USB-to-serial chip, commonly CP210x or CH340.

## Application configuration

The project `.env` is configured for the ESP32 firmware:

```dotenv
AUTO_START_SORTING_ON_LOGIN=1
ESP32_BAUD_RATE=115200
ESP32_PORT=
```

When exactly one USB serial controller is attached, `ESP32_PORT` can remain
blank. To specify it, list the ports in PowerShell:

```powershell
Get-CimInstance Win32_SerialPort | Select-Object DeviceID, Description
```

Then set the detected port, for example:

```dotenv
ESP32_PORT=COM6
```

Restart Flask whenever `.env` changes.

## Run and verify the complete system

From the project folder:

```powershell
.\.venv\Scripts\python.exe app.py
```

1. Open `http://127.0.0.1:5000` and sign in. A successful login starts the
   camera, YOLO detector, and ESP32 serial bridge automatically. Camera startup
   loads the file configured by `YOLO_MODEL_PATH` and rejects the session unless
   its embedded classes are exactly `Crack`, `Good`, `Rotten`, `Undefined`, and
   `no egg` in that order.
2. Open **Sorting Sessions** and confirm that **ESP32 link** shows
   `Connected on COM... @ 115200`.
3. Put one egg on the load cell. The load-cell gate stays closed and the ESP32
   sends `Egg Detected`; it does not start its final weighing yet.
4. Flask clears detections from the previous egg. From each new inference frame
   it keeps exactly one highest-confidence result from the trained classes
   `Crack`, `Good`, `Rotten`, `Undefined`, and `no egg`.
5. `no egg` is never recorded. The other four labels must agree for three
   consecutive frames before Flask locks one quality for the physical egg.
6. Flask sends `MEASURE:<QUALITY>` to the ESP32.
7. The ESP32 takes three readings, calculates the final weight and size, then
   keeps the egg held while it waits for a route command.
8. Flask combines the locked quality with the weight and sends `SORT:<SIZE>`.
9. The ESP32 opens the load-cell gate, waits for the egg's travel time, moves
   the correct size gate, and sends `SERVO SORTED : <SIZE>`.
10. Only after that hardware confirmation does Flask save one row in Egg
   Records. Dashboard totals update automatically on their next poll.
11. Duplicate controller messages are ignored until `Egg Left` resets the
    cycle, so one physical egg cannot create multiple records.

Logging out stops both the ESP32 serial bridge and camera session. If an
operator manually stops the session, **Restart Camera & ESP32** starts it again.

The web page's **Advance Load-cell Gate** button sends `ADVANCE` to the same
ESP32. Every sensor and servo is controlled through this one controller.

## Weight sizes and physical routes

| Saved size | Weight | Physical route |
|---|---:|---|
| Peewee | below 42 g | Small chute |
| Small | 42-49 g | Small chute |
| Medium | 50-56 g | Medium chute |
| Large | 57-63 g | Large chute |
| Extra Large | 64-70 g | Extra Large chute |
| Jumbo | 71 g and above | Straight/final chute |

Peewee remains a distinct database category, but the supplied mechanism has
only four actuated size gates, so Peewee and Small share a physical chute.

## Calibration and mechanical tuning

The imported ESP32 program used an HX711 calibration factor of `605.0`. Verify
it with a known calibration weight before sorting eggs. If readings are
negative, reverse the load-cell signal pair or use a calibration factor with
the opposite sign.

All mechanism-specific values are near the top of the firmware:

- `calibrationFactor`
- `LOADCELL_CLOSED` and `LOADCELL_OPEN`
- each size gate's `*_CLOSED` and `*_OPEN` angles
- `SM_TRAVEL_TIME_MS` and `LX_TRAVEL_TIME_MS`

Disconnect servo power before changing linkages. Tune one gate at a time with
small angle changes so a servo is not driven against a mechanical stop.

## Troubleshooting

- **Access denied on COM port:** close the IDE Serial Monitor and any other
  serial program, then restart the sorting session.
- **ESP32 repeatedly disconnects or resets:** use a separate servo supply,
  verify common ground, and check the supply's current capacity.
- **No controller found:** set `ESP32_PORT` explicitly and confirm the CP210x
  or CH340 driver is installed.
- **Weight never becomes ready:** check HX711 wiring and calibration, and make
  sure the egg exceeds the 30 g detection threshold.
- **Wrong chute:** verify PCA9685 channel wiring first, then tune gate angles
  and travel time constants.
- **Record saves but gate does not move:** check the latest hardware event for
  `SORT:...`, then verify PCA9685 power, common ground, address `0x40`, and the
  channel mapping above.
