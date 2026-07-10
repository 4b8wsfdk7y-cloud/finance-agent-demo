#!/usr/bin/env python3
"""财务 Agent — 管报底座 + 绩效试算 Demo (D3)"""
from flask import Flask, request, jsonify, render_template_string
import os
import json
import sqlite3
import io
from dotenv import load_dotenv
from cherry_client import chat, chat_json, embed, test_connection

load_dotenv()

app = Flask(__name__)

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

# === 页面 ===
INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>财务 Agent</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f0f2f5;color:#333}
.container{max-width:900px;margin:0 auto;padding:20px}
.header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:40px;border-radius:12px;margin-bottom:24px}
.header h1{font-size:28px;margin-bottom:8px}
.header p{opacity:.9}
.card{background:#fff;padding:24px;border-radius:8px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.card h2{font-size:18px;margin-bottom:16px;color:#444}
.feature-list{list-style:none}
.feature-list li{padding:10px 0;border-bottom:1px solid #f0f0f0}
.feature-list li:last-child{border-bottom:none}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600}
.tag-dev{background:#fff7e6;color:#fa8c16}
.tag-done{background:#f6ffed;color:#52c41a}
a.btn{display:inline-block;padding:10px 24px;background:#667eea;color:#fff;text-decoration:none;border-radius:6px;margin-top:12px}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>💰 财务 Agent</h1>
    <p>管报底座 + 绩效试算 | Demo</p>
  </div>
  <div class="card">
    <h2>📋 功能模块</h2>
    <ul class="feature-list">
      <li>📊 <b>管报底座</b> — 上传报销/对公支付/工资 → AI 归一化 → 飞书文档管报 <span class="tag tag-done">D3 上传+预览已实现</span></li>
      <li>🎯 <b>绩效试算</b> — Bot 交互调参 → 历史业绩回放 → 对比表 <span class="tag tag-dev">D5-D6</span></li>
    </ul>
  </div>
  <div class="card">
    <h2>🔧 系统状态</h2>
    <p>当前进度: <b>D3 多数据源 + 管报预览</b> <span class="tag tag-done">运行中</span></p>
    <p>端口: 5002 | 服务器: 124.222.181.129</p>
    <p><a class="btn" href="/upload">前往上传</a> <a class="btn" href="/api/test-llm" style="background:#52c41a">测试 LLM</a> <a class="btn" href="/api/report/preview" style="background:#722ed1">管报预览</a></p>
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
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f0f2f5;color:#333}
.container{max-width:900px;margin:0 auto;padding:20px}
.header{background:linear-gradient(135deg,#667eea 0%,#764ba2 100%);color:#fff;padding:30px;border-radius:12px;margin-bottom:24px}
.card{background:#fff;padding:24px;border-radius:8px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.card h2{font-size:18px;margin-bottom:16px;color:#444}
.drop-zone{border:2px dashed #d9d9d9;border-radius:8px;padding:40px;text-align:center;color:#999;cursor:pointer;transition:border-color .3s}
.drop-zone:hover{border-color:#667eea}
.drop-zone.has-file{border-color:#52c41a;color:#52c41a}
.btn{display:inline-block;padding:10px 32px;background:#667eea;color:#fff;border:none;border-radius:6px;font-size:14px;cursor:pointer;margin-top:16px}
.btn:disabled{background:#d9d9d9;cursor:not-allowed}
a{color:#667eea}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>📊 上传财务数据</h1>
  </div>
  <div class="card">
    <h2>选择数据源类型</h2>
    <select id="source_type" style="padding:8px;font-size:14px;width:100%;border:1px solid #d9d9d9;border-radius:4px">
      <option value="报销">报销数据</option>
      <option value="对公支付">对公支付审批流</option>
      <option value="工资">工资数据</option>
    </select>
  </div>
  <div class="card">
    <h2>上传文件 (Excel/CSV)</h2>
    <div class="drop-zone" id="drop1">点击或拖拽文件到此处(表头需含"金额"和"摘要")</div>
    <input type="file" id="file1" accept=".xlsx,.xls,.csv" style="display:none">
    <p style="margin-top:8px;color:#999;font-size:13px">支持多文件,每个文件选对应的数据源类型后上传</p>
    <button class="btn" id="upload-btn" disabled>📤 上传并归一化</button>
    <div id="upload-result" style="margin-top:12px"></div>
  </div>
  <div style="text-align:center">
    <p style="margin-top:12px;color:#999"><a href="/">← 返回首页</a> | <a href="/api/report/preview" style="color:#722ed1">查看管报预览 →</a></p>
  </div>
</div>
<script>
const zone=document.getElementById('drop1');
const file=document.getElementById('file1');
const btn=document.getElementById('upload-btn');
const result=document.getElementById('upload-result');
zone.addEventListener('click',()=>file.click());
zone.addEventListener('dragover',e=>{e.preventDefault();zone.style.borderColor='#667eea'});
zone.addEventListener('dragleave',e=>{zone.style.borderColor='#d9d9d9'});
zone.addEventListener('drop',e=>{
  e.preventDefault();
  if(e.dataTransfer.files.length){file.files=e.dataTransfer.files;zone.textContent='✅ '+file.files[0].name;zone.classList.add('has-file');btn.disabled=false}
});
file.addEventListener('change',()=>{
  if(file.files.length){zone.textContent='✅ '+file.files[0].name;zone.classList.add('has-file');btn.disabled=false}
});
btn.addEventListener('click',async()=>{
  if(!file.files.length)return;
  result.textContent='上传 + AI 归一化中(每条约 2 秒)...';
  const fd=new FormData();
  fd.append('file',file.files[0]);
  fd.append('source_type',document.getElementById('source_type').value);
  try{
    const r=await fetch('/api/upload',{method:'POST',body:fd});
    const j=await r.json();
    if(j.ok){
      let html=`<div style="background:#f6ffed;border:1px solid #b7eb8f;border-radius:4px;padding:12px"><b>✅ ${j.count} 条数据已归一化入库</b></div>`;
      html+='<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:8px"><thead><tr><th style="border:1px solid #ddd;padding:6px">摘要</th><th style="border:1px solid #ddd;padding:6px">金额</th><th style="border:1px solid #ddd;padding:6px">一级</th><th style="border:1px solid #ddd;padding:6px">二级</th></tr></thead><tbody>';
      j.results.forEach(r=>{
        html+=`<tr><td style="border:1px solid #ddd;padding:6px">${r.summary||''}</td><td style="border:1px solid #ddd;padding:6px">${r.amount}</td><td style="border:1px solid #ddd;padding:6px">${r.level1}</td><td style="border:1px solid #ddd;padding:6px">${r.level2}</td></tr>`;
      });
      html+='</tbody></table>';
      html+='<p style="margin-top:8px"><a href="/api/report/preview" style="color:#722ed1">查看管报预览 →</a></p>';
      result.innerHTML=html;
    }else{
      result.innerHTML='<pre style="color:red">'+JSON.stringify(j,null,2)+'</pre>';
    }
  }catch(e){result.textContent='错误: '+e.message}
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
        "day": "D3",
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
    data = request.json or {}
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
    data = request.json or {}
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
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    results = []
    for row in rows:
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
        level1 = r.get("level1", "?") if isinstance(r, dict) else "?"
        level2 = r.get("level2", "?") if isinstance(r, dict) else "?"
        confidence = r.get("confidence", 0) if isinstance(r, dict) else 0
        reason = r.get("reason", "") if isinstance(r, dict) else ""
        c.execute(
            "INSERT INTO transactions (source, amount, summary, level1, level2, confidence, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["source"], row["amount"], row["summary"], level1, level2, confidence, reason),
        )
        results.append({**row, "level1": level1, "level2": level2, "confidence": confidence})
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "count": len(results), "results": results})

@app.route("/api/report/preview")
def report_preview():
    """管报预览: 按一级科目汇总"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT level1, level2, COUNT(*) as cnt, SUM(amount) as total FROM transactions GROUP BY level1, level2 ORDER BY level1, level2")
    rows = c.fetchall()
    c.execute("SELECT COUNT(*) as total_count, SUM(amount) as total_amount FROM transactions")
    overall = c.fetchone()
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

@app.route("/api/report", methods=["POST"])
def report():
    """管报生成 (D4 实现)"""
    return jsonify({"ok": True, "message": "D4 实现"})

@app.route("/api/performance", methods=["POST"])
def performance():
    """绩效试算 (D5 实现)"""
    return jsonify({"ok": True, "message": "D5 实现"})

# === 飞书 webhook ===
@app.route("/webhook", methods=["POST"])
def webhook():
    """飞书事件订阅回调"""
    data = request.json or {}
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
    app.run(host="0.0.0.0", port=5002, debug=True)
