"""core —— 归一化、领域归并、六条不变量校验。

这一层是**纯函数**，不碰文件、不碰网络，所以可以放心单独测试。
README §4.2 的六条硬性不变量全部在这里落地。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# ============================================================
# 受控领域表（治理原项目 51 个自由文本 field 的乱象）
#
# 原词库实测 51 个不同 field 取值：18 个主领域（出现 ≥10 次）
# + 24 个 "A/B" 复合标签 + 一批只出现 1 次的单例（JS/React/事件/…）。
# 归并规则见 README §4.6。
# ============================================================
CANONICAL_FIELDS: tuple[str, ...] = (
    "语言", "通用", "数据", "安全", "架构", "工程", "前端", "系统",
    "网络", "测试", "构建", "运维", "分布式", "类型", "并发", "依赖",
    "范式", "性能", "俚语",
)

# 单例/近义 → 规范领域（实测数据驱动）
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # 前端子话题
    "js": ("前端",), "react": ("前端",), "事件": ("前端",),
    "框架": ("前端",), "ui": ("前端",), "字符串": ("前端",),
    # 数据子话题
    "数据库": ("数据",), "缓存": ("数据",), "状态": ("数据",),
    # 并发子话题
    "异步": ("并发",), "线程": ("并发",),
    # 架构子话题
    "兼容层": ("架构",), "设计": ("架构",),
    # 通用
    "产品": ("通用",), "账号": ("通用",),
    # 系统子话题
    "文件系统": ("系统",), "容器": ("系统",), "版本控制": ("系统",),
    "流": ("系统",), "内存": ("系统",),
    # 运维
    "云": ("运维",), "k8s": ("运维",), "kubernetes": ("运维",), "docker": ("运维",),
    # 常见缩写/技术名（实测模型会把它们填进 field）
    "css": ("前端",), "html": ("前端",), "git": ("系统",), "sql": ("数据",),
    # Web 与前端语义重叠 → 统一到前端（原词库 Web 11 条 / 前端 28 条）
    "web": ("前端",),
}


def resolve_domain(token: str) -> str | None:
    """把单个标签解析成规范领域；解析不到返回 None。"""
    t = token.strip()
    if not t:
        return None
    if t in CANONICAL_FIELDS:
        return t
    aliases = FIELD_ALIASES.get(t.lower())
    return aliases[0] if aliases else None


# aspect 是「细分标签」（如 JS / 事件 / 容器），不是自由描述。
# 实测模型会把它填成 "编程语言/变量作用域"、"版本控制/Git" 这种长串 ——
# 那是 note 该干的事，塞进 aspect 会让去重键变得极不稳定。
MAX_ASPECT_LEN = 8


def coerce_aspect(raw: Any) -> str:
    """把 aspect 收敛成短标签。

    规则（越靠前越优先）：
      1. 去掉引号与空白
      2. 若太长且含分隔符 → 取最后一段（"编程语言/变量作用域" → "变量作用域"）
      3. 仍太长 → 取最前一段
      4. 还是太长 → 保留原样，但由 MAX_FIELD_LEN 兜底拦住

    为什么取最后一段：`"版本控制/Git"` → `"Git"`、`"编程语言/变量作用域"` →
    `"变量作用域"`，末尾那截通常才是真正的细分词。
    """
    s = str(raw if raw is not None else "").strip().strip("\"'`").strip()
    if not s or len(s) <= MAX_ASPECT_LEN:
        return s
    parts = [p.strip() for p in re.split(r"[/／|,，、]+", s) if p.strip()]
    if len(parts) > 1:
        for cand in (parts[-1], parts[0]):
            if 0 < len(cand) <= MAX_ASPECT_LEN:
                return cand
    return s


def split_field(raw: Any) -> tuple[list[str], str]:
    """把原始 field 拆成 (规范领域列表, 细分标签 aspect)。

    为什么要 aspect：领域归并会**丢信息**。实测例子 —— 原词库 bind 有三条义项
    「网络/绑定」「JS/绑定」「事件/绑定」，若把 JS 和 事件 都并成「前端」，
    这两条就变成同一个 (zh, field)，去重键撞车、语义区分也没了。

    所以：能解析成规范领域的进 fields（用于筛选/统计），
    其余原样留在 aspect（用于展示与去重），**信息不丢**。

        "系统/并发"  → (["系统","并发"], "")
        "Web/安全"   → (["前端"], "安全")        # Web 归并到前端，安全保留
        "JS"         → (["前端"], "")            # JS 是前端的明确别名，无需 aspect
        "事件"       → (["前端"], "事件")         # 事件→前端 是归并，保留原词
        "React"      → (["前端"], "")            # 明确别名
        "缓存/状态"  → (["数据"], "缓存/状态")     # 两个词都不认识 → 整体留作 aspect
    """
    if raw is None:
        return ["通用"], ""
    if isinstance(raw, (list, tuple)):
        joined = "/".join(str(x).strip() for x in raw if str(x).strip())
        return split_field(joined)

    s = str(raw).strip()
    if not s:
        return ["通用"], ""

    parts = [p.strip() for p in re.split(r"[/／|,，、]+", s) if p.strip()]
    fields: list[str] = []
    aspects: list[str] = []

    for p in parts:
        exact = p in CANONICAL_FIELDS
        alias = FIELD_ALIASES.get(p.lower())
        if exact:
            fields.append(p)
        elif alias:
            fields.append(alias[0])
            # 只有「改名型」别名才需要保留 aspect（事件→前端、JS→前端），
            # 因为原词携带了规范领域表达不出的区分。
            #   · 小写化后作为 aspect → 保证 split_field 幂等
            #     （否则 Web 和 web 会产出不同 aspect，同一含义算出两个去重键）
            #   · 别名目标与规范化后的原词相同（css→前端？否；git→系统？否）
            #     这种情况说明原词已被领域完整吸收，不必再留一份
            low = p.lower()
            if low != alias[0]:
                aspects.append(coerce_aspect(low))
        else:
            aspects.append(coerce_aspect(p))

    if not fields:
        # 一个规范领域都认不出来 → 整体当作 aspect，避免把数据写坏
        fallback = "/".join(parts) if parts else "通用"
        return ["通用"], coerce_aspect(fallback) if fallback != "通用" else ""

    # 注意：这里**不再**对拼好的 aspect 二次 coerce。
    # 每段已各自收敛过，再收敛一次会把 "版本控制/git" 砍成 "git"（实测踩过）。
    return _dedupe(fields) or ["通用"], "/".join(_dedupe(aspects))


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


# ============================================================
# §4.3 查询归一化 —— 必须与前端/用户脚本两侧一致
# ============================================================
_LEAD_JUNK = re.compile(r'^[\s"\'`(\[]+')
_TAIL_JUNK = re.compile(r'[\s"\'`)\].,;:!?]+$')
_SEPARATORS = re.compile(r"[\s_\-]+")


def norm_term(s: Any) -> str:
    """归一化一个词。

    顺序要紧：**先剥标点，再归一化分隔符**（README §4.3）。
    "tree-shaking." → "tree shaking"
    """
    s = str(s if s is not None else "").strip().lower()
    s = _LEAD_JUNK.sub("", s)
    s = _TAIL_JUNK.sub("", s)
    s = _SEPARATORS.sub(" ", s)
    return s.strip()


def loose_key(s: str) -> str:
    """两级索引的第二级：去掉所有分隔符（§4.4）。

    解决 normTerm 合不了的「连写 vs 分写」：
      backpressure ↔ back-pressure ↔ back pressure
      hotspot      ↔ hot spot
    """
    return _SEPARATORS.sub("", str(s or ""))


# ============================================================
# 六条不变量（README §4.2）
# ============================================================
# 边界按真实词库标定（实测 480 词）：zh 最长 15、note 最长 58、每词最多 3 义项。
# 留余量，但足以挡住模型跑偏（例如把整句解释塞进 zh）。
# 注意 zh 允许含括号（39/493 条有，" thunk（延迟计算的包装）" 是合法写法）。
MAX_KEY_LEN = 120
MAX_ZH_LEN = 48
MAX_FIELD_LEN = 24
MAX_NOTE_LEN = 200
MAX_SENSES_PER_TERM = 12
MAX_INCOMING_SENSES = 8

VALID_SRC = ("curated", "api")


class InvariantError(ValueError):
    """不变量被违反。调用方必须保证「最坏情况是没改动」。"""


def validate_zh(raw: Any) -> str:
    zh = str(raw if raw is not None else "").strip()
    if not zh:
        raise InvariantError("义项缺少 zh（译法）")
    if len(zh) > MAX_ZH_LEN:
        raise InvariantError(f"zh 过长（{len(zh)} > {MAX_ZH_LEN}）")
    return zh


def validate_note(raw: Any) -> str:
    note = str(raw if raw is not None else "").strip()
    if len(note) > MAX_NOTE_LEN:
        raise InvariantError(f"note 过长（{len(note)} > {MAX_NOTE_LEN}）")
    return note


def normalize_sense(s: Any, *, default_src: str = "api",
                    allow_unknown_field: bool = True) -> dict:
    """校验并归一化单条义项，返回 {fields, aspect, zh, note, src}。

    去重键是 zh + fields + aspect，所以领域归并**不会**造成语义撞车。
    """
    if not isinstance(s, dict):
        raise InvariantError("义项必须是对象")

    zh = validate_zh(s.get("zh") or s.get("translation"))
    note = validate_note(s.get("note") if s.get("note") is not None else s.get("desc"))

    # 已显式给出 fields（内部/API 直传）→ 信任并归一，不走 split_field
    if s.get("fields"):
        fields = _dedupe([str(f).strip() for f in s["fields"] if str(f).strip()])
        if not fields:
            raise InvariantError("fields 为空")
        aspect = coerce_aspect(s.get("aspect"))
    else:
        fields, aspect = split_field(s.get("field"))

    if not allow_unknown_field and aspect:
        raise InvariantError(f"无法识别的 field：{s.get('field')!r} → aspect={aspect!r}")
    if len(aspect) > MAX_FIELD_LEN:
        raise InvariantError(f"field/aspect 过长（{len(aspect)} > {MAX_FIELD_LEN}）")
    for f in fields:
        if len(f) > MAX_FIELD_LEN:
            raise InvariantError(f"field 过长（{len(f)} > {MAX_FIELD_LEN}）")

    src = s.get("src") or default_src
    if src not in VALID_SRC:
        raise InvariantError(f"src 非法：{src!r}（只能是 {' / '.join(VALID_SRC)}）")
    return {"fields": fields, "aspect": aspect, "zh": zh, "note": note, "src": src}


def validate_key(key: Any) -> tuple[str, str]:
    """校验词条 key，返回 (规范形, 去分隔符形)。

    覆盖不变量 #1~#4：
      #1 全小写   #2 规范形   #3 不动点   #4 归一后不撞车（撞车在索引层检测）
    """
    if key is None:
        raise InvariantError("key 不能为空")
    raw = str(key)
    k = norm_term(raw)
    if not k:
        raise InvariantError("key 归一化后为空")
    if len(k) > MAX_KEY_LEN:
        raise InvariantError(f"key 过长（{len(k)} > {MAX_KEY_LEN}）")
    if raw != k:
        # 不直接拒绝原始写法，但必须让调用方知道规范形 —— 写入一律用规范形
        pass
    if norm_term(k) != k:
        raise InvariantError(f"key 不是规范形（norm={norm_term(k)!r}）")
    return k, loose_key(k)


def merge_senses(existing: Iterable[dict], incoming: Iterable[Any]) -> tuple[list[dict], dict]:
    """把 incoming 合并进 existing —— **修正后的 remember 语义**。

    规则（README §3.4 + §4.2 #5）：
      · 去重键 = zh + fields + aspect（不是单独的 zh）
      · 已存在的键 → 跳过，且【保留原 src】，绝不把 curated 降级成 api
      · 新义项 → 追加，src='api'
      · 既有 curated 义项永不删除、永不覆盖
      · 任一条不合格 → 整批拒绝（保证「最坏情况=没改动」）
    """
    existing_list = [dict(s) for s in (existing or [])]

    cleaned: list[dict] = []
    rejects: list[str] = []
    for s in (incoming or []):
        try:
            cleaned.append(normalize_sense(s))
        except InvariantError as exc:
            rejects.append(str(exc))
    if rejects:
        raise InvariantError("；".join(rejects))
    if not cleaned:
        raise InvariantError("模型没给出任何有效义项")
    if len(cleaned) > MAX_INCOMING_SENSES:
        raise InvariantError(
            f"义项过多（{len(cleaned)} > {MAX_INCOMING_SENSES}），疑似模型跑偏")

    def dedupe_key(s: dict) -> tuple[str, tuple[str, ...], str]:
        return (s.get("zh", ""),
                tuple(s.get("fields") or []),
                s.get("aspect", "") or "")

    # 比较用归一化后的值：库里 zh 可能带前导空格
    # （真实数据 " thunk（延迟计算的包装）"），拿原值比会重复追加。
    seen: set[tuple] = set()
    merged: list[dict] = []
    for s in existing_list:
        try:
            n = normalize_sense(s)
        except InvariantError:
            merged.append(s)          # 既有条目不合规也保留，绝不因清洗而丢数据
            continue
        seen.add(dedupe_key(n))
        merged.append({"fields": n["fields"], "aspect": n["aspect"], "zh": n["zh"],
                       "note": n["note"], "src": n["src"]})

    added = skipped = 0
    for s in cleaned:
        k = dedupe_key(s)
        if k in seen:          # 幂等：已存在就跳过，保留原 src
            skipped += 1
            continue
        seen.add(k)
        merged.append(dict(s))
        added += 1

    if len(merged) > MAX_SENSES_PER_TERM:
        raise InvariantError(
            f"义项数超限（{len(merged)} > {MAX_SENSES_PER_TERM}）")

    stats = {
        "added": added,
        "skipped": skipped,
        "total": len(merged),
        "curated_preserved": sum(1 for s in merged if s.get("src") == "curated"),
    }
    return merged, stats
