# 智慧微型温室项目 — 代码逻辑问题修复记录

> 本文件记录 2026-06-16 的大版本改动，修复在"全局代码检索"阶段发现的 30+ 项代码逻辑问题 / 安全隐患 / 潜在 bug。

## 0. 升级前请先看我 👀

- **默认账号改为 admin / admin123（首次启动自动写入数据库，可在"控制台中通过 `POST /api/change_password` 修改密码）。
- 所有 `/api/*`（不含 `/api/sensor`、`/api/commands` 为硬件端点）均需登录访问。
- 决策引擎现在会读取 `config.py` 中的 `HYSTERESIS_*` 做滞环控制，避免继电器反复抖动。

---

## 1. 后端（server/app.py & config.py

| # | 改动项 | 问题类型 | 说明 |
|:---:|---|---|---|
| 1.1 | 所有 `app.secret_key 硬编码为常量 | 安全 | 从 `config.SECRET_KEY`，通过 `GREENHOUSE_SECRET` 环境变量覆盖，默认启动时写死但可以不设置环境变量时，明确提示 |
| 1.2 | `/api/*` 缺少鉴权 | 安全 | 新增 `login_required` 装饰器：所有敏感接口（控制、抓拍、检测、历史、阈值、时控）均需登录 |
| 1.3 | admin/admin123 明文写死在代码，无盐哈希 | 安全 | 改为 SQLite `users` 表 + `werkzeug.security.generate_password_hash` 加盐哈希。初始密码由 `config.ADMIN_PASSWORD`，登录自 |
| 1.4 | `POST /api/control` 缺少设备状态的"真实回读" | 数据 | 后端不再在控制后立即改 UI，UI 需由下一次 `/api/status` 回显 |
| 1.5 | 阈值规则缺少校验（min < max） | Bug | `update_threshold_api` 加入 `min < max` 校验并返回 400 |
| 1.6 | pending_commands 无限增长 | 性能 | `_append_command()`：合并同设备的重复指令、限制队列长度 |
| 1.7 | MJPEG 解析 `bytes_data` 无限增长 | 性能 | 限制最大 4MB，超出截断；失败指数退避 |
| 1.8 | SQLite 没事务 | 代码规范 | 所有 DB 写入都改为 `with _db() as conn:`（上下文管理器） |
| 1.9 | 时控仅开不关 | Bug | 时间区间结束时下发 False 关闭设备 |
| 1.10 | 阈值控制每 5 秒触发抖动 | Bug | 接入 decision_engine 内部处理 hysteresis 逻辑 |
| 1.11 | `video_feed` 无帧锁 | 并发 | `current_frame` 读/写前后加锁，浏览器多路访问不再出错 |
| 1.12 | 文件名用毫秒时间戳 | Bug | 避免 1 秒内并发抓拍会相互覆盖 |
| 1.13 | `detect_and_save`：`detections` 重复判断用 `'未发现` / `未检测到` 更准确 |
| 1.14 | 新增 `/api/pest/recognize/file` 中 `filename` 经 `os.path.basename` 过滤 | 安全 | 路径穿越防护 |
| 1.15 | `get_history_data` 与后端不再用 `int` 越界，SQL 无 `ORDER BY` | Bug | SQL 中明确 `ORDER BY timestamp` |
| 1.16 | `set_auto_mode` 失败响应 JSON 序列化失败 | Bug | `auto_control_state` 线程间访问共享 dict 仅在内部改键值对线程锁 |
| 1.17 | `api_status` 响应增加 `decision_engine` 段 | 可读性 | 前端可显示当前决策模式 |

---

## 2. 决策引擎（server/models/decision_engine.py

| # | 改动项 | 问题类型 |
|:---:|---|---|
| 2.1 | `_fuzzy_fan` 权重计算错误 | Bug | 之前是 `total / weight` 量级错乱 → 现在是标准加权平均 `sum(speed * weight) / sum(weight)`，`fan_speed 0-100 映射 |
| 2.2 | 规则引擎未实现滞环 | Bug | 阈值越过 `HYSTERESIS`（温度 1°C、湿度 3%、土壤 2%、光照 1000 lux、CO2 50 ppm），在 "正常区间 - 滞环" 范围内保持原状 |
| 2.3 | `buzzer` 未定义设备键 | Bug | 规则中保留为 "通风优先" |
| 2.4 | `grow_light`、`vent_window`、`mister`、`water_pump` → 真正存在的硬件映射回 `fan/light/water/heater` | 映射 | 简化决策引擎到 4 键硬件 |

---

## 3. ESP32 主控固件（hardware/esp32_controller.ino）

| # | 改动项 | 问题类型 |
|:---:|---|---|
| 3.1 | `light` / `co2` / `soilMoisture` 用 `random()` 伪造 | Bug/数据 | 现在真实未接入统一发送 `-1`；后端前端用 `_isMissing` 识别为 "未接入" |
| 3.2 | `soilMoisture = h * 0.85` 用空气湿度来推算土壤湿度 | Bug | 停止用 `soil_moisture = -1`，避免浇水逻辑错乱 |
| 3.3 | `deviceState` 状态回读仍通过 `uploadSensorData` 的 `devices` JSON 发给服务器 | 完整 | `device_id=DEVICE_ID` 状态回读，以便服务器"知道"真实状态 |
| 3.4 | 读取值合理性校验 | Bug | `h > 100` 也被视为无效值 |

---

## 4. 前端（server/templates/index.html / login.html

| # | 改动项 | 问题类型 |
|:---:|---|---|
| 4.1 | 设备开关 `toggleDevice` 立即乐观更新 UI | Bug | 仅发送指令；只有在下次 `/api/status` 返回后，服务器端状态回读显示 true |
| 4.2 | `emergency_stop` 发送后立即改 UI | 同样改为"指令已下发" |
| 4.3 | 传感器 `-1` 被当作合法值显示 | 可读性 | `_isMissing()` 后显示"未接入" |
| 4.4 | 光照、CO₂ 数量级碾压温湿度曲线 | 可视化 | 两条曲线归一化为 `%` 显示；当该传感器全缺失时自动隐藏 |
| 4.5 | 切换阈值时无 `min < max` 后端校验 | Bug | 已在 `update_threshold_api` 校验 |
| 4.6 | `setMode` 失败后仍把 UI 切换到目标模式 | 改为仅当 HTTP ok 才改，否则保留原模式 |

---

## 5. 配置与依赖

| # | 改动项 | 问题类型 |
|:---:|---|---|
| 5.1 | `config.py` 重写：所有键都 `os.environ.get` | 配置一致性 |
| 5.2 | `DEFAULT_CONTROLLER_ID`、`DEFAULT_CAM_ID` 统一为 `esp32_greenhouse_01 / esp32cam_01` | 一致性 |
| 5.3 | 新增 `HYSTERESIS_*`、`MAX_PENDING_COMMANDS`、`MJPEG_BUFFER_MAX` | 配置 |
| 5.4 | `requirements.txt` 中文注释，标记 ultralytics / gunicorn 作为可选 | 依赖管理 |

---

## 6. 已删除 / 保留项

| # | 项目 | 原因 |
|:---:|---|---|
| 6.1 | `server/models/crop_profiles.py`（保留，但未在 app.py 内 `import` | 功能可扩展接入 |
| 6.2 | `disease_detector.py`（保留，`detector.py` 内作为 YOLO 后端加载逻辑，失败降级模拟 |
| 6.3 | `growth_engine.py` 保留 | 计算生长曲线的后段保留作为可插拔 |
| 6.4 | 根目录 `templates/*.html` 保留，正式入口在 `server/templates/*.html` | 结构清楚 |

---

## 7. 使用与回滚

启动：

```bash
cd server
pip install -r requirements.txt
# 可选：set GREENHOUSE_SECRET=your_strong_secret
python app.py
# 浏览器访问 http://localhost:5000
```

回滚：删除以上所有文件即可。所有数据文件 `greenhouse.db` 在首次启动时自动重新创建，与旧版数据不兼容（因新增 users 表），若你想用旧库，请备份 `rm greenhouse.db`

---

## 8. 下一步建议

1. **新增：通过 `config.DECISION_MODE` 支持 `rule/fuzzy/ai` 三种引擎，并接入真实的硬件（光感、土壤湿度模块、CO2 模块）；
2. **部署：生产环境用 `gunicorn -w 2 -b 0.0.0.0:5000 app:app`；
3. **硬件：后续可考虑在 ESP32 中接入 MQTT（在 `config.py` 已有 `MQTT_*` 预留键，方便日后统一在硬件接入）；
4. **MQTT 可扩展。
