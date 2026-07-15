# 💰 财务试算助手

> 企业运营智能化项目 · 财务 Agent Demo
> 管报底座(字段归一化 + 管报生成 + AI 简评) + 绩效试算(开发中)

## 📌 项目简介

财务试算助手是 Cherry Studio 企业运营智能化项目的财务模块 Demo,面向企业 CFO/财务经理场景。通过 AI 自动归一化报销/对公支付/工资流水,生成管理报表,并由 AI 给出财务简评,最终一键发送到飞书群。

**核心能力:**
- 📤 上传 Excel/CSV 流水 → AI 自动识别科目归类
- 📊 管报预览(按一级/二级科目汇总 + 占比可视化)
- 🤖 AI 简评(基于管报数据生成 3-5 条财务点评)
- 📨 一键发送管报 + 简评到飞书群

## 🏗 技术架构

| 层 | 技术栈 | 说明 |
|---|---|---|
| Web 框架 | Flask 3.0 | 单文件 app.py,轻量 |
| LLM | CherryIN 网关 · agent/deepseek-v4-pro | OpenAI 兼容协议 |
| Embedding | CherryIN 网关 · baai/bge-m3 | 用于 RAG(预留) |
| 数据存储 | SQLite (finance.db) | transactions 表 |
| Excel 解析 | openpyxl | 支持 xlsx/xls/csv |
| 飞书集成 | lark-cli 子进程 | 复用已认证 profile,免开发权限申请 |
| 部署 | Ubuntu 24.04 + Python venv | 124.222.181.129:5002 |

## 📂 目录结构

```
.
├── app.py                 # Flask 主应用(含所有路由 + 页面 HTML)
├── cherry_client.py       # CherryIN API 客户端(LLM + Embedding)
├── feishu_client.py       # 飞书客户端(lark-cli 子进程封装)
├── mock_expenses.csv      # 测试用报销数据(10 条)
├── test_normalize.py      # 归一化准确率测试脚本
├── requirements.txt
├── .env.example           # 环境变量模板
└── .gitignore
```

## 🚀 快速开始

### 1. 环境准备

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入:
# - CHERRYIN_API_KEY: CherryIN 网关 API Key
# - LARK_APP_ID / LARK_APP_SECRET: 飞书 Bot 应用凭证
```

### 3. 启动

```bash
python app.py
# 访问 http://localhost:5002
```

### 4. 飞书集成(可选)

飞书消息发送依赖服务器上已认证的 `lark-cli`。安装见 [lark-cli 文档](https://github.com/larksuite/lark-cli)。

认证完成后,`feishu_client.py` 会通过子进程调用 `lark-cli im +messages-send` 发送消息,无需额外申请飞书 Bot 权限。

## 📡 API 接口

| 方法 | 路径 | 功能 | 状态 |
|---|---|---|---|
| GET | `/` | 首页(项目介绍 + 入口) | ✅ |
| GET | `/upload` | 上传页(数据源选择 + 文件上传) | ✅ |
| GET | `/report` | 管报预览页(科目汇总 + AI 简评 + 飞书发送) | ✅ |
| GET | `/health` | 健康检查 | ✅ |
| GET | `/api/test-llm` | 测试 CherryIN 连通性 | ✅ |
| POST | `/api/normalize` | 单条流水归一化 | ✅ |
| POST | `/api/normalize/batch` | 批量归一化 | ✅ |
| POST | `/api/upload` | 上传 Excel/CSV 并归一化入库 | ✅ |
| GET | `/api/report/preview` | 管报预览数据(按科目汇总) | ✅ |
| POST | `/api/report/commentary` | AI 简评(基于管报生成点评) | ✅ |
| GET | `/api/feishu/chats` | 列出 Bot 所在飞书群聊 | ✅ |
| POST | `/api/report/feishu` | 发送管报 + AI 简评到飞书群 | ✅ |
| POST | `/api/performance` | 绩效试算 | ⏳ D5 |
| POST | `/webhook` | 飞书事件订阅回调 | ⏳ |

## 🎯 归一化口径

流水会归一化到以下 4 个一级科目:

| 一级科目 | 典型二级科目 |
|---|---|
| 研发费 | 薪酬、云服务费、软件授权、测试设备 |
| 销售费 | 广告费、差旅费、招待费、市场推广 |
| 管理费 | 办公租金、团队建设、行政耗材、培训费 |
| 营业成本 | 原材料、生产设备、物流仓储、直接人工 |

归一化由 LLM 根据流水摘要 + 金额 + 来源判断,返回 JSON:
```json
{
  "level1": "研发费",
  "level2": "云服务费",
  "confidence": 0.95,
  "reason": "AWS 云服务月度账单"
}
```

## 📊 测试数据

`mock_expenses.csv` 包含 10 条报销记录,覆盖 4 个一级科目:

| 摘要 | 金额 | 归一化结果 |
|---|---|---|
| AWS 云服务月度账单 | 18300 | 研发费 / 云服务费 |
| 飞书企业版年费 | 5200 | 管理费 / 软件订阅 |
| 客户拜访打车费 | 380 | 销售费 / 差旅费 |
| ... | ... | ... |

归一化准确率: **10/10 = 100%**

## 🔄 工作流

```
Excel/CSV 上传
    │
    ▼
AI 字段归一化(逐条流水 → level1/level2)
    │
    ▼
SQLite 入库(transactions 表)
    │
    ▼
管报预览(按科目汇总 + 占比柱状图)
    │
    ▼
AI 简评(LLM 生成 3-5 条财务点评)
    │
    ▼
发送到飞书群(富文本管报 + 简评)
```

## 🌐 部署信息

- **服务器**: 124.222.181.129 (Ubuntu 24.04)
- **端口**: 5002
- **目录**: /home/ubuntu/finance-agent/
- **启动**: `cd /home/ubuntu/finance-agent && nohup .venv/bin/python app.py > server.log 2>&1 &`
- **飞书 Bot**: 财务试算助手(App ID: cli_aada2e7c74b9dce8)

## 📝 更新日志

### 2026-07-15 (D8) — UI 精化 + 发票流重构 + 权限分层 + 飞书上传
- 🎨 UI 重设计 — 深色玻璃拟态风格,标题宋体(Songti SC),正文楷体(Kaiti SC),数字英文无衬线
- 🧾 发票流重构 — `/api/upload` 从 Excel 改为纯发票流:PDF/图片 → OCR 提取 → AI 解析+归一化 → 入库
- 🧾 transactions 表加 `vendor` / `invoice_no` / `invoice_text` / `user_id` 字段(ALTER TABLE 兼容旧库)
- 🧾 `INVOICE_PARSE_PROMPT` 一步提取 invoice_no/invoice_date/vendor/amount/items + level1/level2/confidence/reason
- 🔐 权限分层 — users 表(id/name/role/pin/feishu_open_id) + `/login` `/logout` + Flask session
- 🔐 3 个预置账号:财务管理员(1234, scope=all)/ 张三(1111, scope=mine)/ 李四(2222, scope=mine)
- 🔐 `/api/report/preview?scope=mine|all` 按角色过滤:普通员工强制 mine,财务可选 all
- 🔐 所有页面加 `_current_user()` 检查,未登录跳 `/login`
- 🤖 飞书 Bot 上传发票 — webhook 支持 image/file 消息类型,下载 → OCR → AI 解析 → 入库 → 回复
- 🤖 `feishu_client.download_message_file()` — lark-cli `im message_resource get` 下载图片/文件
- 🤖 `_get_or_create_user_by_open_id()` — 飞书 open_id → users 表自动建档为 employee
- 🤖 `_handle_feishu_file_message()` — 完整处理链:下载 → 文本提取 → AI 解析 → 入库(带 user_id) → Bot 回复
- 🧪 `TestUpload` 重写为发票流测试(5 个)
- 🧪 `TestAuth` 新增权限分层测试(7 个):未登录跳转/登录页/登录成功/错误 PIN/登出/财务看全部/员工强制 mine
- 🧪 测试总数: 37 → 49 个(全绿)

### 2026-07-13 (D6) — 飞书 Bot 交互
- 🤖 飞书 Bot webhook 实现 — `/webhook` 支持 v2 事件格式,收到消息异步处理
- 🤖 4 个指令: 管报(汇总)/绩效(试算)/简评(AI 点评)/帮助(指令列表)
- 🤖 消息去重 — `_PROCESSED_MSG_IDS` 缓存最近 200 条 message_id,飞书重试不重复处理
- 🤖 异步处理 — `threading.Thread` 后台处理指令,webhook 立即返回 200(避免飞书 3 秒超时)
- 🤖 未知消息自动回复提示
- 🧪 新增 `TestWebhook` 扩展 — 5 个测试:帮助/管报/绩效/未知指令/去重
- 🧪 测试总数: 32 → 37 个(全绿)

### 2026-07-13 (D5) — 绩效试算引擎
- 🎯 新增 `performance_rules` 表 — 部门/岗位/系数/目标值/奖金基数(建表时自动插入 8 条默认规则)
- 🎯 新增 `/api/performance/calculate` GET 端点 — 从管报流水反算各部门/岗位达成率与绩效奖金
- 🎯 新增 `/api/performance/feishu` POST 端点 — 把绩效报告推送到飞书群(富文本格式)
- 🎯 新增 `/api/performance/rules` GET 端点 — 列出绩效规则表
- 🎯 新增 `/performance` 页面 — 部门筛选 + 绩效明细表(达成率/系数/奖金/状态)+ 飞书推送
- 🎯 奖金计算公式: `bonus = bonus_base × coefficient × clamp(达成率, 0, 1.5)`
- 🎯 部门→科目映射: 研发部→研发费 / 销售部→销售费 / 管理部→管理费 / 交付部→营业成本
- 🎯 三个状态: 达标 / 超额 / 未达标(< 60% 目标)
- 🧪 新增 `TestPerformance` 测试类 — 7 个测试覆盖:页面渲染/规则加载/空DB计算/有数据计算/部门筛选/奖金公式/飞书缺chat_id
- 🧪 测试总数: 25 → 32 个(全绿)
- ✅ 端到端验证: 10 笔 mock 流水 → 8 条规则试算 → 总奖金 ¥3,714.99 → 全部"未达标"(mock 数据量小,目标几十万到百万)

### 2026-07-11 (D4.3) — 飞书告警系统
- 🚨 飞书告警集成 — `monitor.py` 新增告警引擎,自动推送关键告警到飞书群
- 🚨 3 类告警触发规则:
  - **5xx 错误** — 服务端异常告警(每路径 5 分钟限流,避免告警风暴)
  - **健康检查失败** — DB/LLM 连不上即告警(10 分钟限流)
  - **错误率激增** — 5 分钟窗口 >30% 错误率告警(至少 20 请求,15 分钟限流)
- 🚨 新增 `/api/alert/test` 端点 — 一键发送测试告警,验证飞书链路通畅
- 🚨 `/monitor` 仪表盘新增「飞书告警」卡片(累计告警数)+「最近告警」列表(时间/标题/状态)
- 🚨 仪表盘底部新增「🧪 发送测试告警」按钮(POST /api/alert/test)
- 🧪 新增 `TestAlert` 测试类 — 5 个测试覆盖:端点调用/飞书缺失场景/5xx 触发/5xx 限流/4xx 不触发
- 🧪 测试总数: 20 → 25 个(全绿)
- ✅ 端到端验证: 两个 Agent 测试告警均秒发到飞书群,`alerts_sent` 计数正确

### 2026-07-11 (D4.2) — 监控 + 单元测试
- 📊 新增 `monitor.py` 监控模块:请求统计 + 健康检查 + 结构化日志
- 📊 新增 `/api/stats` 端点 — 请求总数/错误数/错误率/端点明细(平均+最大耗时)/最近 50 条错误
- 📊 新增 `/api/health/full` 端点 — DB 连通性 + LLM 连通性 + 运行时间
- 📊 新增 `/monitor` 监控仪表盘 — 暗色主题 HTML,展示总览/端点统计/最近错误
- 📊 结构化日志 — `logs/finance-agent.log`(10MB 轮转,保留 5 个),记录慢请求(>10s)和 5xx 错误
- 🧪 新增 `test_app.py` — 20 个单元测试(unittest + mock),覆盖 health/stats/normalize/batch/upload/report/webhook
- 🧪 测试运行: `.venv/bin/python test_app.py`(无需 pytest)

### 2026-07-11 (D4.1) — 代码审计修复
- 🔒 `debug=True` 改为环境变量控制(`FLASK_DEBUG=1` 才开),关闭 Werkzeug 调试器 RCE 风险
- 🔒 `MAX_CONTENT_LENGTH=16MB` 限制上传体积,防止内存耗尽
- 🔒 前端所有 `innerHTML` 拼接的用户/LLM 内容加 `escapeHtml()` 转义,堵 XSS
- 🔧 `cherry_client.py` 重写:HTTP 状态码检查 + 空 content 检查 + timeout 分类
- 🔧 `chat_json` 支持 JSON 数组 `[...]` 提取(之前只支持对象 `{...}`)
- 🔧 上传 LLM 失败的行跳过(不再写 `level1="?"` 污染管报),返回 failures 列表
- 🔧 所有 SQLite 调用加 `try/finally`,防止连接泄漏
- 🔧 AI 简评加进程级 LRU 缓存(`_COMMENTARY_CACHE`),同管报数据不重复调 LLM
- 🔧 `report_feishu` 错误检查修正:用 `r1.get("ok")` 代替 `r1.get("code") != 0`
- 🔧 `loadChats` 加 try/catch,修复未闭合括号 `(`
- 🔧 `/api/normalize` 和 `/api/normalize/batch` 加 LLM 错误检查(之前失败也返回 `ok:true`)
- 🔧 `parse_excel` 改为返回 `(rows, error)` 元组,区分"openpyxl 未装"和"文件解析失败"
- 🔧 `chat_json` 失败标记从 `error` 改为 `_error`,避免与 LLM 合法返回的 error 字段混淆
- 🔧 `/api/upload` 加 `source_type` 白名单校验 + 单次最多 200 行限制(防 API 滥用)
- 🔧 `/api/normalize` 和 `/api/normalize/batch` 的 `amount` 加 `float()` 类型转换(非数字容错为 0)
- 🔧 `/api/normalize/batch` 加 `transactions` 类型检查(非 list 直接拒绝)

### 2026-07-10 (D4) — 管报 + AI 简评 + 飞书输出
- ✅ 管报预览页面 `/report` — 科目汇总表 + 占比柱状图 + 总览卡片
- ✅ AI 简评引擎 `/api/report/commentary` — LLM 基于管报数据生成 3-5 条财务点评
- ✅ 飞书 Bot 集成 `feishu_client.py` — 通过 lark-cli 子进程发消息(免权限申请)
- ✅ 管报发送到飞书群 `/api/report/feishu` — 富文本格式管报 + AI 简评
- ✅ 飞书群聊列表 `/api/feishu/chats`
- ✅ 首页导航增加「管报」入口
- 🧪 测试: 10 条报销 → 管报汇总 + AI 简评(云服务费占 30%、团队建设重复记账、招待费偏高) → 飞书群收到 2 条富文本消息

### 2026-07-10 (D3) — Excel 上传 + 管报预览
- ✅ Excel/CSV 上传接口 `/api/upload`(支持 xlsx/xls/csv)
- ✅ 多数据源选择(报销 / 对公支付 / 工资)
- ✅ 自动表头识别(金额列 / 摘要列)
- ✅ SQLite `transactions` 表存储归一化结果
- ✅ 管报预览 API `/api/report/preview`(按一级科目汇总)
- ✅ 新增 openpyxl 依赖
- 🧪 测试: 10 条 mock 报销全部归一化正确,管报 4 科目汇总

### 2026-07-10 (D2) — 归一化引擎
- ✅ CherryIN API 客户端 `cherry_client.py`(LLM + Embedding)
- ✅ 字段归一化 API `/api/normalize` + `/api/normalize/batch`
- ✅ 口径规则配置(研发费 / 销售费 / 管理费 / 营业成本)
- ✅ Mock 报销数据测试脚本 `test_normalize.py`
- ✅ 飞书 webhook 接口 `/webhook`
- ✅ LLM 连通性测试 `/api/test-llm`
- 🧪 归一化准确率 100%(3/3 mock 数据正确归类)

### 2026-07-10 (D1) — 脚手架
- ✅ Flask 脚手架搭建完成
- ✅ 首页 + 上传页 UI
- ✅ `/health` 健康检查接口
- ✅ 部署到 124.222.181.129:5002

## 🗓 路线图

| 阶段 | 日期 | 内容 | 状态 |
|---|---|---|---|
| D1 | 7/10 | Flask 脚手架 + 首页 UI | ✅ 完成 |
| D2 | 7/10 | CherryIN 客户端 + 字段归一化 | ✅ 完成 |
| D3 | 7/10 | Excel 上传 + 管报预览 | ✅ 完成 |
| D4 | 7/10 | 管报页面 + AI 简评 + 飞书输出 | ✅ 完成 |
| D5 | 7/13 | 绩效规则表 + 试算引擎 | ✅ 完成 |
| D6 | 7/13 | 飞书 Bot 交互调参 | ✅ 完成 |
| D7 | 7/13 | Demo 交付 + 录屏 | ✅ 完成 |

## 👥 项目背景

基于 2026-07-09 树杨、鲍天一、俞昊晟线下拜访会议的企业运营智能化项目。

- **主导**: Patrick(Cherry Studio 实习生)
- **辅助**: Yu(数据提供)、Bao(测试)
- **交付**: 7 天 Demo(7/10-7/16)

## 📄 License

Internal Demo — Cherry Studio
