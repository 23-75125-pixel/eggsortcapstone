#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include "HX711.h"

// EggSort unified load-cell and six-bin servo controller.
// Upload this sketch to the Arduino connected as ARDUINO_LOADCELL_PORT.

Adafruit_PWMServoDriver pwm = Adafruit_PWMServoDriver();

const int SERVO_HOME = 150;
const int SERVO_TRIGGER = 425;
const byte SERVO_CHANNELS[6] = {
  0,  // Peewee
  1,  // Small
  2,  // Medium
  3,  // Large
  4,  // Extra Large
  5   // Jumbo
};
const int SERVO_HOLD_MS[6] = {300, 300, 350, 400, 500, 600};

const byte DOUT = 2;
const byte CLK = 3;
HX711 scale;
float calibration_factor = 618.0;

bool eggDetected = false;
bool measurementComplete = false;
int readings[3];
byte readingIndex = 0;
String measuredSize = "";

String classifySize(int weight) {
  if (weight < 42) return "PEEWEE";
  if (weight <= 49) return "SMALL";
  if (weight <= 56) return "MEDIUM";
  if (weight <= 63) return "LARGE";
  if (weight <= 70) return "EXTRA_LARGE";
  return "JUMBO";
}

int sizeIndex(String sizeName) {
  if (sizeName == "PEEWEE") return 0;
  if (sizeName == "SMALL") return 1;
  if (sizeName == "MEDIUM") return 2;
  if (sizeName == "LARGE") return 3;
  if (sizeName == "EXTRA_LARGE") return 4;
  if (sizeName == "JUMBO") return 5;
  return -1;
}

void triggerSizeServo(String sizeName) {
  int index = sizeIndex(sizeName);
  if (index < 0) {
    Serial.println("SERVO ERROR : UNKNOWN SIZE");
    return;
  }

  byte channel = SERVO_CHANNELS[index];
  pwm.setPWM(channel, 0, SERVO_TRIGGER);
  delay(SERVO_HOLD_MS[index]);
  pwm.setPWM(channel, 0, SERVO_HOME);

  Serial.print("SERVO SORTED : ");
  Serial.println(sizeName);
}

void handleSerialCommands() {
  if (!Serial.available()) return;

  String command = Serial.readStringUntil('\n');
  command.trim();
  command.toUpperCase();

  if (command.startsWith("SORT:")) {
    String requestedSize = command.substring(5);
    triggerSizeServo(requestedSize);
    return;
  }

  if (command == "PING") {
    Serial.println("PONG");
    return;
  }

  Serial.print("UNKNOWN COMMAND : ");
  Serial.println(command);
}

void setup() {
  Serial.begin(9600);
  Serial.setTimeout(100);

  pwm.begin();
  pwm.setPWMFreq(50);
  for (byte index = 0; index < 6; index++) {
    pwm.setPWM(SERVO_CHANNELS[index], 0, SERVO_HOME);
  }

  scale.begin(DOUT, CLK);
  scale.set_scale(calibration_factor);
  scale.tare();

  Serial.println("Egg Sorting Ready");
}

void loop() {
  handleSerialCommands();

  float rawWeight = scale.get_units(10);
  if (rawWeight > -1 && rawWeight < 1) rawWeight = 0;
  int weight = round(rawWeight);

  if (eggDetected && weight < 10) {
    Serial.println("Egg Left");
    eggDetected = false;
    measurementComplete = false;
    readingIndex = 0;
    measuredSize = "";
    delay(100);
    return;
  }

  if (!eggDetected && weight >= 10) {
    eggDetected = true;
    measurementComplete = false;
    readingIndex = 0;
    Serial.println("Egg Detected");
  }

  if (!eggDetected || measurementComplete) {
    delay(20);
    return;
  }

  readings[readingIndex] = weight;
  Serial.print("Reading ");
  Serial.print(readingIndex + 1);
  Serial.print(": ");
  Serial.print(weight);
  Serial.println(" g");
  readingIndex++;

  if (readingIndex < 3) {
    delay(50);
    return;
  }

  int finalWeight = round(
    (readings[0] + readings[1] + readings[2]) / 3.0
  );
  measuredSize = classifySize(finalWeight);
  measurementComplete = true;

  Serial.println("====================");
  Serial.print("FINAL WEIGHT : ");
  Serial.print(finalWeight);
  Serial.println(" g");
  Serial.print("SIZE : ");
  Serial.println(measuredSize);
  Serial.println("====================");
  Serial.println("WAITING FOR SORT COMMAND");
}
