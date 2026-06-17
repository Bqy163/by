# ============================================================
# 智慧微型温室系统 - Flask 服务器
# 功能:
#   - 接收传感器数据并存储到 SQLite（按 device_id 区分）
#   - 接收 ESP32-CAM 视频流与抓拍图像
#   - 下发设备控制指令（手动 / 阈值自动 / 时控）
#   - 提供 Web 界面（登录 + 主页环境监测 + 设备控制 + 病虫害识别）
#   - YOLO 病虫害检测（无模型时使用模拟，结果标注图像保存在本地）
# 安全:
#   - 所有 /api/* 与主页需要登录
#   - 使用 werkzeug.security 的加盐哈希保存密码
#   - secret_key 必须通过环境变量设置
# ============================================================

import os
import json
import sqlite3
import hashlib
import threading
import time
import random
from datetime import datetime, timedelta
from functools import wraps
from collections import deque

from flask import (
    Flask, render_template, Response, request,
    jsonify, session, redirect, url_for, send_from_directory, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

import config as cfg

# ============================================================
# 基础配置
# ============================================================
BASE_DIR = cfg.BASE_DIR
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)
app.secret_key = cfg.SECRET_KEY
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

ESP32_CAM_IP = cfg.ESP32_CAM_IP
ESP32_STREAM_URL = cfg.ESP32_STREAM_URL

os.makedirs(cfg.CAPTURE_DIR, exist_ok=True)
os.makedirs(cfg.DETECTION_DIR, exist_ok=True)

# ============================================================
# 数据库初始化（改用 WITH 事务 + users 表）
# ============================================================
def _db():
    conn = sqlite3.connect(cfg.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS sensor_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, temperature REAL, humidity REAL,
            light INTEGER, co2 INTEGER, soil_moisture REAL, soil_temperature REAL, device_id TEXT
        )''')
        # 兼容旧库：尝试追加 soil_temperature 列（如果已存在则忽略）
        try:
            c.execute("ALTER TABLE sensor_data ADD COLUMN soil_temperature REAL")
        except Exception:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS device_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, device TEXT, action TEXT, user TEXT, mode TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS pest_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, image_path TEXT, result TEXT,
            confidence REAL, mode TEXT, detection_json TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS threshold_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            param TEXT UNIQUE, min_val REAL, max_val REAL, enabled INTEGER DEFAULT 1
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS schedule_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT, start_time TEXT, end_time TEXT,
            enabled INTEGER DEFAULT 1, action TEXT
        )''')

        # 初始化默认管理员
        c.execute('SELECT COUNT(*) AS n FROM users')
        if c.fetchone()['n'] == 0:
            pw_hash = generate_password_hash(cfg.ADMIN_PASSWORD, method='pbkdf2:sha256')
            c.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)',
                      (cfg.ADMIN_USERNAME, pw_hash))

        # 初始化默认阈值
        defaults = [
            ("temperature", 18, 32),
            ("humidity", 45, 85),
            ("soil_moisture", 40, 80),
            ("soil_temperature", 15, 35),
            ("co2", 400, 1500),
            ("light", 5000, 50000),
        ]
        for p, mn, mx in defaults:
            c.execute('INSERT OR IGNORE INTO threshold_rules (param, min_val, max_val) VALUES (?, ?, ?)',
                      (p, mn, mx))

        conn.commit()


init_db()


def get_user(username):
    with _db() as conn:
        row = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        return dict(row) if row else None


# ============================================================
# 全局状态（带线程锁）
# ============================================================
_sensor_lock = threading.Lock()
sensor_data = {
    "temperature": 0, "humidity": 0, "light": 0, "co2": 0,
    "soil_moisture": 0, "soil_temperature": -1, "devices": {}, "last_update": None
}

_pending_lock = threading.Lock()
pending_commands = {}

_frame_lock = threading.Lock()
current_frame = None

last_esp32_update = None
last_cam_update = None

# 自动控制模式
auto_control_state = {
    "mode": "manual",              # manual / threshold / schedule
    "threshold_enabled": False,
    "schedule_enabled": False,
}

# 抓拍配置
capture_config = {"enabled": False, "interval": 10}

# ---------- 鉴权装饰器 ----------
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({"success": False, "error": "未登录"}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapped


# ============================================================
# YOLO 病虫害检测（使用 models/disease_detector.py，缺失则降级模拟）
# ============================================================
class PestDetector:
    def __init__(self):
        self.backend = "simulation"
        self.model = None
        self._init_model()

    def _init_model(self):
        # 优先使用 models.disease_detector.DiseaseDetector
        try:
            from models.disease_detector import DiseaseDetector
            detector = DiseaseDetector(
                model_path=cfg.MODEL_PATH,
                confidence=cfg.MODEL_CONFIDENCE,
            )
            if detector.is_ready():
                self.model = detector
                self.backend = "yolo"
                print(f"[PestDetector] YOLO 模型加载成功: {cfg.MODEL_PATH}")
                return
        except Exception as e:
            print(f"[PestDetector] YOLO 加载失败（将使用模拟）: {e}")

        # 降级：模拟模式
        self.backend = "simulation"
        print("[PestDetector] 使用模拟检测模式（放入 .pt 权重文件到 server 目录可启用 YOLO）")

    def detect_and_save(self, image_path):
        import cv2
        import numpy as np

        if not os.path.exists(image_path):
            return {"success": False, "error": f"文件不存在: {image_path}"}

        img = cv2.imread(image_path)
        if img is None:
            return {"success": False, "error": "无法读取图像"}

        detections = []
        annotated = img.copy()

        if self.backend == "yolo" and self.model is not None:
            results = self.model.detect(image_path)
            for det in results:
                cls_name = det.get("class", "unknown")
                conf = det.get("confidence", 0.0)
                detections.append({
                    "class": cls_name,
                    "confidence": round(float(conf), 3),
                    "bbox": det.get("bbox", [0, 0, 0, 0]),
                })
            annotated = self.model.annotate_image(img, detections)
        else:
            # 模拟：随机产生 0~2 个检测结果
            h, w = img.shape[:2]
            disease_pool = ["叶片健康", "轻微叶斑", "蚜虫迹象", "白粉病早期"]
            n = random.randint(0, 2)
            for _ in range(n):
                x1 = random.randint(int(w * 0.1), int(w * 0.4))
                y1 = random.randint(int(h * 0.1), int(h * 0.4))
                x2 = random.randint(int(w * 0.5), int(w * 0.9))
                y2 = random.randint(int(h * 0.5), int(h * 0.9))
                conf = round(random.uniform(0.55, 0.92), 3)
                cls_name = random.choice(disease_pool)
                detections.append({
                    "class": cls_name, "confidence": conf,
                    "bbox": [x1, y1, x2, y2],
                })
                color = (0, 0, 255) if "健康" not in cls_name and "正常" not in cls_name else (0, 200, 0)
                cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
                cv2.putText(annotated, f"{cls_name} {conf:.2f}",
                            (x1, max(0, y1 - 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

        # 顶部时间 + 模式水印
        banner_text = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  [{self.backend.upper()} MODE]"
        cv2.rectangle(annotated, (0, 0), (w, 30), (0, 0, 0), -1)
        cv2.putText(annotated, banner_text, (10, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        # 保存到 static/detections（文件名带毫秒避免并发覆盖）
        fname = f"det_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        out_path = os.path.join(cfg.DETECTION_DIR, fname)
        cv2.imwrite(out_path, annotated, [cv2.IMWRITE_JPEG_QUALITY, 90])

        # 结果摘要
        if detections:
            top = max(detections, key=lambda d: d["confidence"])
            result_summary = f"{top['class']} ({top['confidence']:.0%})"
            conf = top["confidence"]
        else:
            result_summary = "未发现明显病虫害"
            conf = 0.0

        # 写入数据库
        with _db() as conn:
            conn.execute('''INSERT INTO pest_records
                (timestamp, image_path, result, confidence, mode, detection_json)
                VALUES (?, ?, ?, ?, ?, ?)''', (
                datetime.now().isoformat(),
                f"static/detections/{fname}",
                result_summary, conf, self.backend,
                json.dumps(detections, ensure_ascii=False),
            ))
            conn.commit()

        return {
            "success": True,
            "image_url": f"static/detections/{fname}",
            "result_summary": result_summary,
            "confidence": conf,
            "detections": detections,
            "backend": self.backend,
            "timestamp": datetime.now().isoformat(),
        }


detector = PestDetector()


# ============================================================
# 视频流线程（带 buffer 上限与帧锁）
# ============================================================
def video_stream_thread():
    global current_frame
    import cv2
    import numpy as np
    import requests

    MJPEG_BUFFER_MAX = cfg.MJPEG_BUFFER_MAX
    bytes_data = bytes()
    consecutive_failures = 0

    while True:
        try:
            response = requests.get(ESP32_STREAM_URL, stream=True, timeout=10)
            consecutive_failures = 0
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                bytes_data += chunk
                if len(bytes_data) > MJPEG_BUFFER_MAX:
                    # 防止异常数据无限增长，保留最后一段
                    bytes_data = bytes_data[-MJPEG_BUFFER_MAX:]
                a = bytes_data.find(b'\xff\xd8')
                b = bytes_data.find(b'\xff\xd9')
                if a != -1 and b != -1 and b > a:
                    jpg = bytes_data[a:b + 2]
                    bytes_data = bytes_data[b + 2:]
                    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        with _frame_lock:
                            current_frame = frame.copy()
        except Exception:
            consecutive_failures += 1
            # 失败后指数退避，最长 30s
            sleep_s = min(3 + consecutive_failures * 2, 30)
            time.sleep(sleep_s)


# ============================================================
# 自动控制后台线程（阈值 + 时控 + 抗抖动 + 决策引擎）
# ============================================================
def get_threshold_rules():
    with _db() as conn:
        rows = conn.execute('SELECT param, min_val, max_val, enabled FROM threshold_rules').fetchall()
        return {r['param']: {"min": r['min_val'], "max": r['max_val'], "enabled": bool(r['enabled'])}
                for r in rows}


def get_schedule_rules():
    with _db() as conn:
        rows = conn.execute('SELECT device, start_time, end_time, enabled, action FROM schedule_rules').fetchall()
        return [
            {"device": r['device'], "start": r['start_time'], "end": r['end_time'],
             "enabled": bool(r['enabled']), "action": r['action']}
            for r in rows
        ]


def log_device_action(device, action, user="system", mode="manual"):
    with _db() as conn:
        conn.execute('INSERT INTO device_logs (timestamp, device, action, user, mode) VALUES (?, ?, ?, ?, ?)',
                     (datetime.now().isoformat(), device, action, user, mode))
        conn.commit()


def _append_command(device_id, cmd_dict):
    """把命令加入待下发队列，并做长度限制与合并。"""
    with _pending_lock:
        queue = pending_commands.setdefault(device_id, [])
        # 合并同类指令：如果队列里最后一条已经有相同 key，覆盖它
        if queue:
            last = queue[-1]
            for k, v in cmd_dict.items():
                last[k] = v
        else:
            queue.append(dict(cmd_dict))
        # 长度限制
        if len(queue) > cfg.MAX_PENDING_COMMANDS:
            pending_commands[device_id] = queue[-cfg.MAX_PENDING_COMMANDS:]


def _update_device_state(updates):
    """更新 sensor_data.devices 的目标状态（由自动控制逻辑调用）。"""
    with _sensor_lock:
        sensor_data.setdefault("devices", {}).update(updates)


def _threshold_decision(engine):
    """
    使用 DecisionEngine 计算要下发的动作。
    引擎返回 {'commands': {fan: "on"/"off", ...}, 'alerts': [...]}。
    我们把它翻译为 pending_commands。
    """
    with _sensor_lock:
        sensor_copy = dict(sensor_data)

    rules = get_threshold_rules()
    # 把 rules 转化为 target_params 形式
    target_params = {
        "temperature": {"min": rules["temperature"]["min"],
                        "max": rules["temperature"]["max"],
                        "optimal": (rules["temperature"]["min"] + rules["temperature"]["max"]) / 2},
        "humidity": {"min": rules["humidity"]["min"], "max": rules["humidity"]["max"]},
        "soil_moisture": {"min": rules["soil_moisture"]["min"],
                          "max": rules["soil_moisture"]["max"],
                          "optimal": (rules["soil_moisture"]["min"] + rules["soil_moisture"]["max"]) / 2},
        "soil_temperature": {"min": rules["soil_temperature"]["min"],
                             "max": rules["soil_temperature"]["max"],
                             "optimal": (rules["soil_temperature"]["min"] + rules["soil_temperature"]["max"]) / 2},
        "light_lux": (rules["light"]["min"] + rules["light"]["max"]) / 2,
        "co2_ppm": {"optimal": (rules["co2"]["min"] + rules["co2"]["max"]) / 2},
    }

    try:
        result = engine.decide(sensor_copy, target_params=target_params)
    except Exception as e:
        print(f"[auto_control] engine decide error: {e}")
        return

    commands = result.get("commands", {})
    if not commands:
        return

    # 把 "on"/"off" 字符串 → bool；把不存在的设备 key 过滤掉
    to_send = {}
    valid_keys = {"fan", "light", "water", "heater", "vent_window", "mister",
                  "grow_light", "shade", "buzzer"}
    for key, val in commands.items():
        if key not in valid_keys:
            continue
        if isinstance(val, bool):
            to_send[key] = val
        elif isinstance(val, str):
            v = val.lower()
            if v in ("on", "open", "true", "1"):
                to_send[key] = True
            elif v in ("off", "close", "false", "0"):
                to_send[key] = False
    # 只下发硬件中真实存在的：fan / light / water / heater
    hardware_keys = {"fan", "light", "water", "heater"}
    filtered = {k: v for k, v in to_send.items() if k in hardware_keys}
    if not filtered:
        return

    # 写入待下发队列 + 更新本地目标状态
    _append_command(cfg.DEFAULT_CONTROLLER_ID, filtered)
    _update_device_state(filtered)
    for dev, onoff in filtered.items():
        log_device_action(dev, "on" if onoff else "off", mode="threshold")


def _schedule_decision():
    """时控：在时间段内 → 开；离开时间段 → 关。"""
    now = datetime.now().strftime("%H:%M")
    schedules = get_schedule_rules()

    for s in schedules:
        if not s["enabled"]:
            continue
        start, end, dev = s["start"], s["end"], s["device"]
        if not dev or not start or not end:
            continue

        # 支持跨午夜区间（如 22:00 - 06:00）
        if start <= end:
            in_range = start <= now <= end
        else:
            in_range = now >= start or now <= end

        target_on = True
        if not in_range:
            target_on = False

        # 只对与当前硬件相关的设备下发
        if dev not in ("fan", "light", "water", "heater"):
            continue

        _append_command(cfg.DEFAULT_CONTROLLER_ID, {dev: target_on})
        _update_device_state({dev: target_on})


def auto_control_thread():
    """后台循环：根据阈值或时控生成控制指令（使用 DecisionEngine + hysteresis）。"""
    from models.decision_engine import DecisionEngine
    engine = DecisionEngine(mode=cfg.DECISION_MODE)

    # 设备上一次的"目标状态"缓存，用于去抖
    last_targets = {"fan": None, "light": None, "water": None, "heater": None}

    while True:
        try:
            if auto_control_state["threshold_enabled"]:
                # 由 engine._rule_decision 内部做 hysteresis
                _threshold_decision(engine)

            if auto_control_state["schedule_enabled"]:
                _schedule_decision()

        except Exception as e:
            print(f"[auto_control_thread] error: {e}")

        time.sleep(cfg.DECISION_INTERVAL)


# ============================================================
# 工具函数
# ============================================================
def save_sensor_data(data):
    with _db() as conn:
        conn.execute('''INSERT INTO sensor_data
            (timestamp, temperature, humidity, light, co2, soil_moisture, soil_temperature, device_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (datetime.now().isoformat(),
             data.get('temperature', 0), data.get('humidity', 0),
             data.get('light', 0), data.get('co2', 0),
             data.get('soil_moisture', 0), data.get('soil_temperature', -1),
             data.get('device_id', ''),
            ))
        conn.commit()


def get_history_data(days=None, date=None):
    """返回历史传感器数据。

    调用方式：
    - get_history_data(date='2026-06-15')   → 查单日，按 30 分钟桶聚合，48 个点
    - get_history_data(days=30)              → 查近 N 天，**每天一个点（当天所有数据的平均值）**

    缺失的桶返回 None（前端用 null，让 Chart.js spanGaps=true 跳过）。

    同时返回每日统计：
    - 单日: result["stats"] = {date, temperature: {max, min, avg}, humidity, light, co2, soil_moisture}
    - 多日: result["daily_stats"] = [ {date, temperature: {max,min,avg}, ...}, ... ]，
            与 result["timestamps"] 一一对应。
    """
    avg = ("(CASE WHEN temperature < 0 THEN NULL ELSE AVG(temperature) END) AS temperature, "
           "(CASE WHEN humidity    < 0 THEN NULL ELSE AVG(humidity)    END) AS humidity, "
           "(CASE WHEN light       < 0 THEN NULL ELSE AVG(light)       END) AS light, "
           "(CASE WHEN co2         < 0 THEN NULL ELSE AVG(co2)         END) AS co2, "
           "(CASE WHEN soil_moisture < 0 THEN NULL ELSE AVG(soil_moisture) END) AS soil_moisture, "
           "(CASE WHEN soil_temperature < 0 THEN NULL ELSE AVG(soil_temperature) END) AS soil_temperature")

    stat_avg = ("AVG(NULLIF(temperature, -1)) AS t_avg, "
                "MIN(NULLIF(temperature, -1)) AS t_min, "
                "MAX(NULLIF(temperature, -1)) AS t_max, "
                "AVG(NULLIF(humidity,    -1)) AS h_avg, "
                "MIN(NULLIF(humidity,    -1)) AS h_min, "
                "MAX(NULLIF(humidity,    -1)) AS h_max, "
                "AVG(NULLIF(light,       -1)) AS l_avg, "
                "MIN(NULLIF(light,       -1)) AS l_min, "
                "MAX(NULLIF(light,       -1)) AS l_max, "
                "AVG(NULLIF(co2,         -1)) AS c_avg, "
                "MIN(NULLIF(co2,         -1)) AS c_min, "
                "MAX(NULLIF(co2,         -1)) AS c_max, "
                "AVG(NULLIF(soil_moisture, -1)) AS sm_avg, "
                "MIN(NULLIF(soil_moisture, -1)) AS sm_min, "
                "MAX(NULLIF(soil_moisture, -1)) AS sm_max, "
                "AVG(NULLIF(soil_temperature, -1)) AS st_avg, "
                "MIN(NULLIF(soil_temperature, -1)) AS st_min, "
                "MAX(NULLIF(soil_temperature, -1)) AS st_max, "
                "COUNT(*) AS n")

    def _fmt(v, digits=2):
        if v is None:
            return None
        try:
            return round(float(v), digits)
        except Exception:
            return None

    def _stat_row(avg_v, mn_v, mx_v, n):
        """把 AVG/MIN/MAX 三个值打包为 dict。avg=NULL → 全部 None。"""
        if avg_v is None and mn_v is None and mx_v is None:
            return {"max": None, "min": None, "avg": None, "n": n or 0}
        return {"max": _fmt(mx_v), "min": _fmt(mn_v), "avg": _fmt(avg_v), "n": n or 0}

    with _db() as conn:
        if date:
            # ===== 单日查询：按 30 分钟桶聚合 + 整日统计 =====
            bucket_expr = ("substr(timestamp, 12, 2) || ':' || "
                           "CASE WHEN substr(timestamp, 15, 2) < '30' THEN '00' ELSE '30' END")
            rows = conn.execute(
                f'''SELECT {bucket_expr} AS bucket,
                           COUNT(*) AS n,
                           {avg}
                    FROM sensor_data
                    WHERE timestamp >= ? AND timestamp < ?
                    GROUP BY bucket
                    ORDER BY bucket''',
                (f"{date}T00:00:00", f"{date}T23:59:59"),
            ).fetchall()
            stat = conn.execute(
                f'''SELECT {stat_avg}
                    FROM sensor_data
                    WHERE timestamp >= ? AND timestamp < ?''',
                (f"{date}T00:00:00", f"{date}T23:59:59"),
            ).fetchone()
            daily_stats_rows = []
        else:
            # ===== 多日查询：按日期聚合 =====
            d = max(1, min(365, int(days or 30)))
            start_date = (datetime.now() - timedelta(days=d)).date()
            rows = conn.execute(
                f'''SELECT substr(timestamp, 1, 10) AS bucket,
                           COUNT(*) AS n,
                           {avg}
                    FROM sensor_data
                    WHERE timestamp >= ?
                    GROUP BY substr(timestamp, 1, 10)
                    ORDER BY bucket''',
                (f"{start_date}T00:00:00",),
            ).fetchall()
            stat = None
            # 每日统计：按日期返回 max/min/avg
            daily_stats_rows = conn.execute(
                f'''SELECT substr(timestamp, 1, 10) AS bucket,
                           {stat_avg}
                    FROM sensor_data
                    WHERE timestamp >= ?
                    GROUP BY substr(timestamp, 1, 10)
                    ORDER BY bucket''',
                (f"{start_date}T00:00:00",),
            ).fetchall()

    by_bucket = {row["bucket"]: row for row in rows}
    by_bucket_stat = {row["bucket"]: row for row in daily_stats_rows}

    def _val(row, key):
        if not row:
            return None
        v = row[key]
        try:
            return None if v is None else round(float(v), 2)
        except Exception:
            return None

    if date:
        # 单日：48 个 30 分钟桶
        buckets = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
        month_day = date[5:]
        timestamps = [f"{month_day} {b}" for b in buckets]
        bucket_minutes = 30
    else:
        # 多日：每天一个点
        d = max(1, min(365, int(days or 30)))
        dates = [(datetime.now() - timedelta(days=i)).date() for i in range(d)][::-1]
        buckets = [str(d) for d in dates]
        timestamps = [d[5:] for d in buckets]
        bucket_minutes = 24 * 60

    result = {
        "timestamps": timestamps,
        "temperature": [], "humidity": [], "light": [], "co2": [],
        "soil_moisture": [], "soil_temperature": [],
        "days": days,
        "date": date,
        "bucket_minutes": bucket_minutes,
    }

    for b in buckets:
        row = by_bucket.get(b)
        result["temperature"].append(_val(row, "temperature"))
        result["humidity"].append(_val(row, "humidity"))
        result["light"].append(_val(row, "light"))
        result["co2"].append(_val(row, "co2"))
        result["soil_moisture"].append(_val(row, "soil_moisture"))
        result["soil_temperature"].append(_val(row, "soil_temperature"))

    # ============ 统计区 ============
    if date:
        result["stats"] = {
            "date": date,
            "samples": stat["n"] if stat else 0,
            "temperature": _stat_row(stat["t_avg"] if stat else None,
                                     stat["t_min"] if stat else None,
                                     stat["t_max"] if stat else None,
                                     stat["n"]    if stat else 0),
            "humidity":    _stat_row(stat["h_avg"] if stat else None,
                                     stat["h_min"] if stat else None,
                                     stat["h_max"] if stat else None,
                                     stat["n"]    if stat else 0),
            "light":       _stat_row(stat["l_avg"] if stat else None,
                                     stat["l_min"] if stat else None,
                                     stat["l_max"] if stat else None,
                                     stat["n"]    if stat else 0),
            "co2":         _stat_row(stat["c_avg"] if stat else None,
                                     stat["c_min"] if stat else None,
                                     stat["c_max"] if stat else None,
                                     stat["n"]    if stat else 0),
            "soil_moisture": _stat_row(stat["sm_avg"] if stat else None,
                                       stat["sm_min"] if stat else None,
                                       stat["sm_max"] if stat else None,
                                       stat["n"]    if stat else 0),
            "soil_temperature": _stat_row(stat["st_avg"] if stat else None,
                                           stat["st_min"] if stat else None,
                                           stat["st_max"] if stat else None,
                                           stat["n"]    if stat else 0),
        }
    else:
        result["daily_stats"] = []
        for b in buckets:
            s = by_bucket_stat.get(b)
            if not s:
                result["daily_stats"].append({
                    "date": b,
                    "samples": 0,
                    "temperature": {"max": None, "min": None, "avg": None, "n": 0},
                    "humidity":    {"max": None, "min": None, "avg": None, "n": 0},
                    "light":       {"max": None, "min": None, "avg": None, "n": 0},
                    "co2":         {"max": None, "min": None, "avg": None, "n": 0},
                    "soil_moisture":   {"max": None, "min": None, "avg": None, "n": 0},
                    "soil_temperature":{"max": None, "min": None, "avg": None, "n": 0},
                })
            else:
                result["daily_stats"].append({
                    "date": b,
                    "samples": s["n"] or 0,
                    "temperature": _stat_row(s["t_avg"], s["t_min"], s["t_max"], s["n"]),
                    "humidity":    _stat_row(s["h_avg"], s["h_min"], s["h_max"], s["n"]),
                    "light":       _stat_row(s["l_avg"], s["l_min"], s["l_max"], s["n"]),
                    "co2":         _stat_row(s["c_avg"], s["c_min"], s["c_max"], s["n"]),
                    "soil_moisture":   _stat_row(s["sm_avg"], s["sm_min"], s["sm_max"], s["n"]),
                    "soil_temperature": _stat_row(s["st_avg"], s["st_min"], s["st_max"], s["n"]),
                })
    return result


# ============================================================
# 路由 - 登录
# ============================================================
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if not username or not password:
            return render_template('login.html', error="账号或密码不能为空")
        user = get_user(username)
        if user and check_password_hash(user['password_hash'], password):
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        return render_template('login.html', error="账号或密码错误")
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/change_password', methods=['POST'])
@login_required
def change_password():
    """允许登录用户修改自己的密码（简单自管理）。"""
    data = request.json or {}
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    username = session.get('username')
    if len(new_pw) < 6:
        return jsonify({"success": False, "error": "新密码至少 6 位"}), 400
    user = get_user(username)
    if not user or not check_password_hash(user['password_hash'], old_pw):
        return jsonify({"success": False, "error": "原密码错误"}), 400
    new_hash = generate_password_hash(new_pw, method='pbkdf2:sha256')
    with _db() as conn:
        conn.execute('UPDATE users SET password_hash = ? WHERE username = ?', (new_hash, username))
        conn.commit()
    return jsonify({"success": True})


# ============================================================
# 路由 - 主页
# ============================================================
@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/video_feed')
@login_required
def video_feed():
    def generate():
        import cv2
        while True:
            with _frame_lock:
                frame = current_frame
            if frame is not None:
                ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ret:
                    frame_bytes = buffer.tobytes()
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(0.08)  # ~12 fps 上限，避免浏览器狂刷
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


# ============================================================
# API - 传感器 / 指令 / 控制
# ============================================================
@app.route('/api/upload', methods=['POST'])
@login_required
def upload_image():
    global last_cam_update
    image_data = request.data
    filename = f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    filepath = os.path.join(cfg.CAPTURE_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(image_data)
    last_cam_update = datetime.now()
    return jsonify({"status": "success", "filename": filename})


@app.route('/api/capture/config', methods=['GET'])
@login_required
def get_capture_config():
    return jsonify(capture_config)


@app.route('/api/capture/config', methods=['POST'])
@login_required
def set_capture_config():
    data = request.json or {}
    if 'enabled' in data:
        capture_config['enabled'] = bool(data['enabled'])
    if 'interval' in data:
        capture_config['interval'] = max(1, int(data['interval']))
    return jsonify({"status": "success", "config": capture_config})


@app.route('/api/capture/manual', methods=['POST'])
@login_required
def manual_capture():
    """手动抓拍：优先保存当前视频帧；无视频帧则下发命令给摄像头。"""
    global last_cam_update
    import cv2
    import numpy as np
    filename = f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    filepath = os.path.join(cfg.CAPTURE_DIR, filename)

    with _frame_lock:
        frame = current_frame
    if frame is not None:
        cv2.imwrite(filepath, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        last_cam_update = datetime.now()
        return jsonify({"status": "success", "filename": filename,
                        "url": f"static/captures/{filename}"})
    # 回退：请求硬件拍照
    _append_command(cfg.DEFAULT_CAM_ID, {"capture": True})
    return jsonify({"status": "success", "message": "拍照指令已下发", "filename": filename})


@app.route('/api/sensor', methods=['POST'])
def receive_sensor():
    """硬件上传传感器数据（硬件需带 device_id，无需登录，但需简单签名可扩展）。"""
    global last_esp32_update
    data = request.json or {}
    device_id = data.get('device_id', cfg.DEFAULT_CONTROLLER_ID)

    # 温度/湿度需是合理数字（不是字符串也不是 NaN）
    def _as_float(v, default=0.0):
        try:
            f = float(v)
            if f != f:  # NaN check
                return default
            return f
        except (TypeError, ValueError):
            return default

    with _sensor_lock:
        sensor_data.update({
            "temperature": _as_float(data.get("temperature", 0)),
            "humidity": _as_float(data.get("humidity", 0)),
            "light": int(_as_float(data.get("light", 0))),
            "co2": int(_as_float(data.get("co2", 0))),
            "soil_moisture": _as_float(data.get("soil_moisture", 0)),
            "soil_temperature": _as_float(data.get("soil_temperature", -1)),
            "devices": data.get("devices", {}),
            "last_update": datetime.now().isoformat(),
        })
    last_esp32_update = datetime.now()
    save_sensor_data({**data, "device_id": device_id})
    return jsonify({"status": "ok"})


@app.route('/api/commands', methods=['GET'])
def get_commands():
    """硬件拉取待下发命令。"""
    device = request.args.get('device', cfg.DEFAULT_CONTROLLER_ID)
    with _pending_lock:
        queue = pending_commands.get(device, [])
        pending_commands[device] = []
    return jsonify(queue)


@app.route('/api/control', methods=['POST'])
@login_required
def control_device():
    data = request.json or {}
    device_id = data.get('device', cfg.DEFAULT_CONTROLLER_ID)
    user = session.get('username', 'system')

    command = {}
    valid_keys = {"fan", "light", "water", "heater", "all_off"}
    for key in valid_keys:
        if key in data:
            command[key] = bool(data[key]) if key != "all_off" else bool(data[key])

    if not command:
        return jsonify({"success": False, "error": "未指定有效控制项"}), 400

    _append_command(device_id, command)

    for key, val in command.items():
        action = "on" if val else "off"
        with _sensor_lock:
            if key == "all_off" and val:
                for d in ['fan', 'light', 'water', 'heater']:
                    sensor_data.setdefault("devices", {})[d] = False
                log_device_action(key, "off", user=user, mode="manual")
            else:
                sensor_data.setdefault("devices", {})[key] = bool(val)
                log_device_action(key, action, user=user, mode="manual")

    return jsonify({"status": "success", "command": command})


# ============================================================
# API - 状态 / 历史
# ============================================================
@app.route('/api/status')
@login_required
def get_status():
    esp32_online = False
    cam_online = False
    if last_esp32_update:
        esp32_online = (datetime.now() - last_esp32_update).total_seconds() < 15
    if last_cam_update:
        cam_online = (datetime.now() - last_cam_update).total_seconds() < 30

    with _sensor_lock:
        sensor_copy = {
            "temperature": sensor_data.get("temperature", 0),
            "humidity": sensor_data.get("humidity", 0),
            "light": sensor_data.get("light", 0),
            "co2": sensor_data.get("co2", 0),
            "soil_moisture": sensor_data.get("soil_moisture", 0),
            "soil_temperature": sensor_data.get("soil_temperature", -1),
            "devices": dict(sensor_data.get("devices", {})),
            "last_update": sensor_data.get("last_update"),
        }

    return jsonify({
        "sensor": sensor_copy,
        "system": {
            "esp32_online": esp32_online,
            "cam_online": cam_online,
            "mode": auto_control_state["mode"],
            "threshold_enabled": auto_control_state["threshold_enabled"],
            "schedule_enabled": auto_control_state["schedule_enabled"],
            "decision_engine": cfg.DECISION_MODE,
        },
        "timestamp": datetime.now().isoformat(),
    })


@app.route('/api/history')
@login_required
def get_history():
    """支持两种查询方式：
    - /api/history?date=2026-06-15   → 查单日
    - /api/history?days=5            → 查近 N 天（跨天平均）
    """
    date = request.args.get('date')
    days = request.args.get('days', type=int)
    return jsonify(get_history_data(days=days, date=date))


# ============================================================
# API - 自动控制 (阈值 / 时控)
# ============================================================
@app.route('/api/auto/mode', methods=['POST'])
@login_required
def set_auto_mode():
    data = request.json or {}
    mode = data.get("mode", "manual")
    if mode in ("manual", "threshold", "schedule"):
        auto_control_state["mode"] = mode
        auto_control_state["threshold_enabled"] = (mode == "threshold")
        auto_control_state["schedule_enabled"] = (mode == "schedule")
        log_device_action(
            "system", f"切换到{mode}模式",
            user=session.get('username', 'system'), mode="system",
        )
    return jsonify({"status": "success", "mode": mode, **auto_control_state})


@app.route('/api/auto/mode', methods=['GET'])
@login_required
def get_auto_mode():
    return jsonify(auto_control_state)


@app.route('/api/threshold/rules', methods=['GET'])
@login_required
def get_threshold_api():
    return jsonify(get_threshold_rules())


@app.route('/api/threshold/rules', methods=['POST'])
@login_required
def update_threshold_api():
    data = request.json or {}
    # 校验：每个参数必须 min < max
    valid_params = {"temperature", "humidity", "soil_moisture", "soil_temperature", "co2", "light"}
    to_write = {}
    for param, payload in data.items():
        if param not in valid_params:
            continue
        try:
            mn = float(payload.get("min", 0))
            mx = float(payload.get("max", 100))
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": f"参数 {param} 的数值非法"}), 400
        if mn >= mx:
            return jsonify({"success": False, "error": f"{param}: 最小值必须小于最大值"}), 400
        enabled = 1 if payload.get("enabled", True) else 0
        to_write[param] = (mn, mx, enabled)

    with _db() as conn:
        for param, (mn, mx, en) in to_write.items():
            conn.execute('INSERT OR REPLACE INTO threshold_rules (param, min_val, max_val, enabled) VALUES (?, ?, ?, ?)',
                         (param, mn, mx, en))
        conn.commit()
    return jsonify(get_threshold_rules())


@app.route('/api/schedule/rules', methods=['GET'])
@login_required
def get_schedule_api():
    return jsonify(get_schedule_rules())


@app.route('/api/schedule/rules', methods=['POST'])
@login_required
def update_schedule_api():
    data = request.json or {}
    # 清空旧规则，写入新规则
    new_rules = data.get("rules", [])
    with _db() as conn:
        conn.execute('DELETE FROM schedule_rules')
        for item in new_rules:
            device = item.get("device", "")
            start = item.get("start_time", "")
            end = item.get("end_time", "")
            if not device or not start or not end:
                continue
            conn.execute(
                'INSERT INTO schedule_rules (device, start_time, end_time, enabled, action) VALUES (?, ?, ?, ?, ?)',
                (device, start, end, 1 if item.get("enabled", True) else 0, item.get("action", "on")),
            )
        conn.commit()
    return jsonify({"status": "success", "rules": get_schedule_rules()})


# ============================================================
# API - 病虫害识别
# ============================================================
@app.route('/api/pest/recognize', methods=['POST'])
@login_required
def pest_recognize():
    """触发一次检测：优先抓当前视频帧；否则使用最新抓拍。"""
    import cv2

    filename = None
    target_path = None
    with _frame_lock:
        frame = current_frame
    if frame is not None:
        filename = f"capture_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        target_path = os.path.join(cfg.CAPTURE_DIR, filename)
        cv2.imwrite(target_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])

    if not target_path or not os.path.exists(target_path):
        imgs = sorted([f for f in os.listdir(cfg.CAPTURE_DIR) if f.endswith(('.jpg', '.jpeg', '.png'))])
        if not imgs:
            return jsonify({"success": False, "error": "没有可用图片，请先确保摄像头在线或已手动抓拍"}), 400
        filename = imgs[-1]
        target_path = os.path.join(cfg.CAPTURE_DIR, filename)

    result = detector.detect_and_save(target_path)
    if not result.get("success"):
        return jsonify(result), 500
    return jsonify(result)


@app.route('/api/pest/recognize/file', methods=['POST'])
@login_required
def pest_recognize_file():
    """对指定文件名做检测（文件需在 static/captures 目录下）。"""
    filename = (request.json or {}).get("filename")
    if not filename:
        return jsonify({"success": False, "error": "缺少 filename"}), 400
    # 显式 basename 防止路径穿越
    filename = os.path.basename(filename)
    target_path = os.path.join(cfg.CAPTURE_DIR, filename)
    if not os.path.exists(target_path):
        return jsonify({"success": False, "error": "文件不存在"}), 404
    result = detector.detect_and_save(target_path)
    if not result.get("success"):
        return jsonify(result), 500
    return jsonify(result)


@app.route('/api/pest/records')
@login_required
def pest_records():
    limit = request.args.get('limit', 30, type=int)
    limit = max(1, min(500, limit))
    with _db() as conn:
        rows = conn.execute(
            'SELECT id, timestamp, image_path, result, confidence, mode '
            'FROM pest_records ORDER BY id DESC LIMIT ?', (limit,)
        ).fetchall()
    return jsonify([{
        "id": r['id'], "timestamp": r['timestamp'],
        "image_path": r['image_path'], "result": r['result'],
        "confidence": r['confidence'], "mode": r['mode']
    } for r in rows])


@app.route('/api/captures')
@login_required
def list_captures():
    imgs = sorted(
        [f for f in os.listdir(cfg.CAPTURE_DIR) if f.endswith(('.jpg', '.jpeg', '.png'))],
        reverse=True,
    )
    return jsonify([{"filename": f, "url": f"static/captures/{f}"} for f in imgs[:50]])


# ============================================================
# 启动
# ============================================================
if __name__ == '__main__':
    threading.Thread(target=video_stream_thread, daemon=True).start()
    threading.Thread(target=auto_control_thread, daemon=True).start()

    print("=" * 60)
    print("  智慧微型温室服务器已启动")
    print("=" * 60)
    print(f"  访问地址: http://{cfg.SERVER_HOST}:{cfg.SERVER_PORT}")
    print(f"  默认账号: {cfg.ADMIN_USERNAME} / {cfg.ADMIN_PASSWORD}  "
          f"(首次启动自动写入，可在 /login 登录后通过 POST /api/change_password 修改)")
    print(f"  检测后端: {detector.backend.upper()}")
    print(f"  决策引擎: {cfg.DECISION_MODE.upper()}")
    print("=" * 60)

    app.run(host=cfg.SERVER_HOST, port=cfg.SERVER_PORT, debug=False, threaded=True)
