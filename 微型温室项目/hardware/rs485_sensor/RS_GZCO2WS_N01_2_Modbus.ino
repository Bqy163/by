/*
 * ============================================================
 *  仁科 RS-GZCO2WS-N01-2 光照CO2温湿度变送器
 *  Modbus RTU 读取示例代码
 * ============================================================
 *  传感器型号: RS-GZCO2WS-N01-2
 *  品牌: 山东仁科 (Renke)
 *  通信: RS485 Modbus RTU
 *  默认参数: 9600, 8, N, 1
 *  默认地址: 0x01
 *
 *  线序:
 *    棕色 = 电源正 (DC 10-30V)
 *    黑色 = 电源负 (GND)
 *    黄色 = 485-A
 *    蓝色 = 485-B
 *
 *  量程:
 *    光照: 0-65535 lux
 *    CO2:  0-5000 ppm
 *
 *  依赖库:
 *    - ModbusMaster (Arduino IDE库管理器安装)
 *    - SoftwareSerial (Arduino内置)
 * ============================================================
 */

#include <ModbusMaster.h>
#include <SoftwareSerial.h>

// ========================
// 硬件配置
// ========================

// RS485软串口引脚 (Arduino UNO)
#define RS485_RX_PIN    2    // 接RS485模块 TXD
#define RS485_TX_PIN    3    // 接RS485模块 RXD
#define RS485_DE_RE_PIN 4    // 接RS485模块 DE/RE 方向控制

// 传感器电源 (如果Arduino供电不足，请使用外部电源)
// 传感器需要 DC 10-30V，Arduino 5V可能不够，建议外接12V电源

// ========================
// Modbus配置
// ========================

#define SENSOR_ADDR     0x01      // 传感器从机地址 (默认0x01)
#define MODBUS_BAUD     9600      // 波特率

// 寄存器地址 (基于仁科传感器通用协议)
// 功能码 0x03 读取保持寄存器
#define REG_TEMPERATURE     0x0000   // 温度 (有符号16位, ×0.1 = °C)
#define REG_HUMIDITY        0x0001   // 湿度 (无符号16位, ×0.1 = %RH)
#define REG_LIGHT_HIGH      0x0002   // 光照高16位 (32位数据)
#define REG_LIGHT_LOW       0x0003   // 光照低16位
#define REG_CO2             0x0004   // CO2浓度 (无符号16位, = ppm)

// 读取起始地址和数量
#define READ_START_ADDR     0x0000
#define READ_REG_COUNT      5        // 读取5个寄存器

// ========================
// 全局变量
// ========================

SoftwareSerial rs485Serial(RS485_RX_PIN, RS485_TX_PIN);
ModbusMaster node;

// 传感器数据结构
struct SensorData {
    float temperature;      // 温度 °C
    float humidity;         // 湿度 %RH
    uint32_t light;         // 光照 lux (32位)
    uint16_t co2;           // CO2 ppm
    bool valid;             // 数据是否有效
    uint32_t readCount;     // 成功读取次数
    uint32_t errorCount;    // 错误次数
} sensorData;

// ========================
// RS485方向控制回调
// ========================

void preTransmission() {
    digitalWrite(RS485_DE_RE_PIN, HIGH);
    delayMicroseconds(10);
}

void postTransmission() {
    delayMicroseconds(10);
    digitalWrite(RS485_DE_RE_PIN, LOW);
}

// ========================
// 初始化
// ========================

void setup() {
    // 初始化串口监视器
    Serial.begin(115200);
    while (!Serial) { ; }

    Serial.println(F("========================================"));
    Serial.println(F("  RS-GZCO2WS-N01-2 传感器读取程序"));
    Serial.println(F("  仁科 光照CO2温湿度变送器"));
    Serial.println(F("========================================"));
    Serial.println();

    // 初始化RS485方向控制
    pinMode(RS485_DE_RE_PIN, OUTPUT);
    digitalWrite(RS485_DE_RE_PIN, LOW);

    // 初始化RS485软串口
    rs485Serial.begin(MODBUS_BAUD);

    // 初始化Modbus主站
    node.begin(SENSOR_ADDR, rs485Serial);
    node.preTransmission(preTransmission);
    node.postTransmission(postTransmission);

    // 初始化数据结构
    sensorData.valid = false;
    sensorData.temperature = 0;
    sensorData.humidity = 0;
    sensorData.light = 0;
    sensorData.co2 = 0;
    sensorData.readCount = 0;
    sensorData.errorCount = 0;

    // 打印配置信息
    Serial.print(F("传感器地址: 0x"));
    if (SENSOR_ADDR < 0x10) Serial.print(F("0"));
    Serial.println(SENSOR_ADDR, HEX);
    Serial.print(F("波特率: "));
    Serial.print(MODBUS_BAUD);
    Serial.println(F(" bps (8N1)"));
    Serial.print(F("读取寄存器: 0x"));
    if (READ_START_ADDR < 0x10) Serial.print(F("0"));
    Serial.print(READ_START_ADDR, HEX);
    Serial.print(F(" ~ 0x"));
    Serial.print(READ_START_ADDR + READ_REG_COUNT - 1, HEX);
    Serial.print(F(" (共"));
    Serial.print(READ_REG_COUNT);
    Serial.println(F("个)"));
    Serial.println();
    Serial.println(F("开始读取数据..."));
    Serial.println();
}

// ========================
// 主循环
// ========================

void loop() {
    if (readSensorData()) {
        printSensorData();
        sensorData.readCount++;
    } else {
        sensorData.errorCount++;
        printErrorHelp();
    }

    Serial.println(F("----------------------------------------"));
    delay(2000);
}

// ========================
// 读取传感器数据
// ========================

bool readSensorData() {
    uint8_t result;

    // 使用功能码0x03读取保持寄存器
    result = node.readHoldingRegisters(READ_START_ADDR, READ_REG_COUNT);

    if (result == node.ku8MBSuccess) {
        // 读取成功，解析数据

        // 寄存器0: 温度 (有符号16位, ×0.1)
        int16_t rawTemp = (int16_t)node.getResponseBuffer(0);
        sensorData.temperature = rawTemp * 0.1f;

        // 寄存器1: 湿度 (无符号16位, ×0.1)
        uint16_t rawHumi = node.getResponseBuffer(1);
        sensorData.humidity = rawHumi * 0.1f;

        // 寄存器2-3: 光照 (32位无符号整数)
        // 高16位在0x0002，低16位在0x0003
        uint16_t lightHigh = node.getResponseBuffer(2);
        uint16_t lightLow  = node.getResponseBuffer(3);
        sensorData.light = ((uint32_t)lightHigh << 16) | lightLow;

        // 寄存器4: CO2浓度 (无符号16位, 直接为ppm)
        sensorData.co2 = node.getResponseBuffer(4);

        sensorData.valid = true;
        return true;

    } else {
        sensorData.valid = false;
        return false;
    }
}

// ========================
// 打印传感器数据
// ========================

void printSensorData() {
    Serial.println(F("╔══════════════════════════════════════╗"));
    Serial.println(F("║      传感器数据读取成功 ✓            ║"));
    Serial.println(F("╠══════════════════════════════════════╣"));

    // 温度
    Serial.print(F("║  温度:     "));
    Serial.print(sensorData.temperature, 1);
    Serial.print(F(" °C"));
    if (sensorData.temperature > 35.0 || sensorData.temperature < 10.0) {
        Serial.print(F("  ⚠️ 异常"));
    }
    Serial.println();

    // 湿度
    Serial.print(F("║  湿度:     "));
    Serial.print(sensorData.humidity, 1);
    Serial.print(F(" %RH"));
    if (sensorData.humidity > 90.0 || sensorData.humidity < 30.0) {
        Serial.print(F("  ⚠️ 异常"));
    }
    Serial.println();

    // 光照
    Serial.print(F("║  光照:     "));
    Serial.print(sensorData.light);
    Serial.print(F(" lux"));
    if (sensorData.light < 1000) {
        Serial.print(F("  ⚠️ 偏弱"));
    } else if (sensorData.light > 50000) {
        Serial.print(F("  ⚠️ 过强"));
    }
    Serial.println();

    // CO2
    Serial.print(F("║  CO2:      "));
    Serial.print(sensorData.co2);
    Serial.print(F(" ppm"));
    if (sensorData.co2 > 1500) {
        Serial.print(F("  ⚠️ 偏高"));
    } else if (sensorData.co2 < 400) {
        Serial.print(F("  ⚠️ 偏低"));
    }
    Serial.println();

    Serial.println(F("╠══════════════════════════════════════╣"));
    Serial.print(F("║  成功次数: "));
    Serial.print(sensorData.readCount);
    Serial.print(F("  |  错误次数: "));
    Serial.print(sensorData.errorCount);
    Serial.println();
    Serial.println(F("╚══════════════════════════════════════╝"));
}

// ========================
// 错误帮助信息
// ========================

void printErrorHelp() {
    Serial.println(F("[错误] 传感器读取失败！"));
    Serial.println();
    Serial.println(F("请检查以下项目："));
    Serial.println(F("  1. 接线是否正确："));
    Serial.println(F("     棕=电源正(10-30V), 黑=电源负(GND)"));
    Serial.println(F("     黄=485-A, 蓝=485-B"));
    Serial.println(F("  2. RS485模块接线："));
    Serial.println(F("     A-A, B-B (不要交叉)"));
    Serial.println(F("  3. 传感器地址是否为 0x01"));
    Serial.println(F("     (可通过拨码开关或上位机修改)"));
    Serial.println(F("  4. 波特率是否匹配 (默认9600)"));
    Serial.println(F("  5. 传感器是否已上电 (需10-30VDC)"));
    Serial.println();
    Serial.println(F("如果仍无法读取，请尝试扫描寄存器地址："));
    Serial.println(F("  在setup()末尾添加: scanAllRegisters();"));
}

// ========================
// 扫描寄存器地址 (调试用)
// ========================

void scanAllRegisters() {
    Serial.println(F("开始扫描寄存器地址..."));
    Serial.println(F("地址    值(HEX)   值(DEC)"));
    Serial.println(F("------  --------  --------"));

    for (uint16_t addr = 0; addr <= 0x0010; addr++) {
        uint8_t result = node.readHoldingRegisters(addr, 1);

        if (result == node.ku8MBSuccess) {
            uint16_t value = node.getResponseBuffer(0);
            Serial.print(F("0x"));
            if (addr < 0x10) Serial.print(F("0"));
            Serial.print(addr, HEX);
            Serial.print(F("    0x"));
            if (value < 0x1000) Serial.print(F("0"));
            if (value < 0x0100) Serial.print(F("0"));
            if (value < 0x0010) Serial.print(F("0"));
            Serial.print(value, HEX);
            Serial.print(F("    "));
            Serial.println(value);
        } else {
            Serial.print(F("0x"));
            if (addr < 0x10) Serial.print(F("0"));
            Serial.print(addr, HEX);
            Serial.println(F("    --无响应--"));
        }

        delay(100);
    }

    Serial.println(F("扫描完成"));
}

// ========================
// 修改传感器地址 (调试用)
// ========================

bool changeSensorAddress(uint8_t newAddress) {
    if (newAddress < 1 || newAddress > 247) {
        Serial.println(F("错误: 地址必须在1-247之间"));
        return false;
    }

    // 使用功能码0x06写入单个寄存器
    // 仁科传感器通常将地址存储在寄存器0x0100或0x0000
    uint8_t result = node.writeSingleRegister(0x0100, newAddress);

    if (result == node.ku8MBSuccess) {
        Serial.print(F("地址修改成功！新地址: 0x"));
        Serial.println(newAddress, HEX);
        return true;
    } else {
        Serial.println(F("地址修改失败"));
        return false;
    }
}

// ========================
// 修改波特率 (调试用)
// ========================

bool changeBaudRate(uint16_t baudCode) {
    // 波特率代码: 0=2400, 1=4800, 2=9600, 3=19200, 4=38400, 5=57600, 6=115200
    uint8_t result = node.writeSingleRegister(0x0101, baudCode);

    if (result == node.ku8MBSuccess) {
        Serial.print(F("波特率修改成功！代码: "));
        Serial.println(baudCode);
        return true;
    } else {
        Serial.println(F("波特率修改失败"));
        return false;
    }
}
