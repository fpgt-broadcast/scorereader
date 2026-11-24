// ScoreReader USB bridge for an Arduino Nano (e.g. a left-over CrunchLabs board).
//
// A cheap stand-in for the ScoreBot on a current-loop console: an H11L1 optocoupler turns the 20 mA loop into a
// logic signal on D8, the Nano re-sends every byte unchanged over USB at 115200 baud, and ScoreReader reads it
// with Source = Serial, Baud = 115200 (whatever the console's own speed is).
//
// Jumpers to GND pick the console speed and polarity, read once at power-up / reset:
//   D4 D5   console baud              D6
//   -- --   19200 (Daktronics)        open = inverted (current loop, the usual case)
//   GND --  2400  (Electro-Mech)      GND  = normal (flip this if --sniff shows garbage)
//   -- GND  9600
//   GND GND 38400
// The built-in LED flickers while data arrives.
#include <SoftwareSerial.h>

const long BAUDS[] = {19200, 2400, 9600, 38400};
SoftwareSerial *console;
unsigned long lastByte = 0;

void setup() {
  pinMode(4, INPUT_PULLUP);
  pinMode(5, INPUT_PULLUP);
  pinMode(6, INPUT_PULLUP);
  pinMode(LED_BUILTIN, OUTPUT);
  byte sel = (digitalRead(4) == LOW) | ((digitalRead(5) == LOW) << 1);
  bool invert = digitalRead(6) == HIGH;  // no jumper on D6 = inverted
  static SoftwareSerial s(8, 9, invert);  // RX D8, TX D9 (unused)
  console = &s;
  console->begin(BAUDS[sel]);
  Serial.begin(115200);  // faster than any console speed above, so the USB side never falls behind
}

void loop() {
  while (console->available()) {
    Serial.write(console->read());
    lastByte = millis();
  }
  digitalWrite(LED_BUILTIN, millis() - lastByte < 50);
}
