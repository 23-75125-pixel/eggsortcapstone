#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include "HX711.h"

// EggSort+ ESP32 controller
//
// The ESP32 owns the load cell and every servo. The PC owns the camera,
// quality model, database, and dashboard. One egg follows this handshake:
//   ESP32 -> Egg Detected
//   PC    -> MEASURE:CRACK | GOOD | ROTTEN | UNDEFINED
//   ESP32 -> FINAL WEIGHT / SIZE
//   PC    -> SORT:<SIZE>
//   ESP32 -> SERVO SORTED / Egg Left

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver(0x40);
HX711 scale;

// ESP32 wiring.
static const uint8_t SDA_PIN = 21;
static const uint8_t SCL_PIN = 22;
static const uint8_t HX711_DOUT_PIN = 19;
static const uint8_t HX711_CLK_PIN = 18;

// PCA9685 channels from the proven mechanical sketch.
static const uint8_t LOADCELL_SERVO = 1;
static const uint8_t SMALL_SERVO = 5;
static const uint8_t MEDIUM_SERVO = 2;
static const uint8_t LARGE_SERVO = 4;
static const uint8_t EXTRA_LARGE_SERVO = 3;

// Proven load-cell gate settings.
static const int LOADCELL_CLOSED = 320;
static const int LOADCELL_OPEN = 195;
static const int LOADCELL_OPEN_SPEED = 15;
static const unsigned long LOADCELL_OPEN_TIME = 3000;

// Proven sorting-gate settings (degrees).
static const int SERVO_MIN = 150;
static const int SERVO_MAX = 600;
static const int SMALL_CLOSED = 80;
static const int SMALL_OPEN = 0;
static const int MEDIUM_CLOSED = 0;
static const int MEDIUM_OPEN = 80;
static const int LARGE_CLOSED = 80;
static const int LARGE_OPEN = 0;
static const int EXTRA_LARGE_CLOSED = 0;
static const int EXTRA_LARGE_OPEN = 80;
static const int SIZE_CLOSE_SPEED = 20;
static const unsigned long SIZE_GATE_OPEN_TIME = 1500;
static const unsigned long SM_TRAVEL_TIME = 1900;
static const unsigned long LX_TRAVEL_TIME = 5000;

static const float CALIBRATION_FACTOR = 605.0f;
static const int EGG_PRESENT_THRESHOLD_GRAMS = 15;
static const int EGG_CLEAR_THRESHOLD_GRAMS = 5;
static const int STABLE_TOLERANCE_GRAMS = 2;
static const uint8_t STABLE_SAMPLE_COUNT = 3;
static const int INVALID_WEIGHT = -10000;
static const unsigned long SAMPLE_INTERVAL_MS = 600;
static const unsigned long STATUS_INTERVAL_MS = 15000;
static const unsigned long IDLE_WEIGHT_INTERVAL_MS = 3000;

bool eggDetected = false;
bool measurementAuthorized = false;
bool measurementReady = false;
bool sorting = false;
bool pcaReady = false;
bool hx711Ready = false;

String lockedQuality = "";
String measuredSize = "";
int readingNumber = 0;
int finalWeight = 0;
int emptyReadingCount = 0;
int occupiedReadingCount = 0;
int stableWeights[STABLE_SAMPLE_COUNT] = {0, 0, 0};
uint8_t stableWeightCount = 0;
unsigned long lastReadingAt = 0;
unsigned long lastStatusAt = 0;

int angleToPulse(int angle) {
  angle = constrain(angle, 0, 180);
  return map(angle, 0, 180, SERVO_MIN, SERVO_MAX);
}

void setServoAngle(uint8_t channel, int angle) {
  pwm.setPWM(channel, 0, angleToPulse(angle));
}

void moveServoSlow(uint8_t channel, int fromAngle, int toAngle, int delayMs) {
  int step = (toAngle >= fromAngle) ? 1 : -1;
  for (int angle = fromAngle; angle != toAngle; angle += step) {
    setServoAngle(channel, angle);
    delay(delayMs);
  }
  setServoAngle(channel, toAngle);
}

void moveLoadCellServoSlow(int fromPulse, int toPulse, int delayMs) {
  int step = (toPulse >= fromPulse) ? 1 : -1;
  for (int pulse = fromPulse; pulse != toPulse; pulse += step) {
    pwm.setPWM(LOADCELL_SERVO, 0, pulse);
    delay(delayMs);
  }
  pwm.setPWM(LOADCELL_SERVO, 0, toPulse);
}

int readWeight() {
  // HX711 DOUT goes high between conversions. A one-shot is_ready() check can
  // therefore report a healthy 10 SPS module as unavailable. Wait through the
  // conversion window before deciding that the sensor is disconnected.
  if (!scale.wait_ready_timeout(1000)) {
    hx711Ready = false;
    return INVALID_WEIGHT;
  }

  hx711Ready = true;
  // A reversed A+/A- load-cell connection changes only the sign. Using the
  // magnitude lets the calibrated scale work with either polarity.
  float units = fabs(scale.get_units(5));
  if (isnan(units) || isinf(units)) return INVALID_WEIGHT;
  if (units < 1.5f) units = 0.0f;
  return (int)round(units);
}

void addStableWeight(int weight) {
  if (stableWeightCount < STABLE_SAMPLE_COUNT) {
    stableWeights[stableWeightCount++] = weight;
    return;
  }
  for (uint8_t index = 1; index < STABLE_SAMPLE_COUNT; index++) {
    stableWeights[index - 1] = stableWeights[index];
  }
  stableWeights[STABLE_SAMPLE_COUNT - 1] = weight;
}

bool stableWeightAvailable() {
  if (stableWeightCount < STABLE_SAMPLE_COUNT) return false;
  int minimum = stableWeights[0];
  int maximum = stableWeights[0];
  for (uint8_t index = 1; index < STABLE_SAMPLE_COUNT; index++) {
    minimum = min(minimum, stableWeights[index]);
    maximum = max(maximum, stableWeights[index]);
  }
  return maximum - minimum <= STABLE_TOLERANCE_GRAMS;
}

int averagedStableWeight() {
  long total = 0;
  for (uint8_t index = 0; index < STABLE_SAMPLE_COUNT; index++) {
    total += stableWeights[index];
  }
  return (int)round((float)total / STABLE_SAMPLE_COUNT);
}

String classifySize(int weight) {
  if (weight < 42) return "PEEWEE";
  if (weight <= 49) return "SMALL";
  if (weight <= 56) return "MEDIUM";
  if (weight <= 63) return "LARGE";
  if (weight <= 70) return "EXTRA_LARGE";
  return "JUMBO";
}

bool validQuality(const String &quality) {
  return quality == "CRACK" || quality == "GOOD" ||
         quality == "ROTTEN" || quality == "UNDEFINED";
}

bool validSize(const String &size) {
  return size == "PEEWEE" || size == "SMALL" || size == "MEDIUM" ||
         size == "LARGE" || size == "EXTRA_LARGE" || size == "JUMBO";
}

void closeAllServos() {
  if (!pcaReady) return;
  pwm.setPWM(LOADCELL_SERVO, 0, LOADCELL_CLOSED);
  setServoAngle(SMALL_SERVO, SMALL_CLOSED);
  setServoAngle(MEDIUM_SERVO, MEDIUM_CLOSED);
  setServoAngle(LARGE_SERVO, LARGE_CLOSED);
  setServoAngle(EXTRA_LARGE_SERVO, EXTRA_LARGE_CLOSED);
}

void resetEggState() {
  eggDetected = false;
  measurementAuthorized = false;
  measurementReady = false;
  sorting = false;
  lockedQuality = "";
  measuredSize = "";
  readingNumber = 0;
  finalWeight = 0;
  emptyReadingCount = 0;
  occupiedReadingCount = 0;
  stableWeightCount = 0;
  for (uint8_t index = 0; index < STABLE_SAMPLE_COUNT; index++) {
    stableWeights[index] = 0;
  }
  lastReadingAt = 0;
  lastStatusAt = millis();
}

void printHardwareStatus() {
  int weight = readWeight();
  Serial.print("HX711 READY : ");
  Serial.println(hx711Ready ? "YES" : "NO");
  Serial.print("PCA9685 READY : ");
  Serial.println(pcaReady ? "YES" : "NO");
  if (weight == INVALID_WEIGHT) {
    Serial.println("LIVE WEIGHT : UNAVAILABLE");
  } else {
    Serial.print("LIVE WEIGHT : ");
    Serial.print(weight);
    Serial.println(" g");
  }
  Serial.print("CONTROLLER STATE : ");
  if (sorting) Serial.println("SORTING");
  else if (measurementReady) Serial.println("WAITING FOR SORT");
  else if (measurementAuthorized) Serial.println("MEASURING");
  else if (eggDetected) Serial.println("WAITING FOR CAMERA");
  else Serial.println("IDLE");
}

void activateRouteServo(const String &size) {
  // Peewee and Small share the first physical chute. Jumbo continues
  // straight because this four-gate mechanism has no separate Jumbo gate.
  if (size == "PEEWEE" || size == "SMALL") {
    moveServoSlow(SMALL_SERVO, SMALL_CLOSED, SMALL_OPEN, SIZE_CLOSE_SPEED);
    delay(SIZE_GATE_OPEN_TIME);
    moveServoSlow(SMALL_SERVO, SMALL_OPEN, SMALL_CLOSED, SIZE_CLOSE_SPEED);
  } else if (size == "MEDIUM") {
    moveServoSlow(MEDIUM_SERVO, MEDIUM_CLOSED, MEDIUM_OPEN, SIZE_CLOSE_SPEED);
    delay(SIZE_GATE_OPEN_TIME);
    moveServoSlow(MEDIUM_SERVO, MEDIUM_OPEN, MEDIUM_CLOSED, SIZE_CLOSE_SPEED);
  } else if (size == "LARGE") {
    moveServoSlow(LARGE_SERVO, LARGE_CLOSED, LARGE_OPEN, SIZE_CLOSE_SPEED);
    delay(SIZE_GATE_OPEN_TIME);
    moveServoSlow(LARGE_SERVO, LARGE_OPEN, LARGE_CLOSED, SIZE_CLOSE_SPEED);
  } else if (size == "EXTRA_LARGE") {
    moveServoSlow(EXTRA_LARGE_SERVO, EXTRA_LARGE_CLOSED,
                  EXTRA_LARGE_OPEN, SIZE_CLOSE_SPEED);
    delay(SIZE_GATE_OPEN_TIME);
    moveServoSlow(EXTRA_LARGE_SERVO, EXTRA_LARGE_OPEN,
                  EXTRA_LARGE_CLOSED, SIZE_CLOSE_SPEED);
  }
}

void waitForEggToLeave() {
  int clearSamples = 0;
  unsigned long startedAt = millis();

  while (clearSamples < 3 && millis() - startedAt < 15000) {
    int weight = readWeight();
    if (weight != INVALID_WEIGHT && weight <= EGG_CLEAR_THRESHOLD_GRAMS) {
      clearSamples++;
    } else {
      clearSamples = 0;
    }
    delay(250);
  }

  Serial.println("Egg Left");
  resetEggState();
}

void performSort(const String &requestedSize) {
  if (!pcaReady) {
    Serial.println("SORT REJECTED: PCA9685 NOT FOUND");
    return;
  }
  sorting = true;

  String routeSize = measuredSize;
  if (requestedSize != measuredSize) {
    Serial.print("SORT SIZE MISMATCH; USING MEASURED SIZE: ");
    Serial.println(measuredSize);
  }

  Serial.print("SORTING : ");
  Serial.println(routeSize);

  // Release the egg from the load cell with the working slow-open motion.
  moveLoadCellServoSlow(LOADCELL_CLOSED, LOADCELL_OPEN,
                        LOADCELL_OPEN_SPEED);
  delay(LOADCELL_OPEN_TIME);
  pwm.setPWM(LOADCELL_SERVO, 0, LOADCELL_CLOSED);

  if (routeSize == "PEEWEE" || routeSize == "SMALL" ||
      routeSize == "MEDIUM") {
    delay(SM_TRAVEL_TIME);
  } else if (routeSize == "LARGE" || routeSize == "EXTRA_LARGE") {
    delay(LX_TRAVEL_TIME);
  }

  activateRouteServo(routeSize);

  Serial.print("SERVO SORTED : ");
  Serial.println(routeSize);
  waitForEggToLeave();
}

void advanceLoadCellGate() {
  if (eggDetected || sorting) {
    Serial.println("ADVANCE REJECTED: EGG CYCLE ACTIVE");
    return;
  }
  if (!pcaReady) {
    Serial.println("ADVANCE REJECTED: PCA9685 NOT FOUND");
    return;
  }

  Serial.println("ADVANCE STARTED");
  moveLoadCellServoSlow(LOADCELL_CLOSED, LOADCELL_OPEN,
                        LOADCELL_OPEN_SPEED);
  delay(LOADCELL_OPEN_TIME);
  pwm.setPWM(LOADCELL_SERVO, 0, LOADCELL_CLOSED);
  Serial.println("ADVANCE COMPLETE");
}

void handleSerialCommands() {
  while (Serial.available() > 0) {
    String command = Serial.readStringUntil('\n');
    command.trim();
    command.toUpperCase();

    if (command.length() == 0) {
      continue;
    }

    if (command == "PING") {
      Serial.println("PONG");
      continue;
    }

    if (command == "STATUS") {
      printHardwareStatus();
      continue;
    }

    if (command == "TARE") {
      if (eggDetected || sorting) {
        Serial.println("TARE REJECTED: REMOVE EGG FIRST");
      } else if (!scale.wait_ready_timeout(1000)) {
        Serial.println("TARE REJECTED: HX711 NOT READY");
      } else {
        scale.tare(15);
        resetEggState();
        Serial.println("TARE COMPLETE");
        printHardwareStatus();
      }
      continue;
    }

    if (command == "ADVANCE") {
      advanceLoadCellGate();
      continue;
    }

    if (command.startsWith("MEASURE:")) {
      String quality = command.substring(8);
      quality.trim();

      if (!eggDetected) {
        Serial.println("MEASURE REJECTED: NO EGG");
      } else if (measurementReady || sorting) {
        Serial.println("MEASURE REJECTED: CYCLE ALREADY MEASURED");
      } else if (!validQuality(quality)) {
        Serial.println("MEASURE REJECTED: INVALID QUALITY");
      } else {
        lockedQuality = quality;
        measurementAuthorized = true;
        stableWeightCount = 0;
        readingNumber = 0;
        lastReadingAt = 0;
        Serial.print("CAMERA QUALITY : ");
        Serial.println(lockedQuality);
        Serial.println("MEASUREMENT STARTED");
      }
      continue;
    }

    if (command.startsWith("SORT:")) {
      String requestedSize = command.substring(5);
      requestedSize.trim();
      requestedSize.replace(" ", "_");

      if (!measurementReady) {
        Serial.println("SORT REJECTED: WEIGHT NOT READY");
      } else if (sorting) {
        Serial.println("SORT REJECTED: SORT ALREADY ACTIVE");
      } else if (!validSize(requestedSize)) {
        Serial.println("SORT REJECTED: INVALID SIZE");
      } else {
        performSort(requestedSize);
      }
      continue;
    }

    Serial.print("UNKNOWN COMMAND : ");
    Serial.println(command);
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(100);

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.beginTransmission(0x40);
  pcaReady = Wire.endTransmission() == 0;
  if (pcaReady) {
    pwm.begin();
    pwm.setPWMFreq(50);
    delay(10);
    closeAllServos();
  }

  scale.begin(HX711_DOUT_PIN, HX711_CLK_PIN);
  unsigned long hxStartedAt = millis();
  while (!scale.is_ready() && millis() - hxStartedAt < 3000) {
    delay(25);
  }
  hx711Ready = scale.is_ready();
  if (hx711Ready) {
    scale.set_scale(CALIBRATION_FACTOR);
    scale.tare(15);
  }

  resetEggState();
  Serial.println("Egg Sorting Ready");
  printHardwareStatus();
}

void loop() {
  handleSerialCommands();

  if (sorting) {
    return;
  }

  unsigned long now = millis();

  if (!eggDetected) {
    if (now - lastReadingAt < 350) {
      return;
    }
    lastReadingAt = now;

    int weight = readWeight();
    if (weight != INVALID_WEIGHT &&
        weight >= EGG_PRESENT_THRESHOLD_GRAMS) {
      occupiedReadingCount++;
    } else {
      occupiedReadingCount = 0;
    }

    if (weight != INVALID_WEIGHT &&
        now - lastStatusAt >= IDLE_WEIGHT_INTERVAL_MS) {
      lastStatusAt = now;
      Serial.print("LIVE WEIGHT : ");
      Serial.print(weight);
      Serial.println(" g");
    }

    if (occupiedReadingCount >= 2) {
      eggDetected = true;
      stableWeightCount = 0;
      readingNumber = 0;
      emptyReadingCount = 0;
      occupiedReadingCount = 0;
      lastStatusAt = now;
      Serial.println("Egg Detected");
      Serial.println("WAITING FOR CAMERA QUALITY");
    }
    return;
  }

  if (!measurementAuthorized && !measurementReady) {
    if (now - lastReadingAt >= 500) {
      lastReadingAt = now;
      int weight = readWeight();
      if (weight != INVALID_WEIGHT && weight <= EGG_CLEAR_THRESHOLD_GRAMS) {
        emptyReadingCount++;
        if (emptyReadingCount >= 3) {
          Serial.println("Egg Left");
          resetEggState();
          return;
        }
      } else {
        emptyReadingCount = 0;
      }
    }

    if (now - lastStatusAt >= STATUS_INTERVAL_MS) {
      lastStatusAt = now;
      Serial.println("WAITING FOR CAMERA QUALITY");
    }
    return;
  }

  if (measurementAuthorized && !measurementReady) {
    if (now - lastReadingAt < SAMPLE_INTERVAL_MS) {
      return;
    }
    lastReadingAt = now;

    int weight = readWeight();
    if (weight == INVALID_WEIGHT) {
      Serial.println("LOAD CELL NOT READY");
      return;
    }

    if (weight <= EGG_CLEAR_THRESHOLD_GRAMS) {
      emptyReadingCount++;
      if (emptyReadingCount >= 3) {
        Serial.println("Egg Left");
        resetEggState();
      }
      return;
    }
    emptyReadingCount = 0;

    readingNumber++;
    Serial.print("Reading ");
    Serial.print(readingNumber);
    Serial.print(": ");
    Serial.print(weight);
    Serial.println(" g");

    addStableWeight(weight);

    if (stableWeightAvailable()) {
      finalWeight = averagedStableWeight();
      measuredSize = classifySize(finalWeight);
      measurementAuthorized = false;
      measurementReady = true;
      lastStatusAt = now;

      Serial.print("FINAL WEIGHT : ");
      Serial.print(finalWeight);
      Serial.println(" g");
      Serial.print("SIZE : ");
      Serial.println(measuredSize);
      Serial.println("WAITING FOR SORT COMMAND");
    }
    return;
  }

  if (measurementReady && now - lastStatusAt >= STATUS_INTERVAL_MS) {
    lastStatusAt = now;
    Serial.println("WAITING FOR SORT COMMAND");
  }
}
