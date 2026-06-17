/*
 * ============================================================
 *  ESP32 智慧温室主控制器
 * ============================================================
 *  硬件: ESP32-DevKitC + DHT11 + SSD1306 OLED (128x64)
 *  控制: 4路继电器(风扇/灯/浇水/升温)
 *  配网: WiFiManager
 *  通信: HTTP POST/GET 与 Flask服务器交互
 * ============================================================
 *  依赖库: WiFiManager, ArduinoJson, DHT, U8g2
 * ============================================================
 */

#include <WiFiManager.h>
#include <HTTPClient.h>3
#include <ArduinoJson.h>
#include <DHT.h>
#include <U8g2lib.h>

// DHT11 传感器
#define DHT_PIN    15  // GPIO15 (改用空闲引脚，避免GPIO4冲突)
#define DHT_TYPE   DHT11
DHT dht(DHT_PIN, DHT_TYPE);

// OLED 屏幕 (I2C)
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE, 22, 21);

// 继电器引脚
#define RELAY_FAN     13
#define RELAY_LIGHT   12
#define RELAY_WATER   14
#define RELAY_HEATER  27

// 服务器配置（修改为你的电脑实际IP）
const char* SERVER_IP = "10.19.136.xxx";  // ← 修改这里！
const int SERVER_PORT = 5000;
const char* DEVICE_ID = "esp32_greenhouse_01";

// 时间间隔
const unsigned long SENSOR_INTERVAL = 3000;
const unsigned long COMMAND_INTERVAL = 1000;
const unsigned long OLED_INTERVAL = 3000;

// 全局状态
struct DeviceState {
    bool fan = false;
    bool light = false;
    bool water = false;
    bool heater = false;
} deviceState;

struct SensorData {
    float temperature = 0;
    float humidity = 0;
    uint32_t light = 0;
    uint16_t co2 = 0;
    float soilMoisture = 0;
    float soilTemperature = -1;  // 土壤温度（未接入时为 -1）
    bool valid = false;
} sensorData;

unsigned long lastSensorUpload = 0;
unsigned long lastCommandCheck = 0;
unsigned long lastOledUpdate = 0;
int oledPage = 0;
WiFiManager wm;

// ========================
void setup() {
    Serial.begin(115200);
    delay(1000);

    Serial.println("========================================");
    Serial.println("  ESP32 智慧温室主控制器");
    Serial.println("========================================");

    initPins();
    
    // DHT11 初始化并等待预热（至少2秒）
    Serial.println("初始化DHT11传感器...");
    dht.begin();
    delay(2000);  // 关键：等待传感器预热
    
    initOLED();
    initWiFi();

    Serial.println("系统初始化完成");
}

void initPins() {
    pinMode(RELAY_FAN, OUTPUT);
    pinMode(RELAY_LIGHT, OUTPUT);
    pinMode(RELAY_WATER, OUTPUT);
    pinMode(RELAY_HEATER, OUTPUT);

    digitalWrite(RELAY_FAN, LOW);
    digitalWrite(RELAY_LIGHT, LOW);
    digitalWrite(RELAY_WATER, LOW);
    digitalWrite(RELAY_HEATER, LOW);

    Serial.println("继电器初始化完成");
}

void initOLED() {
    u8g2.begin();
    u8g2.setFont(u8g2_font_unifont_t_chinese2);
    u8g2.clearBuffer();

    u8g2.firstPage();
    do {
        u8g2.drawStr(8, 20, "智慧温室系统");
        u8g2.drawStr(18, 40, "初始化中...");
        u8g2.drawStr(8, 60, "v2.0");
    } while (u8g2.nextPage());

    Serial.println("OLED初始化完成");
}

void initWiFi() {
    wm.setConfigPortalTimeout(180);
    wm.setAPStaticIPConfig(IPAddress(192,168,4,1), IPAddress(192,168,4,1), IPAddress(255,255,255,0));

    bool res = wm.autoConnect("Greenhouse-Setup", "12345678");

    if (res) {
        Serial.println("WiFi连接成功!");
        Serial.print("IP: ");
        Serial.println(WiFi.localIP());
    } else {
        Serial.println("WiFi配网失败，重启...");
        delay(3000);
        ESP.restart();
    }
}

// ========================
// 读取 DHT11 传感器。
// 注意：光照 / CO2 / 土壤湿度 目前没有真实硬件，统一发送 -1 标记为"未接入"。
// 后端会把这些值保留为 0 或原始值，前端/控制台判断为 -1 时显示为"未接入"。
bool readDHTSensor() {
    const int maxRetries = 3;
    int retry = 0;
    float t = NAN;
    float h = NAN;

    while (retry < maxRetries) {
        t = dht.readTemperature();
        h = dht.readHumidity();

        if (!isnan(t) && !isnan(h) && t > 0 && h >= 0 && h <= 100) {
            sensorData.temperature = t;
            sensorData.humidity = h;
            // 以下传感器未连接：用 -1 标记未接入，避免 random() 伪造误导
            sensorData.light = -1;
            sensorData.co2 = -1;
            sensorData.soilMoisture = -1;
            sensorData.soilTemperature = -1;  // 土壤温度传感器未接入
            sensorData.valid = true;
            return true;
        }

        retry++;
        delay(200);
        Serial.printf("DHT11读取失败，重试第%d次...\n", retry);
    }

    Serial.println("DHT11读取失败（已重试3次）");
    sensorData.valid = false;
    return false;
}

void uploadSensorData() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi未连接");
        return;
    }

    HTTPClient http;
    String url = String("http://") + SERVER_IP + ":" + SERVER_PORT + "/api/sensor";

    http.begin(url);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(5000);

    StaticJsonDocument<512> doc;
    doc["device_id"] = DEVICE_ID;
    doc["temperature"] = sensorData.temperature;
    doc["humidity"] = sensorData.humidity;
    doc["light"] = sensorData.light;
    doc["co2"] = sensorData.co2;
    doc["soil_moisture"] = sensorData.soilMoisture;
    doc["soil_temperature"] = sensorData.soilTemperature;

    JsonObject devices = doc.createNestedObject("devices");
    devices["fan"] = deviceState.fan;
    devices["light"] = deviceState.light;
    devices["water"] = deviceState.water;
    devices["heater"] = deviceState.heater;

    String jsonString;
    serializeJson(doc, jsonString);

    int httpCode = http.POST(jsonString);

    if (httpCode > 0) {
        Serial.printf("[HTTP] 上报成功: %d\n", httpCode);
    } else {
        Serial.printf("[HTTP] 上报失败: %d\n", httpCode);
    }

    http.end();
}

void checkAndExecuteCommands() {
    if (WiFi.status() != WL_CONNECTED) return;

    HTTPClient http;
    String url = String("http://") + SERVER_IP + ":" + SERVER_PORT + "/api/commands?device=" + DEVICE_ID;
    http.begin(url);
    http.setTimeout(5000);

    int httpCode = http.GET();

    if (httpCode == 200) {
        String payload = http.getString();
        StaticJsonDocument<1024> doc;
        DeserializationError error = deserializeJson(doc, payload);

        if (!error) {
            if (doc.is<JsonArray>()) {
                for (JsonObject cmd : doc.as<JsonArray>()) executeCommand(cmd);
            } else if (doc.is<JsonObject>()) {
                executeCommand(doc.as<JsonObject>());
            }
        }
    }

    http.end();
}

void executeCommand(JsonObject doc) {
    if (doc.containsKey("fan")) {
        deviceState.fan = doc["fan"];
        digitalWrite(RELAY_FAN, deviceState.fan ? HIGH : LOW);
        Serial.printf("风扇: %s\n", deviceState.fan ? "开" : "关");
    }
    if (doc.containsKey("light")) {
        deviceState.light = doc["light"];
        digitalWrite(RELAY_LIGHT, deviceState.light ? HIGH : LOW);
        Serial.printf("补光灯: %s\n", deviceState.light ? "开" : "关");
    }
    if (doc.containsKey("water")) {
        deviceState.water = doc["water"];
        digitalWrite(RELAY_WATER, deviceState.water ? HIGH : LOW);
        Serial.printf("浇水: %s\n", deviceState.water ? "开" : "关");
    }
    if (doc.containsKey("heater")) {
        deviceState.heater = doc["heater"];
        digitalWrite(RELAY_HEATER, deviceState.heater ? HIGH : LOW);
        Serial.printf("升温: %s\n", deviceState.heater ? "开" : "关");
    }
    if (doc.containsKey("all_off") && doc["all_off"]) {
        allDevicesOff();
    }
}

void allDevicesOff() {
    deviceState.fan = deviceState.light = deviceState.water = deviceState.heater = false;
    digitalWrite(RELAY_FAN, LOW);
    digitalWrite(RELAY_LIGHT, LOW);
    digitalWrite(RELAY_WATER, LOW);
    digitalWrite(RELAY_HEATER, LOW);
    Serial.println("所有设备已关闭");
}

void updateOLED() {
    unsigned long now = millis();

    if (now - lastOledUpdate >= OLED_INTERVAL) {
        lastOledUpdate = now;
        oledPage = (oledPage + 1) % 4;
    }

    if (!sensorData.valid) return;

    u8g2.clearBuffer();

    if (oledPage == 0) {
        u8g2.setFont(u8g2_font_unifont_t_chinese2);
        u8g2.drawStr(0, 14, "━━ 实时环境 ━━");

        char buf[32];
        u8g2.setFont(u8g2_font_fur42_tn);
        snprintf(buf, sizeof(buf), "%.1f", sensorData.temperature);
        u8g2.drawStr(0, 52, buf);
        u8g2.setFont(u8g2_font_unifont_t_chinese2);
        u8g2.drawStr(90, 52, "*C");

        u8g2.setFont(u8g2_font_fur30_tn);
        snprintf(buf, sizeof(buf), "%.0f%%", sensorData.humidity);
        u8g2.drawStr(0, 64, buf);
        u8g2.drawStr(80, 64, "湿度");

    } else if (oledPage == 1) {
        u8g2.setFont(u8g2_font_unifont_t_chinese2);
        u8g2.drawStr(0, 14, "━━ 光照 & CO2 ━━");

        char buf[32];
        u8g2.setFont(u8g2_font_fur20_tn);
        u8g2.drawStr(0, 34, "光照:");
        snprintf(buf, sizeof(buf), "%lu lux", sensorData.light);
        u8g2.drawStr(55, 34, buf);

        u8g2.drawStr(0, 56, "CO2:");
        snprintf(buf, sizeof(buf), "%u ppm", sensorData.co2);
        u8g2.drawStr(55, 56, buf);

    } else if (oledPage == 2) {
        u8g2.setFont(u8g2_font_unifont_t_chinese2);
        u8g2.drawStr(0, 14, "━━ 设备状态 ━━");

        char buf[32];
        u8g2.setFont(u8g2_font_fur20_tn);
        u8g2.drawStr(0, 34, "土壤:");
        snprintf(buf, sizeof(buf), "%.1f%%", sensorData.soilMoisture);
        u8g2.drawStr(55, 34, buf);

        u8g2.setFont(u8g2_font_unifont_t_chinese2);
        u8g2.drawStr(0, 50, deviceState.fan ? "[风扇 ON ]" : "[风扇 OFF]");
        u8g2.drawStr(0, 64, deviceState.light ? "[补光 ON ]" : "[补光 OFF]");

    } else if (oledPage == 3) {
        u8g2.setFont(u8g2_font_unifont_t_chinese2);
        u8g2.drawStr(0, 14, "━━ 系统信息 ━━");

        char buf[40];
        u8g2.setFont(u8g2_font_5x8_tr);
        if (WiFi.status() == WL_CONNECTED) {
            snprintf(buf, sizeof(buf), "IP: %s", WiFi.localIP().toString().c_str());
            u8g2.drawStr(0, 28, buf);
        } else {
            u8g2.drawStr(0, 28, "WiFi: 断开");
        }

        snprintf(buf, sizeof(buf), "RSSI: %d dBm", WiFi.RSSI());
        u8g2.drawStr(0, 40, buf);
        u8g2.drawStr(0, 52, DEVICE_ID);

        u8g2.setFont(u8g2_font_unifont_t_chinese2);
        snprintf(buf, sizeof(buf), "%.0fs", millis() / 1000.0);
        u8g2.drawStr(0, 64, "运行:");
        u8g2.drawStr(72, 64, buf);
    }

    u8g2.sendBuffer();
}

void printSensorData() {
    Serial.println("┌────────────────────────────────────────┐");
    Serial.println("│           DHT11 传感器数据              │");
    Serial.println("├────────────────────────────────────────┤");
    Serial.printf("│  温度:      %.1f °C\n", sensorData.temperature);
    Serial.printf("│  湿度:      %.1f %%\n", sensorData.humidity);
    Serial.printf("│  光照:      %lu lux\n", sensorData.light);
    Serial.printf("│  CO2:       %u ppm\n", sensorData.co2);
    Serial.printf("│  土壤湿度:  %.1f %%\n", sensorData.soilMoisture);
    Serial.println("└────────────────────────────────────────┘");
}

// ========================
void loop() {
    unsigned long now = millis();

    if (now - lastSensorUpload >= SENSOR_INTERVAL) {
        lastSensorUpload = now;
        if (readDHTSensor()) {
            printSensorData();
            uploadSensorData();
        }
    }

    if (now - lastCommandCheck >= COMMAND_INTERVAL) {
        lastCommandCheck = now;
        checkAndExecuteCommands();
    }

    updateOLED();

    delay(10);
}
