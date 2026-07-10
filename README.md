# 💰 财务试算助手 Bot

> 企业运营智能化项目 · 财务 Agent Demo
> 主导: Patrick | 协助: 俞昊昇(Yu) / 鲍天一(Bao)

## 📋 项目简介

基于 Flask + CherryIN(deepseek-v4-pro) 的财务智能 Agent,包含两大模块:

- **📊 管报底座** — 上传报销/对公支付/工资 Excel → AI 字段归一化 → 自动生成飞书文档管报
- **🎯 绩效试算** — 飞书 Bot 交互调参 → 历史业绩回放 → 新旧规则对比表

## 🏗️ 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python Flask |
| LLM | CherryIN agent/deepseek-v4-pro |
| 数据库 | SQLite + 飞书多维表格 |
| 飞书交互 | lark-cli + Bot |
| 文档输出 | lark-doc skill |

## 🚀 部署信息

- **服务器**: 124.222.181.129
- **端口**: 5002
- **目录**: /home/ubuntu/finance-agent/
- **Demo 入口**: http://124.222.181.129:5002/
- **上传页**: http://124.222.181.129:5002/upload

## 📅 开发时间线(7 天)

| 天 | 日期 | 主线任务 | 状态 |
|---|---|---|---|
| D1 | 7/10 周四 | 口径对齐 + 多维表格结构 + Flask 骨架 | ✅ 完成 |
| D2 | 7/11 周五 | 报销数据导入 + 字段归一化 Prompt | ⏳ |
| D3 | 7/12 周六 | 对公支付/工资数据源接入 | ⏳ |
| D4 | 7/13 周日 | 管报飞书文档输出 + 内测 | ⏳ |
| D5 | 7/14 周一 | 绩效规则表 + 试算引擎 | ⏳ |
| D6 | 7/15 周二 | Bot 交互调参 | ⏳ |
| D7 | 7/16 周三 | **Demo 交付** + 录屏 | ⏳ |

## ⚙️ 本地开发

```bash
# 1. 克隆
git clone https://github.com/4b8wsfdk7y-cloud/-bot_demo.git
cd -bot_demo

# 2. 虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env 填入实际 API key

# 5. 启动
python app.py
```

## 📁 项目结构

```
.
├── app.py              # Flask 主程序
├── requirements.txt    # Python 依赖
├── .env.example        # 环境变量模板
├── .gitignore
└── README.md
```

## ⚠️ 局限性说明

- Demo 用 mock + 真实混合数据,正式上线前需与代理记账数据校验 1-2 个月偏差
- 绩效试算为最简版(个人业绩 × 提成比例),不支持跨期结算/团队提成
- AI 算数不可靠:数值计算全走多维表格公式,AI 只做语义归类

## 📄 项目文档

- [飞书项目计划文档](https://acnwi1crgmwa.feishu.cn/docx/RdxwdfKWronVQXxNojxc8eT4njc)

## 📝 更新日志

### 2026-07-10 (D1)
- Flask 脚手架搭建完成
- 首页 + 上传页 UI
- /health 健康检查接口
- 部署到 124.222.181.129:5002
