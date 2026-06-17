# ============================================
# 作物种植配方管理
# ============================================

import json
import os
from datetime import datetime


class CropProfileManager:
    """作物种植配方管理器 - 管理标准化种植参数"""

    def __init__(self, profiles_path="data/crop_profiles.json"):
        self.profiles_path = profiles_path
        self.profiles = self._load_profiles()
        self.current_crop = None
        self.plant_date = None

    def _load_profiles(self):
        """加载种植配方数据"""
        if os.path.exists(self.profiles_path):
            with open(self.profiles_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"crops": {}}

    def get_available_crops(self):
        """获取可用作物列表"""
        crops = []
        for key, crop in self.profiles.get("crops", {}).items():
            crops.append({
                "id": key,
                "name": crop["name"],
                "name_en": crop.get("name_en", key),
                "category": crop.get("category", ""),
                "cycle_days": crop.get("growth_cycle_days", 0)
            })
        return crops

    def get_crop_profile(self, crop_id):
        """获取指定作物的完整配方"""
        return self.profiles.get("crops", {}).get(crop_id)

    def set_current_crop(self, crop_id, plant_date=None):
        """设置当前种植的作物"""
        if crop_id not in self.profiles.get("crops", {}):
            raise ValueError(f"Unknown crop: {crop_id}")
        self.current_crop = crop_id
        self.plant_date = plant_date or datetime.now()
        return True

    def get_current_stage(self):
        """获取当前作物所处生长阶段及参数"""
        if not self.current_crop or not self.plant_date:
            return None

        crop = self.get_crop_profile(self.current_crop)
        if not crop:
            return None

        days_elapsed = (datetime.now() - self.plant_date).days

        for stage_id, stage in crop.get("stages", {}).items():
            start, end = stage["days"]
            if start <= days_elapsed <= end:
                return {
                    "stage_id": stage_id,
                    "stage_name": stage["name"],
                    "days_elapsed": days_elapsed,
                    "total_days": crop["growth_cycle_days"],
                    "progress": round(days_elapsed / crop["growth_cycle_days"] * 100, 1),
                    "params": stage["params"]
                }

        # 超出最大天数，返回最后阶段
        stages = list(crop.get("stages", {}).items())
        if stages:
            last_stage_id, last_stage = stages[-1]
            return {
                "stage_id": last_stage_id,
                "stage_name": last_stage["name"] + "(已成熟)",
                "days_elapsed": days_elapsed,
                "total_days": crop["growth_cycle_days"],
                "progress": 100.0,
                "params": last_stage["params"]
            }
        return None

    def get_target_params(self):
        """获取当前阶段的目标环境参数"""
        stage_info = self.get_current_stage()
        if stage_info:
            return stage_info["params"]
        return None

    def get_disease_risk_info(self, disease_name):
        """获取病虫害风险信息"""
        if not self.current_crop:
            return None
        crop = self.get_crop_profile(self.current_crop)
        return crop.get("disease_risk", {}).get(disease_name)

    def get_all_disease_risks(self):
        """获取当前作物所有病虫害风险"""
        if not self.current_crop:
            return []
        crop = self.get_crop_profile(self.current_crop)
        risks = []
        for name, info in crop.get("disease_risk", {}).items():
            risks.append({"name": name, **info})
        return risks

    def calculate_growth_progress(self):
        """计算生长进度（基于积温模型简化版）"""
        stage_info = self.get_current_stage()
        if not stage_info:
            return {"progress": 0, "stage": "unknown", "health": "unknown"}

        return {
            "progress": stage_info["progress"],
            "stage": stage_info["stage_name"],
            "days_elapsed": stage_info["days_elapsed"],
            "total_days": stage_info["total_days"],
            "health": "growing" if stage_info["progress"] < 100 else "mature"
        }
