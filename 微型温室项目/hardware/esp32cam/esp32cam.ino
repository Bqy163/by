/*
 * ============================================================
 *  ESP32-CAM 智慧温室视频监控 - 完整版
 * ============================================================
 *  硬件: AI-Thinker ESP32-CAM (OV2640)
 *  功能: 实时视频流 + 定时抓拍上传 + WiFi通信
 *  通信: HTTP MJPEG视频流 + POST图像到Flask服务器
 * ============================================================
 */

#include "esp_camera.h"
#include <WiFi.h>
#include <HTTPClient.h>
#include "esp_http_server.h"
#include <ArduinoJson.h>

// ========================
// 配置参数 - 根据实际修改
// ========================

// WiFi配置
const char* WIFI_SSID = "你知道密码？";
const char* WIFI_PASSWORD = "123456789";

// Flask服务器地址（修改为你的电脑实际IP）
const char* SERVER_IP = "10.19.136.xxx";  // ← 修改这里！
const int SERVER_PORT = 5000;

// 设备ID
const char* DEVICE_ID = "esp32cam_01";

// 拍照配置（默认关闭自动拍照）
bool captureEnabled = false;      // 是否启用自动拍照
unsigned long captureInterval = 10000;  // 默认间隔（毫秒）

// 配置更新间隔（秒）
const unsigned long CONFIG_UPDATE_INTERVAL = 30000;  // 30秒更新一次配置

// ========================
// Camera引脚定义 (AI-Thinker)
// ========================

#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22
#define LED_GPIO_NUM       4

// ========================
// HTTP服务器
// ========================

httpd_handle_t stream_httpd = NULL;

// 时间戳
unsigned long lastCapture = 0;
unsigned long lastConfigUpdate = 0;

// WiFi状态
bool wifiConnected = false;

// ========================
// 摄像头初始化
// ========================

void setupCamera() {
    camera_config_t config;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;
    config.pin_d0 = Y2_GPIO_NUM;
    config.pin_d1 = Y3_GPIO_NUM;
    config.pin_d2 = Y4_GPIO_NUM;
    config.pin_d3 = Y5_GPIO_NUM;
    config.pin_d4 = Y6_GPIO_NUM;
    config.pin_d5 = Y7_GPIO_NUM;
    config.pin_d6 = Y8_GPIO_NUM;
    config.pin_d7 = Y9_GPIO_NUM;
    config.pin_xclk = XCLK_GPIO_NUM;
    config.pin_pclk = PCLK_GPIO_NUM;
    config.pin_vsync = VSYNC_GPIO_NUM;
    config.pin_href = HREF_GPIO_NUM;
    config.pin_sscb_sda = SIOD_GPIO_NUM;
    config.pin_sscb_scl = SIOC_GPIO_NUM;
    config.pin_pwdn = PWDN_GPIO_NUM;
    config.pin_reset = RESET_GPIO_NUM;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;  // 彩色JPEG格式
    
    // 强制低分辨率以提高兼容性
    config.frame_size = FRAMESIZE_QVGA;  // 320x240
    config.jpeg_quality = 12;
    config.fb_count = 1;

    // 初始化摄像头
    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("摄像头初始化失败: 0x%x\n", err);
        Serial.println("尝试备用配置...");
        
        // 备用配置 - 调整XCLK频率和格式
        config.xclk_freq_hz = 10000000;
        config.pixel_format = PIXFORMAT_JPEG;
        err = esp_camera_init(&config);
        
        if (err != ESP_OK) {
            Serial.printf("备用配置也失败: 0x%x\n", err);
            Serial.println("摄像头模块可能不兼容或接线错误");
            return;
        }
    }
    
    // 配置传感器
    sensor_t * s = esp_camera_sensor_get();
    if (s) {
        s->set_framesize(s, FRAMESIZE_QVGA);
        s->set_brightness(s, 0);
        s->set_contrast(s, 0);
        s->set_saturation(s, 0);
    }
    
    // LED引脚
    pinMode(LED_GPIO_NUM, OUTPUT);
    digitalWrite(LED_GPIO_NUM, LOW);
    
    Serial.println(F("摄像头初始化完成"));
}

// ========================
// WiFi连接
// ========================

void connectWiFi() {
    Serial.print(F("连接WiFi: "));
    Serial.println(WIFI_SSID);
    
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    
    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 30) {
        delay(500);
        Serial.print(".");
        attempts++;
    }
    
    if (WiFi.status() == WL_CONNECTED) {
        wifiConnected = true;
        Serial.println();
        Serial.print(F("WiFi已连接, IP: "));
        Serial.println(WiFi.localIP());
    } else {
        wifiConnected = false;
        Serial.println();
        Serial.println(F("WiFi连接失败"));
    }
}

void checkWiFi() {
    if (WiFi.status() != WL_CONNECTED) {
        if (wifiConnected) {
            wifiConnected = false;
            Serial.println(F("WiFi断开，尝试重连..."));
        }
        WiFi.reconnect();
        
        int attempts = 0;
        while (WiFi.status() != WL_CONNECTED && attempts < 10) {
            delay(500);
            attempts++;
        }
        
        if (WiFi.status() == WL_CONNECTED) {
            wifiConnected = true;
            Serial.println(F("WiFi重连成功"));
        }
    }
}

// ========================
// 获取拍照配置
// ========================

void updateCaptureConfig() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi未连接，跳过配置更新");
        return;
    }
    
    HTTPClient http;
    String url = String("http://") + SERVER_IP + ":" + SERVER_PORT + "/api/capture/config";
    Serial.printf("获取拍照配置: %s\n", url.c_str());
    
    http.begin(url);
    http.setTimeout(5000);
    
    int httpResponseCode = http.GET();
    
    if (httpResponseCode == HTTP_CODE_OK) {
        String payload = http.getString();
        Serial.printf("配置响应: %s\n", payload.c_str());
        
        // 解析JSON
        DynamicJsonDocument doc(1024);
        deserializeJson(doc, payload);
        
        if (doc.containsKey("enabled")) {
            captureEnabled = doc["enabled"].as<bool>();
        }
        if (doc.containsKey("interval")) {
            captureInterval = doc["interval"].as<unsigned long>() * 1000;  // 秒转毫秒
        }
        
        Serial.printf("拍照配置更新: enabled=%d, interval=%d ms\n", captureEnabled, captureInterval);
    } else {
        Serial.printf("获取配置失败! 错误码: %d\n", httpResponseCode);
    }
    
    http.end();
}

// ========================
// 视频流处理
// ========================

static esp_err_t stream_handler(httpd_req_t *req) {
    camera_fb_t * fb = NULL;
    esp_err_t res = ESP_OK;
    size_t _jpg_buf_len = 0;
    uint8_t * _jpg_buf = NULL;
    char * part_buf[64];
    
    // 设置响应类型为 multipart/x-mixed-replace
    res = httpd_resp_set_type(req, "multipart/x-mixed-replace;boundary=frame");
    if(res != ESP_OK){
        return res;
    }
    
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");

    while(true){
        fb = esp_camera_fb_get();
        if (!fb) {
            Serial.println(F("摄像头捕获失败"));
            res = ESP_FAIL;
        } else {
            if(fb->format != PIXFORMAT_JPEG){
                bool jpeg_converted = frame2jpg(fb, 80, &_jpg_buf, &_jpg_buf_len);
                esp_camera_fb_return(fb);
                fb = NULL;
                if(!jpeg_converted){
                    Serial.println(F("JPEG压缩失败"));
                    res = ESP_FAIL;
                }
            } else {
                _jpg_buf_len = fb->len;
                _jpg_buf = fb->buf;
            }
        }
        
        if(res == ESP_OK){
            res = httpd_resp_send_chunk(req, "--frame\r\n", 10);
        }
        if(res == ESP_OK){
            size_t hlen = snprintf((char *)part_buf, 64, "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n", _jpg_buf_len);
            res = httpd_resp_send_chunk(req, (const char *)part_buf, hlen);
        }
        if(res == ESP_OK){
            res = httpd_resp_send_chunk(req, (const char *)_jpg_buf, _jpg_buf_len);
        }
        if(res == ESP_OK){
            res = httpd_resp_send_chunk(req, "\r\n", 2);
        }
        
        if(fb){
            esp_camera_fb_return(fb);
            fb = NULL;
            _jpg_buf = NULL;
        } else if(_jpg_buf){
            free(_jpg_buf);
            _jpg_buf = NULL;
        }
        
        if(res != ESP_OK){
            break;
        }
    }
    
    return res;
}

// ========================
// 启动HTTP服务器
// ========================

void startCameraServer() {
    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.server_port = 80;
    config.ctrl_port = 32768;

    httpd_uri_t stream_uri = {
        .uri       = "/stream",
        .method    = HTTP_GET,
        .handler   = stream_handler,
        .user_ctx  = NULL
    };
    
    if (httpd_start(&stream_httpd, &config) == ESP_OK) {
        httpd_register_uri_handler(stream_httpd, &stream_uri);
        Serial.println(F("视频流服务器启动成功"));
        Serial.println(F("访问地址: http://<ESP32-CAM-IP>/stream"));
    } else {
        Serial.println(F("视频流服务器启动失败"));
    }
}

// ========================
// 抓拍并上传
// ========================

void captureAndUpload() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi未连接，跳过上传");
        return;
    }
    
    // 开启LED辅助照明
    digitalWrite(LED_GPIO_NUM, HIGH);
    delay(100);
    
    camera_fb_t * fb = esp_camera_fb_get();
    if(!fb) {
        Serial.println("抓拍失败");
        digitalWrite(LED_GPIO_NUM, LOW);
        return;
    }
    
    Serial.printf("抓拍成功，图像大小: %d bytes\n", fb->len);
    digitalWrite(LED_GPIO_NUM, LOW);
    
    HTTPClient http;
    String url = String("http://") + SERVER_IP + ":" + SERVER_PORT + "/api/upload";
    Serial.printf("上传到: %s\n", url.c_str());
    
    http.begin(url);
    http.addHeader("Content-Type", "image/jpeg");
    http.addHeader("X-Device-ID", DEVICE_ID);
    http.setTimeout(5000);
    
    int httpResponseCode = http.POST(fb->buf, fb->len);
    
    if(httpResponseCode > 0) {
        Serial.printf("图像上传成功! HTTP状态码: %d\n", httpResponseCode);
        String payload = http.getString();
        Serial.printf("服务器响应: %s\n", payload.c_str());
    } else {
        Serial.printf("图像上传失败! 错误码: %d\n", httpResponseCode);
        Serial.printf("错误信息: %s\n", http.errorToString(httpResponseCode).c_str());
    }
    
    http.end();
    esp_camera_fb_return(fb);
}

// ========================
// 初始化
// ========================

void setup() {
    Serial.begin(115200);
    delay(1000);
    
    Serial.println(F("========================================"));
    Serial.println(F("  ESP32-CAM 智慧温室视频监控"));
    Serial.println(F("========================================"));
    
    // 连接WiFi
    connectWiFi();
    
    // 初始化摄像头
    setupCamera();
    
    // 启动视频流服务器
    startCameraServer();
    
    Serial.println(F("系统初始化完成"));
    Serial.print(F("视频流地址: http://"));
    Serial.print(WiFi.localIP());
    Serial.println(F("/stream"));
}

// ========================
// 主循环
// ========================

void loop() {
    // 检查WiFi
    checkWiFi();
    
    // 定时更新配置
    unsigned long currentMillis = millis();
    if (currentMillis - lastConfigUpdate >= CONFIG_UPDATE_INTERVAL) {
        lastConfigUpdate = currentMillis;
        updateCaptureConfig();
    }
    
    // 定时抓拍上传（仅在启用时）
    if (captureEnabled) {
        if (currentMillis - lastCapture >= captureInterval) {
            lastCapture = currentMillis;
            captureAndUpload();
        }
    }
    
    delay(100);
}
