#!/usr/bin/env python3
"""财务 Agent — 管报底座 + 绩效试算 Demo (D4)"""
from flask import Flask, request, jsonify, render_template_string, redirect
import os
import json
import sqlite3
import io
from dotenv import load_dotenv
from cherry_client import chat, chat_json, embed, test_connection
from feishu_client import list_chats as feishu_list_chats, send_post as feishu_send_post, send_text as feishu_send_text, download_message_file as feishu_download_file
from monitor import init_monitor

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "finance-agent-secret-dev")
from flask import session as flask_session
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB

# === 配置 ===
CHERRYIN_API_KEY = os.environ.get("CHERRYIN_API_KEY", "")
CHERRYIN_BASE_URL = os.environ.get("CHERRYIN_BASE_URL", "https://express-ent-admin.cherryin.ai/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "agent/deepseek-v4-pro")

DB_PATH = os.path.join(os.path.dirname(__file__), "finance.db")
ALERT_CHAT_ID = os.environ.get("FEISHU_ALERT_CHAT_ID", "")  # 留空=跳过飞书推送

# === 数据库初始化 ===
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT,
        amount REAL,
        summary TEXT,
        level1 TEXT,
        level2 TEXT,
        confidence REAL,
        reason TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # === 权限分层:users 表 ===
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'employee',
        pin TEXT,
        feishu_open_id TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # 预置账号(首次建表)
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO users (name, role, pin) VALUES (?, ?, ?)", ("财务管理员", "financial", "1234"))
        c.execute("INSERT INTO users (name, role, pin) VALUES (?, ?, ?)", ("张三", "employee", "1111"))
        c.execute("INSERT INTO users (name, role, pin) VALUES (?, ?, ?)", ("李四", "employee", "2222"))

    # === 发票流改造:给 transactions 表加字段(兼容旧库) ===
    try:
        c.execute("ALTER TABLE transactions ADD COLUMN vendor TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE transactions ADD COLUMN invoice_no TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE transactions ADD COLUMN invoice_text TEXT")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE transactions ADD COLUMN user_id INTEGER")
    except Exception:
        pass

    # D5: 绩效规则表
    c.execute("""CREATE TABLE IF NOT EXISTS performance_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        department TEXT NOT NULL,
        position TEXT NOT NULL,
        coefficient REAL DEFAULT 1.0,
        target_amount REAL DEFAULT 0,
        bonus_base REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # 插入默认规则(首次建表时)
    c.execute("SELECT COUNT(*) FROM performance_rules")
    if c.fetchone()[0] == 0:
        default_rules = [
            ("研发部", "工程师", 1.2, 500000, 8000),
            ("研发部", "主管", 1.5, 800000, 15000),
            ("销售部", "销售", 1.0, 300000, 6000),
            ("销售部", "经理", 1.8, 1000000, 20000),
            ("管理部", "行政", 0.8, 200000, 5000),
            ("管理部", "总监", 2.0, 500000, 25000),
            ("交付部", "交付工程师", 1.1, 400000, 7000),
            ("交付部", "主管", 1.4, 700000, 12000),
        ]
        c.executemany("INSERT INTO performance_rules (department, position, coefficient, target_amount, bonus_base) VALUES (?, ?, ?, ?, ?)", default_rules)
    conn.commit()
    conn.close()

init_db()

# === 监控初始化 ===
init_monitor(app, service_name="finance-agent", db_path=DB_PATH, llm_test_fn=test_connection,
             alert_feishu_fn=feishu_send_post, alert_chat_id=ALERT_CHAT_ID)

def _current_user():
    """从 session 取当前用户,未登录返回 None"""
    uid = flask_session.get("user_id")
    if not uid:
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT id, name, role FROM users WHERE id=?", (uid,))
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "role": row[2]}

def _require_user():
    """要求登录,返回 user 或 (None, error_response)"""
    u = _current_user()
    if not u:
        return None, jsonify({"ok": False, "error": "未登录"}), 401
    return u, None


def _get_or_create_user_by_open_id(open_id, name=None):
    """通过飞书 open_id 查或建用户。返回 user dict 或 None。"""
    if not open_id:
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT id, name, role FROM users WHERE feishu_open_id=?", (open_id,))
        row = c.fetchone()
        if row:
            return {"id": row[0], "name": row[1], "role": row[2]}
        # 自动建档为 employee
        c.execute("INSERT INTO users (name, role, feishu_open_id) VALUES (?, ?, ?)",
                  (name or f"飞书用户_{open_id[-6:]}", "employee", open_id))
        conn.commit()
        uid = c.lastrowid
        return {"id": uid, "name": name or f"飞书用户_{open_id[-6:]}", "role": "employee"}
    finally:
        conn.close()


# === 口径规则 ===

# === OCR 配置(发票扫描件识别) ===
_OCR_MAX_PAGES = 50
_OCR_DPI = 200
_OCR_MIN_CHARS = 20

ACCOUNTING_RULES = """
一、研发费:
  - 研发人员薪酬(直接归属研发项目)
  - 软件开发外包费
  - 开发工具/软件许可
  - 测试费、云服务费(研发用)
  - 研发相关差旅

二、销售费:
  - 销售人员薪酬+提成
  - 拜访客户差旅
  - 客户招待费
  - 市场推广/广告
  - 销售佣金

三、管理费:
  - 行政人员薪酬
  - 办公租金
  - 办公用品
  - 非销售差旅(内部会议、培训)
  - 团队建设
  - 法务/财务/HR 职能费用

四、营业成本:
  - 产品采购成本
  - 外包服务交付成本
  - 交付人员薪酬
"""

# === 归一化 Prompt ===
NORMALIZE_PROMPT = """你是财务归类助手。根据以下流水信息,归一化到标准科目。

## 口径规则
{rules}

## 流水信息
- 金额: {amount} 元
- 摘要: {summary}
- 来源: {source}

## 输出要求
返回 JSON(不要其他文字):
{{
  "level1": "研发费|销售费|管理费|营业成本",
  "level2": "二级科目(如:差旅费/薪酬/软件/招待费等)",
  "confidence": 0.0到1.0,
  "reason": "30字以内判断依据"
}}
"""

# === 发票解析+归一化 Prompt(一步到位) ===
INVOICE_PARSE_PROMPT = """你是财务发票解析助手。从 OCR 提取的发票文本中,提取结构化字段并归一化科目。

## 口径规则
{rules}

## 发票 OCR 文本
{invoice_text}

## 输出要求
返回 JSON(不要其他文字):
{{
  "invoice_no": "发票号码(没找到则空字符串)",
  "invoice_date": "开票日期 YYYY-MM-DD(没找到则空)",
  "vendor": "销售方名称(没找到则空)",
  "amount": 0.0,
  "items": ["商品/服务明细1", "商品/服务明细2"],
  "level1": "研发费|销售费|管理费|营业成本",
  "level2": "二级科目(如:差旅费/薪酬/软件/招待费/办公用品等)",
  "confidence": 0.0到1.0,
  "reason": "30字以内判断依据"
}}

注意:
- amount 是价税合计金额(发票总金额),纯数字不要带¥或元
- 如果有多行商品,items 列出每行
- 如果 OCR 文本残缺无法判断,confidence 给低分(0.3 以下),level1 给最可能猜测
- 如果完全不是发票(如普通文本),level1 给"未归类",confidence 给 0
"""


# === D4: AI 简评 Prompt ===
REPORT_COMMENTARY_PROMPT = """你是财务分析师。根据以下管报汇总数据,写出 3-5 条简短点评。

要求:
- 指出占比最高的科目
- 发现异常波动或值得关注的点
- 给出 1-2 条优化建议
- 每条不超过 50 字
- 用中文,口语化,像同事汇报

管报数据:
{report_data}
"""

# === D4: 管报页面 ===
REPORT_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>管报预览 · 财务 Agent</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,sans-serif;background:#f0f2f5;color:#1a1a2e}
.nav{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);padding:20px 40px;display:flex;justify-content:space-between;align-items:center}
.nav h1{color:#fff;font-size:20px}
.nav a{color:#fff;text-decoration:none;margin-left:20px;opacity:.9}
.nav a:hover{opacity:1}
.container{max-width:1100px;margin:0 auto;padding:30px 20px}
.card{background:#fff;border-radius:16px;padding:24px;margin-bottom:20px;box-shadow:0 2px 12px rgba(0,0,0,.06)}
.card h2{font-size:18px;margin-bottom:16px;color:#333}
.stat-row{display:flex;gap:16px;margin-bottom:8px}
.stat-box{flex:1;background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);border-radius:12px;padding:20px;color:#fff}
.stat-box .label{font-size:12px;opacity:.8}
.stat-box .value{font-size:26px;font-weight:700;margin-top:4px}
table{width:100%;border-collapse:collapse}
th{text-align:left;padding:10px;background:#f7f8fc;color:#555;font-size:13px;border-bottom:2px solid #e8e8f0}
td{padding:10px;border-bottom:1px solid #f0f0f5;font-size:14px}
.l1-row{background:#f7f8fc;font-weight:600}
.bar-container{width:80px;height:8px;background:#e8e8f0;border-radius:4px;overflow:hidden;display:inline-block;vertical-align:middle}
.bar-fill{height:100%;background:linear-gradient(90deg,#667eea,#764ba2);border-radius:4px}
.commentary{background:#fff8e6;border-left:4px solid #f0a020;padding:16px 20px;border-radius:8px;margin-top:8px}
.commentary p{margin:6px 0;line-height:1.6}
.commentary .tag{display:inline-block;background:#f0a020;color:#fff;font-size:11px;padding:2px 8px;border-radius:10px;margin-right:6px}
.feishu-btn{background:linear-gradient(135deg,#3370ff,#5286ff);color:#fff;border:none;padding:10px 24px;border-radius:8px;font-size:14px;cursor:pointer;font-family:inherit}
.feishu-btn:hover{opacity:.9;transform:translateY(-1px)}
.feishu-btn:disabled{opacity:.5;cursor:not-allowed}
.chat-select{padding:8px 12px;border:1px solid #ddd;border-radius:8px;font-size:14px;margin-right:8px;min-width:200px}
.result-msg{margin-top:10px;padding:10px;border-radius:8px;font-size:13px;display:none}
.result-msg.success{background:#e6f7e6;color:#2d8c2d;display:block}
.result-msg.error{background:#fce8e8;color:#c92a2a;display:block}
.loading{text-align:center;padding:40px;color:#888}
.loading .spin{display:inline-block;width:32px;height:32px;border:3px solid #e8e8f0;border-top:3px solid #667eea;border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<div class="nav">
    <h1>💰 财务 Agent</h1>
    <div>
        <a href="/">首页</a>
        <a href="/upload">上传</a>
        <a href="/report">管报</a>
        <a href="/performance">绩效</a>
    </div>
</div>
<div class="container">
    <div class="card">
        <h2>📈 总览</h2>
        <div class="stat-row">
            <div class="stat-box"><div class="label">总笔数</div><div class="value" id="total-count">-</div></div>
            <div class="stat-box"><div class="label">总金额</div><div class="value" id="total-amount">-</div></div>
            <div class="stat-box"><div class="label">科目数</div><div class="value" id="cat-count">-</div></div>
        </div>
    </div>
    <div class="card">
        <h2>📋 科目汇总</h2>
        <table>
            <thead><tr><th>一级科目</th><th>二级科目</th><th>笔数</th><th>金额</th><th>占比</th></tr></thead>
            <tbody id="report-tbody"></tbody>
        </table>
    </div>
    <div class="card">
        <h2>🤖 AI 简评</h2>
        <div id="commentary-area"><div class="loading"><div class="spin"></div><p style="margin-top:10px">AI 正在分析管报...</p></div></div>
    </div>
    <div class="card">
        <h2>📤 发送到飞书</h2>
        <p style="color:#666;font-size:13px;margin-bottom:12px">选择群聊后,把管报 + AI 简评发到飞书群</p>
        <select class="chat-select" id="chat-select"><option value="">加载群聊中...</option></select>
        <button class="feishu-btn" id="send-feishu" disabled>发送到飞书</button>
        <div class="result-msg" id="feishu-result"></div>
    </div>
</div>
<script>
function escapeHtml(s){if(s==null)return '';return String(s).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
async function loadReport(){
    const r=await fetch('/api/report/preview');
    const d=await r.json();
    if(!d.ok)return;
    document.getElementById('total-count').textContent=d.total_count+' 笔';
    document.getElementById('total-amount').textContent='¥'+d.total_amount.toLocaleString();
    document.getElementById('cat-count').textContent=Object.keys(d.summary).length+' 个';
    const tbody=document.getElementById('report-tbody');
    const gt=d.total_amount;
    let html='';
    for(const[l1,info]of Object.entries(d.summary)){
        html+='<tr class="l1-row"><td>'+escapeHtml(l1)+'</td><td>—</td><td>'+info.count+'</td><td>¥'+info.total.toLocaleString()+'</td><td>'+((info.total/gt)*100).toFixed(1)+'%</td></tr>';
        for(const item of info.items){
            html+='<tr><td>└</td><td>'+escapeHtml(item.level2)+'</td><td>'+item.count+'</td><td>¥'+item.total.toLocaleString()+'</td><td><div class="bar-container"><div class="bar-fill" style="width:'+((item.total/info.total)*100)+'%"></div></div></td></tr>';
        }
    }
    tbody.innerHTML=html;
}
async function loadCommentary(){
    const r=await fetch('/api/report/commentary',{method:'POST'});
    const d=await r.json();
    const area=document.getElementById('commentary-area');
    if(d.ok&&d.commentary){
        area.innerHTML='<div class="commentary">'+d.commentary.split('\\n').map(p=>p.trim()?'<p><span class="tag">💡</span>'+escapeHtml(p)+'</p>':'').join('')+'</div>';
    }else{
        area.innerHTML='<p style="color:#999">'+escapeHtml(d.error||'简评生成失败')+'</p>';
    }
}
async function loadChats(){
    try{
        const r=await fetch('/api/feishu/chats');
        const d=await r.json();
        const sel=document.getElementById('chat-select');
        if(d.ok&&d.chats&&d.chats.length>0){
            sel.innerHTML=d.chats.map(c=>'<option value="'+escapeHtml(c.chat_id)+'">'+escapeHtml(c.name)+'</option>').join('');
            document.getElementById('send-feishu').disabled=false;
        }else{
            sel.innerHTML='<option value="">无可用群聊 ('+escapeHtml(d.error||'未知错误')+')</option>';
        }
    }catch(e){
        const sel=document.getElementById('chat-select');
        sel.innerHTML='<option value="">加载失败 ('+escapeHtml(e.message)+')</option>';
    }
}
document.getElementById('send-feishu').addEventListener('click',async()=>{
    const chatId=document.getElementById('chat-select').value;
    if(!chatId)return;
    const btn=document.getElementById('send-feishu');
    const msg=document.getElementById('feishu-result');
    btn.disabled=true;btn.textContent='发送中...';
    msg.className='result-msg';
    try{
        const r=await fetch('/api/report/feishu',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:chatId})});
        const d=await r.json();
        if(d.ok){msg.className='result-msg success';msg.textContent='✅ 已发送到飞书群';}
        else{msg.className='result-msg error';msg.textContent='❌ '+(d.error||'发送失败');}
    }catch(e){msg.className='result-msg error';msg.textContent='❌ '+e.message;}
    btn.disabled=false;btn.textContent='发送到飞书';
});
loadReport();loadCommentary();loadChats();
</script>
</body>
</html>
"""

PERFORMANCE_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>绩效试算 · 财务 Agent</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',system-ui,sans-serif;background:#f5f6fa;color:#333;line-height:1.6}
.nav{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:16px 32px;display:flex;justify-content:space-between;align-items:center;box-shadow:0 2px 8px rgba(0,0,0,.1)}
.nav h1{font-size:20px;font-weight:700}
.nav a{color:#fff;text-decoration:none;margin-left:16px;font-size:14px;opacity:.85;transition:opacity .2s}
.nav a:hover{opacity:1}
.nav a.active{font-weight:600;opacity:1;border-bottom:2px solid #fff}
.container{max-width:1000px;margin:0 auto;padding:24px 16px}
.card{background:#fff;border-radius:12px;padding:24px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card h2{font-size:18px;font-weight:700;margin-bottom:16px;color:#1a1a2e}
.stat-row{display:flex;gap:16px;flex-wrap:wrap}
.stat-box{flex:1;min-width:140px;background:#f8f9ff;border-radius:8px;padding:16px;text-align:center}
.stat-box .label{font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.5px}
.stat-box .value{font-size:24px;font-weight:700;margin-top:4px;color:#667eea}
.stat-box .value.green{color:#2d8c2d}
.stat-box .value.orange{color:#fa8c16}
.filter-bar{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.filter-bar select{padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;font-family:inherit}
.btn{padding:8px 20px;border:none;border-radius:6px;font-size:14px;font-weight:500;cursor:pointer;font-family:inherit;transition:opacity .2s}
.btn:hover{opacity:.85}
.btn-primary{background:#667eea;color:#fff}
.btn-feishu{background:#3370ff;color:#fff}
.btn:disabled{opacity:.5;cursor:not-allowed}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 12px;color:#666;font-weight:500;border-bottom:2px solid #eee;background:#fafafa}
td{padding:10px 12px;border-bottom:1px solid #f0f0f0}
tr:hover{background:#f8f9ff}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.badge-green{background:#e6f7e6;color:#2d8c2d}
.badge-orange{background:#fff7e6;color:#fa8c16}
.badge-red{background:#fce8e8;color:#c92a2a}
.loading{text-align:center;padding:40px;color:#888}
.loading .spin{display:inline-block;width:32px;height:32px;border:3px solid #e8e8f0;border-top:3px solid #667eea;border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.result-msg{margin-top:10px;padding:10px;border-radius:8px;font-size:13px;display:none}
.result-msg.success{background:#e6f7e6;color:#2d8c2d;display:block}
.result-msg.error{background:#fce8e8;color:#c92a2a;display:block}
.feishu-section{margin-top:16px;padding-top:16px;border-top:1px solid #eee}
.feishu-section select{padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;margin-right:8px;min-width:200px}
</style>
</head>
<body>
<div class="nav">
    <h1>💰 财务 Agent</h1>
    <div>
        <a href="/">首页</a>
        <a href="/upload">上传</a>
        <a href="/report">管报</a>
        <a href="/performance">绩效</a>
        <a href="/performance" class="active">绩效</a>
    </div>
</div>
<div class="container">
    <div class="card">
        <h2>🎯 绩效试算</h2>
        <p style="color:#666;font-size:13px;margin-bottom:16px">基于管报流水和绩效规则表,自动计算各部门/岗位的达成率与绩效奖金</p>
        <div class="filter-bar">
            <label>部门筛选:</label>
            <select id="dept-filter" onchange="loadPerformance()">
                <option value="">全部部门</option>
            </select>
            <button class="btn btn-primary" onclick="loadPerformance()">🔄 刷新</button>
        </div>
        <div class="stat-row">
            <div class="stat-box"><div class="label">总流水</div><div class="value" id="total-amount">-</div></div>
            <div class="stat-box"><div class="label">绩效奖金合计</div><div class="value green" id="total-bonus">-</div></div>
            <div class="stat-box"><div class="label">规则数</div><div class="value orange" id="rules-count">-</div></div>
        </div>
    </div>
    <div class="card">
        <h2>📋 绩效明细</h2>
        <div id="perf-area"><div class="loading"><div class="spin"></div><p style="margin-top:10px">计算中...</p></div></div>
    </div>
    <div class="card">
        <h2>📤 发送到飞书</h2>
        <p style="color:#666;font-size:13px;margin-bottom:12px">把绩效报告推送到飞书群</p>
        <select id="chat-select" style="padding:8px 12px;border:1px solid #ddd;border-radius:6px;font-size:14px;margin-right:8px;min-width:200px"><option value="">加载群聊中...</option></select>
        <button class="btn btn-feishu" id="send-feishu" disabled>发送绩效报告</button>
        <div class="result-msg" id="feishu-result"></div>
    </div>
</div>
<script>
function escapeHtml(s){if(s==null)return '';return String(s).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
async function loadPerformance(){
    const dept=document.getElementById('dept-filter').value;
    const url='/api/performance/calculate'+(dept?('?department='+encodeURIComponent(dept)):'');
    const r=await fetch(url);
    const d=await r.json();
    if(!d.ok){document.getElementById('perf-area').innerHTML='<p style="color:#c92a2a">'+escapeHtml(d.error||'计算失败')+'</p>';return;}
    document.getElementById('total-amount').textContent='¥'+(d.total_amount||0).toLocaleString();
    document.getElementById('total-bonus').textContent='¥'+(d.total_bonus||0).toLocaleString();
    document.getElementById('rules-count').textContent=(d.rules_count||0)+' 条';
    const tbody=document.getElementById('perf-area');
    if(!d.departments||d.departments.length===0){tbody.innerHTML='<p style="color:#888;text-align:center;padding:20px">暂无数据</p>';return;}
    let html='<table><thead><tr><th>部门</th><th>岗位</th><th>目标</th><th>实际</th><th>达成率</th><th>系数</th><th>奖金基数</th><th>应发奖金</th><th>状态</th></tr></thead><tbody>';
    for(const item of d.departments){
        const badgeClass=item.status==='超额'?'badge-green':(item.status==='未达标'?'badge-red':'badge-green');
        html+='<tr><td>'+escapeHtml(item.department)+'</td><td>'+escapeHtml(item.position)+'</td><td>¥'+item.target_amount.toLocaleString()+'</td><td>¥'+item.actual_amount.toLocaleString()+'</td><td>'+item.achievement_rate+'%</td><td>'+item.coefficient+'</td><td>¥'+item.bonus_base.toLocaleString()+'</td><td><strong>¥'+item.bonus.toLocaleString()+'</strong></td><td><span class="badge '+badgeClass+'">'+escapeHtml(item.status)+'</span></td></tr>';
    }
    html+='</tbody></table>';
    tbody.innerHTML=html;
}
async function loadChats(){
    try{
        const r=await fetch('/api/feishu/chats');
        const d=await r.json();
        const sel=document.getElementById('chat-select');
        if(d.ok&&d.chats&&d.chats.length>0){
            sel.innerHTML=d.chats.map(c=>'<option value="'+escapeHtml(c.chat_id)+'">'+escapeHtml(c.name)+'</option>').join('');
            document.getElementById('send-feishu').disabled=false;
        }else{
            sel.innerHTML='<option value="">无可用群聊</option>';
        }
    }catch(e){document.getElementById('chat-select').innerHTML='<option value="">加载失败</option>';}
}
async function loadDepts(){
    try{
        const r=await fetch('/api/performance/calculate');
        const d=await r.json();
        if(d.ok&&d.departments){
            const depts=[...new Set(d.departments.map(x=>x.department))];
            const sel=document.getElementById('dept-filter');
            const cur=sel.value;
            sel.innerHTML='<option value="">全部部门</option>'+depts.map(x=>'<option value="'+escapeHtml(x)+'">'+escapeHtml(x)+'</option>').join('');
            sel.value=cur;
        }
    }catch(e){}
}
document.getElementById('send-feishu').addEventListener('click',async()=>{
    const chatId=document.getElementById('chat-select').value;
    if(!chatId)return;
    const btn=document.getElementById('send-feishu');
    const result=document.getElementById('feishu-result');
    btn.disabled=true;btn.textContent='发送中...';
    result.className='result-msg';result.style.display='none';
    try{
        const r=await fetch('/api/performance/feishu',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({chat_id:chatId,department:document.getElementById('dept-filter').value})});
        const d=await r.json();
        if(d.ok){result.className='result-msg success';result.textContent='✅ 发送成功!奖金合计 ¥'+(d.total_bonus||0).toLocaleString();}
        else{result.className='result-msg error';result.textContent='❌ '+escapeHtml(d.error||'发送失败');}
    }catch(e){result.className='result-msg error';result.textContent='❌ 网络错误';}
    btn.disabled=false;btn.textContent='发送绩效报告';
});
loadPerformance().then(loadDepts);loadChats();
</script>
</body>
</html>
"""

# === 页面 ===
LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · 财务 Agent</title>
<style>
:root{--c-primary:#6366f1;--c-primary-3:#a855f7;--c-bg:#0f0f1a;--c-surface:rgba(255,255,255,.04);--c-text:#e4e4e7;--c-text-dim:#a1a1aa;--c-text-muted:#71717a;--c-border:rgba(255,255,255,.08);--c-border-hover:rgba(139,92,246,.4)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Kaiti SC","STKaiti","楷体",sans-serif;background:var(--c-bg);color:var(--c-text);min-height:100vh;display:flex;align-items:center;justify-content:center}
body::before{content:'';position:fixed;inset:0;z-index:-1;background:radial-gradient(ellipse 80% 50% at 20% 0%,rgba(99,102,241,.15),transparent),radial-gradient(ellipse 60% 50% at 80% 30%,rgba(168,85,247,.12),transparent),var(--c-bg)}
.card{background:var(--c-surface);border:1px solid var(--c-border);border-radius:20px;padding:40px;width:360px;max-width:90vw}
.logo{width:56px;height:56px;border-radius:16px;background:linear-gradient(135deg,var(--c-primary),var(--c-primary-3));display:flex;align-items:center;justify-content:center;font-size:28px;margin:0 auto 20px;box-shadow:0 8px 24px rgba(99,102,241,.4)}
h1{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Songti SC","STSong","宋体",sans-serif;font-size:22px;font-weight:700;text-align:center;margin-bottom:6px}
.subtitle{text-align:center;color:var(--c-text-dim);font-size:13px;margin-bottom:28px}
.field{margin-bottom:16px}
label{display:block;font-size:12px;font-weight:600;color:var(--c-text-muted);margin-bottom:6px;letter-spacing:.3px}
input{width:100%;padding:11px 14px;background:rgba(255,255,255,.05);border:1px solid var(--c-border);border-radius:10px;color:var(--c-text);font-size:14px;font-family:inherit;outline:none;transition:border .2s}
input:focus{border-color:var(--c-border-hover)}
.btn{width:100%;padding:12px;border:none;border-radius:10px;background:linear-gradient(135deg,var(--c-primary),var(--c-primary-3));color:#fff;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .2s;margin-top:8px}
.btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(99,102,241,.4)}
.hint{margin-top:20px;padding:12px;background:rgba(255,255,255,.03);border-radius:8px;font-size:12px;color:var(--c-text-muted);line-height:1.7}
.hint b{color:var(--c-text-dim)}
.error{color:#fca5a5;font-size:13px;text-align:center;margin-top:12px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">💰</div>
  <h1>财务 Agent 登录</h1>
  <p class="subtitle">输入姓名和 PIN 码</p>
  <form method="POST" action="/login">
    <div class="field"><label>姓名</label><input name="name" required autofocus placeholder="财务管理员 / 张三 / 李四"></div>
    <div class="field"><label>PIN 码</label><input name="pin" type="password" required placeholder="4位数字"></div>
    <button class="btn" type="submit">登 录</button>
    {% if error %}<p class="error">{{ error }}</p>{% endif %}
  </form>
  <div class="hint">
    <b>测试账号:</b><br>
    财务管理员 / 1234(看全部)<br>
    张三 / 1111(只看自己)<br>
    李四 / 2222(只看自己)
  </div>
</div>
</body>
</html>"""

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>财务 Agent · 企业运营智能化</title>
<style>
:root{
  --c-primary:#6366f1;--c-primary-2:#8b5cf6;--c-primary-3:#a855f7;
  --c-bg:#0f0f1a;--c-bg-2:#1a1a2e;--c-surface:rgba(255,255,255,.04);--c-surface-2:rgba(255,255,255,.08);
  --c-text:#e4e4e7;--c-text-dim:#a1a1aa;--c-text-muted:#71717a;
  --c-border:rgba(255,255,255,.08);--c-border-hover:rgba(139,92,246,.4);
  --c-green:#10b981;--c-amber:#f59e0b;--c-red:#ef4444;--c-blue:#3b82f6;
  --radius:16px;--radius-sm:10px;
  --shadow:0 8px 32px rgba(0,0,0,.3);--shadow-glow:0 0 40px rgba(139,92,246,.15);
}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Segoe UI","Kaiti SC","STKaiti","KaiTi","楷体",sans-serif;
  background:var(--c-bg);color:var(--c-text);line-height:1.6;overflow-x:hidden;
  min-height:100vh;
}
/* 装饰性背景 */
body::before{content:'';position:fixed;inset:0;z-index:-2;background:
  radial-gradient(ellipse 80% 50% at 20% 0%,rgba(99,102,241,.15),transparent),
  radial-gradient(ellipse 60% 50% at 80% 30%,rgba(168,85,247,.12),transparent),
  radial-gradient(ellipse 50% 50% at 50% 100%,rgba(139,92,246,.1),transparent),
  var(--c-bg)}
body::after{content:'';position:fixed;inset:0;z-index:-1;opacity:.4;
  background-image:linear-gradient(rgba(255,255,255,.015) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.015) 1px,transparent 1px);
  background-size:60px 60px;mask-image:radial-gradient(ellipse 80% 60% at 50% 30%,#000,transparent)}

/* 顶部导航 */
.nav{position:sticky;top:0;z-index:100;backdrop-filter:blur(20px);background:rgba(15,15,26,.7);border-bottom:1px solid var(--c-border)}
.nav-inner{max-width:1100px;margin:0 auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between}
.nav-brand{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Songti SC","STSong","SimSun","宋体",sans-serif;display:flex;align-items:center;gap:10px;font-size:17px;font-weight:700;color:var(--c-text);text-decoration:none}
.nav-brand-icon{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,var(--c-primary),var(--c-primary-3));display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 4px 12px rgba(99,102,241,.4)}
.nav-brand-text{background:linear-gradient(135deg,#fff,#a78bfa);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.nav-links{display:flex;gap:4px;align-items:center}
.nav-links a{padding:8px 14px;border-radius:8px;font-size:13.5px;font-weight:500;color:var(--c-text-dim);text-decoration:none;transition:all .2s}
.nav-links a:hover{color:var(--c-text);background:var(--c-surface)}
.nav-status{display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.2);font-size:12px;color:var(--c-green);font-weight:600}
.nav-status .dot{width:6px;height:6px;border-radius:50%;background:var(--c-green);box-shadow:0 0 8px var(--c-green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

/* 主容器 */
.wrap{max-width:1100px;margin:0 auto;padding:40px 24px 80px}

/* Hero */
.hero{position:relative;padding:60px 0 40px;text-align:center}
.hero-tag{display:inline-flex;align-items:center;gap:8px;padding:6px 14px;border-radius:20px;background:var(--c-surface-2);border:1px solid var(--c-border);font-size:12.5px;font-weight:600;color:var(--c-primary-2);margin-bottom:24px;letter-spacing:.5px}
.hero-tag::before{content:'';width:6px;height:6px;border-radius:50%;background:var(--c-primary-2);box-shadow:0 0 8px var(--c-primary-2)}
.hero h1{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Songti SC","STSong","SimSun","宋体",sans-serif;font-size:clamp(38px,6vw,64px);font-weight:800;line-height:1.1;letter-spacing:-.02em;margin-bottom:20px}
.hero h1 .grad{background:linear-gradient(135deg,#818cf8 0%,#c084fc 50%,#e879f9 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.hero p{font-size:17px;color:var(--c-text-dim);max-width:600px;margin:0 auto 36px;line-height:1.7}
.hero-cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:8px;padding:13px 28px;border-radius:12px;font-size:14.5px;font-weight:600;text-decoration:none;transition:all .25s;cursor:pointer;border:none;font-family:inherit}
.btn-primary{background:linear-gradient(135deg,var(--c-primary),var(--c-primary-3));color:#fff;box-shadow:0 4px 20px rgba(99,102,241,.4)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(99,102,241,.5)}
.btn-ghost{background:var(--c-surface);color:var(--c-text);border:1px solid var(--c-border)}
.btn-ghost:hover{background:var(--c-surface-2);border-color:var(--c-border-hover)}

/* 统计卡片 */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:48px 0}
.stat{position:relative;padding:24px;border-radius:var(--radius);background:var(--c-surface);border:1px solid var(--c-border);overflow:hidden;transition:all .3s}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:1px;background:linear-gradient(90deg,transparent,rgba(139,92,246,.5),transparent)}
.stat:hover{border-color:var(--c-border-hover);transform:translateY(-3px);box-shadow:var(--shadow-glow)}
.stat-label{font-size:12px;color:var(--c-text-muted);font-weight:600;letter-spacing:.5px;text-transform:uppercase;margin-bottom:8px}
.stat-value{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;font-size:30px;font-weight:800;letter-spacing:-.02em}
.stat-value .unit{font-size:14px;font-weight:500;color:var(--c-text-dim);margin-left:4px}
.stat-trend{font-size:12px;color:var(--c-green);margin-top:6px;font-weight:600}

/* 分区标题 */
.section{margin:56px 0 24px}
.section-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px}
.section-title{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Songti SC","STSong","SimSun","宋体",sans-serif;font-size:22px;font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
.section-title::before{content:'';width:4px;height:24px;border-radius:2px;background:linear-gradient(135deg,var(--c-primary),var(--c-primary-3))}
.section-sub{font-size:13.5px;color:var(--c-text-muted)}

/* 功能卡片 */
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.feat{position:relative;padding:28px;border-radius:var(--radius);background:var(--c-surface);border:1px solid var(--c-border);transition:all .3s;cursor:default;overflow:hidden}
.feat::after{content:'';position:absolute;top:-50%;right:-50%;width:200%;height:200%;background:radial-gradient(circle,rgba(139,92,246,.06),transparent 50%);opacity:0;transition:opacity .3s;pointer-events:none}
.feat:hover{border-color:var(--c-border-hover);transform:translateY(-4px)}
.feat:hover::after{opacity:1}
.feat-icon{width:48px;height:48px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;margin-bottom:16px;background:var(--c-surface-2)}
.feat-icon.purple{background:linear-gradient(135deg,rgba(99,102,241,.2),rgba(168,85,247,.2));box-shadow:0 0 20px rgba(99,102,241,.15)}
.feat-icon.green{background:linear-gradient(135deg,rgba(16,185,129,.2),rgba(52,211,153,.2));box-shadow:0 0 20px rgba(16,185,129,.15)}
.feat-icon.blue{background:linear-gradient(135deg,rgba(59,130,246,.2),rgba(96,165,250,.2));box-shadow:0 0 20px rgba(59,130,246,.15)}
.feat-icon.amber{background:linear-gradient(135deg,rgba(245,158,11,.2),rgba(251,191,36,.2));box-shadow:0 0 20px rgba(245,158,11,.15)}
.feat h3{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Songti SC","STSong","SimSun","宋体",sans-serif;font-size:16.5px;font-weight:700;margin-bottom:8px}
.feat p{font-size:13.5px;color:var(--c-text-dim);line-height:1.65;margin-bottom:14px}
.feat-badge{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:12px;font-size:11.5px;font-weight:600}
.badge-done{background:rgba(16,185,129,.12);color:var(--c-green);border:1px solid rgba(16,185,129,.2)}

/* CTA 大卡片 */
.cta-card{margin-top:48px;padding:48px;border-radius:24px;background:linear-gradient(135deg,rgba(99,102,241,.1),rgba(168,85,247,.1));border:1px solid var(--c-border);text-align:center;position:relative;overflow:hidden}
.cta-card::before{content:'';position:absolute;inset:0;background:radial-gradient(circle at 50% 0%,rgba(139,92,246,.15),transparent 60%);pointer-events:none}
.cta-card h2{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Songti SC","STSong","SimSun","宋体",sans-serif;font-size:26px;font-weight:700;margin-bottom:10px;position:relative}
.cta-card p{color:var(--c-text-dim);margin-bottom:24px;position:relative}
.cta-card .hero-cta{position:relative}

/* 技术栈 */
.tech{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:48px;padding-top:32px;border-top:1px solid var(--c-border)}
.tech-item{padding:6px 14px;border-radius:8px;background:var(--c-surface);border:1px solid var(--c-border);font-size:12px;color:var(--c-text-dim);font-weight:500;transition:all .2s}
.tech-item:hover{color:var(--c-text);border-color:var(--c-border-hover)}

/* 底部 */
.footer{text-align:center;padding:32px 0 0;color:var(--c-text-muted);font-size:12.5px}
.footer a{color:var(--c-text-dim);text-decoration:none}

/* 响应式 */
@media(max-width:640px){
  .nav-links{display:none}
  .hero{padding:40px 0 24px}
  .stats{grid-template-columns:repeat(2,1fr)}
  .features{grid-template-columns:1fr}
}
</style>
</head>
<body>
<nav class="nav">
  <div class="nav-inner">
    <a class="nav-brand" href="/">
      <span class="nav-brand-icon">💰</span>
      <span class="nav-brand-text">Finance Agent</span>
    </a>
    <div class="nav-links">
      <a href="/upload">数据上传</a>
      <a href="/report">管报预览</a>
      <a href="/performance">绩效试算</a>
      <a href="/monitor">监控</a>
    </div>
    <div class="nav-status"><span class="dot"></span> 运行中</div>
  </div>
</nav>

<div class="wrap">
  <!-- Hero -->
  <section class="hero">
    <div class="hero-tag">D7 · 企业运营智能化 Demo</div>
    <h1>智能<span class="grad">财务归一化</span><br>与绩效试算底座</h1>
    <p>上传报销 / 对公支付 / 工资 Excel,AI 自动归一化科目,生成管报 + AI 简评 + 飞书文档,支持绩效规则回放与奖金试算。</p>
    <div class="hero-cta">
      <a class="btn btn-primary" href="/upload">📤 上传数据</a>
      <a class="btn btn-ghost" href="/report">📈 查看管报</a>
      <a class="btn btn-ghost" href="/performance">🎯 绩效试算</a>
    </div>
  </section>

  <!-- 实时统计 -->
  <div class="stats" id="stats">
    <div class="stat">
      <div class="stat-label">流水总量</div>
      <div class="stat-value"><span id="s-count">—</span><span class="unit">条</span></div>
      <div class="stat-trend" id="s-trend">加载中...</div>
    </div>
    <div class="stat">
      <div class="stat-label">流水总额</div>
      <div class="stat-value">¥<span id="s-amount">—</span></div>
      <div class="stat-trend" style="color:var(--c-text-muted)">归一化后</div>
    </div>
    <div class="stat">
      <div class="stat-label">AI 模型</div>
      <div class="stat-value" style="font-size:18px">deepseek-v4-pro</div>
      <div class="stat-trend" style="color:var(--c-text-muted)">CherryIN 网关</div>
    </div>
    <div class="stat">
      <div class="stat-label">服务状态</div>
      <div class="stat-value" style="font-size:18px;color:var(--c-green)">● Online</div>
      <div class="stat-trend" style="color:var(--c-text-muted)" id="s-uptime">端口 5002</div>
    </div>
  </div>

  <!-- 功能模块 -->
  <div class="section">
    <div class="section-head">
      <div class="section-title">功能模块</div>
      <div class="section-sub">7 天交付 · 38 个单元测试全绿</div>
    </div>
    <div class="features">
      <div class="feat">
        <div class="feat-icon purple">📊</div>
        <h3>管报底座</h3>
        <p>上传 Excel → AI 字段归一化 → 自动汇总生成管报,支持一级 / 二级科目分类与金额统计。</p>
        <span class="feat-badge badge-done">✅ 已上线</span>
      </div>
      <div class="feat">
        <div class="feat-icon blue">🤖</div>
        <h3>AI 简评</h3>
        <p>基于管报数据生成 AI 经营简评,识别异常波动与重点科目,辅助财务决策。</p>
        <span class="feat-badge badge-done">✅ 已上线</span>
      </div>
      <div class="feat">
        <div class="feat-icon green">🎯</div>
        <h3>绩效试算</h3>
        <p>按部门 / 岗位设置目标与奖金基数,基于流水自动计算达成率与奖金,支持飞书 Bot 调参。</p>
        <span class="feat-badge badge-done">✅ 已上线</span>
      </div>
      <div class="feat">
        <div class="feat-icon amber">📤</div>
        <h3>飞书输出</h3>
        <p>管报 / 简评 / 绩效试算结果一键推送到飞书群,支持 Bot 交互式查询(帮助 / 管报 / 绩效)。</p>
        <span class="feat-badge badge-done">✅ 已上线</span>
      </div>
      <div class="feat">
        <div class="feat-icon purple">📡</div>
        <h3>监控告警</h3>
        <p>5xx 错误自动告警,请求量 / 错误率 / 端点耗时实时监控,飞书推送(待配置项目群)。</p>
        <span class="feat-badge badge-done">✅ 已上线</span>
      </div>
      <div class="feat">
        <div class="feat-icon blue">🔄</div>
        <h3>批量归一化</h3>
        <p>支持批量流水 AI 归一化,自动分类研发费 / 销售费 / 管理费 / 营业成本四级科目。</p>
        <span class="feat-badge badge-done">✅ 已上线</span>
      </div>
    </div>
  </div>

  <!-- CTA -->
  <div class="cta-card">
    <h2>开始体验</h2>
    <p>上传你的第一份财务数据,体验 AI 归一化与管报生成全流程</p>
    <div class="hero-cta">
      <a class="btn btn-primary" href="/upload">📤 立即上传</a>
      <a class="btn btn-ghost" href="/report">📈 管报预览 API</a>
    </div>
  </div>

  <!-- 技术栈 -->
  <div class="tech">
    <span class="tech-item">Python Flask</span>
    <span class="tech-item">CherryIN API</span>
    <span class="tech-item">DeepSeek V4 Pro</span>
    <span class="tech-item">SQLite</span>
    <span class="tech-item">飞书 OpenAPI</span>
    <span class="tech-item">unittest</span>
  </div>

  <div class="footer">
    企业运营智能化 Demo · 财务 Agent v1.0 · 服务器 124.222.181.129:5002<br>
    基于 2026-07-09 线下拜访会议需求 · 7 天敏捷交付
  </div>
</div>

<script>
async function loadStats(){
  try{
    const [statsResp, reportResp] = await Promise.all([
      fetch('/api/stats').then(r=>r.json()).catch(()=>null),
      fetch('/api/report/preview').then(r=>r.json()).catch(()=>null)
    ]);
    if(reportResp && reportResp.ok){
      document.getElementById('s-count').textContent = reportResp.total_count || 0;
      document.getElementById('s-amount').textContent = (reportResp.total_amount||0).toLocaleString('zh-CN',{maximumFractionDigits:0});
      document.getElementById('s-trend').textContent = reportResp.total_count > 0 ? '已入库' : '暂无数据';
    }
    if(statsResp && statsResp.uptime_human){
      document.getElementById('s-uptime').textContent = '运行 ' + statsResp.uptime_human;
    }
  }catch(e){console.log('stats load failed',e)}
}
loadStats();
</script>
</body>
</html>"""

UPLOAD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>发票上传 · 财务 Agent</title>
<style>
:root{--c-primary:#6366f1;--c-primary-3:#a855f7;--c-bg:#0f0f1a;--c-surface:rgba(255,255,255,.04);--c-surface-2:rgba(255,255,255,.08);--c-text:#e4e4e7;--c-text-dim:#a1a1aa;--c-text-muted:#71717a;--c-border:rgba(255,255,255,.08);--c-border-hover:rgba(139,92,246,.4);--c-green:#10b981;--c-amber:#f59e0b;--c-red:#ef4444;--radius:16px}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Kaiti SC","STKaiti","楷体",sans-serif;background:var(--c-bg);color:var(--c-text);line-height:1.6;min-height:100vh}
body::before{content:'';position:fixed;inset:0;z-index:-1;background:radial-gradient(ellipse 80% 50% at 20% 0%,rgba(99,102,241,.15),transparent),radial-gradient(ellipse 60% 50% at 80% 30%,rgba(168,85,247,.12),transparent),var(--c-bg)}
.nav{position:sticky;top:0;z-index:100;backdrop-filter:blur(20px);background:rgba(15,15,26,.7);border-bottom:1px solid var(--c-border)}
.nav-inner{max-width:900px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;justify-content:space-between}
.nav-brand{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Songti SC","STSong","宋体",sans-serif;display:flex;align-items:center;gap:10px;font-size:16px;font-weight:700;color:var(--c-text);text-decoration:none}
.nav-brand-icon{width:32px;height:32px;border-radius:10px;background:linear-gradient(135deg,var(--c-primary),var(--c-primary-3));display:flex;align-items:center;justify-content:center;font-size:16px}
.nav a{color:var(--c-text-dim);text-decoration:none;font-size:13.5px;margin-left:16px}
.nav a:hover{color:var(--c-text)}
.wrap{max-width:900px;margin:0 auto;padding:32px 24px}
h1{font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue","Songti SC","STSong","宋体",sans-serif;font-size:28px;font-weight:800;margin-bottom:8px}
.subtitle{color:var(--c-text-dim);margin-bottom:32px}
.card{background:var(--c-surface);border:1px solid var(--c-border);border-radius:var(--radius);padding:28px;margin-bottom:20px}
.drop{border:2px dashed var(--c-border);border-radius:12px;padding:48px 24px;text-align:center;cursor:pointer;transition:all .3s;background:rgba(255,255,255,.02)}
.drop:hover{border-color:var(--c-border-hover);background:rgba(139,92,246,.05)}
.drop.dragover{border-color:var(--c-primary);background:rgba(99,102,241,.08)}
.drop.has-file{border-color:var(--c-green);background:rgba(16,185,129,.06)}
.drop-icon{font-size:48px;margin-bottom:12px}
.drop-text{font-size:16px;font-weight:600;margin-bottom:4px}
.drop-hint{font-size:13px;color:var(--c-text-muted)}
.btn{display:inline-flex;align-items:center;gap:8px;padding:13px 36px;border-radius:12px;font-size:15px;font-weight:600;border:none;cursor:pointer;font-family:inherit;transition:all .25s}
.btn-primary{background:linear-gradient(135deg,var(--c-primary),var(--c-primary-3));color:#fff;box-shadow:0 4px 20px rgba(99,102,241,.4)}
.btn-primary:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 6px 28px rgba(99,102,241,.5)}
.btn:disabled{background:var(--c-surface-2);color:var(--c-text-muted);cursor:not-allowed;box-shadow:none}
.loading{display:inline-block;width:18px;height:18px;border:2px solid var(--c-border);border-top-color:var(--c-primary);border-radius:50%;animation:spin 1s linear infinite;vertical-align:middle;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
.result{margin-top:20px}
.result-card{background:var(--c-surface);border:1px solid var(--c-border);border-radius:12px;padding:20px;margin-bottom:12px}
.field-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 24px}
.field{display:flex;flex-direction:column;gap:2px}
.field-label{font-size:12px;color:var(--c-text-muted);font-weight:600;letter-spacing:.3px}
.field-value{font-size:14px;color:var(--c-text);font-weight:500}
.lvl-tag{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:600}
.lvl-dev{background:rgba(99,102,241,.15);color:#818cf8;border:1px solid rgba(99,102,241,.3)}
.lvl-sales{background:rgba(245,158,11,.15);color:#fbbf24;border:1px solid rgba(245,158,11,.3)}
.lvl-mgmt{background:rgba(16,185,129,.15);color:#34d399;border:1px solid rgba(16,185,129,.3)}
.lvl-cost{background:rgba(236,72,153,.15);color:#f472b6;border:1px solid rgba(236,72,153,.3)}
.confidence-bar{height:6px;border-radius:3px;background:var(--c-surface-2);overflow:hidden;margin-top:4px}
.confidence-fill{height:100%;border-radius:3px;transition:width .5s}
.ocr-preview{margin-top:12px;padding:12px;background:rgba(0,0,0,.2);border-radius:8px;font-size:12px;color:var(--c-text-muted);max-height:200px;overflow-y:auto;white-space:pre-wrap;font-family:monospace}
.items-list{display:flex;flex-wrap:wrap;gap:6px}
.item-tag{padding:4px 10px;background:var(--c-surface-2);border-radius:6px;font-size:12px;color:var(--c-text-dim)}
.back{display:inline-flex;align-items:center;gap:4px;color:var(--c-text-muted);font-size:13px;text-decoration:none;margin-top:16px}
.back:hover{color:var(--c-text)}
.error{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);color:#fca5a5;padding:12px 16px;border-radius:8px;margin-top:12px}
</style>
</head>
<body>
<nav class="nav"><div class="nav-inner">
  <a class="nav-brand" href="/"><span class="nav-brand-icon">💰</span>Finance Agent</a>
  <div><a href="/">首页</a><a href="/report">管报</a><a href="/performance">绩效</a></div>
</div></nav>

<div class="wrap">
  <h1>📄 发票上传</h1>
  <p class="subtitle">上传发票 PDF 或图片 → OCR 提取 → AI 解析字段 + 归一化科目 → 入库</p>

  <div class="card">
    <div class="drop" id="drop">
      <div class="drop-icon">🧾</div>
      <div class="drop-text">点击或拖拽发票到此处</div>
      <div class="drop-hint">支持 PDF / JPG / PNG(扫描件自动 OCR)· 单文件</div>
    </div>
    <input type="file" id="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.txt" style="display:none">
    <div style="text-align:center;margin-top:20px">
      <button class="btn btn-primary" id="btn" disabled>🚀 上传并解析</button>
    </div>
    <div class="result" id="result"></div>
    <a href="/" class="back">← 返回首页</a>
    <a href="/report" class="back" style="margin-left:16px;color:#a78bfa">📈 查看管报 →</a>
  </div>
</div>

<script>
function escapeHtml(s){if(s==null)return'';return String(s).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
const zone=document.getElementById('drop'),file=document.getElementById('file'),btn=document.getElementById('btn'),result=document.getElementById('result');
zone.addEventListener('click',()=>file.click());
zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('dragover')});
zone.addEventListener('dragleave',()=>zone.classList.remove('dragover'));
zone.addEventListener('drop',e=>{e.preventDefault();zone.classList.remove('dragover');if(e.dataTransfer.files.length){file.files=e.dataTransfer.files;showFile()}});
file.addEventListener('change',showFile);
function showFile(){if(file.files.length){zone.innerHTML='<div class="drop-icon">✅</div><div class="drop-text">'+escapeHtml(file.files[0].name)+'</div><div class="drop-hint">'+(file.files[0].size/1024).toFixed(1)+' KB · 点击重新选择</div>';zone.classList.add('has-file');btn.disabled=false}}
btn.addEventListener('click',async()=>{
  if(!file.files.length)return;
  result.innerHTML='<div style="text-align:center;padding:32px"><span class="loading"></span>OCR 提取 + AI 解析中(约 10-30 秒)...</div>';
  const fd=new FormData();fd.append('file',file.files[0]);
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const j=await r.json();
    if(j.ok){
      const d=j.result;
      const lvlClass={'研发费':'lvl-dev','销售费':'lvl-sales','管理费':'lvl-mgmt','营业成本':'lvl-cost','未归类':'lvl-cost'};
      const cls=lvlClass[d.level1]||'lvl-cost';
      const confColor=d.confidence>=0.8?'#10b981':d.confidence>=0.5?'#f59e0b':'#ef4444';
      let html='<div class="result-card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px"><h2 style="font-size:18px;font-weight:700">✅ 解析完成</h2><span class="lvl-tag '+cls+'">'+escapeHtml(d.level1)+' / '+escapeHtml(d.level2)+'</span></div>';
      html+='<div class="field-grid">';
      html+='<div class="field"><span class="field-label">发票号码</span><span class="field-value">'+escapeHtml(d.invoice_no||'—')+'</span></div>';
      html+='<div class="field"><span class="field-label">开票日期</span><span class="field-value">'+escapeHtml(d.invoice_date||'—')+'</span></div>';
      html+='<div class="field"><span class="field-label">销售方</span><span class="field-value">'+escapeHtml(d.vendor||'—')+'</span></div>';
      html+='<div class="field"><span class="field-label">金额</span><span class="field-value" style="font-size:18px;font-weight:700;color:#34d399">¥'+(d.amount||0).toLocaleString('zh-CN')+'</span></div>';
      html+='</div>';
      if(d.items&&d.items.length){html+='<div class="field" style="margin-top:12px"><span class="field-label">商品明细</span><div class="items-list">'+d.items.map(it=>'<span class="item-tag">'+escapeHtml(it)+'</span>').join('')+'</div></div>'}
      html+='<div class="field" style="margin-top:12px"><span class="field-label">AI 判断</span><span class="field-value">'+escapeHtml(d.reason||'—')+'</span>';
      html+='<div class="confidence-bar"><div class="confidence-fill" style="width:'+(d.confidence*100)+'%;background:'+confColor+'"></div></div>';
      html+='<span style="font-size:11px;color:var(--c-text-muted)">置信度 '+(d.confidence*100).toFixed(0)+'%</span></div>';
      if(j.ocr_text_preview){html+='<details style="margin-top:12px"><summary style="cursor:pointer;font-size:12px;color:var(--c-text-muted)">查看 OCR 原文</summary><div class="ocr-preview">'+escapeHtml(j.ocr_text_preview)+'</div></details>'}
      html+='</div>';
      html+='<div style="text-align:center;margin-top:12px"><a href="/report" style="color:#a78bfa;font-weight:600;text-decoration:none">📈 查看管报预览 →</a></div>';
      result.innerHTML=html;
    }else{
      result.innerHTML='<div class="error">❌ '+escapeHtml(j.error||JSON.stringify(j))+'</div>';
    }
  }catch(e){result.innerHTML='<div class="error">❌ 网络错误: '+escapeHtml(e.message)+'</div>'}
});
</script>
</body>
</html>"""

# === 路由 ===
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return LOGIN_HTML.replace("{% if error %}", "").replace("{% endif %}", "").replace("{{ error }}", "")
    name = request.form.get("name", "").strip()
    pin = request.form.get("pin", "").strip()
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT id, name, role FROM users WHERE name=? AND pin=?", (name, pin))
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        return LOGIN_HTML.replace("{% if error %}", "").replace("{% endif %}", "").replace("{{ error }}", "姓名或 PIN 码错误")
    flask_session["user_id"] = row[0]
    return redirect("/")


@app.route("/logout")
def logout_page():
    flask_session.clear()
    return redirect("/login")


@app.route("/")
def index():
    u = _current_user()
    if not u:
        return redirect("/login")
    return INDEX_HTML

@app.route("/upload")
def upload_page():
    u = _current_user()
    if not u:
        return redirect("/login")
    return UPLOAD_HTML

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "finance-agent",
        "day": "D4",
        "port": 5002,
        "llm_model": LLM_MODEL,
    })

# === API ===
@app.route("/api/test-llm")
def test_llm():
    """测试 CherryIN 连通性"""
    result = test_connection()
    return jsonify(result)

@app.route("/api/normalize", methods=["POST"])
def normalize():
    """字段归一化: 输入流水,输出归一化科目"""
    data = request.get_json(silent=True) or {}
    amount = data.get("amount", 0)
    try:
        amount = float(amount)
    except (ValueError, TypeError):
        amount = 0
    summary = data.get("summary", "")
    source = data.get("source", "未知")

    if not summary:
        return jsonify({"ok": False, "error": "summary is required"})

    prompt = NORMALIZE_PROMPT.format(
        rules=ACCOUNTING_RULES,
        amount=amount,
        summary=summary,
        source=source,
    )

    result = chat_json([
        {"role": "system", "content": "你是财务归类助手,严格按口径规则归一化流水科目。只返回 JSON。"},
        {"role": "user", "content": prompt},
    ], temperature=0.1)

    if isinstance(result, dict) and result.get("_error"):
        return jsonify({"ok": False, "error": result["_error"], "stage": "normalize"})
    if not isinstance(result, dict) or "level1" not in result:
        return jsonify({"ok": False, "error": "LLM 返回格式异常", "stage": "normalize", "raw": str(result)[:200]})
    return jsonify({"ok": True, "result": result, "input": {"amount": amount, "summary": summary, "source": source}})

@app.route("/api/normalize/batch", methods=["POST"])
def normalize_batch():
    """批量归一化"""
    data = request.get_json(silent=True) or {}
    transactions = data.get("transactions", [])
    if not isinstance(transactions, list):
        return jsonify({"ok": False, "error": "transactions must be a list"})
    results = []
    for tx in transactions:
        if not isinstance(tx, dict):
            results.append({"input": tx, "ok": False, "error": "invalid transaction"})
            continue
        amount = tx.get("amount", 0)
        try:
            amount = float(amount)
        except (ValueError, TypeError):
            amount = 0
        summary = tx.get("summary", "")
        source = tx.get("source", "未知")
        if not summary:
            results.append({"ok": False, "error": "no summary"})
            continue
        prompt = NORMALIZE_PROMPT.format(
            rules=ACCOUNTING_RULES, amount=amount, summary=summary, source=source,
        )
        r = chat_json([
            {"role": "system", "content": "你是财务归类助手。只返回 JSON。"},
            {"role": "user", "content": prompt},
        ], temperature=0.1)
        if isinstance(r, dict) and r.get("_error"):
            results.append({"input": tx, "ok": False, "error": r["_error"]})
        elif not isinstance(r, dict) or "level1" not in r:
            results.append({"input": tx, "ok": False, "error": "LLM 返回格式异常"})
        else:
            results.append({"input": tx, "ok": True, "result": r})
    return jsonify({"ok": True, "count": len(results), "success_count": len([r for r in results if r.get("ok")]), "results": results})

# === 数据上传 ===
def _ocr_pdf(pdf_bytes):
    """对扫描 PDF 做 OCR,返回 (text, error)。复用法务 Agent 的 OCR 实现。"""
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError as e:
        return None, f"OCR 依赖未安装: {e}"
    try:
        images = convert_from_bytes(pdf_bytes, dpi=_OCR_DPI, first_page=1, last_page=_OCR_MAX_PAGES)
    except Exception as e:
        return None, f"PDF 转图片失败: {e}"
    if not images:
        return None, "PDF 无可渲染页面"
    pages_text = []
    for img in images:
        try:
            pages_text.append(pytesseract.image_to_string(img, lang="chi_sim+eng").strip())
        except Exception:
            pages_text.append("")
        img.close()
    text = "\n\n".join(t for t in pages_text if t)
    return text, None


def extract_text_from_upload(f, filename):
    """从上传文件提取文本。成功返回 str,失败返回 dict。
    支持: PDF(文本型/扫描型), 图片(png/jpg,直接 OCR), TXT"""
    raw = f.read()
    if not raw:
        return {"ok": False, "error": "文件为空"}

    fl = filename.lower()

    # --- 图片直接 OCR ---
    if fl.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")):
        try:
            import pytesseract
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(raw))
            text = pytesseract.image_to_string(img, lang="chi_sim+eng").strip()
            if not text:
                return {"ok": False, "error": "图片 OCR 未提取到文本(图片可能模糊或无文字)"}
            return text
        except ImportError as e:
            return {"ok": False, "error": f"OCR 依赖未安装: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"图片 OCR 失败: {e}"}

    # --- PDF: 先 pypdf,文本短则降级 OCR ---
    if fl.endswith(".pdf"):
        text = ""
        try:
            from pypdf import PdfReader
            import io as _io
            reader = PdfReader(_io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            return {"ok": False, "error": "pypdf not installed"}
        except Exception as e:
            return {"ok": False, "error": f"PDF 解析失败: {e}"}

        if len(text.strip()) < _OCR_MIN_CHARS:
            ocr_text, ocr_err = _ocr_pdf(raw)
            if ocr_err:
                return {"ok": False, "error": f"扫描件需 OCR,但 OCR 失败: {ocr_err}"}
            if not ocr_text.strip():
                return {"ok": False, "error": "OCR 未提取到文本(图片可能模糊或为纯图形)"}
            return ocr_text
        return text

    # --- TXT ---
    if fl.endswith(".txt"):
        text = raw.decode("utf-8", errors="ignore")
        if not text.strip():
            return {"ok": False, "error": "文件为空"}
        return text

    return {"ok": False, "error": "不支持的文件格式(请上传 PDF / 图片 / TXT)"}


def parse_excel(file_storage):
    """解析 Excel/CSV,返回 (rows, error) 元组。成功时 error=None,失败时 rows=None"""
    filename = file_storage.filename
    if filename.endswith(".csv"):
        import csv
        stream = io.TextIOWrapper(file_storage.stream, encoding="utf-8-sig")
        reader = csv.DictReader(stream)
        rows = []
        for row in reader:
            # 自动找金额列和摘要列
            amount = 0
            summary = ""
            for k, v in row.items():
                if k and v:
                    kl = k.lower().strip()
                    if any(x in kl for x in ["金额", "amount", "amt", "总额", "费用"]):
                        try:
                            amount = float(str(v).replace(",", "").replace("¥", "").strip())
                        except (ValueError, TypeError):
                            pass
                    elif any(x in kl for x in ["摘要", "summary", "说明", "备注", "事由", "用途"]):
                        summary = str(v).strip()
            if summary or amount:
                rows.append({"summary": summary, "amount": amount, "source": "上传"})
        return rows, None
    else:
        try:
            import openpyxl
        except ImportError:
            return None, "openpyxl not installed"
        try:
            wb = openpyxl.load_workbook(file_storage.stream, data_only=True)
            ws = wb.active
            headers = [str(cell.value or "").strip() for cell in ws[1]]
            rows = []
            for row in ws.iter_rows(min_row=2, values_only=True):
                row_dict = dict(zip(headers, row))
                amount = 0
                summary = ""
                for k, v in row_dict.items():
                    if k and v is not None:
                        kl = k.lower().strip()
                        if any(x in kl for x in ["金额", "amount", "amt", "总额", "费用"]):
                            try:
                                amount = float(str(v).replace(",", "").replace("¥", "").strip())
                            except (ValueError, TypeError):
                                pass
                        elif any(x in kl for x in ["摘要", "summary", "说明", "备注", "事由", "用途"]):
                            summary = str(v).strip()
                if summary or amount:
                    rows.append({"summary": summary, "amount": amount, "source": "上传"})
            return rows, None
        except Exception as e:
            return None, f"Excel 解析失败: {e}"

@app.route("/api/upload", methods=["POST"])
def upload():
    """上传发票 PDF/图片,OCR 提取 → AI 解析+归一化 → 入库"""
    u = _current_user()
    if not u:
        return jsonify({"ok": False, "error": "未登录"}), 401
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "file is required"})
    filename = f.filename or "upload.bin"

    # 1. 提取文本
    extracted = extract_text_from_upload(f, filename)
    if isinstance(extracted, dict) and not extracted.get("ok", True):
        return jsonify(extracted)
    invoice_text = extracted
    if len(invoice_text.strip()) < 5:
        return jsonify({"ok": False, "error": "提取到的文本过短,无法解析"})

    # 2. AI 解析 + 归一化(一步)
    prompt = INVOICE_PARSE_PROMPT.format(rules=ACCOUNTING_RULES, invoice_text=invoice_text[:6000])
    r = chat_json([
        {"role": "system", "content": "你是财务发票解析助手。从 OCR 文本提取字段并归一化科目。只返回 JSON。"},
        {"role": "user", "content": prompt},
    ], temperature=0.1)

    if not isinstance(r, dict) or r.get("_error") or "level1" not in r:
        return jsonify({"ok": False, "error": r.get("_error", "LLM 返回异常") if isinstance(r, dict) else "LLM 返回非 dict",
                         "stage": "parse", "ocr_text_preview": invoice_text[:500]})

    # 3. 解析金额(容错)
    try:
        amount = float(str(r.get("amount", 0)).replace(",", "").replace("¥", "").replace("元", "").strip() or 0)
    except (ValueError, TypeError):
        amount = 0

    level1 = r.get("level1", "未归类")
    level2 = r.get("level2", "未归类")
    confidence = max(0, min(1.0, float(r.get("confidence", 0))))
    reason = r.get("reason", "")
    vendor = r.get("vendor", "")
    invoice_no = r.get("invoice_no", "")
    invoice_date = r.get("invoice_date", "")
    items = r.get("items", [])
    summary = " / ".join(items) if items else (vendor or "发票")

    # 4. 入库
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO transactions (source, amount, summary, level1, level2, confidence, reason, vendor, invoice_no, invoice_text, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("发票", amount, summary, level1, level2, confidence, reason, vendor, invoice_no, invoice_text[:5000], flask_session.get("user_id")),
        )
        conn.commit()
        tx_id = c.lastrowid
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "transaction_id": tx_id,
        "result": {
            "invoice_no": invoice_no,
            "invoice_date": invoice_date,
            "vendor": vendor,
            "amount": amount,
            "items": items,
            "level1": level1,
            "level2": level2,
            "confidence": confidence,
            "reason": reason,
        },
        "ocr_text_preview": invoice_text[:500],
    })


@app.route("/api/report/preview")
def report_preview():
    """管报预览: 按一级科目汇总。财务可看全部,普通员工只看自己。"""
    u = _current_user()
    if not u:
        return jsonify({"ok": False, "error": "未登录"}), 401
    scope = request.args.get("scope", "mine")
    # 普通员工强制 mine;财务可选 all
    if u["role"] != "financial":
        scope = "mine"
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        if scope == "all":
            c.execute("SELECT level1, level2, COUNT(*) as cnt, SUM(amount) as total FROM transactions GROUP BY level1, level2 ORDER BY level1, level2")
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) as total_count, SUM(amount) as total_amount FROM transactions")
            overall = c.fetchone()
        else:
            c.execute("SELECT level1, level2, COUNT(*) as cnt, SUM(amount) as total FROM transactions WHERE user_id=? GROUP BY level1, level2 ORDER BY level1, level2", (u["id"],))
            rows = c.fetchall()
            c.execute("SELECT COUNT(*) as total_count, SUM(amount) as total_amount FROM transactions WHERE user_id=?", (u["id"],))
            overall = c.fetchone()
    finally:
        conn.close()
    summary = {}
    for r in rows:
        level1, level2, cnt, total = r
        if level1 not in summary:
            summary[level1] = {"total": 0, "count": 0, "items": []}
        summary[level1]["total"] += total or 0
        summary[level1]["count"] += cnt
        summary[level1]["items"].append({"level2": level2, "count": cnt, "total": round(total or 0, 2)})
    return jsonify({
        "ok": True,
        "total_count": overall[0],
        "total_amount": round(overall[1] or 0, 2),
        "summary": {k: {**v, "total": round(v["total"], 2)} for k, v in summary.items()},
    })

# === D4: 管报页面 + AI 简评 + 飞书输出 ===
# 简评缓存(进程级,报表 hash → commentary)
_COMMENTARY_CACHE = {}

def _get_report_data():
    """取管报数据,返回 (rows, overall) 或 (None, None)"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT level1, level2, COUNT(*) as cnt, SUM(amount) as total FROM transactions GROUP BY level1, level2 ORDER BY level1, level2")
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) as total_count, SUM(amount) as total_amount FROM transactions")
        overall = c.fetchone()
    finally:
        conn.close()
    if not rows:
        return None, None
    return rows, overall

def _build_report_text(rows, overall):
    """构建管报文本(供 AI 简评用)"""
    lines = [f"总笔数: {overall[0]}, 总金额: ¥{overall[1] or 0:,.2f}"]
    cur_l1 = None
    for level1, level2, cnt, total in rows:
        if level1 != cur_l1:
            lines.append(f"\n【{level1}】")
            cur_l1 = level1
        lines.append(f"  {level2}: {cnt}笔, ¥{total or 0:,.2f}")
    return "\n".join(lines)

def _generate_commentary(rows, overall):
    """生成 AI 简评(带缓存,避免重复调 LLM)"""
    report_data = _build_report_text(rows, overall)
    cache_key = hash(report_data)
    if cache_key in _COMMENTARY_CACHE:
        return _COMMENTARY_CACHE[cache_key], None
    prompt = REPORT_COMMENTARY_PROMPT.format(report_data=report_data)
    result = chat([
        {"role": "system", "content": "你是财务分析师,正在给同事做管报简评。直接输出 3-5 行简评,每行一个要点。"},
        {"role": "user", "content": prompt},
    ], temperature=0.3)
    if result.get("error"):
        return None, result["error"]
    commentary = result.get("content", "").strip()
    if commentary:
        _COMMENTARY_CACHE[cache_key] = commentary
    return commentary, None

@app.route("/report")
def report_page():
    """管报预览页面"""
    u = _current_user()
    if not u:
        return redirect("/login")
    return REPORT_HTML

@app.route("/api/report/commentary", methods=["POST"])
def report_commentary():
    """AI 简评: 基于管报数据生成 3-5 条点评"""
    rows, overall = _get_report_data()
    if rows is None:
        return jsonify({"ok": False, "error": "暂无数据,请先上传"})
    commentary, err = _generate_commentary(rows, overall)
    if err:
        return jsonify({"ok": False, "error": f"AI 简评生成失败: {err}"})
    return jsonify({"ok": True, "commentary": commentary})

@app.route("/api/feishu/chats")
def feishu_chats():
    """列出 Bot 所在的飞书群聊"""
    return jsonify(feishu_list_chats())

@app.route("/api/report/feishu", methods=["POST"])
def report_feishu():
    """把管报 + AI 简评发到飞书群"""
    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id", "")
    if not chat_id:
        return jsonify({"ok": False, "error": "chat_id is required"})

    rows, overall = _get_report_data()
    if rows is None:
        return jsonify({"ok": False, "error": "暂无数据"})

    # 构建管报富文本
    paragraphs = [[
        {"tag": "text", "text": f"总笔数 {overall[0]}, 总金额 ¥{overall[1] or 0:,.2f}\n"},
    ]]
    cur_l1 = None
    cur_items = []
    for level1, level2, cnt, total in rows:
        if level1 != cur_l1:
            if cur_items:
                paragraphs.append(cur_items)
            cur_l1 = level1
            cur_items = [{"tag": "text", "text": f"\n【{level1}】\n"}]
        cur_items.append({"tag": "text", "text": f"  {level2}: {cnt}笔 ¥{total or 0:,.2f}\n"})
    if cur_items:
        paragraphs.append(cur_items)

    # 1. 发送管报
    r1 = feishu_send_post(chat_id, "📊 管报预览", paragraphs)
    if not r1.get("ok"):
        return jsonify({"ok": False, "error": f"管报发送失败: {r1.get('error', 'unknown')}", "report_sent": False})

    # 2. 生成 AI 简评(带缓存,不重复调 LLM)
    commentary, err = _generate_commentary(rows, overall)
    if err or not commentary:
        return jsonify({"ok": True, "report_sent": True, "commentary_sent": False, "commentary_error": err or "empty commentary"})

    # 3. 发送简评
    comm_paragraphs = [[{"tag": "text", "text": commentary}]]
    r2 = feishu_send_post(chat_id, "🤖 AI 简评", comm_paragraphs)
    if not r2.get("ok"):
        return jsonify({"ok": True, "report_sent": True, "commentary_sent": False, "commentary": commentary, "commentary_error": r2.get("error", "unknown")})
    return jsonify({"ok": True, "report_sent": True, "commentary_sent": True, "commentary": commentary})

# === D5: 绩效试算 ===
PERFORMANCE_PROMPT = """你是绩效管理专家。根据以下数据给出绩效简评(2-3 条):

{data}

要求:
1. 每条一行,直接给结论
2. 关注达成率、超额/未达标、系数合理性
3. 不要客套话
"""

def _get_performance_rules():
    """取所有绩效规则,返回 list of dict"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT department, position, coefficient, target_amount, bonus_base FROM performance_rules ORDER BY department, position")
        rows = c.fetchall()
    finally:
        conn.close()
    return [{"department": r[0], "position": r[1], "coefficient": r[2],
             "target_amount": r[3], "bonus_base": r[4]} for r in rows]


def _calculate_performance(department=None):
    """计算绩效。返回 dict: {departments: [...], summary: {...}}

    逻辑:
      - 每个部门的流水 = transactions 表里 source 匹配部门名的金额合计
        (source 字段目前是"报销/对公支付/工资",不直接区分部门,
         所以用 LLM 归一化后的 level2 项关联部门关键词)
      - 达成率 = 部门实际流水 / target_amount
      - 绩效奖金 = bonus_base × coefficient × (达成率 clamp 到 [0, 1.5])
    """
    rules = _get_performance_rules()
    if not rules:
        return None

    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        # 按一级科目汇总,用于关联部门
        c.execute("SELECT level1, SUM(amount) FROM transactions GROUP BY level1")
        l1_totals = {r[0]: r[1] or 0 for r in c.fetchall()}
        c.execute("SELECT COUNT(*) as cnt, SUM(amount) as total FROM transactions")
        overall = c.fetchone()
    finally:
        conn.close()

    total_amount = overall[1] or 0
    total_count = overall[0] or 0

    # 部门 → 科目映射(简化模型:哪类费用占比高就归哪个部门)
    DEPT_SUBJECT_MAP = {
        "研发部": ["研发费"],
        "销售部": ["销售费"],
        "管理部": ["管理费"],
        "交付部": ["营业成本"],
    }

    results = []
    for rule in rules:
        dept = rule["department"]
        # 部门实际流水 = 映射科目金额合计(简化模型)
        subjects = DEPT_SUBJECT_MAP.get(dept, [])
        actual = sum(l1_totals.get(s, 0) for s in subjects)

        target = rule["target_amount"]
        achievement_rate = (actual / target) if target > 0 else 0
        # 达成率 clamp [0, 1.5]
        clamped_rate = max(0, min(1.5, achievement_rate))
        bonus = rule["bonus_base"] * rule["coefficient"] * clamped_rate

        status = "达标"
        if achievement_rate < 0.6:
            status = "未达标"
        elif achievement_rate >= 1.0:
            status = "超额"

        results.append({
            "department": dept,
            "position": rule["position"],
            "coefficient": rule["coefficient"],
            "target_amount": target,
            "actual_amount": actual,
            "achievement_rate": round(achievement_rate * 100, 1),
            "bonus_base": rule["bonus_base"],
            "bonus": round(bonus, 2),
            "status": status,
        })

    if department:
        results = [r for r in results if r["department"] == department]

    total_bonus = sum(r["bonus"] for r in results)

    return {
        "rules_count": len(rules),
        "total_count": total_count,
        "total_amount": total_amount,
        "total_bonus": round(total_bonus, 2),
        "departments": results,
        "level1_totals": l1_totals,
    }


@app.route("/performance")
def performance_page():
    """绩效试算页面"""
    return PERFORMANCE_HTML


@app.route("/api/performance/calculate", methods=["GET"])
def performance_calculate():
    """计算绩效试算结果"""
    department = request.args.get("department", "").strip()
    data = _calculate_performance(department or None)
    if data is None:
        return jsonify({"ok": False, "error": "未配置绩效规则"})
    return jsonify({"ok": True, **data})


@app.route("/api/performance/feishu", methods=["POST"])
def performance_send_feishu():
    """把绩效报告推送到飞书群"""
    data = request.get_json(silent=True) or {}
    chat_id = data.get("chat_id", "").strip()
    if not chat_id:
        return jsonify({"ok": False, "error": "缺少 chat_id"})

    dept = data.get("department", "").strip() or None
    result = _calculate_performance(dept)
    if result is None:
        return jsonify({"ok": False, "error": "未配置绩效规则"})

    # 构建飞书富文本
    title = "📊 绩效试算报告"
    paragraphs = [
        [{"tag": "text", "text": f"总流水: ¥{result['total_amount']:,.2f} ({result['total_count']} 笔)\n绩效奖金合计: ¥{result['total_bonus']:,.2f}\n"}],
    ]

    # 按部门分组
    for item in result["departments"]:
        status_emoji = {"达标": "✅", "超额": "🚀", "未达标": "⚠️"}.get(item["status"], "")
        line = f"{status_emoji} {item['department']} · {item['position']}\n  目标: ¥{item['target_amount']:,.0f} | 实际: ¥{item['actual_amount']:,.0f} | 达成率: {item['achievement_rate']}%\n  系数: {item['coefficient']} | 奖金基数: ¥{item['bonus_base']:,.0f} → 应发: ¥{item['bonus']:,.2f}"
        paragraphs.append([{"tag": "text", "text": line}])

    r = feishu_send_post(chat_id, title, paragraphs)
    if not r.get("ok"):
        return jsonify({"ok": False, "error": r.get("error", "发送失败")})
    return jsonify({"ok": True, "sent": True, "total_bonus": result["total_bonus"]})


@app.route("/api/performance/rules", methods=["GET"])
def performance_rules_api():
    """列出绩效规则"""
    return jsonify({"ok": True, "rules": _get_performance_rules()})


# === 飞书 webhook ===
# 已处理消息 ID 去重(飞书会重试)
_PROCESSED_MSG_IDS = set()
_MAX_MSG_CACHE = 200


def _handle_feishu_file_message(msg_id, msg_type, content, chat_id, sender_open_id):
    """处理飞书图片/文件消息:下载 → OCR → AI 解析 → 入库 → 回复"""
    print(f"[FILE_HANDLER] start msg_id={msg_id} type={msg_type} content_keys={list(content.keys())}", flush=True)
    try:
        # 提取 file_key / image_key
        if msg_type == "image":
            file_key = content.get("image_key", "")
            file_type = "image"
            filename = f"feishu_image_{file_key[:16]}.png"
        elif msg_type == "file":
            file_key = content.get("file_key", "")
            file_type = "file"
            filename = content.get("file_name", f"feishu_file_{file_key[:16]}")
        else:
            return

        if not file_key:
            feishu_send_text(chat_id, "❌ 无法获取文件 key")
            return

        feishu_send_text(chat_id, "📥 正在下载文件...")

        # 1. 下载文件
        dl = feishu_download_file(msg_id, file_key, file_type=file_type)
        if not dl.get("ok"):
            feishu_send_text(chat_id, f"❌ 文件下载失败: {dl.get('error', '未知错误')}")
            return

        file_bytes = dl["data"]
        feishu_send_text(chat_id, f"📄 文件已下载({len(file_bytes)} 字节),OCR 提取中...")

        # 2. 提取文本(复用 extract_text_from_upload)
        import io as _io
        extracted = extract_text_from_upload(_io.BytesIO(file_bytes), filename)
        if isinstance(extracted, dict) and not extracted.get("ok", True):
            feishu_send_text(chat_id, f"❌ 文本提取失败: {extracted.get('error', '未知')}")
            return
        invoice_text = extracted
        if len(invoice_text.strip()) < 5:
            feishu_send_text(chat_id, "❌ 提取到的文本过短,无法解析(图片可能模糊或无文字)")
            return

        # 3. AI 解析 + 归一化
        feishu_send_text(chat_id, "🤖 AI 解析中(约 10-30 秒)...")
        prompt = INVOICE_PARSE_PROMPT.format(rules=ACCOUNTING_RULES, invoice_text=invoice_text[:6000])
        r = chat_json([
            {"role": "system", "content": "你是财务发票解析助手。从 OCR 文本提取字段并归一化科目。只返回 JSON。"},
            {"role": "user", "content": prompt},
        ], temperature=0.1)

        if not isinstance(r, dict) or r.get("_error") or "level1" not in r:
            feishu_send_text(chat_id, f"❌ AI 解析失败: {r.get('_error', '返回异常') if isinstance(r, dict) else '非 dict'}")
            return

        # 4. 解析金额
        try:
            amount = float(str(r.get("amount", 0)).replace(",", "").replace("¥", "").replace("元", "").strip() or 0)
        except (ValueError, TypeError):
            amount = 0

        level1 = r.get("level1", "未归类")
        level2 = r.get("level2", "未归类")
        confidence = max(0, min(1.0, float(r.get("confidence", 0))))
        reason = r.get("reason", "")
        vendor = r.get("vendor", "")
        invoice_no = r.get("invoice_no", "")
        items = r.get("items", [])
        summary = " / ".join(items) if items else (vendor or "发票")

        # 5. 映射到 user(飞书 open_id → users 表)
        user = _get_or_create_user_by_open_id(sender_open_id)

        # 6. 入库
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO transactions (source, amount, summary, level1, level2, confidence, reason, vendor, invoice_no, invoice_text, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("发票", amount, summary, level1, level2, confidence, reason, vendor, invoice_no, invoice_text[:5000], user["id"] if user else None),
            )
            conn.commit()
        finally:
            conn.close()

        # 7. 回复结果
        conf_emoji = "✅" if confidence >= 0.8 else "⚠️" if confidence >= 0.5 else "❓"
        lines = [
            f"✅ 发票已解析入库",
            "",
            f"发票号: {invoice_no or '—'}",
            f"销售方: {vendor or '—'}",
            f"金额: ¥{amount:,.2f}",
            f"科目: {level1} / {level2}",
            f"{conf_emoji} 置信度: {confidence*100:.0f}%",
            f"理由: {reason}",
        ]
        if user:
            lines.append(f"归属: {user['name']}")
        lines.append("")
        lines.append('发送"管报"查看汇总')
        feishu_send_text(chat_id, "\n".join(lines))

    except Exception as e:
        try:
            feishu_send_text(chat_id, f"❌ 处理文件时出错: {e}")
        except Exception:
            pass


def _handle_feishu_message(text, chat_id):
    """处理飞书消息指令,异步调用(不阻塞 webhook 响应)"""
    print(f"[MSG_HANDLER] text={text}", flush=True)
    text = (text or "").strip()
    try:
        if "帮助" in text or text.lower() in ("help", "?", "？"):
            feishu_send_text(chat_id,
                "🤖 财务试算助手 · 指令列表\n"
                "──────────────\n"
                "管报  — 查看最新管报汇总\n"
                "绩效  — 查看绩效试算结果\n"
                "简评  — AI 生成财务简评\n"
                "帮助  — 显示本指令列表\n"
                "──────────────\n"
                "直接发送关键词即可,无需@")

        elif "管报" in text:
            rows, overall = _get_report_data()
            if rows is None:
                feishu_send_text(chat_id, "📊 暂无流水数据,请先上传 Excel/CSV。")
                return
            report = _build_report_text(rows, overall)
            feishu_send_text(chat_id, "📊 最新管报汇总\n" + report)

        elif "绩效" in text:
            data = _calculate_performance()
            if data is None:
                feishu_send_text(chat_id, "🎯 暂无绩效规则配置。")
                return
            lines = [
                f"🎯 绩效试算结果",
                f"总流水: ¥{data['total_amount']:,.2f} ({data['total_count']} 笔)",
                f"绩效奖金合计: ¥{data['total_bonus']:,.2f}",
                "",
            ]
            for item in data["departments"]:
                emoji = {"达标": "✅", "超额": "🚀", "未达标": "⚠️"}.get(item["status"], "")
                lines.append(f"{emoji} {item['department']}·{item['position']}: 达成率 {item['achievement_rate']}% → ¥{item['bonus']:,.0f}")
            feishu_send_text(chat_id, "\n".join(lines))

        elif "简评" in text:
            rows, overall = _get_report_data()
            if rows is None:
                feishu_send_text(chat_id, "🤖 暂无流水数据,无法生成简评。")
                return
            feishu_send_text(chat_id, "🤖 AI 正在分析管报,请稍候...")
            commentary, err = _generate_commentary(rows, overall)
            if err:
                feishu_send_text(chat_id, f"❌ 简评生成失败: {err}")
            else:
                feishu_send_text(chat_id, "🤖 AI 简评\n" + commentary)

        else:
            feishu_send_text(chat_id,
                f"收到: {text[:50]}\n发送\"帮助\"查看可用指令。")
    except Exception as e:
        try:
            feishu_send_text(chat_id, f"❌ 处理消息时出错: {e}")
        except Exception:
            pass


@app.route("/webhook", methods=["POST"])
def webhook():
    """飞书事件订阅回调

    支持飞书 v2 事件格式(header.event_type)和 v1 格式(event.type)。
    收到消息后立即返回 200,异步处理指令(避免飞书 3 秒超时)。
    """
    data = request.get_json(silent=True) or {}
    # challenge 验证
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})

    # 解析消息(v2 和 v1 兼容)
    header = data.get("header", {})
    event_type = header.get("event_type") or data.get("type", "")
    event = data.get("event", {})
    msg = event.get("message", {})

    if not msg:
        return jsonify({"ok": True})

    # 消息去重(飞书会重试)
    msg_id = msg.get("message_id", "")
    if msg_id and msg_id in _PROCESSED_MSG_IDS:
        return jsonify({"ok": True, "dedup": True})
    if msg_id:
        _PROCESSED_MSG_IDS.add(msg_id)
        if len(_PROCESSED_MSG_IDS) > _MAX_MSG_CACHE:
            _PROCESSED_MSG_IDS.pop()

    chat_id = msg.get("chat_id", "")
    content_str = msg.get("content", "{}")
    try:
        content = json.loads(content_str) if isinstance(content_str, str) else content_str
    except (json.JSONDecodeError, TypeError):
        content = {}
    text = content.get("text", "")

    # 获取消息类型和发送者
    msg_type = msg.get("message_type", "")
    sender = event.get("sender", {}).get("sender_id", {}).get("open_id", "")

    print(f"[WEBHOOK] msg_type={msg_type} chat_id={chat_id} sender={sender} text={text[:50] if text else ''}", flush=True)
    # 异步处理(不阻塞 webhook 响应)
    if chat_id:
        import threading
        if msg_type in ("image", "file"):
            # 图片/文件消息 → 发票上传流程
            t = threading.Thread(
                target=_handle_feishu_file_message,
                args=(msg_id, msg_type, content, chat_id, sender),
                daemon=True,
            )
            t.start()
        elif text:
            # 文本消息 → 指令处理
            t = threading.Thread(target=_handle_feishu_message, args=(text, chat_id), daemon=True)
            t.start()

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=os.environ.get("FLASK_DEBUG") == "1")
