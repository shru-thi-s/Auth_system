#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <NimBLEDevice.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
#define BUZZER_PIN 26
#define BLE_SCAN_SECONDS 5
#define ENABLE_SERIAL_FALLBACK true

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

const char* WIFI_SSID = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* API_URL = "http://YOUR_PC_IP:8000/predict";
const char* KEYBOARD_MAC = "";  // Optional: "AA:BB:CC:DD:EE:FF"

const char* DEVICE_ID = "esp32-01";
const char* SESSION_ID = "live-session-001";

unsigned long keyTimestamps[20];
int tsCount = 0;
unsigned long lastKeyTime = 0;
unsigned long lastAlertMs = 0;
volatile int pendingKeyEvents = 0;
bool bleConnected = false;
unsigned long sessionEventCount = 0;
float sessionMeanInterval = 0.0f;
float sessionM2 = 0.0f;
unsigned long sessionPauseCount = 0;
bool apiHealthy = true;

NimBLEAdvertisedDevice* discoveredKeyboard = nullptr;
NimBLEClient* bleClient = nullptr;
NimBLERemoteCharacteristic* inputReportChar = nullptr;
uint8_t previousKeys[6] = {0, 0, 0, 0, 0, 0};

const NimBLEUUID HID_SERVICE_UUID("1812");
const NimBLEUUID REPORT_CHAR_UUID("2a4d");

bool containsKey(const uint8_t* keys, uint8_t keyCode) {
  for (int i = 0; i < 6; i++) {
    if (keys[i] == keyCode) {
      return true;
    }
  }
  return false;
}

void queueKeyEvent() {
  pendingKeyEvents++;
}

void onKeyboardReport(NimBLERemoteCharacteristic* remoteChar, uint8_t* data, size_t length, bool isNotify) {
  (void)remoteChar;
  (void)isNotify;

  if (length < 8) {
    return;
  }

  uint8_t currentKeys[6];
  for (int i = 0; i < 6; i++) {
    currentKeys[i] = data[i + 2];
  }

  for (int i = 0; i < 6; i++) {
    uint8_t keyCode = currentKeys[i];
    if (keyCode != 0 && !containsKey(previousKeys, keyCode)) {
      queueKeyEvent();
    }
  }

  for (int i = 0; i < 6; i++) {
    previousKeys[i] = currentKeys[i];
  }
}

class KeyboardScanCallbacks : public NimBLEAdvertisedDeviceCallbacks {
  void onResult(NimBLEAdvertisedDevice* device) override {
    if (!device->isAdvertisingService(HID_SERVICE_UUID)) {
      return;
    }

    if (strlen(KEYBOARD_MAC) > 0) {
      String addr = String(device->getAddress().toString().c_str());
      addr.toUpperCase();
      String expected = String(KEYBOARD_MAC);
      expected.toUpperCase();
      if (addr != expected) {
        return;
      }
    }

    discoveredKeyboard = device;
    NimBLEDevice::getScan()->stop();
  }
};

bool connectToKeyboard() {
  discoveredKeyboard = nullptr;
  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(new KeyboardScanCallbacks(), true);
  scan->setActiveScan(true);
  scan->start(BLE_SCAN_SECONDS, false);

  if (discoveredKeyboard == nullptr) {
    return false;
  }

  bleClient = NimBLEDevice::createClient();
  if (!bleClient->connect(discoveredKeyboard)) {
    return false;
  }

  NimBLERemoteService* hidService = bleClient->getService(HID_SERVICE_UUID);
  if (hidService == nullptr) {
    bleClient->disconnect();
    return false;
  }

  inputReportChar = hidService->getCharacteristic(REPORT_CHAR_UUID);
  if (inputReportChar == nullptr || !inputReportChar->canNotify()) {
    bleClient->disconnect();
    return false;
  }

  if (!inputReportChar->subscribe(true, onKeyboardReport)) {
    bleClient->disconnect();
    return false;
  }

  for (int i = 0; i < 6; i++) {
    previousKeys[i] = 0;
  }

  bleConnected = true;
  return true;
}

void recordKeyEvent(unsigned long nowMs) {
  if (lastKeyTime > 0 && tsCount < 20) {
    unsigned long interval = nowMs - lastKeyTime;
    keyTimestamps[tsCount++] = interval;

    sessionEventCount++;
    float delta = (float)interval - sessionMeanInterval;
    sessionMeanInterval += delta / (float)sessionEventCount;
    float delta2 = (float)interval - sessionMeanInterval;
    sessionM2 += delta * delta2;

    if (interval > 500) {
      sessionPauseCount++;
    }
  }
  lastKeyTime = nowMs;
}

float getSessionStd() {
  if (sessionEventCount < 2) {
    return 0.0f;
  }
  return sqrt(sessionM2 / (float)sessionEventCount);
}

void showOfflineFallback(float meanInterval, float stdInterval, float wpm, float pauseRatio) {
  const float meanBoundsLow = 45.0f;
  const float meanBoundsHigh = 450.0f;
  const float stdMax = 250.0f;
  const float wpmLow = 15.0f;
  const float wpmHigh = 130.0f;
  const float pauseMax = 0.45f;

  float score = 0.0f;
  if (meanInterval < meanBoundsLow || meanInterval > meanBoundsHigh) score += 0.35f;
  if (stdInterval > stdMax) score += 0.20f;
  if (wpm < wpmLow || wpm > wpmHigh) score += 0.25f;
  if (pauseRatio > pauseMax) score += 0.20f;

  char line2[32];
  snprintf(line2, sizeof(line2), "Offline: %.2f", score);

  if (score >= 0.5f) {
    showStatus("ALERT! Offline", line2);
    if (millis() - lastAlertMs > 2500) {
      setBuzzer(true);
      delay(250);
      setBuzzer(false);
      lastAlertMs = millis();
    }
  } else {
    showStatus("Typing Normal", line2);
    setBuzzer(false);
  }
}

float computeMean(const float* arr, int n) {
  if (n <= 0) return 0;
  float sum = 0;
  for (int i = 0; i < n; i++) sum += arr[i];
  return sum / n;
}

float computeStd(const float* arr, int n, float mean) {
  if (n <= 1) return 0;
  float s = 0;
  for (int i = 0; i < n; i++) {
    float d = arr[i] - mean;
    s += d * d;
  }
  return sqrt(s / n);
}

void showStatus(const char* line1, const char* line2) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 8);
  display.println(line1);
  display.setCursor(0, 28);
  display.println(line2);
  display.display();
}

void setBuzzer(bool on) {
  digitalWrite(BUZZER_PIN, on ? HIGH : LOW);
}

void processFeatureWindow() {
  if (tsCount < 8) {
    return;
  }

  float intervals[20];
  int pauseCount = 0;
  for (int i = 0; i < tsCount; i++) {
    intervals[i] = (float)keyTimestamps[i];
    if (intervals[i] > 500) {
      pauseCount++;
    }
  }

  float meanInterval = computeMean(intervals, tsCount);
  float stdInterval = computeStd(intervals, tsCount, meanInterval);
  float wpm = 12000.0 / max(meanInterval, 1.0f);
  float pauseRatio = (float)pauseCount / (float)tsCount;

  if (WiFi.status() != WL_CONNECTED) {
    apiHealthy = false;
    showStatus("WiFi disconnected", "Offline mode");
    showOfflineFallback(meanInterval, stdInterval, wpm, pauseRatio);
    WiFi.reconnect();
    tsCount = 0;
    return;
  }

  HTTPClient http;
  http.begin(API_URL);
  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<256> req;
  req["device_id"] = DEVICE_ID;
  req["session_id"] = SESSION_ID;
  JsonObject features = req.createNestedObject("features");
  features["mean_interval"] = meanInterval;
  features["std_interval"] = stdInterval;
  features["wpm"] = wpm;
  features["pause_ratio"] = pauseRatio;

  String body;
  serializeJson(req, body);

  int code = http.POST(body);
  if (code > 0) {
    apiHealthy = true;
    String response = http.getString();
    StaticJsonDocument<256> resp;
    DeserializationError err = deserializeJson(resp, response);
    if (!err) {
      const char* status = resp["status"] | "normal";
      float score = resp["score"] | 0.0;

      char line2[32];
      snprintf(line2, sizeof(line2), "Score: %.2f", score);

      if (strcmp(status, "anomaly") == 0) {
        showStatus("ALERT! Unusual", line2);
        if (millis() - lastAlertMs > 2500) {
          setBuzzer(true);
          delay(250);
          setBuzzer(false);
          lastAlertMs = millis();
        }
      } else {
        showStatus("Typing Normal", line2);
        setBuzzer(false);
      }
    }
  } else {
    apiHealthy = false;
    showOfflineFallback(meanInterval, stdInterval, wpm, pauseRatio);
  }

  http.end();
  if (tsCount > 10) {
    for (int i = 10; i < tsCount; i++) {
      keyTimestamps[i - 10] = keyTimestamps[i];
    }
    tsCount = 10;
  } else {
    tsCount = 0;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(BUZZER_PIN, OUTPUT);
  setBuzzer(false);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    for (;;) {
      delay(1000);
    }
  }

  showStatus("Connecting WiFi...", "Please wait");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
  }

  NimBLEDevice::init("ESP32-Typing-Monitor");
  showStatus("BLE keyboard", "Scanning...");
  bool connected = connectToKeyboard();

  if (connected) {
    showStatus("Typing Monitor", "BLE connected");
    Serial.println("BLE keyboard connected.");
  } else if (ENABLE_SERIAL_FALLBACK) {
    showStatus("BLE not found", "Serial fallback");
    Serial.println("BLE keyboard not found. Serial fallback enabled.");
  } else {
    showStatus("BLE not found", "Retry in loop");
    Serial.println("BLE keyboard not found. Will retry.");
  }

  delay(800);
}

void loop() {
  if ((!bleConnected || (bleClient != nullptr && !bleClient->isConnected())) && !connectToKeyboard()) {
    if (!ENABLE_SERIAL_FALLBACK) {
      showStatus("BLE reconnecting", "Waiting keyboard");
      delay(500);
      return;
    }
  }

  while (pendingKeyEvents > 0) {
    pendingKeyEvents--;
    recordKeyEvent(millis());
  }

  if (ENABLE_SERIAL_FALLBACK && Serial.available() > 0) {
    char c = Serial.read();
    if (c != '\n' && c != '\r') {
      recordKeyEvent(millis());
    }
  }

  processFeatureWindow();
  delay(20);
}
