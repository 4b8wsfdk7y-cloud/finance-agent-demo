#!/usr/bin/env python3
"""财务 Agent 单元测试

运行:
    cd /home/ubuntu/finance-agent
    .venv/bin/python -m pytest test_app.py -v

或直接运行:
    .venv/bin/python test_app.py
"""
import os
import sys
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# 在导入 app 前设置环境
os.environ.setdefault("CHERRYIN_API_KEY", "test-key")

# 临时 DB
_, TEST_DB = tempfile.mkstemp(suffix=".db")
os.environ.setdefault("DB_PATH", TEST_DB)

sys.path.insert(0, os.path.dirname(__file__))

# patch DB_PATH before import
with patch("app.DB_PATH", TEST_DB):
    pass

import app as finance_app


class FinanceAppTestCase(unittest.TestCase):
    """Flask app 测试基类"""

    def setUp(self):
        # 用临时 DB
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        finance_app.DB_PATH = self.db_path
        finance_app.init_db()
        self.app = finance_app.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_mock_data(self, count=5):
        """插入 mock 数据到 transactions 表"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        for i in range(count):
            c.execute(
                "INSERT INTO transactions (source, amount, summary, level1, level2, confidence, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("报销", 1000 + i * 100, f"测试报销{i}", "研发费" if i % 2 == 0 else "管理费", f"二级{i}", 0.9, "测试"),
            )
        conn.commit()
        conn.close()


class TestHealth(FinanceAppTestCase):
    """健康检查端点"""

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "finance-agent")
        self.assertEqual(data["port"], 5002)

    def test_health_full(self):
        r = self.client.get("/api/health/full")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("db", data)
        self.assertIn("stats", data)


class TestStats(FinanceAppTestCase):
    """监控统计端点"""

    def test_stats_empty(self):
        r = self.client.get("/api/stats")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["ok"], True)
        self.assertIn("total_requests", data)
        self.assertIn("uptime_seconds", data)
        self.assertIn("alerts_sent", data)

    def test_stats_tracks_requests(self):
        # 发几个请求
        self.client.get("/health")
        self.client.get("/health")
        self.client.get("/api/stats")

        r = self.client.get("/api/stats")
        data = r.get_json()
        self.assertGreater(data["total_requests"], 3)
        self.assertIn("/health", data["endpoints"])

    def test_monitor_page(self):
        r = self.client.get("/monitor")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"finance-agent", r.data)
        self.assertIn("监控".encode(), r.data)


class TestAlert(FinanceAppTestCase):
    """告警系统测试"""

    @patch("monitor._send_feishu_alert")
    def test_alert_test_endpoint(self, mock_send):
        """测试告警端点应调用飞书发送"""
        mock_send.return_value = True
        r = self.client.post("/api/alert/test")
        data = r.get_json()
        self.assertTrue(data["ok"])
        mock_send.assert_called_once()

    @patch("monitor._send_feishu_alert")
    def test_alert_test_endpoint_no_feishu(self, mock_send):
        """未配置飞书时应返回 ok:False"""
        mock_send.return_value = False
        r = self.client.post("/api/alert/test")
        data = r.get_json()
        self.assertFalse(data["ok"])

    @patch("monitor._send_feishu_alert")
    def test_5xx_triggers_alert(self, mock_send):
        """5xx 错误应触发飞书告警(通过直接调 _track_request 模拟)"""
        from monitor import _track_request
        mock_send.return_value = True
        _track_request("/fake-500", "GET", 500, 0.5, "127.0.0.1")
        mock_send.assert_called()

    @patch("monitor._send_feishu_alert")
    def test_5xx_throttled(self, mock_send):
        """同端点 5xx 5 分钟内只告警一次"""
        from monitor import _track_request
        mock_send.return_value = True
        _track_request("/fake-throttle", "GET", 500, 0.5, "127.0.0.1")
        _track_request("/fake-throttle", "GET", 500, 0.5, "127.0.0.1")
        _track_request("/fake-throttle", "GET", 500, 0.5, "127.0.0.1")
        self.assertEqual(mock_send.call_count, 1)

    @patch("monitor._send_feishu_alert")
    def test_4xx_no_alert(self, mock_send):
        """4xx 错误不触发飞书告警(只有 5xx 才告警)"""
        from monitor import _track_request
        mock_send.return_value = True
        _track_request("/fake-404", "GET", 404, 0.1, "127.0.0.1")
        mock_send.assert_not_called()


class TestNormalize(FinanceAppTestCase):
    """/api/normalize 测试"""

    def test_missing_summary(self):
        r = self.client.post("/api/normalize", json={"amount": 100})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("summary", data["error"])

    def test_bad_amount_type(self):
        """amount 非数字应容错为 0,不崩溃"""
        r = self.client.post("/api/normalize", json={
            "summary": "测试", "amount": "不是数字", "source": "test",
        })
        self.assertEqual(r.status_code, 200)

    @patch("app.chat_json")
    def test_normalize_success(self, mock_chat):
        mock_chat.return_value = {"level1": "研发费", "level2": "测试费", "confidence": 0.9, "reason": "测试"}
        r = self.client.post("/api/normalize", json={
            "summary": "买了测试工具", "amount": 500, "source": "报销",
        })
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["level1"], "研发费")

    @patch("app.chat_json")
    def test_normalize_llm_error(self, mock_chat):
        mock_chat.return_value = {"_error": "LLM timeout", "raw": ""}
        r = self.client.post("/api/normalize", json={
            "summary": "test", "amount": 100, "source": "test",
        })
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("timeout", data["error"])


class TestNormalizeBatch(FinanceAppTestCase):
    """/api/normalize/batch 测试"""

    def test_non_list_transactions(self):
        r = self.client.post("/api/normalize/batch", json={"transactions": "notalist"})
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("list", data["error"])

    @patch("app.chat_json")
    def test_batch_success(self, mock_chat):
        mock_chat.return_value = {"level1": "管理费", "level2": "办公", "confidence": 0.8, "reason": "ok"}
        r = self.client.post("/api/normalize/batch", json={
            "transactions": [
                {"summary": "办公用品", "amount": 200, "source": "报销"},
                {"summary": "午餐", "amount": 50, "source": "报销"},
            ]
        })
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["success_count"], 2)


class TestUpload(FinanceAppTestCase):
    """/api/upload 测试"""

    def test_no_file(self):
        r = self.client.post("/api/upload")
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("file", data["error"])

    def test_bad_source_type(self):
        """非法 source_type 应被白名单拒绝(需同时传文件才能过 file 检查)"""
        import io
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["摘要", "金额"])
        ws.append(["测试", 100])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        r = self.client.post("/api/upload", data={
            "source_type": "恶意",
            "file": (buf, "test.xlsx"),
        }, content_type="multipart/form-data")
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("source_type", data["error"])


class TestReportPreview(FinanceAppTestCase):
    """/api/report/preview 测试"""

    def test_empty_db(self):
        r = self.client.get("/api/report/preview")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["total_count"], 0)
        self.assertEqual(data["total_amount"], 0)

    def test_with_data(self):
        self._insert_mock_data(5)
        r = self.client.get("/api/report/preview")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["total_count"], 5)
        self.assertGreater(data["total_amount"], 0)
        self.assertIn("研发费", data["summary"])
        self.assertIn("管理费", data["summary"])


class TestPages(FinanceAppTestCase):
    """页面渲染测试"""

    def test_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Agent", r.data)

    def test_upload_page(self):
        r = self.client.get("/upload")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"escapeHtml", r.data)

    def test_report_page(self):
        r = self.client.get("/report")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"escapeHtml", r.data)

    def test_performance_page(self):
        r = self.client.get("/performance")
        self.assertEqual(r.status_code, 200)
        self.assertIn("绩效试算".encode(), r.data)


class TestPerformance(FinanceAppTestCase):
    """D5: 绩效试算测试"""

    def test_rules_loaded(self):
        """默认 8 条绩效规则应在建表时插入"""
        r = self.client.get("/api/performance/rules")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(len(data["rules"]), 8)
        depts = {rule["department"] for rule in data["rules"]}
        self.assertIn("研发部", depts)
        self.assertIn("销售部", depts)

    def test_calculate_empty_db(self):
        """空流水也应返回结果(达成率 0)"""
        r = self.client.get("/api/performance/calculate")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["total_count"], 0)
        self.assertEqual(data["total_amount"], 0)
        self.assertGreater(len(data["departments"]), 0)
        # 所有部门达成率应为 0
        for dept in data["departments"]:
            self.assertEqual(dept["achievement_rate"], 0.0)
            self.assertEqual(dept["status"], "未达标")

    def test_calculate_with_data(self):
        """有流水后应计算达成率"""
        self._insert_mock_data(5)
        r = self.client.get("/api/performance/calculate")
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["total_count"], 5)
        self.assertGreater(data["total_amount"], 0)
        self.assertGreater(data["total_bonus"], 0)

    def test_filter_by_department(self):
        """按部门筛选应只返回该部门"""
        self._insert_mock_data(5)
        r = self.client.get("/api/performance/calculate?department=研发部")
        data = r.get_json()
        self.assertTrue(data["ok"])
        for dept in data["departments"]:
            self.assertEqual(dept["department"], "研发部")

    def test_bonus_calculation(self):
        """奖金 = bonus_base × coefficient × clamp(达成率, 0, 1.5)"""
        self._insert_mock_data(5)
        r = self.client.get("/api/performance/calculate")
        data = r.get_json()
        for dept in data["departments"]:
            # 用原始数据反算,避免 achievement_rate round 精度损失
            rate = dept["actual_amount"] / dept["target_amount"] if dept["target_amount"] > 0 else 0
            clamped = max(0, min(1.5, rate))
            expected = dept["bonus_base"] * dept["coefficient"] * clamped
            self.assertAlmostEqual(dept["bonus"], round(expected, 2), places=2)

    def test_feishu_no_chat_id(self):
        """推飞书缺 chat_id 应报错"""
        r = self.client.post("/api/performance/feishu", json={})
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertIn("chat_id", data["error"])


class TestWebhook(FinanceAppTestCase):
    """/webhook 测试"""

    def test_challenge(self):
        r = self.client.post("/webhook", json={"challenge": "test123"})
        data = r.get_json()
        self.assertEqual(data["challenge"], "test123")

    def test_empty_event(self):
        r = self.client.post("/webhook", json={"event": {}})
        data = r.get_json()
        self.assertTrue(data["ok"])

    @patch("app.feishu_send_text")
    def test_handle_help(self, mock_send):
        """帮助指令应调用飞书发送"""
        mock_send.return_value = {"ok": True}
        finance_app._handle_feishu_message("帮助", "oc_test")
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        self.assertIn("指令列表", sent_text)

    @patch("app.feishu_send_text")
    def test_handle_report(self, mock_send):
        """管报指令应调用飞书发送(无数据时返回提示)"""
        mock_send.return_value = {"ok": True}
        finance_app._handle_feishu_message("管报", "oc_test")
        mock_send.assert_called_once()

    @patch("app.feishu_send_text")
    def test_handle_performance(self, mock_send):
        """绩效指令应调用飞书发送"""
        mock_send.return_value = {"ok": True}
        self._insert_mock_data(3)
        finance_app._handle_feishu_message("绩效", "oc_test")
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        self.assertIn("绩效", sent_text)

    @patch("app.feishu_send_text")
    def test_handle_unknown(self, mock_send):
        """未知指令应回复提示"""
        mock_send.return_value = {"ok": True}
        finance_app._handle_feishu_message("随便说", "oc_test")
        mock_send.assert_called_once()
        sent_text = mock_send.call_args[0][1]
        self.assertIn("帮助", sent_text)

    def test_webhook_dedup(self):
        """同一 message_id 发两次应去重"""
        payload = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {"message": {
                "message_id": "dedup_test_001",
                "chat_id": "oc_test",
                "content": '{"text":"帮助"}',
            }},
        }
        with patch("app.feishu_send_text") as mock_send:
            mock_send.return_value = {"ok": True}
            r1 = self.client.post("/webhook", json=payload)
            d1 = r1.get_json()
            r2 = self.client.post("/webhook", json=payload)
            d2 = r2.get_json()
            # 第二次应标记去重
            self.assertTrue(d2.get("dedup"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
