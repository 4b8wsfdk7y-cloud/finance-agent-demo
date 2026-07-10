#!/usr/bin/env python3
"""CherryIN API 客户端 — LLM + Embedding"""
import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

CHERRYIN_API_KEY = os.environ.get("CHERRYIN_API_KEY", "")
CHERRYIN_BASE_URL = os.environ.get("CHERRYIN_BASE_URL", "https://express-ent-admin.cherryin.ai/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "agent/deepseek-v4-pro")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "baai/bge-m3")


def chat(messages, model=None, temperature=0.3, timeout=120):
    """调用 LLM (OpenAI 兼容)
    Args:
        messages: [{"role": "system|user|assistant", "content": "..."}]
    Returns:
        {"content": "...", "raw": {...}}
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
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"content": content, "raw": data}
    except Exception as e:
        return {"content": "", "raw": {}, "error": str(e)}


def chat_json(messages, model=None, temperature=0.1, timeout=120):
    """调用 LLM 并解析 JSON 结果"""
    result = chat(messages, model, temperature, timeout)
    if result.get("error"):
        return result
    content = result["content"]
    # 尝试提取 JSON
    try:
        # 直接解析
        return json.loads(content)
    except json.JSONDecodeError:
        # 尝试从 ```json ... ``` 中提取
        if "```json" in content:
            start = content.find("```json") + 7
            end = content.find("```", start)
            if end > start:
                return json.loads(content[start:end].strip())
        # 尝试从 { ... } 中提取
        start = content.find("{")
        end = content.rfind("}") + 1
        if end > start:
            return json.loads(content[start:end])
        return {"error": "JSON parse failed", "raw": content}


def embed(texts, model=None, timeout=60):
    """调用 Embedding API
    Args:
        texts: str 或 list[str]
    Returns:
        {"embeddings": [[...], ...], "raw": {...}}
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
        data = resp.json()
        embeddings = [item["embedding"] for item in data.get("data", [])]
        return {"embeddings": embeddings, "raw": data}
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
    }
