# ============================================================
# 智慧微型温室系统 - 服务器配置
# 注意：敏感信息请使用环境变量覆盖，不要把真实密钥提交到仓库。
# ============================================================

import os

# ---------- 服务器 ----------
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "5000"))

# 强烈建议通过环境变量设置，例如:
#   set GREENHOUSE_SECRET=your_very_long_random_string_here
SECRET_KEY = os.environ.get(
    "GREENHOUSE_SECRET",
    "change_me_in_production_please_use_a_long_random_string_32+"
)

# ---------- ESP32 设备 ----------
ESP32_CAM_IP = os.environ.get("ESP32_CAM_IP", "192.168.31.169")
ESP32_STREAM_URL = f"http://{ESP32_CAM_IP}/stream"
# 默认设备 ID（与 esp32_controller.ino / esp32cam.ino 保持一致）
DEFAULT_CONTROLLER_ID = "esp32_greenhouse_01"
DEFAULT_CAM_ID = "esp32cam_01"

# ---------- 默认管理员账号 ----------
# 登录时使用 werkzeug.security 的 pbkdf2 校验，
# 初始哈希对应明文 admin/admin123（首次启动会写入 DB，之后可自由修改）
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
# 若你要直接改密码，设置 ADMIN_PASSWORD 环境变量，哈希会在启动时自动重新生成
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# ---------- 路径 ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE_PATH = os.path.join(BASE_DIR, "greenhouse.db")
CAPTURE_DIR = os.path.join(BASE_DIR, "static", "captures")
DETECTION_DIR = os.path.join(BASE_DIR, "static", "detections")
CROP_PROFILES_PATH = os.path.join(BASE_DIR, "data", "crop_profiles.json")

# ---------- YOLO 病虫害识别 ----------
MODEL_PATH = os.environ.get("MODEL_PATH", "")   # 空则用模拟模式
MODEL_CONFIDENCE = float(os.environ.get("MODEL_CONFIDENCE", "0.45"))

# ---------- 决策引擎 ----------
# 可选值: rule / fuzzy / ai
DECISION_MODE = os.environ.get("DECISION_MODE", "rule")
DECISION_INTERVAL = int(os.environ.get("DECISION_INTERVAL", "5"))

# ---------- 自动控制参数（抗抖动） ----------
# hysteresis：滞后量。参数必须越过 min-hysteresis 才开，越过 max+hysteresis 才关，
# 避免阈值附近反复抖动导致继电器寿命缩短。
HYSTERESIS_TEMP = float(os.environ.get("HYSTERESIS_TEMP", "1.0"))
HYSTERESIS_HUMIDITY = float(os.environ.get("HYSTERESIS_HUMIDITY", "3.0"))
HYSTERESIS_SOIL = float(os.environ.get("HYSTERESIS_SOIL", "2.0"))
HYSTERESIS_LIGHT = int(os.environ.get("HYSTERESIS_LIGHT", "1000"))
HYSTERESIS_CO2 = int(os.environ.get("HYSTERESIS_CO2", "50"))

# ---------- 命令队列 ----------
MAX_PENDING_COMMANDS = int(os.environ.get("MAX_PENDING_COMMANDS", "50"))

# ---------- MJPEG 解析 ----------
MJPEG_BUFFER_MAX = int(os.environ.get("MJPEG_BUFFER_MAX", "4194304"))  # 4 MB
