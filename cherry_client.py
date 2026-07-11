#!/usr/bin/env python3
"""CherryIN API 客户端 — LLM + Embedding"""
import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

CHERRYIN_API_KEY = os.environ.get("CHERRYIN_API_KEY", "")
CHERRYIN_BASE_URL = os.environ.get("CHERRYIN_BASE_URL", "https://express-ent-admin.cherryin.ai/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "agent/deepseek-v4-pro")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "baai/bge-m3")


def chat(messages, model=None, temperature=0.3, timeout=120):
    """调用 LLM (OpenAI 兼容)
    Args:
        messages: [{"role": "system|user|assistant", "content": "..."}]
    Returns:
        {"content": "...", "raw": {...}} 成功
        {"content": "", "raw": {...}, "error": "..."} 失败
    """
    try:
        resp = requests.post(
            f"{CHERRYIN_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {CHERRYIN_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model or LLM_MODEL,
                "messages": messages,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        # 检查 HTTP 错误(401/429/500 等)
        if resp.status_code != 200:
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message") or err_data.get("message") or f"HTTP {resp.status_code}"
            except Exception:
                err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return {"content": "", "raw": {}, "error": err_msg}
        data = resp.json()
        choices = data.get("choices") or [{}]
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        if not content:
            return {"content": "", "raw": data, "error": "empty content from LLM"}
        return {"content": content, "raw": data}
    except requests.exceptions.Timeout:
        return {"content": "", "raw": {}, "error": "LLM request timeout"}
    except Exception as e:
        return {"content": "", "raw": {}, "error": str(e)}


def chat_json(messages, model=None, temperature=0.1, timeout=120):
    """调用 LLM 并解析 JSON 结果(支持对象 {} 和数组 [])"""
    result = chat(messages, model, temperature, timeout)
    if result.get("error"):
        return result
    content = result["content"]

    def _try_load(s):
        try:
            return json.loads(s), True
        except json.JSONDecodeError:
            return None, False

    # 1. 直接解析
    parsed, ok = _try_load(content)
    if ok:
        return parsed

    # 2. 从 ```json ... ``` 中提取
    if "```json" in content:
        start = content.find("```json") + 7
        end = content.find("```", start)
        if end > start:
            parsed, ok = _try_load(content[start:end].strip())
            if ok:
                return parsed

    # 3. 从 { ... } 中提取(对象)
    start = content.find("{")
    end = content.rfind("}") + 1
    if end > start:
        parsed, ok = _try_load(content[start:end])
        if ok:
            return parsed

    # 4. 从 [ ... ] 中提取(数组)
    start = content.find("[")
    end = content.rfind("]") + 1
    if end > start:
        parsed, ok = _try_load(content[start:end])
        if ok:
            return parsed

    return {"error": "JSON parse failed", "raw": content[:500]}


def embed(texts, model=None, timeout=60):
    """调用 Embedding API
    Args:
        texts: str 或 list[str]
    Returns:
        {"embeddings": [[...], ...], "raw": {...}} 成功
        {"embeddings": [], "raw": {...}, "error": "..."} 失败
    """
    if isinstance(texts, str):
        texts = [texts]
    try:
        resp = requests.post(
            f"{CHERRYIN_BASE_URL}/embeddings",
            headers={
                "Authorization": f"Bearer {CHERRYIN_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model or EMBED_MODEL,
                "input": texts,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message") or err_data.get("message") or f"HTTP {resp.status_code}"
            except Exception:
                err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            return {"embeddings": [], "raw": {}, "error": err_msg}
        data = resp.json()
        embeddings = [item["embedding"] for item in data.get("data", [])]
        if not embeddings:
            return {"embeddings": [], "raw": data, "error": "empty embeddings"}
        return {"embeddings": embeddings, "raw": data}
    except requests.exceptions.Timeout:
        return {"embeddings": [], "raw": {}, "error": "embedding request timeout"}
    except Exception as e:
        return {"embeddings": [], "raw": {}, "error": str(e)}


def test_connection():
    """测试 API 连通性"""
    result = chat(
        [{"role": "user", "content": "回复 'OK'"}],
        temperature=0,
        timeout=30,
    )
    return {
        "llm_ok": not result.get("error"),
        "llm_response": result.get("content", "")[:50],
        "model": LLM_MODEL,
        "error": result.get("error", ""),
    }
