#include <WiFi.h>
#include <ModbusMaster.h>

// Khai báo chân LED (kích mức thấp)
#define LED1 5
#define LED2 12
#define LED3 13

// Chân UART2
#define RXD2 17
#define TXD2 16

// Thông tin WiFi
const char* ssid = "iMaker";
const char* password = "imaker12345";

// Modbus
ModbusMaster node;
uint16_t startAddress = 0;
uint8_t quantity = 10;
uint16_t data[10];
char datachar[10];

// Cờ xác nhận WiFi đã kết nối
bool wifiConnected = false;

void setup() {
  Serial.begin(115200);

  // Cấu hình các chân LED
  pinMode(LED1, OUTPUT);
  pinMode(LED2, OUTPUT);
  pinMode(LED3, OUTPUT);
  pinMode(0, INPUT_PULLUP);
  digitalWrite(LED1, HIGH); // Tắt mặc định
  digitalWrite(LED2, HIGH);
  digitalWrite(LED3, HIGH);

  // Kết nối WiFi
  WiFi.begin(ssid, password);
  Serial.print("Đang kết nối WiFi");
  int retry = 0;
  while (WiFi.status() != WL_CONNECTED && retry < 20) {
    delay(500);
    Serial.print(".");
    retry++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\n✅ Đã kết nối WiFi!");
    // Bật 3 LED (kích mức thấp)
    digitalWrite(LED1, LOW); delay(200);
    digitalWrite(LED2, LOW); delay(200);
    digitalWrite(LED3, LOW); delay(200);
    digitalWrite(LED1, HIGH); // Tắt mặc định
    digitalWrite(LED2, HIGH);
    digitalWrite(LED3, HIGH);
    digitalWrite(LED1, LOW); delay(200);
    digitalWrite(LED2, LOW); delay(200);
    digitalWrite(LED3, LOW); delay(200);

    wifiConnected = true;
  } else {
    Serial.println("\n❌ Kết nối WiFi thất bại.");
  }

  // Khởi tạo Modbus nếu WiFi đã kết nối
  if (wifiConnected) {
    Serial2.begin(9600, SERIAL_8N1, RXD2, TXD2);
    node.begin(1, Serial2);
  }
}

void loop() {
  if (!wifiConnected) return;
  if (!digitalRead(0)) Serial.println("button");
  uint8_t result = node.readHoldingRegisters(startAddress, quantity);
  if (result == node.ku8MBSuccess) {
    for (uint8_t j = 0; j < quantity; j++) {
      data[j] = node.getResponseBuffer(j);
      datachar[j] = (char)data[j];
      Serial.print("Register ");
      Serial.print(j);
      Serial.print(": ");
      Serial.println(datachar[j]);
    }
    string_cv();
  } else {
    Serial.print("❌ Modbus read failed. Error code: ");
    Serial.println(result);
  }
  delay(1000); // Đọc mỗi giây
}

void string_cv() {
  String combinedString = "";
  for (int j = 0; j < quantity; j++) {
    if (datachar[j] != ' ') {
      combinedString.concat(datachar[j]);
    }
  }
  combinedString.trim();
  Serial.println("Chuỗi ghép: " + combinedString);
}
