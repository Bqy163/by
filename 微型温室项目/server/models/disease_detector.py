# ============================================
# 病虫害检测模块
# ============================================

import cv2
import numpy as np
import os
from datetime import datetime


class DiseaseDetector:
    """病虫害检测模块 - 基于YOLO"""

    def __init__(self, model_path="best.pt", confidence=0.5):
        self.model_path = model_path
        self.confidence = confidence
        self.model = None
        self.detection_history = []
        self._init_model()

    def _init_model(self):
        """初始化YOLO模型"""
        try:
            from ultralytics import YOLO
            if os.path.exists(self.model_path):
                self.model = YOLO(self.model_path)
                print(f"YOLO model loaded: {self.model_path}")
            else:
                print(f"Model not found: {self.model_path}")
        except ImportError:
            print("ultralytics not installed. Run: pip install ultralytics")
        except Exception as e:
            print(f"Model load error: {e}")

    def detect(self, image):
        """
        检测图像中的病虫害
        image: numpy数组 (BGR) 或文件路径
        返回: 检测结果列表
        """
        if self.model is None:
            return []

        if isinstance(image, str):
            image = cv2.imread(image)
            if image is None:
                return []

        results = self.model(image, conf=self.confidence)
        detections = []

        for result in results:
            boxes = result.boxes
            for box in boxes:
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                conf = float(box.conf[0])
                cls_id = int(box.cls[0])
                class_name = self.model.names[cls_id]

                detections.append({
                    "class": class_name,
                    "confidence": round(conf, 3),
                    "bbox": [int(x1), int(y1), int(x2), int(y2)]
                })

        # 保存到历史
        record = {
            "timestamp": datetime.now().isoformat(),
            "detections": detections,
            "count": len(detections)
        }
        self.detection_history.append(record)

        return detections

    def annotate_image(self, image, detections):
        """在图像上绘制检测框"""
        annotated = image.copy()
        for det in detections:
            x1, y1, x2, y2 = det["bbox"]
            label = f"{det['class']} {det['confidence']:.2f}"

            # 颜色：高置信度红色，中置信度黄色，低置信度绿色
            if det["confidence"] > 0.8:
                color = (0, 0, 255)
            elif det["confidence"] > 0.6:
                color = (0, 255, 255)
            else:
                color = (0, 255, 0)

            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(annotated, label, (x1, y1 - 10),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        return annotated

    def save_detection_image(self, image, detections, save_dir="static/detections"):
        """保存带标注的检测图像"""
        if not detections:
            return None

        os.makedirs(save_dir, exist_ok=True)
        annotated = self.annotate_image(image, detections)

        filename = f"detection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        filepath = os.path.join(save_dir, filename)
        cv2.imwrite(filepath, annotated)
        return filepath

    def get_statistics(self):
        """获取检测统计"""
        if not self.detection_history:
            return {"total_detections": 0, "by_class": {}}

        stats = {"total_detections": 0, "by_class": {}}
        for record in self.detection_history:
            for det in record["detections"]:
                stats["total_detections"] += 1
                cls = det["class"]
                stats["by_class"][cls] = stats["by_class"].get(cls, 0) + 1

        return stats

    def is_ready(self):
        """模型是否已加载"""
        return self.model is not None
