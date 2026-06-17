# ============================================================
# AI 决策引擎（被 app.auto_control_thread 调用）
# 模式: rule / fuzzy / ai
# 关键改进:
#   - 在 rule 模式中加入 hysteresis（滞后），避免阈值附近反复开关继电器
#   - 修正了模糊逻辑的权重计算（之前 total/weight 量级不匹配会导致风扇反向调速）
#   - ai 模式: 先跑 fuzzy 再叠加 rule 的 alarms（不冲突）
# ============================================================

import math
from datetime import datetime

# -------- hysteresis 配置 --------
HYSTERESIS = {
    # 参数名: (低侧滞后, 高侧滞后)
    # 含义: 温度越过 max+h_high 才开风扇；
    #       温度低于 min-h_low 才开加热；
    #       在中间区间保持上一次的设备状态不变。
    "temperature": (1.0, 1.0),
    "humidity":    (3.0, 3.0),
    "soil_moisture": (2.0, 2.0),
    "light_lux":   (1000, 1000),
    "co2_ppm":     (50, 50),
}


class DecisionEngine:
    def __init__(self, mode="rule"):
        self.mode = mode
        self.auto_control = False
        self.decision_log = []
        self.alert_log = []
        # 设备上一次的"目标状态"，用于 hysteresis 保持
        self._last_target = {
            "fan": False, "heater": False, "mister": False,
            "water_pump": False, "grow_light": False, "shade": False,
            "vent_window": False,
        }

    # ---------- 对外主接口 ----------
    def decide(self, sensor_data, target_params=None, detection_result=None):
        if self.mode == "rule":
            return self._rule_decision(sensor_data, target_params, detection_result)
        elif self.mode == "fuzzy":
            return self._fuzzy_decision(sensor_data, target_params, detection_result)
        else:
            return self._ai_decision(sensor_data, target_params, detection_result)

    # =====================
    #  模式 1: 规则引擎（带 hysteresis）
    # =====================
    def _rule_decision(self, sensor_data, target_params, detection_result):
        commands = {}
        alerts = []

        temp = float(sensor_data.get("temperature", 25))
        humidity = float(sensor_data.get("humidity", 65))
        soil = float(sensor_data.get("soil_moisture", 65))
        light = float(sensor_data.get("light", 15000))
        co2 = float(sensor_data.get("co2", 400))

        if target_params:
            t_min = float(target_params.get("temperature", {}).get("min", 15))
            t_max = float(target_params.get("temperature", {}).get("max", 35))
            t_opt = float(target_params.get("temperature", {}).get("optimal", (t_min + t_max) / 2))
            h_min = float(target_params.get("humidity", {}).get("min", 40))
            h_max = float(target_params.get("humidity", {}).get("max", 85))
            s_min = float(target_params.get("soil_moisture", {}).get("min", 40))
            s_max = float(target_params.get("soil_moisture", {}).get("max", 80))
            s_opt = float(target_params.get("soil_moisture", {}).get("optimal", (s_min + s_max) / 2))
            light_target = float(target_params.get("light_lux", 25000))
            co2_opt = float(target_params.get("co2_ppm", {}).get("optimal", 800))
        else:
            t_min, t_max, t_opt = 15, 35, 25
            h_min, h_max = 40, 85
            s_min, s_max, s_opt = 40, 80, 65
            light_target = 25000
            co2_opt = 800

        h_low, h_high = HYSTERESIS.get("temperature", (1.0, 1.0))
        hu_low, hu_high = HYSTERESIS.get("humidity", (3.0, 3.0))
        so_low, so_high = HYSTERESIS.get("soil_moisture", (2.0, 2.0))
        li_low, li_high = HYSTERESIS.get("light_lux", (1000, 1000))
        co_low, co_high = HYSTERESIS.get("co2_ppm", (50, 50))

        # --------- 温度控制 ---------
        if temp > t_max + h_high:
            commands["fan"] = "on"
            commands["vent_window"] = "open"
            commands["heater"] = "off"
            alerts.append({"level": "warning", "msg": f"温度偏高: {temp:.1f}°C"})
        elif temp < t_min - h_low:
            commands["heater"] = "on"
            commands["fan"] = "off"
            commands["grow_light"] = "on"  # 灯也发热
            alerts.append({"level": "warning", "msg": f"温度偏低: {temp:.1f}°C"})
        else:
            # 正常区间：保持上一次状态，不扰动继电器
            # 但如果温度越过最优点较远，也做轻微调温
            if temp > t_opt + h_high + 1:
                commands["fan"] = "on"
            elif temp < t_opt - h_low - 1:
                commands["heater"] = "on"

        # --------- 湿度控制 ---------
        if humidity > h_max + hu_high:
            commands["fan"] = "on"
            commands["vent_window"] = "open"
            commands["mister"] = "off"
            alerts.append({"level": "warning", "msg": f"湿度过高: {humidity:.0f}%"})
        elif humidity < h_min - hu_low:
            commands["mister"] = "on"
            alerts.append({"level": "info", "msg": f"湿度过低: {humidity:.0f}%"})

        # --------- 土壤湿度（浇水） ---------
        if soil < s_min - so_low:
            commands["water_pump"] = "on"
            alerts.append({"level": "warning", "msg": f"土壤干燥: {soil:.0f}%"})
        elif soil > s_max + so_high:
            commands["water_pump"] = "off"
        elif soil < s_opt - so_low - 1:
            commands["water_pump"] = "on"

        # --------- 光照 ---------
        if light < light_target - li_low:
            commands["grow_light"] = "on"
            commands["shade"] = "close"
            if light < light_target * 0.5:
                alerts.append({"level": "info", "msg": f"光照不足: {light:.0f} lux"})
        elif light > light_target + li_high:
            commands["grow_light"] = "off"
            commands["shade"] = "close" if light > light_target * 1.5 else "open"

        # --------- CO2 ---------
        if co2 < co2_opt - co_low:
            commands["vent_window"] = "close"  # 保留 CO2
        elif co2 > co2_opt + co_high:
            commands["vent_window"] = "open"  # 排出多余 CO2
            if co2 > co2_opt + co_high * 2:
                alerts.append({"level": "info", "msg": f"CO2 偏高: {co2:.0f} ppm"})

        # --------- 病虫害响应 ---------
        if detection_result and detection_result.get("detections"):
            for det in detection_result["detections"]:
                disease = det["class"]
                conf = float(det.get("confidence", 0.0))
                if conf < 0.5:
                    continue
                alerts.append({"level": "danger" if conf > 0.8 else "warning",
                               "msg": f"检测到 {disease}, 置信度 {conf:.0%}"})
                # 通用响应：通风降湿
                commands["fan"] = "on"
                commands["vent_window"] = "open"
                if "mite" in disease or "aphid" in disease:
                    commands["mister"] = "on"
                if "mold" in disease or "fungus" in disease:
                    commands["mister"] = "off"

        # --------- 合并 last_target 并更新 ---------
        for k in list(commands.keys()):
            self._last_target[k] = (commands[k] in ("on", "open", True))

        result = {
            "mode": "rule",
            "commands": commands,
            "alerts": alerts,
            "timestamp": datetime.now().isoformat(),
        }
        self.decision_log.append(result)
        if len(self.decision_log) > 200:
            self.decision_log = self.decision_log[-200:]
        return result

    # =====================
    #  模式 2: 模糊逻辑（修复权重计算）
    # =====================
    def _fuzzy_decision(self, sensor_data, target_params, detection_result):
        temp = float(sensor_data.get("temperature", 25))
        humidity = float(sensor_data.get("humidity", 65))
        soil = float(sensor_data.get("soil_moisture", 65))
        light = float(sensor_data.get("light", 15000))

        commands = {}
        alerts = []

        # --- 风扇 ---
        fan_on = self._fuzzy_fan(temp, humidity)
        commands["fan"] = "on" if fan_on >= 50 else "off"
        commands["fan_speed"] = int(fan_on)

        # --- 加热 ---
        heat_on = self._fuzzy_heater(temp)
        commands["heater"] = "on" if heat_on >= 50 else "off"
        commands["heat_power"] = int(heat_on)

        # --- 浇水 ---
        water_on = self._fuzzy_water(soil)
        commands["water_pump"] = "on" if water_on >= 50 else "off"
        commands["water_amount"] = int(water_on)

        # --- 补光 ---
        light_on = self._fuzzy_light(light)
        commands["grow_light"] = "on" if light_on >= 50 else "off"
        commands["light_brightness"] = int(light_on)

        # --- 加湿 ---
        mist_on = self._fuzzy_mist(humidity)
        commands["mister"] = "on" if mist_on >= 50 else "off"
        commands["mist_level"] = int(mist_on)

        result = {
            "mode": "fuzzy",
            "commands": commands,
            "alerts": alerts,
            "fuzzy_outputs": {
                "fan_speed": int(fan_on),
                "heat_power": int(heat_on),
                "water_amount": int(water_on),
                "light_brightness": int(light_on),
                "mist_level": int(mist_on),
            },
            "timestamp": datetime.now().isoformat(),
        }
        self.decision_log.append(result)
        if len(self.decision_log) > 200:
            self.decision_log = self.decision_log[-200:]
        return result

    # --------- 三角隶属度 ---------
    def _triangle(self, x, a, b, c):
        if x <= a or x >= c:
            return 0.0
        elif x <= b:
            return (x - a) / (b - a) if b != a else 0
        else:
            return (c - x) / (c - b) if c != b else 0

    # --------- 子推理 ---------
    def _fuzzy_fan(self, temp, humidity):
        hot = self._triangle(temp, 28, 33, 40)
        warm = self._triangle(temp, 22, 27, 32)
        humid = self._triangle(humidity, 75, 85, 100)

        # 规则强度（0~1）
        strength_1 = min(hot, humid)       # 高温高湿 → 全速
        strength_2 = min(warm, humid)      # 温热高湿 → 中速
        strength_3 = hot                   # 热但湿度一般 → 中
        strength_4 = warm                  # 温热 → 低

        # 加权平均（修正后的算法：每档 speed 乘其权重 / 权重和）
        speeds = [100, 70, 60, 30]
        weights = [strength_1, strength_2, strength_3, strength_4]
        total_w = sum(weights)
        if total_w == 0:
            return 0.0
        weighted = sum(s * w for s, w in zip(speeds, weights)) / total_w
        return min(100.0, max(0.0, weighted))

    def _fuzzy_heater(self, temp):
        cold = self._triangle(temp, 5, 10, 18)
        cool = self._triangle(temp, 12, 18, 24)
        # cold 温度低 → 强加热；cool 弱加热
        if cold <= 0 and cool <= 0:
            return 0.0
        total_w = cold + cool
        return min(100.0, (cold * 100 + cool * 40) / total_w)

    def _fuzzy_water(self, soil):
        dry = self._triangle(soil, 20, 35, 50)
        medium_dry = self._triangle(soil, 40, 50, 60)
        if dry <= 0 and medium_dry <= 0:
            return 0.0
        total_w = dry + medium_dry
        return min(100.0, (dry * 100 + medium_dry * 40) / total_w)

    def _fuzzy_light(self, light):
        dark = self._triangle(light, 0, 3000, 8000)
        dim = self._triangle(light, 5000, 12000, 20000)
        if dark <= 0 and dim <= 0:
            return 0.0
        total_w = dark + dim
        return min(100.0, (dark * 100 + dim * 40) / total_w)

    def _fuzzy_mist(self, humidity):
        dry = self._triangle(humidity, 20, 35, 50)
        medium_dry = self._triangle(humidity, 40, 50, 60)
        if dry <= 0 and medium_dry <= 0:
            return 0.0
        total_w = dry + medium_dry
        return min(100.0, (dry * 100 + medium_dry * 40) / total_w)

    # =====================
    #  模式 3: AI 增强
    # =====================
    def _ai_decision(self, sensor_data, target_params, detection_result):
        fuzzy_result = self._fuzzy_decision(sensor_data, target_params, detection_result)
        rule_result = self._rule_decision(sensor_data, target_params, detection_result)

        # 优先使用规则引擎的报警；设备指令以模糊为主，危险时叠加规则
        commands = dict(fuzzy_result["commands"])
        alerts = list(rule_result["alerts"])

        for alert in rule_result.get("alerts", []):
            if alert.get("level") == "danger":
                commands["fan"] = "on"  # 强制通风
                commands["buzzer"] = "on"

        result = {
            "mode": "ai",
            "commands": commands,
            "alerts": alerts,
            "fuzzy_outputs": fuzzy_result.get("fuzzy_outputs", {}),
            "ai_analysis": self._analyze_trends(),
            "timestamp": datetime.now().isoformat(),
        }
        self.decision_log.append(result)
        if len(self.decision_log) > 200:
            self.decision_log = self.decision_log[-200:]
        return result

    def _analyze_trends(self):
        if len(self.decision_log) < 5:
            return {"status": "insufficient_data", "message": "数据不足"}
        recent = self.decision_log[-20:]
        alert_count = sum(len(d.get("alerts", [])) for d in recent)
        if alert_count > 10:
            return {"status": "unstable",
                    "message": "近期警报频繁，建议检查系统状态或调整配方"}
        elif alert_count > 5:
            return {"status": "attention", "message": "部分参数波动较大"}
        return {"status": "stable", "message": "系统运行稳定"}

    def get_decision_log(self, limit=50):
        return self.decision_log[-limit:]
