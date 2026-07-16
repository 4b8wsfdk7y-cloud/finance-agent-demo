#!/usr/bin/env python3
"""飞书客户端 — 纯 REST API 实现(不依赖 lark-cli)

用 tenant_access_token 调飞书 OpenAPI,支持:
- send_text / send_post 发消息
- download_message_file 下载图片/文件
- list_chats 列群
- get_user_info 查用户(open_id → name)

Railway / 本地 / 服务器都能跑,只需 LARK_APP_ID + LARK_APP_SECRET 环境变量。
"""
import os
import json
import time
import requests

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
BASE = "https://open.feishu.cn/open-apis"

DEFAULT_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "")

_token_cache = {"token": None, "expires": 0}


def _get_token():
    """获取 tenant_access_token,带缓存(有效期 2 小时,提前 5 分钟刷新)"""
    if _token_cache["token"] and time.time() < _token_cache["expires"] - 300:
        return _token_cache["token"]
    r = requests.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=10,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires"] = time.time() + data.get("expire", 7200)
    return _token_cache["token"]


def _headers():
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


def send_text(chat_id, text):
    """发送纯文本消息。chat_id 可省略用 DEFAULT_CHAT_ID。"""
    chat_id = chat_id or DEFAULT_CHAT_ID
    try:
        r = requests.post(
            f"{BASE}/im/v1/messages?receive_id_type=chat_id",
            headers=_headers(),
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
            timeout=15,
        )
        data = r.json()
        if data.get("code") == 0:
            return {"ok": True}
        return {"ok": False, "error": data.get("msg", "unknown"), "raw": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_post(chat_id, title, paragraphs):
    """发送富文本消息(post 格式)
    paragraphs: [[{"tag":"text","text":"..."}], ...]
    """
    chat_id = chat_id or DEFAULT_CHAT_ID
    post_content = {"zh_cn": {"title": title, "content": paragraphs}}
    try:
        r = requests.post(
            f"{BASE}/im/v1/messages?receive_id_type=chat_id",
            headers=_headers(),
            json={
                "receive_id": chat_id,
                "msg_type": "post",
                "content": json.dumps(post_content),
            },
            timeout=30,
        )
        data = r.json()
        if data.get("code") == 0:
            return {"ok": True}
        return {"ok": False, "error": data.get("msg", "unknown"), "raw": data}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def download_message_file(message_id, file_key, file_type="file", timeout=60):
    """下载飞书消息里的图片或文件
    file_type: "file" 或 "image"
    成功返回 {"ok": True, "data": bytes}
    """
    try:
        url = f"{BASE}/im/v1/messages/{message_id}/resources/{file_key}"
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {_get_token()}"},
            params={"type": file_type},
            timeout=timeout,
        )
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        if not r.content:
            return {"ok": False, "error": "文件为空"}
        return {"ok": True, "data": r.content}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_chats():
    """列出 Bot 所在的群聊"""
    try:
        r = requests.get(
            f"{BASE}/im/v1/chats",
            headers=_headers(),
            params={"page_size": 50, "user_id_type": "open_id"},
            timeout=15,
        )
        data = r.json()
        if data.get("code") != 0:
            return {"ok": False, "error": data.get("msg", "unknown"), "raw": data}
        items = data.get("data", {}).get("items", [])
        chats = [
            {"chat_id": c.get("chat_id", ""), "name": c.get("name", ""), "type": c.get("chat_mode", "")}
            for c in items
        ]
        return {"ok": True, "chats": chats}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_user_info(open_id):
    """查飞书用户信息(open_id → name)"""
    try:
        r = requests.get(
            f"{BASE}/contact/v3/users/{open_id}",
            headers=_headers(),
            params={"user_id_type": "open_id"},
            timeout=10,
        )
        data = r.json()
        if data.get("code") == 0:
            user = data.get("data", {}).get("user", {})
            return {"ok": True, "name": user.get("name", ""), "open_id": open_id}
        return {"ok": False, "error": data.get("msg", "unknown")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# 兼容别名(财务代码用 feishu_send_text / feishu_download_file 等名称)
send_text.__name__ = "send_text"
