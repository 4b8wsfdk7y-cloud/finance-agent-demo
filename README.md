## 📝 更新日志
### 2026-07-10 (D3)
- Excel/CSV 上传接口 (/api/upload,支持 xlsx/xls/csv)
- 多数据源选择(报销/对公支付/工资)
- 自动表头识别(金额/摘要列)
- SQLite transactions 表存储归一化结果
- 管报预览 API (/api/report/preview,按一级科目汇总)
- openpyxl 依赖
- 测试: 10 条 mock 报销全部归一化正确,管报 4 科目汇总

### 2026-07-10 (D2)
- CherryIN API 客户端 (cherry_client.py)
- 字段归一化 API (/api/normalize + /api/normalize/batch)
- 口径规则配置 (研发费/销售费/管理费/营业成本)
- Mock 报销数据测试脚本 (test_normalize.py)
- 飞书 webhook 接口 (/webhook)
- LLM 连通性测试 (/api/test-llm)
- 归一化准确率 100% (3/3 mock 数据正确归类)

### 2026-07-10 (D1)
- Flask 脚手架搭建完成
- 首页 + 上传页 UI
- /health 健康检查接口
- 部署到 124.222.181.129:5002
