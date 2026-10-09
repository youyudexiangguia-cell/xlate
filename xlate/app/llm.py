"""llm —— LLM 适配层。

把「调模型」这件事收敛到一处：prompt、结构化输出、解析、重试、超时、
错误映射。换模型只改这里（README §6.2）。

关键安全点：**API Key 只在服务端**，从 .env / 环境变量读，
永不进前端或用户脚本（README §7.2）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

import httpx

log = logging.getLogger("xlate.llm")

DEFAULT_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"
API_KEY_ENV = "DEEPSEEK_API_KEY"
TIMEOUT_S = 30.0
MAX_TERM_LEN = 60          # 与用户脚本 MAX_TERM_LEN 保持一致

# README §3.5：真正的秘方。**核心要求一个字都别改** —— 少了任一条产出质量都会明显下降。
# v1.0 增补：明确 field / aspect / note 三者的分工。
#   实测教训：不写清楚，模型会把领域写成 "编程语言/变量作用域" 这种长串塞进 field，
#   让义项的去重键变得极不稳定（同一句话换个说法就是一条新义项）。
PROMPT_TEMPLATE = """你在帮一位中文母语的软件开发者读英文技术文档。
用户给的词是「{term}」，它出现在计算机语境中。请：
1. 给出该词在中国开发者社区里的惯用译法（禁止直译）
2. 一词多义时按领域分列
3. 每个义项用 5~15 字说明它出现在什么场景
4. 不解释背景，不客套

字段要求：
· field：一个**短领域名**，只填一个词，如 网络 / 数据 / 前端 / 系统 / 并发 / 安全 / 构建
  不要写成 "编程语言/变量作用域" 这种长串，也不要写句子
· note：上面第 3 条说的场景说明写在这里（这才是长文本该待的地方）
· zh：译法本身，要短，能直接用

只输出 JSON：{{"term":"…","senses":[{{"field":"…","zh":"…","note":"…"}}]}}"""


class LLMError(RuntimeError):
    """带 HTTP 语义的模型调用错误。"""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class LLMNotConfigured(LLMError):
    def __init__(self, message: str = "未配置 LLM：请在 .env 里填 DEEPSEEK_API_KEY，"
                                      "或设置同名环境变量") -> None:
        super().__init__(message, status=503)


# ============================================================
# .env 支持
#
# 目的：让 Key 可以**直接写在一个纯文本文件里**（符合"直接写进文件"的习惯），
# 但又不进源码、不进 Git。
#
# 只用标准库解析，不引入 python-dotenv：少一个依赖，而 .env 格式本身很简单。
# ============================================================
ENV_FILE = ".env"


def _env_candidates() -> list[Path]:
    """查找顺序：仓库根 → xlate/ → 当前工作目录。"""
    return [
        Path(__file__).resolve().parent.parent.parent / ENV_FILE,   # <repo>/.env
        Path(__file__).resolve().parent.parent / ENV_FILE,          # <repo>/xlate/.env
        Path.cwd() / ENV_FILE,
    ]


def _strip_trailing_comment(value: str) -> str:
    """剥掉未加引号值后面的行尾注释。

    规则：`#` 前必须是空白。若 `#` 处在**未闭合的引号内**则不算注释
    （例如 `KEY="ab#cd"` 的 # 在引号里，要保留）。
    """
    for marker in (" #", "\t#"):
        at = value.find(marker)
        if at == -1:
            continue
        head = value[:at]
        # 引号未闭合 → 这里的 # 属于值本身
        if head.count('"') % 2 or head.count("'") % 2:
            continue
        return head.rstrip()
    return value


def _parse_env_text(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        # 顺序要紧：**先去引号，再剥行尾注释**。
        # 反过来会把 `"deepseek-reasoner"   # 注释` 的引号留在值里（实测踩过）。
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = _strip_trailing_comment(value)
            # 去引号后可能又露出行尾注释，例如 `"x" # c`（前面没闭合判断时）
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
        out[key] = value
    return out


def load_env_file(path: Path | None = None, *, override: bool = False) -> Path | None:
    """把 .env 读进 os.environ，返回实际读取的文件路径（没找到则 None）。

    默认**不覆盖**已存在的环境变量 —— 临时设的环境变量仍能压过 .env。
    """
    candidates = [Path(path)] if path else _env_candidates()
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            values = _parse_env_text(cand.read_text(encoding="utf-8"))
        except OSError:
            continue
        for key, value in values.items():
            if override or key not in os.environ:
                os.environ[key] = value
        log.info("已载入 %s（%d 项）", cand, len(values))
        return cand
    return None


class LLMProvider(Protocol):
    async def senses(self, term: str) -> list[dict]: ...


def build_prompt(term: str) -> str:
    return PROMPT_TEMPLATE.format(term=str(term)[:MAX_TERM_LEN])


def is_configured() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip())


class DeepSeekProvider:
    """DeepSeek 适配器。结构化输出用 response_format=json_object。"""

    def __init__(self, api_key: str | None = None,
                 api_url: str | None = None,
                 model: str | None = None,
                 timeout: float = TIMEOUT_S) -> None:
        self.api_key = (api_key if api_key is not None
                        else os.environ.get(API_KEY_ENV, "")).strip()
        self.api_url = api_url or os.environ.get("XLATE_LLM_URL", DEFAULT_API_URL)
        self.model = model or os.environ.get("XLATE_LLM_MODEL", DEFAULT_MODEL)
        self.timeout = timeout

    async def senses(self, term: str) -> list[dict]:
        if not self.api_key:
            raise LLMNotConfigured()

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": build_prompt(term)}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.api_url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM 请求超时（{self.timeout:g}s）", 504) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 网络错误：{exc}", 502) from exc

        self._raise_for_status(resp.status_code, resp.text)

        try:
            outer = resp.json()
        except ValueError as exc:
            raise LLMError("LLM 返回不是合法 JSON", 502) from exc

        try:
            content = outer["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM 响应结构异常：取不到 choices[0].message.content", 502) from exc

        try:
            parsed = json.loads(content)
        except ValueError as exc:
            raise LLMError(f"LLM 输出的 content 不是合法 JSON：{exc}", 502) from exc

        if not isinstance(parsed, dict):
            raise LLMError("LLM 输出顶层不是对象", 502)
        raw_senses = parsed.get("senses")
        if not isinstance(raw_senses, list):
            raise LLMError("LLM 输出缺少 senses 数组", 502)
        return [s for s in raw_senses if isinstance(s, dict)]

    @staticmethod
    def _raise_for_status(status: int, body: str) -> None:
        if status == 200:
            return
        detail = str(body or "")[:160]
        if status == 401:
            raise LLMError("LLM 拒绝了 API Key（401）", 502)
        if status == 402:
            raise LLMError("LLM 账户余额不足（402）", 502)
        if status == 429:
            raise LLMError("LLM 限流（429），请稍后重试", 429)
        raise LLMError(f"LLM 返回 HTTP {status}：{detail}", 502)


# -------------------------------------------------- 测试替身
class StubProvider:
    """测试用：预置每个词的返回，不碰网络。

    用途是验证「校验 → 合并 → 落盘」这条链路，而不是验证模型本身。
    """

    def __init__(self, table: dict[str, Any] | None = None,
                 default: Any = None) -> None:
        self.table = table or {}
        self.default = default
        self.calls: list[str] = []

    async def senses(self, term: str) -> list[dict]:
        self.calls.append(term)
        value = self.table.get(term, self.default)
        if value is None:
            return []
        if isinstance(value, Exception):
            raise value
        return list(value)


# -------------------------------------------------- 依赖注入
_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        _provider = DeepSeekProvider()
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    """测试用：注入替身；传 None 恢复默认。"""
    global _provider
    _provider = provider
