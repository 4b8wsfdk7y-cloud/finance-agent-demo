#!/usr/bin/env python3
"""生成 mock 报销数据,用于财务 Agent 测试"""
import json
import requests

AGENT_URL = "http://localhost:5002"

MOCK_EXPENSES = [
    {"amount": 3500, "summary": "差旅费 - 北京出差拜访客户", "source": "报销"},
    {"amount": 1280, "summary": "餐饮费 - 团队月度聚餐", "source": "报销"},
    {"amount": 8800, "summary": "软件采购 - JetBrains 开发工具", "source": "对公支付"},
    {"amount": 4500, "summary": "招待费 - 客户招待餐", "source": "报销"},
    {"amount": 2300, "summary": "办公用品 - 日常文具采购", "source": "报销"},
    {"amount": 56000, "summary": "工资发放 - 研发团队薪酬", "source": "工资"},
    {"amount": 18000, "summary": "云服务费 - 阿里云 ECS+RDS", "source": "对公支付"},
    {"amount": 7500, "summary": "差旅费 - 上海参加行业会议", "source": "报销"},
    {"amount": 3200, "summary": "广告投放 - 朋友圈广告", "source": "对公支付"},
    {"amount": 12000, "summary": "外包服务费 - 设计外包", "source": "对公支付"},
    {"amount": 980, "summary": "打车费 - 客户拜访交通", "source": "报销"},
    {"amount": 6800, "summary": "团建活动 - 部门户外活动", "source": "报销"},
]


def main():
    print(f"测试 {len(MOCK_EXPENSES)} 条 mock 报销数据\n")
    for i, tx in enumerate(MOCK_EXPENSES, 1):
        print(f"[{i}/{len(MOCK_EXPENSES)}] {tx['summary']}")
        try:
            resp = requests.post(
                f"{AGENT_URL}/api/normalize",
                json=tx,
                timeout=60,
            )
            result = resp.json()
            if result.get("ok"):
                r = result.get("result", {})
                if isinstance(r, dict):
                    print(f"  → {r.get('level1','?')} / {r.get('level2','?')} (置信度: {r.get('confidence','?')})")
                else:
                    print(f"  → {r}")
            else:
                print(f"  ❌ {result.get('error')}")
        except Exception as e:
            print(f"  ❌ 请求异常: {e}")
    print("\n=== 测试完成 ===")


if __name__ == "__main__":
    main()
