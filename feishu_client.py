#!/usr/bin/env python3
"""飞书客户端 — 通过 lark-cli 子进程发消息
lark-cli 已在服务器上配置好默认 profile,直接用子进程调用即可
"""
import os
import json
import subprocess

# 默认群聊 ID (wyl的测试群 — 默认 bot 已在群内)
DEFAULT_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_00d62cfc111423dab932a402a3965da4")

LARK_CLI = os.environ.get("LARK_CLI", "lark-cli")


def _run_lark(args, timeout=30):
    """运行 lark-cli 命令,返回解析后的 JSON"""
    try:
        r = subprocess.run(
            [LARK_CLI] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr[:500] or f"exit {r.returncode}", "stdout": r.stdout[:500]}
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            # lark-cli 成功时也可能输出非 JSON 的提示信息
            if '"ok": true' in r.stdout or '"ok":true' in r.stdout:
                return {"ok": True, "code": 0}
            return {"ok": False, "error": "non-JSON output", "stdout": r.stdout[:500]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_chats():
    """列出 Bot 所在的群聊"""
    result = _run_lark(["im", "+chat-list", "--types=group"])
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", ""), "raw": result}
    chats_data = result.get("data", {}).get("chats", [])
    chats = []
    for c in chats_data:
        chats.append({
            "chat_id": c.get("chat_id", ""),
            "name": c.get("name", ""),
            "type": c.get("chat_mode", ""),
        })
    return {"ok": True, "chats": chats}


def send_text(receive_id, text, receive_id_type="chat_id"):
    """发送纯文本消息"""
    result = _run_lark([
        "im", "+messages-send",
        "--chat-id", receive_id,
        "--text", text,
    ])
    if result.get("ok"):
        return {"ok": True, "code": 0}
    return {"ok": False, "error": result.get("error", ""), "raw": result}


def send_post(receive_id, title, paragraphs, receive_id_type="chat_id"):
    """发送富文本消息(post 格式)
    Args:
        receive_id: 群聊 ID
        title: 标题
        paragraphs: [[{"tag":"text","text":"..."}], ...] 富文本节点
    """
    post_content = {
        "zh_cn": {
            "title": title,
            "content": paragraphs,
        }
    }
    result = _run_lark([
        "im", "+messages-send",
        "--chat-id", receive_id,
        "--msg-type", "post",
        "--content", json.dumps(post_content),
    ], timeout=45)
    if result.get("ok"):
        return {"ok": True, "code": 0}
    return {"ok": False, "error": result.get("error", ""), "raw": result}


def download_message_file(message_id, file_key, file_type="file", timeout=60):
    """下载飞书消息中的图片或文件附件。

    Args:
        message_id: 消息 ID (om_xxx)
        file_key: 文件 key (file_xxx 或 img_xxx)
        file_type: "file" 或 "image"
    Returns:
        {"ok": True, "data": bytes} 或 {"ok": False, "error": str}
    """
    # lark-cli im message_resource get --message-id xxx --file-key xxx --type file|image
    import tempfile
    import os
    try:
        with tempfile.NamedTemporaryFile(suffix="_feishu_dl", delete=False) as tmp:
            tmp_path = tmp.name
        r = subprocess.run(
            [LARK_CLI, "im", "message_resource", "get",
             "--message-id", message_id,
             "--file-key", file_key,
             "--type", file_type,
             "--output", tmp_path],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr[:500] or f"exit {r.returncode}"}
        with open(tmp_path, "rb") as f:
            data = f.read()
        os.unlink(tmp_path)
        if not data:
            return {"ok": False, "error": "下载文件为空"}
        return {"ok": True, "data": data}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "下载超时"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
