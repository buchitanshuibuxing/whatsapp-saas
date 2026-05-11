#!/usr/bin/env python3
"""
WhatsApp SaaS — 翻译引擎抽象层
9 个翻译引擎：Google / Baidu / DeepL / Qwen / DeepSeek / GLM / Moonshot / OpenAI / Claude
支持多引擎 fallback、SHA256 缓存、租户自定义配置
"""

import hashlib
import json
import sqlite3
import time
import urllib.request
import urllib.parse
import urllib.error
import ssl

# ============ 翻译引擎基类 ============

class TranslationEngine:
    """翻译引擎基类。所有引擎继承此类。"""
    name = "base"
    label = "Base"

    def __init__(self, api_key=None, api_secret=None):
        self.api_key = api_key
        self.api_secret = api_secret

    def translate(self, text, target_lang, source_lang="auto"):
        """返回 translated_text 字符串。子类必须实现。"""
        raise NotImplementedError

    def is_configured(self):
        """检查引擎是否已配置（有必要的 API key）。"""
        return True


# ============ Google 免费翻译 ============

class GoogleTranslate(TranslationEngine):
    name = "google"
    label = "Google 免费翻译"

    def translate(self, text, target_lang, source_lang="auto"):
        base_url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": source_lang,
            "tl": target_lang,
            "dt": "t",
            "q": text
        }
        url = base_url + "?" + urllib.parse.urlencode(params)

        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        data = json.loads(resp.read().decode())

        # Google 返回格式: [[["translated text", "original", ...]], ...]
        try:
            parts = []
            for segment in data[0]:
                if segment and segment[0]:
                    parts.append(segment[0])
            return "".join(parts)
        except (IndexError, TypeError):
            raise Exception("Google 翻译解析失败")


# ============ 百度翻译 API ============

class BaiduTranslate(TranslationEngine):
    name = "baidu"
    label = "百度翻译 API"

    def is_configured(self):
        return bool(self.api_key and self.api_secret)

    def translate(self, text, target_lang, source_lang="auto"):
        if not self.is_configured():
            raise Exception("百度翻译未配置 API Key")

        # 百度语言代码映射
        baidu_lang_map = {"zh": "zh", "en": "en", "ja": "jp", "ko": "kor",
                          "fr": "fra", "de": "de", "es": "spa", "ar": "ara",
                          "ru": "ru", "pt": "pt", "it": "it", "auto": "auto"}
        to_lang = baidu_lang_map.get(target_lang, target_lang)
        from_lang = baidu_lang_map.get(source_lang, source_lang)

        salt = str(int(time.time() * 1000))
        sign_str = self.api_key + text + salt + self.api_secret
        sign = hashlib.md5(sign_str.encode()).hexdigest()

        params = {
            "q": text,
            "from": from_lang,
            "to": to_lang,
            "appid": self.api_key,
            "salt": salt,
            "sign": sign
        }
        url = "https://fanyi-api.baidu.com/api/trans/vip/translate?" + urllib.parse.urlencode(params)

        ctx = ssl.create_default_context()
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        data = json.loads(resp.read().decode())

        if "error_code" in data:
            error_msgs = {
                "52000": "百度翻译：成功（无错误）",
                "52001": "百度翻译：请求超时",
                "52002": "百度翻译：系统错误",
                "52003": "百度翻译：未授权用户（API Key 错误）",
                "54000": "百度翻译：必填参数为空",
                "54001": "百度翻译：签名错误",
                "54003": "百度翻译：访问频率受限",
                "54004": "百度翻译：账户余额不足",
                "54005": "百度翻译：长文本请求频繁",
                "58000": "百度翻译：客户端 IP 非法",
                "58001": "百度翻译：译文语言不支持",
            }
            msg = error_msgs.get(str(data["error_code"]), f"百度翻译错误: {data['error_code']}")
            raise Exception(msg)

        try:
            result = []
            for item in data.get("trans_result", []):
                result.append(item.get("dst", ""))
            return "\n".join(result)
        except:
            raise Exception("百度翻译解析失败")


# ============ DeepL API ============

class DeepLTranslate(TranslationEngine):
    name = "deepl"
    label = "DeepL API"

    def is_configured(self):
        return bool(self.api_key)

    def translate(self, text, target_lang, source_lang="auto"):
        if not self.is_configured():
            raise Exception("DeepL 未配置 API Key")

        # DeepL 语言代码: ZH, EN, JA, KO, FR, DE, ES, AR, ...
        deepl_lang = target_lang.upper()
        if deepl_lang == "ZH":
            deepl_lang = "ZH"
        if source_lang == "auto":
            source_lang = ""

        params = urllib.parse.urlencode({
            "text": text,
            "target_lang": deepl_lang,
            "source_lang": source_lang.upper() if source_lang else ""
        }).encode()

        # 使用免费 API 端点
        endpoint = "https://api-free.deepl.com/v2/translate" if "free" in (self.api_secret or "") else "https://api.deepl.com/v2/translate"

        ctx = ssl.create_default_context()
        req = urllib.request.Request(endpoint, data=params, headers={
            "Authorization": f"DeepL-Auth-Key {self.api_key}",
            "Content-Type": "application/x-www-form-urlencoded"
        })
        resp = urllib.request.urlopen(req, timeout=15, context=ctx)
        data = json.loads(resp.read().decode())

        try:
            translations = data.get("translations", [])
            return translations[0]["text"] if translations else text
        except:
            raise Exception("DeepL 翻译解析失败")


# ============ OpenAI 兼容接口基类（国内大模型共用） ============

class OpenAICompatibleTranslate(TranslationEngine):
    """通用 OpenAI Chat Completions 兼容翻译引擎。
    所有国内大模型（DeepSeek/Qwen/GLM/Moonshot）都兼容此接口。"""

    def __init__(self, api_key=None, api_secret=None, api_base=None, model=None):
        super().__init__(api_key, api_secret)
        self.api_base = api_base or self.default_api_base
        self.model = model or self.default_model

    @property
    def default_api_base(self):
        return ""

    @property
    def default_model(self):
        return ""

    def is_configured(self):
        return bool(self.api_key)

    def translate(self, text, target_lang, source_lang="auto"):
        if not self.is_configured():
            raise Exception(f"{self.label} 未配置 API Key")

        lang_names = {
            "zh": "Chinese", "en": "English", "ja": "Japanese",
            "ko": "Korean", "fr": "French", "de": "German",
            "es": "Spanish", "ar": "Arabic", "ru": "Russian",
            "pt": "Portuguese", "it": "Italian"
        }
        tl_name = lang_names.get(target_lang, target_lang)

        system_prompt = (
            f"You are a professional translator. "
            f"Translate the following text to {tl_name}. "
            f"Output ONLY the translated text, no explanations, no quotes, no prefixes."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]

        body = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 4000,
        }

        endpoint = self.api_base.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        data_bytes = json.dumps(body).encode()
        ctx = ssl.create_default_context()
        req = urllib.request.Request(endpoint, data=data_bytes, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=30, context=ctx)
        result = json.loads(resp.read().decode())

        try:
            return result["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError):
            raise Exception(f"{self.label} 翻译响应解析失败: {json.dumps(result)[:200]}")


# ============ 国内大模型 ============

class DeepSeekTranslate(OpenAICompatibleTranslate):
    name = "deepseek"
    label = "DeepSeek"
    default_api_base = "https://api.deepseek.com/v1"
    default_model = "deepseek-chat"


class QwenTranslate(OpenAICompatibleTranslate):
    name = "qwen"
    label = "通义千问 (阿里云)"
    default_api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    default_model = "qwen-turbo"


class GLMTranslate(OpenAICompatibleTranslate):
    name = "glm"
    label = "智谱 GLM"
    default_api_base = "https://open.bigmodel.cn/api/paas/v4"
    default_model = "glm-4-flash"


class MoonshotTranslate(OpenAICompatibleTranslate):
    name = "moonshot"
    label = "月之暗面 Moonshot (Kimi)"
    default_api_base = "https://api.moonshot.cn/v1"
    default_model = "moonshot-v1-8k"


class OpenAITranslate(OpenAICompatibleTranslate):
    name = "openai"
    label = "OpenAI"
    default_api_base = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"


# ============ Claude (Anthropic) ============

class ClaudeTranslate(TranslationEngine):
    name = "claude"
    label = "Claude (Anthropic)"

    def is_configured(self):
        return bool(self.api_key)

    def translate(self, text, target_lang, source_lang="auto"):
        if not self.is_configured():
            raise Exception("Claude 未配置 API Key")

        lang_names = {
            "zh": "Chinese", "en": "English", "ja": "Japanese",
            "ko": "Korean", "fr": "French", "de": "German",
            "es": "Spanish", "ar": "Arabic", "ru": "Russian"
        }
        tl_name = lang_names.get(target_lang, target_lang)

        body = {
            "model": "claude-3-5-haiku-20241022",
            "max_tokens": 4000,
            "system": f"You are a professional translator. Translate to {tl_name}. Output ONLY the translated text.",
            "messages": [
                {"role": "user", "content": text}
            ]
        }

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }

        data_bytes = json.dumps(body).encode()
        ctx = ssl.create_default_context()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=data_bytes, headers=headers, method="POST")
        resp = urllib.request.urlopen(req, timeout=30, context=ctx)
        result = json.loads(resp.read().decode())

        try:
            return result["content"][0]["text"].strip()
        except (KeyError, IndexError, TypeError):
            raise Exception(f"Claude 翻译响应解析失败")


# ============ 引擎注册表 ============

ENGINE_REGISTRY = {
    "google": GoogleTranslate,
    "baidu": BaiduTranslate,
    "deepl": DeepLTranslate,
    "qwen": QwenTranslate,
    "deepseek": DeepSeekTranslate,
    "glm": GLMTranslate,
    "moonshot": MoonshotTranslate,
    "openai": OpenAITranslate,
    "claude": ClaudeTranslate,
}

ENGINE_LABELS = {name: cls.label for name, cls in ENGINE_REGISTRY.items()}


def get_engine(name, api_key=None, api_secret=None, api_base=None, model=None):
    """通过名称获取翻译引擎实例。"""
    if name not in ENGINE_REGISTRY:
        raise ValueError(f"未知翻译引擎: {name}")
    cls = ENGINE_REGISTRY[name]
    if issubclass(cls, OpenAICompatibleTranslate) and cls != OpenAITranslate:
        return cls(api_key=api_key, api_secret=api_secret, api_base=api_base, model=model)
    elif name == "openai":
        return cls(api_key=api_key, api_secret=api_secret, api_base=api_base, model=model)
    else:
        return cls(api_key=api_key, api_secret=api_secret)


# ============ 翻译管理器 ============

class TranslationManager:
    """多引擎翻译管理器，带缓存和 fallback。"""

    def __init__(self, tenant_id, db_path="/opt/whatsapp-saas/admin.db"):
        self.tenant_id = tenant_id
        self.db_path = db_path
        self._fallback_order = None
        self._settings = None

    @property
    def db(self):
        return sqlite3.connect(self.db_path)

    def _load_settings(self):
        """加载租户翻译设置和引擎配置。"""
        if self._settings is not None:
            return self._settings

        conn = self.db
        conn.row_factory = sqlite3.Row
        settings = {}

        # 租户设置
        row = conn.execute(
            "SELECT * FROM tenant_settings WHERE tenant_id=?", (self.tenant_id,)
        ).fetchone()
        if row:
            settings = dict(row)
        else:
            settings = {
                "translate_target_lang": "zh",
                "translate_engine": "google",
                "translate_fallback_order": "google,baidu,deepseek,openai",
                "receive_target_lang": "zh",
                "send_target_lang": "zh",
                "receive_engine": "google",
                "send_engine": "google",
            }

        # 翻译引擎配置
        rows = conn.execute(
            "SELECT * FROM translation_config WHERE tenant_id=? AND enabled=1 ORDER BY priority",
            (self.tenant_id,)
        ).fetchall()
        settings["engines"] = {}
        for r in rows:
            settings["engines"][r["engine"]] = dict(r)

        conn.close()
        self._settings = settings
        return settings

    def _get_fallback_order(self):
        """获取 fallback 引擎顺序列表。"""
        if self._fallback_order is not None:
            return self._fallback_order

        settings = self._load_settings()
        order = settings.get("translate_fallback_order", "google,baidu,deepseek,openai")
        self._fallback_order = [e.strip() for e in order.split(",") if e.strip()]
        return self._fallback_order

    def _check_cache(self, text, source_lang, target_lang):
        """检查翻译缓存。返回 None 表示未命中。"""
        hash_key = hashlib.sha256(f"{text}|{source_lang}|{target_lang}".encode()).hexdigest()
        conn = self.db
        row = conn.execute(
            "SELECT * FROM translation_cache WHERE source_text_hash=?",
            (hash_key,)
        ).fetchone()
        conn.close()
        if row:
            return row[5]  # translated_text (fixed: index 5 not 4)
        return None

    def _save_cache(self, text, source_lang, target_lang, translated, engine):
        """保存翻译到缓存。"""
        hash_key = hashlib.sha256(f"{text}|{source_lang}|{target_lang}".encode()).hexdigest()
        conn = self.db
        try:
            conn.execute(
                "INSERT OR IGNORE INTO translation_cache (source_text_hash, source_text, source_lang, target_lang, translated_text, engine) VALUES (?, ?, ?, ?, ?, ?)",
                (hash_key, text, source_lang, target_lang, translated, engine)
            )
            conn.commit()
        except:
            pass
        finally:
            conn.close()

    def translate(self, text, target_lang=None, source_lang="auto", preferred_engine=None, direction=None):
        """翻译单条文本。
        返回: {original, translated, source_lang, target_lang, engine, from_cache}

        流程:
        1. 检查缓存
        2. 优先使用 preferred_engine（如果有且配置正确）
        3. 按 fallback 顺序尝试引擎
        4. 成功则写入缓存
        """
        settings = self._load_settings()
        if direction == 'out':
            target_lang = target_lang or settings.get("send_target_lang", settings.get("translate_target_lang", "zh"))
        else:
            target_lang = target_lang or settings.get("receive_target_lang", settings.get("translate_target_lang", "zh"))

        # 1. 检查缓存
        cached = self._check_cache(text, source_lang, target_lang)
        if cached:
            return {
                "original": text,
                "translated": cached,
                "source_lang": source_lang,
                "target_lang": target_lang,
                "engine": "cache",
                "from_cache": True
            }

        # 1.5 方向默认引擎
        if preferred_engine is None and direction:
            if direction == 'out':
                preferred_engine = settings.get("send_engine") or preferred_engine
            else:
                preferred_engine = settings.get("receive_engine") or preferred_engine

        # 2. 确定尝试顺序
        engines_to_try = []
        if preferred_engine and preferred_engine != "auto":
            engines_to_try.append(preferred_engine)

        fallback = self._get_fallback_order()
        for eng in fallback:
            if eng not in engines_to_try and eng in ENGINE_REGISTRY:
                engines_to_try.append(eng)

        # 3. 逐个尝试
        last_error = None
        engine_configs = settings.get("engines", {})

        for eng_name in engines_to_try:
            try:
                cfg = engine_configs.get(eng_name, {})
                engine = get_engine(
                    eng_name,
                    api_key=cfg.get("api_key"),
                    api_secret=cfg.get("api_secret"),
                    api_base=cfg.get("api_base"),
                    model=cfg.get("model"),
                )

                if not engine.is_configured():
                    if eng_name == "google":
                        pass  # Google 不需要配置
                    else:
                        continue  # 跳过未配置的引擎

                translated = engine.translate(text, target_lang, source_lang)

                # 4. 写入缓存
                self._save_cache(text, source_lang, target_lang, translated, eng_name)

                return {
                    "original": text,
                    "translated": translated,
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "engine": eng_name,
                    "from_cache": False
                }

            except Exception as e:
                last_error = f"{eng_name}: {e}"
                continue

        # 所有引擎都失败
        raise Exception(f"所有翻译引擎均失败。最后错误: {last_error}")

    def batch_translate(self, texts, target_lang=None, source_lang="auto", direction=None):
        """批量翻译（用于消息列表加载时）。返回与 texts 对应的翻译列表。"""
        results = []
        for text in texts:
            try:
                result = self.translate(text, target_lang, source_lang, direction=direction)
                results.append(result)
            except Exception as e:
                results.append({
                    "original": text,
                    "translated": None,
                    "error": str(e),
                    "from_cache": False
                })
        return results


# ============ 快速测试 ============

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python3 translation_engine.py <text> [target_lang]")
        sys.exit(1)

    text = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else "zh"

    # 测试 Google（不需要 API key）
    print(f"=== Google 翻译: {text} → {target} ===")
    try:
        engine = GoogleTranslate()
        result = engine.translate(text, target)
        print(f"结果: {result}")
    except Exception as e:
        print(f"错误: {e}")

    # 测试 DeepSeek（如果有 key）
    import os
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        print(f"\n=== DeepSeek 翻译: {text} → {target} ===")
        try:
            engine = DeepSeekTranslate(api_key=deepseek_key)
            result = engine.translate(text, target)
            print(f"结果: {result}")
        except Exception as e:
            print(f"错误: {e}")
    else:
        print("\n设置 DEEPSEEK_API_KEY 环境变量可测试 DeepSeek")
