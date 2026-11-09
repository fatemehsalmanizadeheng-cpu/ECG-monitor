// ECG acquisition: ESP32 (WROOM32 DevKit) + AD8232
// Sends "value,leadoff" lines over USB serial at 250 samples/s.
//
// Wiring:
//   AD8232 3.3V   -> ESP32 3V3
//   AD8232 GND    -> ESP32 GND
//   AD8232 OUTPUT -> ESP32 GPIO34
//   AD8232 LO+    -> ESP32 GPIO32
//   AD8232 LO-    -> ESP32 GPIO33
//   AD8232 SDN    -> leave unconnected

const int PIN_OUT = 34;   // analog output of AD8232 (ADC1, input only)
const int PIN_LOP = 32;   // lead-off +
const int PIN_LOM = 33;   // lead-off -

const int FS = 250;                              // samples per second
const unsigned long PERIOD_US = 1000000UL / FS;  // time between samples
unsigned long nextT;

void setup() {
  Serial.begin(115200);
  pinMode(PIN_LOP, INPUT);
  pinMode(PIN_LOM, INPUT);
  analogReadResolution(12);                       // values 0..4095
  analogSetPinAttenuation(PIN_OUT, ADC_11db);     // input range up to ~3.3 V
  nextT = micros();
}

void loop() {
  unsigned long now = micros();
  if ((long)(now - nextT) >= 0) {
    nextT += PERIOD_US;
    int leadOff = (digitalRead(PIN_LOP) == HIGH || digitalRead(PIN_LOM) == HIGH) ? 1 : 0;
    int value = analogRead(PIN_OUT);
    Serial.print(value);
    Serial.print(',');
    Serial.println(leadOff);
  }
}
