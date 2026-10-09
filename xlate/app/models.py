"""models —— Pydantic 契约层。

放在这里的校验是**入口防线**：不合法的东西在进 service 之前就被挡住。
但注意：入口校验不能替代 core.merge_senses 的整批校验，
因为 LLM 返回的内容要经过同一套不变量（§4.2 #6）。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from . import core

Src = Literal["curated", "api"]
Source = Literal["glossary", "llm"]


# ============================================================
# 输出
# ============================================================
class SenseOut(BaseModel):
    """一条义项。

    `fields` 是规范领域列表（"系统/并发" 已拆成 ["系统","并发"]），
    `aspect` 是细分标签（如 "事件"、"容器"），用于区分归并后同领域的义项。
    """
    fields: list[str]
    aspect: str = ""
    zh: str
    note: str = ""
    src: Src = "api"

    @property
    def field(self) -> str:
        """展示用：领域 + 细分标签。"""
        base = "/".join(self.fields)
        return f"{base}/{self.aspect}" if self.aspect else base

    @classmethod
    def of(cls, s: dict) -> "SenseOut":
        return cls(fields=list(s.get("fields") or []),
                   aspect=s.get("aspect") or "",
                   zh=s.get("zh") or "",
                   note=s.get("note") or "",
                   src=s.get("src") or "api")


class LookupOut(BaseModel):
    """GET /api/lookup 的响应。

    `source` 必须如实返回，让前端看得见缓存是否生效（README §6.3 关键约束）。
    """
    term: str                 # 归一化后的词
    raw: str                  # 用户原始输入
    source: Source            # glossary=本地命中 / llm=本次调用模型
    cached: bool              # llm 时：是否已写入词库（下次即 glossary）
    senses: list[SenseOut]
    matched_key: str | None = None
    stats: dict[str, Any] | None = None


class Counts(BaseModel):
    terms: int
    senses: int
    polysemous: int


class GlossaryOut(BaseModel):
    counts: Counts
    terms: dict[str, list[SenseOut]]


class MutationOut(BaseModel):
    ok: bool = True
    added: int = 0
    skipped: int = 0
    merged: int = 0
    rejected: list[str] = Field(default_factory=list)
    key: str | None = None
    stats: dict[str, Any] | None = None


class FieldStat(BaseModel):
    field: str
    senses: int


class StatsOut(BaseModel):
    counts: Counts
    fields: list[FieldStat]
    src_distribution: dict[str, int]
    canonical_fields: list[str]
    unknown_fields: list[str] = Field(default_factory=list)


class HealthOut(BaseModel):
    ok: bool = True
    glossary_path: str
    terms: int
    llm_configured: bool


# ============================================================
# 输入
# ============================================================
class SenseIn(BaseModel):
    """写入用的义项。

    `field` 允许 "系统/并发" 这种复合写法，服务端按 core.split_field 拆分：
    能解析成规范领域的进 `fields`，其余进 `aspect`（信息不丢，见 core.split_field）。
    也可以直接给 `fields` + `aspect`，两者都给时以 `field` 为准。
    """
    field: str | None = None
    fields: list[str] | None = None
    aspect: str = ""
    zh: str
    note: str | None = None
    src: Src = "api"

    @field_validator("zh")
    @classmethod
    def _zh(cls, v: str) -> str:
        return core.validate_zh(v)

    @field_validator("note")
    @classmethod
    def _note(cls, v: str | None) -> str:
        return core.validate_note(v)

    @field_validator("aspect")
    @classmethod
    def _aspect(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > core.MAX_FIELD_LEN:
            raise ValueError(f"aspect 过长（{len(v)} > {core.MAX_FIELD_LEN}）")
        return v

    def to_payload(self) -> dict:
        """转成 core.normalize_sense 能吃的形状。

        规则（顺序很重要）：
          1. 显式给了 `fields` → 直接用，配上传入的 `aspect`（编辑器走这条，
             保证「载入 → 编辑 → 保存」逐字往返）
          2. 只给了 `field`，且 `aspect` 为空 → 让 core 去拆 "系统/并发"
          3. 只给了 `field`，但**同时显式给了 aspect** → 把 field 当领域、
             aspect 原样保留（`field="网络", aspect="协议"` 必须留住「协议」）
        """
        fields = [str(f).strip() for f in (self.fields or []) if str(f).strip()]
        aspect = (self.aspect or "").strip()

        if fields:
            return {"fields": fields, "aspect": aspect, "zh": self.zh,
                    "note": self.note, "src": self.src}

        raw_field = (self.field or "").strip()
        if not raw_field:
            raise core.InvariantError("义项必须给出 field 或 fields")

        if aspect:
            # field 当作（可能是复合的）领域写法，拆出来的 aspect 丢弃、用显式的
            domains, _derived = core.split_field(raw_field)
            return {"fields": domains, "aspect": aspect, "zh": self.zh,
                    "note": self.note, "src": self.src}

        return {"field": raw_field, "zh": self.zh, "note": self.note, "src": self.src}


class TermIn(BaseModel):
    key: str
    senses: list[SenseIn] = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        k, _ = core.validate_key(v)
        return k


class GlossaryIn(BaseModel):
    """POST /api/glossary 的 body：{ "terms": { "<key>": [ {...} ] } }"""
    terms: dict[str, list[SenseIn]]

    @field_validator("terms")
    @classmethod
    def _terms(cls, v: dict[str, list[SenseIn]]) -> dict[str, list[SenseIn]]:
        if not v:
            raise ValueError("terms 不能为空")
        for k in v:
            core.validate_key(k)
        return v


class ImportIn(BaseModel):
    """导入既有 glossary.json。允许宽松：未知 field 保留原值。"""
    terms: dict[str, list[dict]]
    meta: dict[str, Any] | None = None


class ReplacementIn(BaseModel):
    """编辑器保存用：整体替换某个词的义项列表。

    `senses` 是**权威列表** —— 不在里面的义项会被删除（响应里的
    `removed` 会如实报告，前端据此提示）。
    """
    key: str
    senses: list[SenseIn] = Field(min_length=1)

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        k, _ = core.validate_key(v)
        return k
