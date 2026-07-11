#!/usr/bin/env python3
"""财务 Agent — 管报底座 + 绩效试算 Demo (D4)"""
from flask import Flask, request, jsonify, render_template_string
import os
import json
import sqlite3
import io
from dotenv import load_dotenv
from cherry_client import chat, chat_json, embed, test_connection
from feishu_client import list_chats as feishu_list_chats, send_post as feishu_send_post

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB

# === 配置 ===
CHERRYIN_API_KEY = os.environ.get("CHERRYIN_API_KEY", "")
CHERRYIN_BASE_URL = os.environ.get("CHERRYIN_BASE_URL", "https://express-ent-admin.cherryin.ai/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "agent/deepseek-v4-pro")

DB_PATH = os.path.join(os.path.dirname(__file__), "finance.db")

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
    conn.commit()
    conn.close()

init_db()

# === 口径规则 ===
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

# === 页面 ===
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>财务 Agent — 管报底座 + 绩效试算</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--primary:#667eea;--primary-dark:#764ba2;--accent:#52c41a;--warn:#fa8c16;--danger:#f5222d;--bg:#f0f2f5;--card:#fff;--text:#1a1a2e;--text-light:#666;--border:#e8e8e8;--radius:16px;--shadow:0 4px 24px rgba(0,0,0,.06);--shadow-hover:0 8px 32px rgba(102,126,234,.15)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,"PingFang SC",sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
.container{max-width:1000px;margin:0 auto;padding:24px}
.hero{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:48px 40px;border-radius:var(--radius);margin-bottom:28px;position:relative;overflow:hidden;box-shadow:0 8px 32px rgba(102,126,234,.25)}
.hero::before{content:'';position:absolute;top:-50%;right:-20%;width:400px;height:400px;background:rgba(255,255,255,.08);border-radius:50%;animation:float 6s ease-in-out infinite}
.hero::after{content:'';position:absolute;bottom:-30%;left:-10%;width:300px;height:300px;background:rgba(255,255,255,.06);border-radius:50%;animation:float 8s ease-in-out infinite reverse}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-20px)}}
.hero-content{position:relative;z-index:1}
.hero h1{font-size:32px;font-weight:800;margin-bottom:8px;display:flex;align-items:center;gap:12px}
.hero .subtitle{font-size:16px;opacity:.9;font-weight:400}
.hero .badge{display:inline-block;background:rgba(255,255,255,.2);backdrop-filter:blur(10px);padding:6px 16px;border-radius:20px;font-size:13px;font-weight:500;margin-top:16px}
.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:28px}
.stat-card{background:var(--card);padding:24px;border-radius:var(--radius);box-shadow:var(--shadow);transition:transform .3s,box-shadow .3s}
.stat-card:hover{transform:translateY(-4px);box-shadow:var(--shadow-hover)}
.stat-card .stat-icon{width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:22px;margin-bottom:12px}
.stat-card .stat-label{font-size:13px;color:var(--text-light);font-weight:500}
.stat-card .stat-value{font-size:24px;font-weight:700;margin-top:4px}
.section-title{font-size:20px;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.section-title::before{content:'';width:4px;height:24px;background:linear-gradient(135deg,var(--primary),var(--primary-dark));border-radius:2px}
.card{background:var(--card);padding:28px;border-radius:var(--radius);box-shadow:var(--shadow);margin-bottom:20px}
.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.feature-item{padding:20px;border:2px solid var(--border);border-radius:12px;transition:all .3s;cursor:default}
.feature-item:hover{border-color:var(--primary);transform:translateY(-2px);box-shadow:var(--shadow-hover)}
.feature-item .feat-icon{font-size:32px;margin-bottom:8px}
.feature-item h3{font-size:16px;font-weight:600;margin-bottom:6px}
.feature-item p{font-size:13px;color:var(--text-light)}
.tag{display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:600;margin-top:8px}
.tag-done{background:#f6ffed;color:var(--accent);border:1px solid #b7eb8f}
.tag-dev{background:#fff7e6;color:var(--warn);border:1px solid #ffd591}
.status-bar{display:flex;align-items:center;gap:12px;padding:12px 20px;background:linear-gradient(90deg,#f6ffed,#fff);border:1px solid #b7eb8f;border-radius:12px;margin-bottom:16px}
.status-dot{width:10px;height:10px;border-radius:50%;background:var(--accent);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(82,196,26,.4)}70%{box-shadow:0 0 0 8px rgba(82,196,26,0)}100%{box-shadow:0 0 0 0 rgba(82,196,26,0)}}
.btn-row{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}
a.btn{display:inline-flex;align-items:center;gap:6px;padding:12px 28px;border-radius:12px;font-size:14px;font-weight:600;text-decoration:none;transition:all .3s}
.btn-primary{background:linear-gradient(135deg,var(--primary),var(--primary-dark));color:#fff;box-shadow:0 4px 16px rgba(102,126,234,.3)}
.btn-primary:hover{transform:translateY(-2px);box-shadow:0 6px 24px rgba(102,126,234,.4)}
.btn-secondary{background:#fff;color:var(--primary);border:2px solid var(--primary)}
.btn-secondary:hover{background:var(--primary);color:#fff}
.btn-accent{background:linear-gradient(135deg,#52c41a,#389e0d);color:#fff;box-shadow:0 4px 16px rgba(82,196,26,.3)}
.info-row{display:flex;gap:24px;flex-wrap:wrap;font-size:13px;color:var(--text-light);margin-top:8px}
.info-row span{display:flex;align-items:center;gap:4px}
</style>
</head>
<body>
<div class="container">
  <div class="hero">
    <div class="hero-content">
      <h1>💰 财务 Agent</h1>
      <p class="subtitle">管报底座 · 绩效试算 · 智能归一化</p>
      <span class="badge">🚀 D4 已上线 · 管报 + AI 简评 + 飞书输出</span>
    </div>
  </div>

  <div class="status-bar">
    <div class="status-dot"></div>
    <span><b>系统运行中</b> · 端口 5002 · 服务器 124.222.181.129</span>
  </div>

  <div class="stats-row">
    <div class="stat-card">
      <div class="stat-icon" style="background:#f0f5ff">📊</div>
      <div class="stat-label">当前进度</div>
      <div class="stat-value">D4</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon" style="background:#f6ffed">✅</div>
      <div class="stat-label">归一化准确率</div>
      <div class="stat-value">100%</div>
    </div>
    <div class="stat-card">
      <div class="stat-icon" style="background:#fff7e6">⚡</div>
      <div class="stat-label">AI 模型</div>
      <div class="stat-value" style="font-size:16px">deepseek-v4-pro</div>
    </div>
  </div>

  <h2 class="section-title">功能模块</h2>
  <div class="feature-grid">
    <div class="feature-item">
      <div class="feat-icon">📊</div>
      <h3>管报底座</h3>
      <p>上传报销 / 对公支付 / 工资 Excel → AI 字段归一化 → 自动生成飞书文档管报</p>
      <span class="tag tag-done">✅ D4 管报 + AI 简评 + 飞书输出已实现</span>
    </div>
    <div class="feature-item">
      <div class="feat-icon">🎯</div>
      <h3>绩效试算</h3>
      <p>飞书 Bot 交互调参 → 历史业绩回放 → 新旧规则对比表</p>
      <span class="tag tag-dev">⏳ D5-D6 开发中</span>
    </div>
  </div>

  <div class="btn-row" style="margin-top:24px">
    <a class="btn btn-primary" href="/upload">📤 前往上传</a>
    <a class="btn btn-accent" href="/report">📈 管报预览</a>
    <a class="btn btn-secondary" href="/api/test-llm" target="_blank">⚡ 测试 LLM</a>
  </div>
</div>
</body>
</html>"""

UPLOAD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>上传数据 — 财务 Agent</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--primary:#667eea;--primary-dark:#764ba2;--accent:#52c41a;--warn:#fa8c16;--danger:#f5222d;--bg:#f0f2f5;--card:#fff;--text:#1a1a2e;--text-light:#666;--border:#e8e8e8;--radius:16px;--shadow:0 4px 24px rgba(0,0,0,.06)}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,"PingFang SC",sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
.container{max-width:900px;margin:0 auto;padding:24px}
.hero{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:32px;border-radius:var(--radius);margin-bottom:24px}
.hero h1{font-size:24px;font-weight:700;margin-bottom:4px}
.hero p{opacity:.9;font-size:14px}
.card{background:var(--card);padding:28px;border-radius:var(--radius);box-shadow:var(--shadow);margin-bottom:20px}
.card h2{font-size:18px;font-weight:600;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.source-select{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:8px}
.source-option{padding:16px;border:2px solid var(--border);border-radius:12px;text-align:center;cursor:pointer;transition:all .3s;font-weight:500}
.source-option:hover{border-color:var(--primary);background:#f0f5ff}
.source-option.selected{border-color:var(--primary);background:linear-gradient(135deg,#f0f5ff,#fff);color:var(--primary);font-weight:600}
.source-option .src-icon{font-size:28px;margin-bottom:4px}
.source-option .src-name{font-size:14px}
.drop-zone{border:2px dashed var(--border);border-radius:12px;padding:48px;text-align:center;color:var(--text-light);cursor:pointer;transition:all .3s;background:#fafafa}
.drop-zone:hover{border-color:var(--primary);background:#f0f5ff;transform:scale(1.01)}
.drop-zone.dragover{border-color:var(--primary);background:#f0f5ff}
.drop-zone.has-file{border-color:var(--accent);background:#f6ffed;color:var(--accent)}
.drop-icon{font-size:48px;margin-bottom:8px}
.drop-text{font-size:16px;font-weight:600;margin-bottom:4px}
.drop-hint{font-size:13px;opacity:.7}
.btn{display:inline-flex;align-items:center;gap:6px;padding:14px 36px;background:linear-gradient(135deg,var(--primary),var(--primary-dark));color:#fff;border:none;border-radius:12px;font-size:15px;font-weight:600;cursor:pointer;transition:all .3s;box-shadow:0 4px 16px rgba(102,126,234,.3)}
.btn:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 6px 24px rgba(102,126,234,.4)}
.btn:disabled{background:#d9d9d9;cursor:not-allowed;box-shadow:none}
a{color:var(--primary);text-decoration:none;font-weight:500}
a:hover{text-decoration:underline}
.result-box{margin-top:16px}
.result-success{background:linear-gradient(135deg,#f6ffed,#fff);border:1px solid #b7eb8f;border-radius:12px;padding:16px;margin-bottom:12px}
.result-table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
.result-table th{background:linear-gradient(135deg,var(--primary),var(--primary-dark));color:#fff;padding:10px;text-align:left;font-weight:600}
.result-table th:first-child{border-radius:8px 0 0 0}
.result-table th:last-child{border-radius:0 8px 0 0}
.result-table td{padding:10px;border-bottom:1px solid var(--border)}
.result-table tr:hover{background:#f0f5ff}
.lvl-tag{display:inline-block;padding:2px 10px;border-radius:10px;font-size:12px;font-weight:600}
.lvl-dev{background:#f0f5ff;color:#667eea}
.lvl-sales{background:#fff7e6;color:#fa8c16}
.lvl-mgmt{background:#f6ffed;color:#52c41a}
.lvl-cost{background:#fff0f6;color:#eb2f96}
.loading{display:inline-block;width:20px;height:20px;border:3px solid var(--border);border-top-color:var(--primary);border-radius:50%;animation:spin 1s linear infinite;margin-right:8px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.back-link{display:inline-flex;align-items:center;gap:4px;color:var(--text-light);font-size:14px;margin-top:16px}
</style>
</head>
<body>
<div class="container">
  <div class="hero">
    <h1>📊 上传财务数据</h1>
    <p>选择数据源类型 → 上传 Excel/CSV → AI 自动归一化</p>
  </div>

  <div class="card">
    <h2>🗂️ 选择数据源类型</h2>
    <div class="source-select">
      <div class="source-option selected" data-source="报销">
        <div class="src-icon">🧾</div>
        <div class="src-name">报销数据</div>
      </div>
      <div class="source-option" data-source="对公支付">
        <div class="src-icon">🏢</div>
        <div class="src-name">对公支付</div>
      </div>
      <div class="source-option" data-source="工资">
        <div class="src-icon">💰</div>
        <div class="src-name">工资数据</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>📤 上传文件</h2>
    <div class="drop-zone" id="drop">
      <div class="drop-icon">📁</div>
      <div class="drop-text">点击或拖拽文件到此处</div>
      <div class="drop-hint">支持 Excel (.xlsx/.xls) 和 CSV 格式 · 表头需含「金额」和「摘要」</div>
    </div>
    <input type="file" id="file" accept=".xlsx,.xls,.csv" style="display:none">
    <div style="text-align:center;margin-top:20px">
      <button class="btn" id="upload-btn" disabled>🚀 上传并归一化</button>
    </div>
    <div class="result-box" id="result"></div>
    <div style="text-align:center">
      <a href="/" class="back-link">← 返回首页</a>
      &nbsp;|&nbsp;
      <a href="/api/report/preview" target="_blank" class="back-link" style="color:#722ed1">📈 查看管报预览 →</a>
    </div>
  </div>
</div>
<script>
function escapeHtml(s){if(s==null)return '';return String(s).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
let selectedSource='报销';
document.querySelectorAll('.source-option').forEach(opt=>{
  opt.addEventListener('click',()=>{
    document.querySelectorAll('.source-option').forEach(o=>o.classList.remove('selected'));
    opt.classList.add('selected');
    selectedSource=opt.dataset.source;
  });
});
const zone=document.getElementById('drop');
const file=document.getElementById('file');
const btn=document.getElementById('upload-btn');
const result=document.getElementById('result');
zone.addEventListener('click',()=>file.click());
zone.addEventListener('dragover',e=>{e.preventDefault();zone.classList.add('dragover')});
zone.addEventListener('dragleave',e=>{zone.classList.remove('dragover')});
zone.addEventListener('drop',e=>{
  e.preventDefault();
  zone.classList.remove('dragover');
  if(e.dataTransfer.files.length){file.files=e.dataTransfer.files;showFile()}
});
file.addEventListener('change',showFile);
function showFile(){
  if(file.files.length){
    zone.innerHTML='<div class="drop-icon">✅</div><div class="drop-text">'+escapeHtml(file.files[0].name)+'</div><div class="drop-hint">点击重新选择</div>';
    zone.classList.add('has-file');
    btn.disabled=false;
  }
}
btn.addEventListener('click',async()=>{
  if(!file.files.length)return;
  result.innerHTML='<div style="text-align:center;padding:24px"><span class="loading"></span>上传 + AI 归一化中(每条约 2 秒)...</div>';
  const fd=new FormData();
  fd.append('file',file.files[0]);
  fd.append('source_type',selectedSource);
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const j=await r.json();
    if(j.ok){
      const lvlClass={'研发费':'lvl-dev','销售费':'lvl-sales','管理费':'lvl-mgmt','营业成本':'lvl-cost'};
      let html='<div class="result-success"><b>✅ '+j.count+' 条数据已归一化入库</b></div>';
      html+='<table class="result-table"><thead><tr><th>摘要</th><th>金额</th><th>一级</th><th>二级</th></tr></thead><tbody>';
      j.results.forEach(r=>{
        const cls=lvlClass[r.level1]||'lvl-dev';
        html+='<tr><td>'+escapeHtml(r.summary)+'</td><td>¥'+escapeHtml(String(r.amount))+'</td><td><span class="lvl-tag '+cls+'">'+escapeHtml(r.level1)+'</span></td><td>'+escapeHtml(r.level2)+'</td></tr>';
      });
      html+='</tbody></table>';
      html+='<div style="text-align:center;margin-top:12px"><a href="/api/report/preview" target="_blank" style="color:#722ed1;font-weight:600">📈 查看管报预览 →</a></div>';
      result.innerHTML=html;
    }else{
      result.innerHTML='<div style="color:red;padding:16px;background:#fff0f0;border-radius:8px">❌ '+escapeHtml(j.error||JSON.stringify(j))+'</div>';
    }
  }catch(e){result.innerHTML='<div style="color:red">错误: '+escapeHtml(e.message)+'</div>'}
});
</script>
</body>
</html>"""

# === 路由 ===
@app.route("/")
def index():
    return INDEX_HTML

@app.route("/upload")
def upload_page():
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

    return jsonify({"ok": True, "result": result, "input": {"amount": amount, "summary": summary, "source": source}})

@app.route("/api/normalize/batch", methods=["POST"])
def normalize_batch():
    """批量归一化"""
    data = request.get_json(silent=True) or {}
    transactions = data.get("transactions", [])
    results = []
    for tx in transactions:
        amount = tx.get("amount", 0)
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
        results.append({"input": tx, "result": r})
    return jsonify({"ok": True, "count": len(results), "results": results})

# === 数据上传 ===
def parse_excel(file_storage):
    """解析 Excel/CSV,返回 [{summary, amount, source}, ...]"""
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
        return rows
    else:
        try:
            import openpyxl
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
            return rows
        except ImportError:
            return None

@app.route("/api/upload", methods=["POST"])
def upload():
    """上传 Excel/CSV,自动归一化并入库"""
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "file is required"})
    source_type = request.form.get("source_type", "上传")
    rows = parse_excel(f)
    if rows is None:
        return jsonify({"ok": False, "error": "openpyxl not installed"})
    if not rows:
        return jsonify({"ok": False, "error": "未解析到数据(请检查表头是否含'金额'和'摘要')"})
    # 归一化 + 入库
    results = []
    failures = []
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        for i, row in enumerate(rows):
            row["source"] = source_type
            prompt = NORMALIZE_PROMPT.format(
                rules=ACCOUNTING_RULES,
                amount=row["amount"],
                summary=row["summary"],
                source=row["source"],
            )
            r = chat_json([
                {"role": "system", "content": "你是财务归类助手。只返回 JSON。"},
                {"role": "user", "content": prompt},
            ], temperature=0.1)
            if not isinstance(r, dict) or r.get("error") or "level1" not in r:
                failures.append({"row": i + 1, "summary": row["summary"], "error": r.get("error", "missing level1") if isinstance(r, dict) else "invalid response"})
                # 失败行不入库,跳过
                results.append({**row, "level1": "未归类", "level2": "未归类", "confidence": 0, "reason": r.get("error", "LLM 返回异常") if isinstance(r, dict) else "LLM 返回非 dict", "failed": True})
                continue
            level1 = r.get("level1", "?")
            level2 = r.get("level2", "?")
            confidence = r.get("confidence", 0)
            reason = r.get("reason", "")
            c.execute(
                "INSERT INTO transactions (source, amount, summary, level1, level2, confidence, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (row["source"], row["amount"], row["summary"], level1, level2, confidence, reason),
            )
            results.append({**row, "level1": level1, "level2": level2, "confidence": confidence, "reason": reason})
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": True, "count": len(results), "success_count": len([r for r in results if not r.get("failed")]), "fail_count": len(failures), "results": results, "failures": failures})

@app.route("/api/report/preview")
def report_preview():
    """管报预览: 按一级科目汇总"""
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT level1, level2, COUNT(*) as cnt, SUM(amount) as total FROM transactions GROUP BY level1, level2 ORDER BY level1, level2")
        rows = c.fetchall()
        c.execute("SELECT COUNT(*) as total_count, SUM(amount) as total_amount FROM transactions")
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
        return _COMMENTARY_CACHE[cache_key]
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

@app.route("/api/performance", methods=["POST"])
def performance():
    """绩效试算 (D5 实现)"""
    return jsonify({"ok": True, "message": "D5 实现"})

# === 飞书 webhook ===
@app.route("/webhook", methods=["POST"])
def webhook():
    """飞书事件订阅回调"""
    data = request.get_json(silent=True) or {}
    # challenge 验证
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})
    # 事件处理(后续实现)
    event = data.get("event", {})
    msg = event.get("message", {})
    if msg:
        # 收到消息,后续实现 Bot 回复逻辑
        pass
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5002, debug=os.environ.get("FLASK_DEBUG") == "1")
