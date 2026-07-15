#!/usr/bin/env python3
"""监控模块 — 请求统计 + 健康检查 + 结构化日志 + 飞书告警

集成方式(在 app.py 里):
    from monitor import init_monitor
    init_monitor(app, service_name="finance-agent", db_path=DB_PATH,
                 llm_test_fn=test_connection, alert_feishu_fn=feishu_send_post,
                 alert_chat_id="oc_xxx")

提供:
- /api/stats        — 请求统计(总数/错误/平均耗时/端点明细)
- /api/health/full  — 完整健康检查(DB + LLM + uptime)
- /api/alert/test   — 发送测试告警(验证飞书通道)
- /monitor          — 监控仪表盘 HTML
- 请求日志写到 logs/{service}.log(10MB 轮转)
- 5xx 错误/健康检查失败/错误率突增 → 自动飞书告警(节流防刷)
"""
import os
import time
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from flask import request, jsonify, render_template_string
import logging
from logging.handlers import RotatingFileHandler

# === 进程级统计(线程安全)===
_STATS_LOCK = threading.Lock()
_STATS = {
    "start_time": time.time(),
    "total_requests": 0,
    "error_count": 0,          # 4xx + 5xx
    "server_error_count": 0,   # 仅 5xx
    "endpoints": {},           # path -> {count, errors, total_duration, max_duration}
    "recent_errors": [],       # 最近 50 条错误 [{time, path, status, method, ip}]
    "alerts_sent": 0,          # 累计告警数
    "recent_alerts": [],       # 最近 20 条告警记录
}
_MAX_RECENT_ERRORS = 50
_MAX_RECENT_ALERTS = 20

# === 告警节流状态 ===
_ALERT_LOCK = threading.Lock()
_ALERT_STATE = {
    "last_5xx_alert": {},        # path -> timestamp(同端点 5 分钟节流)
    "last_health_alert": 0,      # timestamp(健康检查 10 分钟节流)
    "last_error_rate_alert": 0,  # timestamp(错误率 15 分钟节流)
    "error_window": [],          # 最近 5 分钟的请求记录 [(timestamp, is_error)]
}
_ALERT_5XX_COOLDOWN = 300       # 5 分钟
_ALERT_HEALTH_COOLDOWN = 600    # 10 分钟
_ALERT_ERROR_RATE_COOLDOWN = 900  # 15 分钟
_ALERT_ERROR_RATE_THRESHOLD = 0.30  # 错误率 > 30% 触发

# 全局引用(app 初始化时设置)
_ALERT_FEISHU_FN = None
_ALERT_CHAT_ID = None
_ALERT_SERVICE_NAME = "unknown"


def _track_request(path, method, status, duration, ip):
    """记录一次请求(线程安全)+ 触发告警检查"""
    with _STATS_LOCK:
        _STATS["total_requests"] += 1
        is_error = status >= 400
        is_5xx = status >= 500
        if is_error:
            _STATS["error_count"] += 1
        if is_5xx:
            _STATS["server_error_count"] += 1

        if path not in _STATS["endpoints"]:
            _STATS["endpoints"][path] = {
                "count": 0, "errors": 0, "total_duration": 0.0, "max_duration": 0.0,
            }
        ep = _STATS["endpoints"][path]
        ep["count"] += 1
        ep["total_duration"] += duration
        ep["max_duration"] = max(ep["max_duration"], duration)
        if is_error:
            ep["errors"] += 1
            _STATS["recent_errors"].append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "path": path,
                "method": method,
                "status": status,
                "ip": ip,
                "duration_ms": round(duration * 1000, 1),
            })
            if len(_STATS["recent_errors"]) > _MAX_RECENT_ERRORS:
                _STATS["recent_errors"] = _STATS["recent_errors"][-_MAX_RECENT_ERRORS:]

        # 记录到错误率窗口
        now = time.time()
        _ALERT_STATE["error_window"].append((now, is_error))
        # 清理 5 分钟前的记录
        cutoff = now - 300
        _ALERT_STATE["error_window"] = [
            (t, e) for t, e in _ALERT_STATE["error_window"] if t > cutoff
        ]

    # 5xx 告警(同端点 5 分钟节流)
    if is_5xx:
        _trigger_5xx_alert(path, method, status, duration, ip)

    # 错误率突增告警(15 分钟节流)
    _check_error_rate_alert()


def _send_feishu_alert(title, paragraphs):
    """发送飞书告警(带节流和异常保护)"""
    global _ALERT_FEISHU_FN, _ALERT_CHAT_ID
    if not _ALERT_FEISHU_FN or not _ALERT_CHAT_ID:
        return False  # 未配置飞书
    try:
        result = _ALERT_FEISHU_FN(_ALERT_CHAT_ID, title, paragraphs)
        ok = result.get("ok", False) if isinstance(result, dict) else False
        if ok:
            with _STATS_LOCK:
                _STATS["alerts_sent"] += 1
                _STATS["recent_alerts"].append({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "title": title,
                    "ok": True,
                })
                if len(_STATS["recent_alerts"]) > _MAX_RECENT_ALERTS:
                    _STATS["recent_alerts"] = _STATS["recent_alerts"][-_MAX_RECENT_ALERTS:]
        return ok
    except Exception:
        return False


def _trigger_5xx_alert(path, method, status, duration, ip):
    """5xx 错误告警(同端点 5 分钟节流)"""
    now = time.time()
    with _ALERT_LOCK:
        last = _ALERT_STATE["last_5xx_alert"].get(path, 0)
        if now - last < _ALERT_5XX_COOLDOWN:
            return  # 节流中,跳过
        _ALERT_STATE["last_5xx_alert"][path] = now

    title = f"🚨 {_ALERT_SERVICE_NAME} 5xx 告警"
    paragraphs = [[
        {"tag": "text", "text": f"服务: {_ALERT_SERVICE_NAME}\n"},
        {"tag": "text", "text": f"端点: {method} {path}\n"},
        {"tag": "text", "text": f"状态码: {status}\n"},
        {"tag": "text", "text": f"耗时: {duration*1000:.0f} ms\n"},
        {"tag": "text", "text": f"IP: {ip}\n"},
        {"tag": "text", "text": f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"},
        {"tag": "text", "text": f"\n⚠️ 同端点 5 分钟内不重复告警\n"},
        {"tag": "text", "text": f"📊 查看仪表盘: http://124.222.181.129:{'5002' if 'finance' in _ALERT_SERVICE_NAME else '5003'}/monitor\n"},
    ]]
    _send_feishu_alert(title, paragraphs)


def _trigger_health_alert(component, error):
    """健康检查失败告警(10 分钟节流)"""
    now = time.time()
    with _ALERT_LOCK:
        if now - _ALERT_STATE["last_health_alert"] < _ALERT_HEALTH_COOLDOWN:
            return
        _ALERT_STATE["last_health_alert"] = now

    title = f"🚨 {_ALERT_SERVICE_NAME} 健康检查失败"
    paragraphs = [[
        {"tag": "text", "text": f"服务: {_ALERT_SERVICE_NAME}\n"},
        {"tag": "text", "text": f"故障组件: {component}\n"},
        {"tag": "text", "text": f"错误: {str(error)[:200]}\n"},
        {"tag": "text", "text": f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"},
        {"tag": "text", "text": f"\n⚠️ 10 分钟内不重复告警\n"},
    ]]
    _send_feishu_alert(title, paragraphs)


def _check_error_rate_alert():
    """错误率突增告警(15 分钟节流,5 分钟窗口内错误率 >30% 触发)"""
    now = time.time()
    with _ALERT_LOCK:
        if now - _ALERT_STATE["last_error_rate_alert"] < _ALERT_ERROR_RATE_COOLDOWN:
            return
        window = _ALERT_STATE["error_window"]
        if len(window) < 20:
            return  # 请求太少,不告警
        error_count = sum(1 for _, is_err in window if is_err)
        error_rate = error_count / len(window)
        if error_rate < _ALERT_ERROR_RATE_THRESHOLD:
            return
        _ALERT_STATE["last_error_rate_alert"] = now

    title = f"⚠️ {_ALERT_SERVICE_NAME} 错误率突增"
    paragraphs = [[
        {"tag": "text", "text": f"服务: {_ALERT_SERVICE_NAME}\n"},
        {"tag": "text", "text": f"5 分钟窗口: {len(window)} 请求, {error_count} 错误\n"},
        {"tag": "text", "text": f"错误率: {error_rate*100:.1f}%(阈值 30%)\n"},
        {"tag": "text", "text": f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"},
        {"tag": "text", "text": f"\n⚠️ 15 分钟内不重复告警\n"},
    ]]
    _send_feishu_alert(title, paragraphs)


def _get_stats():
    """获取统计快照(线程安全)"""
    with _STATS_LOCK:
        uptime = time.time() - _STATS["start_time"]
        endpoints = {}
        for path, ep in _STATS["endpoints"].items():
            count = ep["count"]
            endpoints[path] = {
                "count": count,
                "errors": ep["errors"],
                "avg_duration_ms": round((ep["total_duration"] / count * 1000), 1) if count > 0 else 0,
                "max_duration_ms": round(ep["max_duration"] * 1000, 1),
            }
        return {
            "service_start": datetime.fromtimestamp(_STATS["start_time"]).strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_seconds": round(uptime, 0),
            "uptime_human": _format_uptime(uptime),
            "total_requests": _STATS["total_requests"],
            "error_count": _STATS["error_count"],
            "server_error_count": _STATS["server_error_count"],
            "error_rate": round(_STATS["error_count"] / _STATS["total_requests"] * 100, 2) if _STATS["total_requests"] > 0 else 0,
            "alerts_sent": _STATS["alerts_sent"],
            "recent_alerts": list(_STATS["recent_alerts"]),
            "endpoints": endpoints,
            "recent_errors": list(_STATS["recent_errors"]),
        }


def _format_uptime(seconds):
    """格式化运行时间"""
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds / 60)}分钟"
    if seconds < 86400:
        return f"{int(seconds / 3600)}小时{int((seconds % 3600) / 60)}分"
    return f"{int(seconds / 86400)}天{int((seconds % 86400) / 3600)}小时"


def _check_db(db_path, alert_on_fail=False):
    """检查 SQLite 连通性"""
    try:
        conn = sqlite3.connect(db_path, timeout=3)
        c = conn.cursor()
        c.execute("SELECT 1")
        conn.close()
        return {"ok": True}
    except Exception as e:
        if alert_on_fail:
            _trigger_health_alert("Database", e)
        return {"ok": False, "error": str(e)}


def _check_llm(test_fn, alert_on_fail=False):
    """检查 LLM 连通性(调用 test_connection)"""
    try:
        result = test_fn()
        ok = result.get("llm_ok", False)
        if not ok and alert_on_fail:
            _trigger_health_alert("LLM", result.get("error", "unknown"))
        return {
            "ok": ok,
            "model": result.get("model", ""),
            "error": result.get("error", ""),
        }
    except Exception as e:
        if alert_on_fail:
            _trigger_health_alert("LLM", e)
        return {"ok": False, "error": str(e)}


def _setup_logging(app, service_name, log_dir):
    """设置结构化日志(轮转文件 + 控制台)"""
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"{service_name}.log")

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 文件日志(10MB 轮转,保留 5 个)
    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    # 控制台日志
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    app.logger.handlers = []  # 清除默认 handler
    app.logger.addHandler(file_handler)
    app.logger.addHandler(console_handler)
    app.logger.setLevel(logging.INFO)
    return log_file


# === 监控仪表盘 HTML ===
MONITOR_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>监控仪表盘 · {{ service }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,sans-serif;background:#0f1929;color:#e0e6ed;line-height:1.6;padding:24px}
.container{max-width:1200px;margin:0 auto}
h1{font-size:24px;font-weight:700;margin-bottom:8px;color:#fff}
.subtitle{color:#8b96a5;font-size:14px;margin-bottom:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:24px}
.card{background:#1a2332;border:1px solid #2a3548;border-radius:12px;padding:20px}
.card .label{font-size:12px;color:#8b96a5;text-transform:uppercase;letter-spacing:.5px}
.card .value{font-size:28px;font-weight:700;margin-top:4px;color:#fff}
.card .sub{font-size:12px;color:#8b96a5;margin-top:4px}
.card.alert .value{color:#f5222d}
.card.warn .value{color:#fa8c16}
.card.ok .value{color:#52c41a}
.section{background:#1a2332;border:1px solid #2a3548;border-radius:12px;padding:24px;margin-bottom:24px}
.section h2{font-size:16px;font-weight:600;margin-bottom:16px;color:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 12px;color:#8b96a5;font-weight:500;border-bottom:1px solid #2a3548}
td{padding:8px 12px;border-bottom:1px solid #1e2838;color:#e0e6ed}
tr:hover{background:#1e2838}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge-red{background:#3d1f1f;color:#f5222d}
.badge-yellow{background:#3d2f1f;color:#fa8c16}
.badge-green{background:#1f3d1f;color:#52c41a}
.refresh-btn{background:#3370ff;color:#fff;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;font-family:inherit;margin-bottom:16px}
.refresh-btn:hover{opacity:.9}
.error-list{max-height:400px;overflow-y:auto}
.empty{color:#8b96a5;text-align:center;padding:20px}
</style>
</head>
<body>
<div class="container">
    <h1>📊 {{ service }} 监控仪表盘</h1>
    <p class="subtitle">启动于 {{ stats.service_start }} · 运行 {{ stats.uptime_human }}</p>
    <button class="refresh-btn" onclick="location.reload()">🔄 刷新</button>

    <div class="grid">
        <div class="card ok"><div class="label">总请求数</div><div class="value">{{ stats.total_requests }}</div><div class="sub">{{ stats.uptime_human }}</div></div>
        <div class="card {{ 'alert' if stats.error_count > 10 else ('warn' if stats.error_count > 0 else 'ok') }}"><div class="label">错误数</div><div class="value">{{ stats.error_count }}</div><div class="sub">错误率 {{ stats.error_rate }}%</div></div>
        <div class="card {{ 'alert' if stats.server_error_count > 0 else 'ok' }}"><div class="label">5xx 服务器错误</div><div class="value">{{ stats.server_error_count }}</div><div class="sub">需关注</div></div>
        <div class="card {{ 'alert' if stats.alerts_sent > 0 else 'ok' }}"><div class="label">飞书告警</div><div class="value">{{ stats.alerts_sent }}</div><div class="sub">已发送</div></div>
    </div>

    <div class="card" style="margin-bottom:24px">
        <div class="label" style="margin-bottom:8px">健康检查</div>
        <div style="font-size:16px">
            {% if health.db.ok %}<span class="badge badge-green">DB OK</span>{% else %}<span class="badge badge-red">DB FAIL</span>{% endif %}
            {% if health.llm.ok %}<span class="badge badge-green">LLM OK</span>{% else %}<span class="badge badge-red">LLM FAIL</span>{% endif %}
        </div>
    </div>

    <div class="section">
        <h2>📋 端点统计</h2>
        {% if stats.endpoints %}
        <table>
            <thead><tr><th>路径</th><th>请求数</th><th>错误数</th><th>平均耗时</th><th>最大耗时</th></tr></thead>
            <tbody>
            {% for path, ep in stats.endpoints.items() %}
            <tr>
                <td>{{ path }}</td>
                <td>{{ ep.count }}</td>
                <td>{% if ep.errors > 0 %}<span class="badge badge-red">{{ ep.errors }}</span>{% else %}0{% endif %}</td>
                <td>{{ ep.avg_duration_ms }} ms</td>
                <td>{{ ep.max_duration_ms }} ms</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}<div class="empty">暂无请求</div>{% endif %}
    </div>

    <div class="section">
        <h2>⚠️ 最近错误(最多 50 条)</h2>
        {% if stats.recent_errors %}
        <table class="error-list">
            <thead><tr><th>时间</th><th>方法</th><th>路径</th><th>状态码</th><th>IP</th><th>耗时</th></tr></thead>
            <tbody>
            {% for err in stats.recent_errors[:20] %}
            <tr>
                <td>{{ err.time }}</td>
                <td>{{ err.method }}</td>
                <td>{{ err.path }}</td>
                <td><span class="badge badge-red">{{ err.status }}</span></td>
                <td>{{ err.ip }}</td>
                <td>{{ err.duration_ms }} ms</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}<div class="empty">🎉 无错误</div>{% endif %}
    </div>

    <div class="section">
        <h2>🔔 最近告警</h2>
        {% if stats.recent_alerts %}
        <table>
            <thead><tr><th>时间</th><th>标题</th><th>状态</th></tr></thead>
            <tbody>
            {% for al in stats.recent_alerts %}
            <tr>
                <td>{{ al.time }}</td>
                <td>{{ al.title }}</td>
                <td>{% if al.ok %}<span class="badge badge-green">已发送</span>{% else %}<span class="badge badge-red">失败</span>{% endif %}</td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}<div class="empty">无告警记录</div>{% endif %}
        <div style="margin-top:12px">
            <form method="POST" action="/api/alert/test" style="display:inline">
                <button type="submit" class="refresh-btn" style="background:#fa8c16">🧪 发送测试告警</button>
            </form>
        </div>
    </div>
</div>
</body>
</html>"""


def init_monitor(app, service_name, db_path=None, llm_test_fn=None, log_dir=None,
                 alert_feishu_fn=None, alert_chat_id=None):
    """初始化监控中间件

    Args:
        app: Flask app
        service_name: 服务名(如 "finance-agent")
        db_path: SQLite 路径(可选,用于健康检查)
        llm_test_fn: LLM 测试函数(可选,如 cherry_client.test_connection)
        log_dir: 日志目录(默认 logs/)
        alert_feishu_fn: 飞书发送函数(可选,如 feishu_send_post)
        alert_chat_id: 飞书群聊 ID(可选,告警目标)
    """
    global _ALERT_FEISHU_FN, _ALERT_CHAT_ID, _ALERT_SERVICE_NAME
    _ALERT_FEISHU_FN = alert_feishu_fn
    _ALERT_CHAT_ID = alert_chat_id
    _ALERT_SERVICE_NAME = service_name

    if log_dir is None:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(db_path or __file__)), "logs")
    log_file = _setup_logging(app, service_name, log_dir)
    app.logger.info(f"监控初始化: {service_name}, 日志: {log_file}")
    if alert_feishu_fn and alert_chat_id:
        app.logger.info(f"飞书告警已启用,目标群: {alert_chat_id}")
    else:
        app.logger.info("飞书告警未配置(需传 alert_feishu_fn + alert_chat_id)")

    # === 请求追踪 ===
    @app.before_request
    def _before():
        request._start_time = time.time()

    @app.after_request
    def _after(response):
        try:
            duration = time.time() - getattr(request, "_start_time", time.time())
            path = request.path
            # 静态资源不追踪
            if not path.startswith("/static") and path != "/favicon.ico":
                _track_request(path, request.method, response.status_code, duration, request.remote_addr or "?")
                # 记录慢请求和错误
                if response.status_code >= 500:
                    app.logger.error(f"{request.method} {path} -> {response.status_code} ({duration*1000:.0f}ms) IP={request.remote_addr}")
                elif duration > 10:
                    app.logger.warning(f"SLOW {request.method} {path} -> {response.status_code} ({duration*1000:.0f}ms)")
        except Exception:
            pass  # 监控本身不能影响请求
        return response

    # === /api/stats ===
    @app.route("/api/stats")
    def _stats():
        return jsonify({"ok": True, "service": service_name, **_get_stats()})

    # === /api/health/full ===
    @app.route("/api/health/full")
    def _health_full():
        health = {"service": service_name, "timestamp": datetime.now().isoformat()}
        if db_path:
            health["db"] = _check_db(db_path, alert_on_fail=True)
        if llm_test_fn:
            health["llm"] = _check_llm(llm_test_fn, alert_on_fail=True)
        health["stats"] = _get_stats()
        return jsonify(health)

    # === /api/alert/test — 发测试告警 ===
    @app.route("/api/alert/test", methods=["POST", "GET"])
    def _alert_test():
        """发送测试告警(验证飞书通道)"""
        # 未配置飞书群时,直接返回明确提示(不算错误)
        if not _ALERT_CHAT_ID:
            return jsonify({"ok": False, "skipped": True, "message": "飞书推送未配置(ALERT_CHAT_ID 为空),已跳过。配置后可推送到飞书群。"})
        title = f"🧪 {_ALERT_SERVICE_NAME} 测试告警"
        paragraphs = [[
            {"tag": "text", "text": f"这是一条测试告警\n"},
            {"tag": "text", "text": f"服务: {_ALERT_SERVICE_NAME}\n"},
            {"tag": "text", "text": f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"},
            {"tag": "text", "text": f"如果你看到这条消息,说明飞书告警通道正常 ✅\n"},
        ]]
        ok = _send_feishu_alert(title, paragraphs)
        if ok:
            return jsonify({"ok": True, "message": "测试告警已发送到飞书群"})
        return jsonify({"ok": False, "error": "告警发送失败(飞书 API 返回错误,可能是 bot 未加入目标群)"})

    # === /monitor 仪表盘 ===
    @app.route("/monitor")
    def _monitor():
        from flask import render_template_string as _render
        health = {"db": _check_db(db_path) if db_path else {"ok": True}, "llm": _check_llm(llm_test_fn) if llm_test_fn else {"ok": True}}
        return _render(MONITOR_HTML, service=service_name, stats=_get_stats(), health=health)

    app.logger.info(f"监控就绪: /api/stats /api/health/full /api/alert/test /monitor")
    return app
