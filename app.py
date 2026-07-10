#!/usr/bin/env python3
"""财务 Agent — 管报底座 + 绩效试算 Demo (D2)"""
from flask import Flask, request, jsonify, render_template_string
import os
import json
from dotenv import load_dotenv
from cherry_client import chat, chat_json, embed, test_connection

load_dotenv()

app = Flask(__name__)

# === 配置 ===
CHERRYIN_API_KEY = os.environ.get("CHERRYIN_API_KEY", "")
CHERRYIN_BASE_URL = os.environ.get("CHERRYIN_BASE_URL", "https://express-ent-admin.cherryin.ai/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "agent/deepseek-v4-pro")

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
      <li>📊 <b>管报底座</b> — 上传报销/对公支付/工资 → AI 归一化 → 飞书文档管报 <span class="tag tag-done">D2 归一化已实现</span></li>
      <li>🎯 <b>绩效试算</b> — Bot 交互调参 → 历史业绩回放 → 对比表 <span class="tag tag-dev">D5-D6</span></li>
    </ul>
  </div>
  <div class="card">
    <h2>🔧 系统状态</h2>
    <p>当前进度: <b>D2 归一化 + webhook</b> <span class="tag tag-done">运行中</span></p>
    <p>端口: 5002 | 服务器: 124.222.181.129</p>
    <p><a class="btn" href="/upload">前往上传</a> <a class="btn" href="/api/test-llm" style="background:#52c41a">测试 LLM</a></p>
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
    <h2>报销数据 (Excel/CSV)</h2>
    <div class="drop-zone" id="drop1">点击或拖拽文件到此处</div>
    <input type="file" id="file1" accept=".xlsx,.xls,.csv" style="display:none">
  </div>
  <div class="card">
    <h2>对公支付审批流 (Excel/CSV)</h2>
    <div class="drop-zone" id="drop2">点击或拖拽文件到此处</div>
    <input type="file" id="file2" accept=".xlsx,.xls,.csv" style="display:none">
  </div>
  <div class="card">
    <h2>工资数据 (Excel/CSV)</h2>
    <div class="drop-zone" id="drop3">点击或拖拽文件到此处</div>
    <input type="file" id="file3" accept=".xlsx,.xls,.csv" style="display:none">
  </div>
  <div style="text-align:center">
    <button class="btn" id="submit" disabled>🚀 生成管报 (D4 实现)</button>
    <p style="margin-top:12px;color:#999"><a href="/">← 返回首页</a></p>
  </div>
</div>
<script>
document.querySelectorAll('.drop-zone').forEach((zone,i)=>{
  const input=document.getElementById('file'+(i+1));
  zone.addEventListener('click',()=>input.click());
  zone.addEventListener('dragover',e=>{e.preventDefault();zone.style.borderColor='#667eea'});
  zone.addEventListener('dragleave',e=>{zone.style.borderColor='#d9d9d9'});
  zone.addEventListener('drop',e=>{
    e.preventDefault();
    if(e.dataTransfer.files.length){input.files=e.dataTransfer.files;zone.textContent='✅ '+input.files[0].name;zone.classList.add('has-file');checkReady()}
  });
  input.addEventListener('change',()=>{
    if(input.files.length){zone.textContent='✅ '+input.files[0].name;zone.classList.add('has-file');checkReady()}
  });
});
function checkReady(){
  const ready=[1,2,3].some(i=>document.getElementById('file'+i).files.length);
  document.getElementById('submit').disabled=!ready;
}
</script>
</body>
</html>"""

# === 路由 ===
@app.route("/")
def index():
    return INDEX_HTML

@app.route("/upload")
def upload():
    return UPLOAD_HTML

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "finance-agent",
        "day": "D2",
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
