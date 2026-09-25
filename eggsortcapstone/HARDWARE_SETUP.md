# EggSort Arduino integration

## Supported controllers

- `servo_loadcell_triggers.ino`: sends load-cell readings, final weight, and
  size over serial at 9600 baud.
- `stooper-servo-loadcell.ino`: accepts `start` over serial to advance the
  stopper servo.
- `arduino/eggsort_unified_controller/eggsort_unified_controller.ino`: the
  recommended controller firmware. It classifies all six weight sizes and
  accepts `SORT:PEEWEE`, `SORT:SMALL`, `SORT:MEDIUM`, `SORT:LARGE`,
  `SORT:EXTRA_LARGE`, and `SORT:JUMBO`.

The Flask service can parse the original sketches. Six-way automatic servo
routing requires uploading the unified controller sketch and wiring PCA9685
channels 0 through 5 to the corresponding bin servos.

## Weight standard

| Size | System range |
|---|---:|
| Peewee | Below 42 g |
| Small | 42–49 g |
| Medium | 50–56 g |
| Large | 57–63 g |
| Extra Large | 64–70 g |
| Jumbo | 71 g and above |

The supplied ranges overlap at 49 g and 56 g. EggSort uses ordered,
non-overlapping boundaries: 49 g is Small and 56 g is Medium.

## Connect the hardware

1. Upload each sketch to its Arduino.
2. Connect the load-cell Arduino and note its Windows COM port.
3. If used, connect the stopper Arduino and note its separate COM port.
4. Close Arduino Serial Monitor and Serial Plotter. Only one application can
   own a COM port at a time.
5. Set the ports before starting Flask:

```powershell
$env:ARDUINO_LOADCELL_PORT = "COM6"
$env:ARDUINO_STOPPER_PORT = "COM6"
.\.venv\Scripts\python.exe app.py
```

`ARDUINO_LOADCELL_PORT` may be omitted when exactly one Arduino is connected;
EggSort will discover it automatically. The stopper port is optional and must
be configured to use the **Advance Stopper** button.

## Real-time data flow

1. **Start Session** starts the camera, YOLO, and serial reader.
2. The load-cell Arduino emits `Egg Detected` and three weight readings.
3. YOLO continuously keeps the recent `good`, `dirty`, or `demage` result.
4. When the Arduino emits `SIZE` or `Egg Left`, EggSort combines the stable
   weight with the strongest camera quality seen in the preceding four seconds.
5. The combined record is stored in SQLite and appears automatically in both
   Sorting Sessions and Egg Records.
6. **Stop Session** closes the camera and serial reader.

The current model label `demage` is normalized to `Damaged` in saved records.

## Optional settings

```powershell
$env:ARDUINO_BAUD_RATE = "9600"
$env:CAMERA_INDEX = "0"
$env:YOLO_CONFIDENCE = "0.25"
$env:YOLO_IMAGE_SIZE = "512"
```

If the Arduino status says access is denied, close Arduino IDE's Serial Monitor,
then wait a few seconds. The bridge reconnects automatically.
