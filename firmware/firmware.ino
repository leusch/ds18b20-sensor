#include <OneWire.h>
#include <DallasTemperature.h>

const uint8_t ONE_WIRE_PIN = 2;  // DS18B20 data line on D2

OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature sensors(&oneWire);

void setup() {
  Serial.begin(9600);
  sensors.begin();
  Serial.print("DS18B20 sensors found: ");
  Serial.println(sensors.getDeviceCount());
}

void loop() {
  sensors.requestTemperatures();
  float t = sensors.getTempCByIndex(0);
  if (t == DEVICE_DISCONNECTED_C) {
    Serial.println("No sensor detected - check wiring and pull-up resistor");
  } else {
    Serial.print("Temperature: ");
    Serial.print(t, 2);
    Serial.println(" C");
  }
  delay(1000);
}
