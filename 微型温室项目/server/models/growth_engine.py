# ============================================
# 作物生长模型引擎
# ============================================

import math
from datetime import datetime, timedelta


class GrowthEngine:
    """
    作物生长模型引擎
    基于积温模型(GDD) + 光合有效辐射(PAR) + 水分胁迫因子的综合生长模型
    """

    def __init__(self):
        self.growth_history = []  # 存储每日生长数据

    def calculate_gdd(self, temp, t_base=10, t_opt=25, t_max=35):
        """
        计算有效积温 (Growing Degree Days)
        GDD = max(0, min(T, Tmax) - Tbase)
        使用修正公式考虑温度过高时的抑制效应
        """
        if temp <= t_base:
            return 0
        elif temp >= t_max:
            # 高温抑制：超过最适温度后积温贡献递减
            excess = temp - t_opt
            reduction = excess / (t_max - t_opt)
            gdd = (t_opt - t_base) * max(0, 1 - reduction)
            return max(0, gdd)
        else:
            return temp - t_base

    def calculate_par_factor(self, light_lux, light_hours, target_lux=25000, target_hours=14):
        """
        计算光合有效辐射因子 (0~1)
        综合考虑光照强度和光照时长
        """
        intensity_factor = min(1.0, light_lux / target_lux)
        duration_factor = min(1.0, light_hours / target_hours)

        # 光照过强也有抑制
        if light_lux > target_lux * 1.5:
            excess_ratio = (light_lux - target_lux * 1.5) / (target_lux * 0.5)
            intensity_factor *= max(0.5, 1 - excess_ratio * 0.3)

        return intensity_factor * duration_factor

    def calculate_water_factor(self, soil_moisture, optimal_moisture=65):
        """
        计算水分胁迫因子 (0~1)
        过干或过湿都会抑制生长
        """
        deviation = abs(soil_moisture - optimal_moisture)
        # 偏差在10以内无影响，超过后线性递减
        if deviation <= 10:
            return 1.0
        elif deviation <= 40:
            return max(0.3, 1.0 - (deviation - 10) / 40)
        else:
            return max(0.1, 0.3 - (deviation - 40) / 60)

    def calculate_co2_factor(self, co2_ppm, optimal=1000):
        """
        计算CO2增强因子 (0.5~1.2)
        CO2浓度在合理范围内促进生长
        """
        if co2_ppm <= 200:
            return 0.5
        elif co2_ppm <= optimal:
            return 0.5 + 0.5 * (co2_ppm - 200) / (optimal - 200)
        elif co2_ppm <= 1500:
            return 1.0 + 0.2 * (co2_ppm - optimal) / 500
        else:
            return 1.2  # 超高CO2不再增加

    def calculate_daily_growth_rate(self, sensor_data, target_params):
        """
        计算每日综合生长速率 (0~1)
        综合积温、光照、水分、CO2四个因子
        """
        temp = sensor_data.get("temperature", 25)
        humidity = sensor_data.get("humidity", 65)
        light = sensor_data.get("light", 15000)
        soil_moisture = sensor_data.get("soil_moisture", 65)
        co2 = sensor_data.get("co2", 400)

        # 从目标参数获取基准值
        t_base = target_params.get("temperature", {}).get("min", 10)
        t_opt = target_params.get("temperature", {}).get("optimal", 25)
        t_max = target_params.get("temperature", {}).get("max", 35)
        target_lux = target_params.get("light_lux", 25000)
        target_hours = target_params.get("light_hours", 14)
        optimal_moisture = target_params.get("soil_moisture", {}).get("optimal", 65)

        # 计算各因子
        gdd = self.calculate_gdd(temp, t_base, t_opt, t_max)
        gdd_normalized = min(1.0, gdd / (t_opt - t_base))  # 归一化

        par_factor = self.calculate_par_factor(light, 12, target_lux, target_hours)
        water_factor = self.calculate_water_factor(soil_moisture, optimal_moisture)
        co2_factor = self.calculate_co2_factor(co2)

        # 综合生长速率（各因子相乘，任一因子过低都会显著降低）
        growth_rate = gdd_normalized * par_factor * water_factor * co2_factor

        return {
            "growth_rate": round(growth_rate, 3),
            "factors": {
                "temperature": round(gdd_normalized, 3),
                "light": round(par_factor, 3),
                "water": round(water_factor, 3),
                "co2": round(co2_factor, 3)
            },
            "gdd_today": round(gdd, 1),
            "recommendations": self._generate_recommendations(
                gdd_normalized, par_factor, water_factor, co2_factor,
                temp, humidity, light, soil_moisture, co2, target_params
            )
        }

    def _generate_recommendations(self, temp_f, light_f, water_f, co2_f,
                                    temp, humidity, light, soil, co2, target):
        """根据各因子生成调控建议"""
        recommendations = []

        if temp_f < 0.5:
            t_opt = target.get("temperature", {}).get("optimal", 25)
            if temp < t_opt:
                recommendations.append({"action": "increase_temp", "msg": f"温度偏低({temp}°C)，建议升温至{t_opt}°C"})
            else:
                recommendations.append({"action": "decrease_temp", "msg": f"温度过高({temp}°C)，建议降温"})

        if light_f < 0.5:
            recommendations.append({"action": "increase_light", "msg": f"光照不足({light}lux)，建议开启补光灯"})
        elif light_f > 0.9:
            recommendations.append({"action": "reduce_light", "msg": "光照过强，建议遮阳"})

        if water_f < 0.5:
            opt = target.get("soil_moisture", {}).get("optimal", 65)
            if soil < opt:
                recommendations.append({"action": "increase_water", "msg": f"土壤过干({soil}%)，建议灌溉"})
            else:
                recommendations.append({"action": "reduce_water", "msg": f"土壤过湿({soil}%)，建议停止灌溉"})

        if co2_f < 0.7:
            recommendations.append({"action": "increase_co2", "msg": f"CO2偏低({co2}ppm)，建议通风或补充CO2"})

        if not recommendations:
            recommendations.append({"action": "maintain", "msg": "环境参数正常，保持当前状态"})

        return recommendations

    def predict_harvest(self, current_progress, avg_growth_rate, remaining_days):
        """
        预测收获时间
        基于当前进度和平均生长速率推算
        """
        if avg_growth_rate <= 0:
            return {"estimated_days": -1, "estimated_date": None, "confidence": 0}

        remaining_progress = 100 - current_progress
        # 假设每天平均贡献的进度
        daily_progress = avg_growth_rate * 0.5  # 简化系数
        if daily_progress <= 0:
            return {"estimated_days": -1, "estimated_date": None, "confidence": 0}

        estimated_days = int(remaining_progress / daily_progress)
        estimated_date = datetime.now() + timedelta(days=estimated_days)

        return {
            "estimated_days": estimated_days,
            "estimated_date": estimated_date.strftime("%Y-%m-%d"),
            "confidence": min(0.9, avg_growth_rate)
        }

    def simulate_scenario(self, sensor_data, target_params, adjustments):
        """
        模拟不同调控方案的效果
        adjustments: {"temperature": +2, "light_hours": +2, ...}
        """
        simulated_data = sensor_data.copy()
        for key, delta in adjustments.items():
            if key in simulated_data:
                simulated_data[key] += delta

        result = self.calculate_daily_growth_rate(simulated_data, target_params)
        return result
