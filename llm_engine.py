#!/usr/bin/env python3
"""
WhatsApp SaaS — AI 大模型抽象层
OpenAI Chat Completions 兼容接口，支持国内大模型 + 国外模型
用于：AI 翻译、对话总结、邮件润色、报价单生成等
"""

import json
import ssl
import urllib.request
import urllib.error

# ============ 提供商注册表 ============

PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "api_base": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "description": "性价比极高，中英翻译质量好"
    },
    "qwen": {
        "name": "通义千问",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-turbo", "qwen-plus", "qwen-max", "qwq-32b"],
        "description": "阿里云大模型，中文理解能力强"
    },
    "glm": {
        "name": "智谱 GLM",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-flash", "glm-4", "glm-4-plus"],
        "description": "清华智谱，中文任务出色"
    },
    "moonshot": {
        "name": "月之暗面 Kimi",
        "api_base": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "description": "长文本处理能力强"
    },
    "openai": {
        "name": "OpenAI",
        "api_base": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"],
        "description": "GPT 系列，综合能力强"
    },
    "claude": {
        "name": "Claude (Anthropic)",
        "api_base": "https://api.anthropic.com/v1",
        "models": ["claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"],
        "description": "Anthropic Claude 系列"
    },
}


# ============ OpenAI 兼容 LLM 客户端 ============

class OpenAICompatibleLLM:
    """通用 OpenAI Chat Completions 兼容接口。
    国内大模型 (DeepSeek/Qwen/GLM/Moonshot) 全部兼容此格式。"""

    def __init__(self, api_key, api_base=None, model=None):
        self.api_key = api_key
        self.api_base = api_base
        self.model = model

    def chat(self, messages, temperature=0.3, max_tokens=2000, response_format=None):
        """调用 /v1/chat/completions，返回 message.content 字符串。
        
        Args:
            messages: [{"role":"system|user|assistant","content":"..."}, ...]
            temperature: 0-2，越低越确定
            max_tokens: 最大返回 token 数
            response_format: {"type":"json_object"} 强制 JSON 输出
        
        Returns:
            str: 响应内容
        """
        if not self.api_key:
            raise Exception("API Key 未配置")

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            body["response_format"] = response_format

        endpoint = self.api_base.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        try:
            data_bytes = json.dumps(body).encode()
            ctx = ssl.create_default_context()
            req = urllib.request.Request(endpoint, data=data_bytes, headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=60, context=ctx)
            result = json.loads(resp.read().decode())

            if "choices" in result:
                return result["choices"][0]["message"]["content"].strip()
            elif "error" in result:
                raise Exception(f"LLM API 错误: {result['error']}")

            raise Exception(f"LLM 响应格式异常: {str(result)[:300]}")
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise Exception(f"LLM HTTP {e.code}: {body[:300]}")
        except urllib.error.URLError as e:
            raise Exception(f"LLM 连接失败: {e.reason}")

    def chat_json(self, messages, temperature=0.1, max_tokens=2000):
        """调用并强制返回 JSON 对象。"""
        result = self.chat(messages, temperature, max_tokens,
                          response_format={"type": "json_object"})
        return json.loads(result)

    def chat_stream(self, messages, temperature=0.3, max_tokens=2000):
        """流式调用（生成器），后续实时翻译/打字机效果可用。"""
        self._add_stream_param(messages)
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True
        }

        endpoint = self.api_base.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        ctx = ssl.create_default_context()
        data_bytes = json.dumps(body).encode()
        req = urllib.request.Request(endpoint, data=data_bytes, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=120, context=ctx)

        for line in resp:
            line = line.decode().strip()
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    chunk = json.loads(line[6:])
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
                except json.JSONDecodeError:
                    continue


# ============ Anthropic Claude 客户端 ============

class AnthropicLLM:
    """Anthropic Claude Messages API。"""

    def __init__(self, api_key, model="claude-3-5-haiku-20241022"):
        self.api_key = api_key
        self.model = model

    def chat(self, messages, system_prompt=None, temperature=0.3, max_tokens=2000):
        """Claude Messages API 调用。"""
        if not self.api_key:
            raise Exception("Anthropic API Key 未配置")

        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [m for m in messages if m["role"] != "system"],
        }
        if system_prompt:
            body["system"] = system_prompt
        elif messages and messages[0]["role"] == "system":
            body["system"] = messages[0]["content"]

        if temperature is not None:
            body["temperature"] = temperature

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }

        try:
            data_bytes = json.dumps(body).encode()
            ctx = ssl.create_default_context()
            req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                        data=data_bytes, headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=60, context=ctx)
            result = json.loads(resp.read().decode())

            if "content" in result:
                return result["content"][0]["text"].strip()
            raise Exception(f"Claude 响应格式异常: {str(result)[:300]}")
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise Exception(f"Claude HTTP {e.code}: {body[:300]}")
        except urllib.error.URLError as e:
            raise Exception(f"Claude 连接失败: {e.reason}")


# ============ LLM 管理器 ============

class LLMManager:
    """统一的大模型调用管理器。从数据库读取租户配置。"""

    def __init__(self, tenant_id, db_path="/opt/whatsapp-saas/admin.db"):
        self.tenant_id = tenant_id
        self.db_path = db_path
        self._settings = None
        self._client = None

    def _load_settings(self):
        if self._settings is not None:
            return self._settings

        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM tenant_settings WHERE tenant_id=?", (self.tenant_id,)
        ).fetchone()
        conn.close()

        self._settings = dict(row) if row else {
            "llm_provider": "deepseek",
            "llm_model": "deepseek-chat",
            "llm_api_key": None,
            "llm_api_base": None,
        }
        return self._settings

    def get_client(self):
        """获取当前配置的大模型客户端。"""
        if self._client is not None:
            return self._client

        settings = self._load_settings()
        provider = settings.get("llm_provider", "deepseek")
        model = settings.get("llm_model", "deepseek-chat")
        api_key = settings.get("llm_api_key", "")
        api_base = settings.get("llm_api_base", "")

        if provider == "claude":
            self._client = AnthropicLLM(api_key=api_key, model=model)
        else:
            base = api_base or PROVIDERS.get(provider, {}).get("api_base", "")
            self._client = OpenAICompatibleLLM(api_key=api_key, api_base=base, model=model)

        return self._client

    def chat(self, messages, temperature=0.3, max_tokens=2000):
        """调用大模型聊天。"""
        client = self.get_client()
        return client.chat(messages, temperature, max_tokens)

    # ============ 外贸 AI 工具 ============

    def summarize_conversation(self, messages_text, language="zh"):
        """对话总结。"""
        prompt = (
            "你是一个专业的外贸助手。请用简短的要点总结以下 WhatsApp 对话的关键内容：\n\n"
            f"对话内容:\n{messages_text}\n\n"
            f"请用{'中文' if language == 'zh' else 'English'}回复，包含："
            "1) 客户需求 2) 讨论的产品 3) 下一步行动 4) 关键日期/截止时间"
        )
        return self.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=500)

    def polish_email(self, draft, tone="professional", language="en"):
        """润色外贸邮件。"""
        tone_map = {
            "professional": "专业正式",
            "friendly": "友好亲切",
            "urgent": "紧急催促",
            "follow_up": "跟进提醒"
        }
        tone_desc = tone_map.get(tone, tone)
        prompt = (
            f"你是一个外贸邮件专家。请将以下邮件草稿润色为{tone_desc}风格，"
            f"修正语法错误，优化表达。保留原意，不要添加新内容。\n\n"
            f"原草稿:\n{draft}\n\n"
            f"请直接输出{'英文' if language == 'en' else '中文'}润色后的邮件："
        )
        return self.chat([{"role": "user", "content": prompt}], temperature=0.4, max_tokens=1000)

    def generate_quotation_text(self, product_info, buyer_info, language="en"):
        """生成报价单描述文本。"""
        prompt = (
            "你是一个外贸报价专家。根据以下产品和买家信息，生成一段专业的报价说明文字。\n\n"
            f"产品信息: {product_info}\n"
            f"买家信息: {buyer_info}\n\n"
            f"用{'英文' if language == 'en' else '中文'}回复，包含：价格条款、交货期、付款方式、有效期。"
        )
        return self.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=500)

    def translate_product_description(self, text, target_lang="en"):
        """专业产品描述翻译（比通用翻译更准确）。"""
        lang_names = {"zh": "中文", "en": "English", "es": "Español", "ar": "العربية"}
        prompt = (
            f"你是一个汽车配件行业的专业翻译。请将以下产品描述翻译为{lang_names.get(target_lang, target_lang)}，"
            "保留专业术语（如 OE Number、零件编号），确保翻译准确。只输出翻译结果。\n\n"
            f"原文: {text}"
        )
        return self.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=1000)

    def extract_customer_info(self, chat_messages, language="zh"):
        """从对话中提取客户信息（用于自动建档）。"""
        prompt = (
            "你是外贸 CRM 助手。从以下 WhatsApp 对话中提取客户信息，返回 JSON：\n\n"
            f"对话:\n{chat_messages}\n\n"
            "返回格式:\n{\n"
            '  "name": "客户名称或空",\n'
            '  "country": "国家代码(CN/US/...)或空",\n'
            '  "company": "公司名或空",\n'
            '  "phone": "明确提到的电话或空",\n'
            '  "email": "明确提到的邮箱或空",\n'
            '  "interests": ["感兴趣的产品1","产品2"],\n'
            '  "buying_intent": "high/medium/low",\n'
            '  "notes": "其他重要信息"\n'
            "}\n只返回 JSON，不要其他文字。"
        )
        try:
            return self.chat([{"role": "user", "content": prompt}],
                           temperature=0.1, max_tokens=500)
        except:
            return '{"error": "提取失败"}'


# ============ 快速测试 ============

if __name__ == "__main__":
    import os
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        client = OpenAICompatibleLLM(api_key=key, api_base="https://api.deepseek.com/v1", model="deepseek-chat")
        result = client.chat([{"role": "user", "content": "Say hello in 3 languages"}])
        print(f"DeepSeek: {result}")
    else:
        print("Set DEEPSEEK_API_KEY to test")
