#include <OneWire.h>
#include <DallasTemperature.h>

const uint8_t ONE_WIRE_PIN = 2;  // DS18B20 data line on D2

OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature sensors(&oneWire);

uint8_t lastCount = 255;  // impossible value so the first count is always reported

void printAddress(const DeviceAddress addr) {
  for (uint8_t i = 0; i < 8; i++) {
    if (addr[i] < 16) Serial.print('0');
    Serial.print(addr[i], HEX);
  }
}

void setup() {
  Serial.begin(9600);
}

void loop() {
  // Rescan the bus every cycle so sensors that are plugged in or removed
  // while running are picked up. There is no fixed sensor limit in this code.
  sensors.begin();
  uint8_t count = sensors.getDeviceCount();
  if (count != lastCount) {
    Serial.print("DS18B20 sensors found: ");
    Serial.println(count);
    lastCount = count;
  }

  if (count == 0) {
    Serial.println("No sensor detected - check wiring and pull-up resistor");
    delay(1000);
    return;
  }

  sensors.requestTemperatures();  // one conversion for all sensors on the bus

  for (uint8_t i = 0; i < count; i++) {
    DeviceAddress addr;
    if (!sensors.getAddress(addr, i)) continue;
    float t = sensors.getTempC(addr);

    Serial.print("Sensor ");
    Serial.print(i + 1);
    Serial.print(" [");
    printAddress(addr);
    Serial.print("]: ");
    if (t == DEVICE_DISCONNECTED_C) {
      Serial.println("read error");
    } else {
      Serial.print(t, 2);
      Serial.println(" C");
    }
  }
  delay(1000);
}
