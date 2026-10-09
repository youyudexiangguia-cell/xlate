"""xlate: 修正后的义项合并算法（remember 的正确语义）。

问题（原实现）：
    glossary.terms[k] = senses.map(s => ({...s, src:'api'}));
整体赋值 → ① 丢掉既有 curated 义项 ② 把 curated 降级成 api ③ 完全不校验

修正后语义：
    逐义项合并（按 zh+field 去重），永不删除、永不降级既有义项。
"""
from __future__ import annotations

import re
from typing import Any

# ---------- §4.3 查询归一化（与 README 逐字一致）----------
def norm_term(s: Any) -> str:
    s = str(s if s is not None else "").strip().lower()
    s = re.sub(r'^[\s"\'`(\[]+', "", s)
    s = re.sub(r'[\s"\'`)\].,;:!?]+$', "", s)
    s = re.sub(r"[\s_\-]+", " ", s)
    return s.strip()


def loose_key(s: str) -> str:
    """两级索引的第二级：去掉所有分隔符。"""
    return re.sub(r"[\s_\-]", "", s)


def field_label(s: dict) -> str:
    """取一条义项的领域展示标签。

    兼容两种 schema：
      · 旧版（用户脚本 / 原始 glossary.json）：{field: "工程/配置"}
      · 新版（FastAPI 后端）：{fields: ["工程"], aspect: "配置"}
    这个文件是**独立参考实现**，两边都要能读。
    """
    if "field" in s and s["field"] is not None:
        return str(s["field"])
    fields = s.get("fields") or []
    base = "/".join(fields)
    aspect = s.get("aspect") or ""
    return f"{base}/{aspect}" if aspect else base


# 边界按真实词库标定（实测）：zh 最长 15、field 最长 8、note 最长 58、每词最多 3 义项。
# 留出余量，但足以挡住 LLM 跑偏（例如把整句解释塞进 zh）。
# 注意：zh 允许含括号（39/493 条有，" thunk（延迟计算的包装）" 就是合法写法），
#      所以 zh 的规则是"短"，不是"纯中文"。
MAX_ZH_LEN = 40
MAX_FIELD_LEN = 24
MAX_NOTE_LEN = 200
MAX_SENSES = 8


class Reject(Exception):
    """校验失败：调用方必须保证"最坏情况是没改动"。"""


def validate_sense(s: Any) -> dict:
    """按 §4.2 #6「先校验，后写入」校验单条义项，并返回【已归一化】的对象。

    重要：返回值里的 zh/field/note 都已 strip。去重比较必须用这个返回值，
    不能拿库里未清洗的原值去比 —— 否则 `" thunk（…）"`（带前导空格）会被判成
    新义项而重复追加。这条是实测踩出来的。
    """
    if not isinstance(s, dict):
        raise Reject("义项必须是对象")
    zh = str(s.get("zh") or s.get("translation") or "").strip()
    if not zh:
        raise Reject("义项缺少 zh（译法）")
    if len(zh) > MAX_ZH_LEN:
        raise Reject(f"zh 过长（{len(zh)} > {MAX_ZH_LEN}）：{zh[:40]!r}")
    field = field_label(s).strip()
    if field and len(field) > MAX_FIELD_LEN:
        raise Reject(f"field 过长（{len(field)}）：{field!r}")
    note = s.get("note")
    note = str(note).strip() if note is not None else ""
    if len(note) > MAX_NOTE_LEN:
        raise Reject(f"note 过长（{len(note)} > {MAX_NOTE_LEN}）")
    src = s.get("src") or "api"
    if src not in ("curated", "api"):
        raise Reject(f"src 非法：{src!r}（只能是 curated / api）")
    return {"field": field, "zh": zh, "note": note, "src": src}


def merge_senses(existing: list[dict] | None, incoming: list[Any]) -> tuple[list[dict], dict]:
    """把 incoming 合并进 existing，返回 (新列表, 统计)。

    规则（§3.4 + §4.2 #5）：
      · 去重键 = zh + field（不是单独的 zh）
      · 已存在的 (zh, field) → 跳过，且【保留原 src】，绝不降级 curated
      · 新义项 → append，src 标记为 api
      · 既有 curated 义项永不删除、永不覆盖
      · 任一条校验失败 → 整批拒绝（不产生部分写入）
    """
    existing = list(existing or [])

    # 先全部校验，任何一条不合格就整批拒绝（保证"最坏是没改动"）
    cleaned: list[dict] = []
    rejected: list[str] = []
    for s in incoming or []:
        try:
            cleaned.append(validate_sense(s))
        except Reject as e:
            rejected.append(str(e))
    if rejected:
        raise Reject("；".join(rejected))
    if not cleaned:
        raise Reject("模型没给出任何有效义项")
    # 超量直接拒绝，不静默截断 —— 静默丢弃会让调用方以为全部入库了
    if len(cleaned) > MAX_SENSES:
        raise Reject(f"义项过多（{len(cleaned)} > {MAX_SENSES}），疑似模型跑偏")

    # 去重比较用【归一化后】的 key，避免库里原值带空格导致重复追加
    seen = set()
    merged: list[dict] = []
    for s in existing:
        try:
            n = validate_sense(s)
        except Reject:
            merged.append(s)          # 既有条目即使不合规也保留，绝不因清洗而丢数据
            continue
        seen.add((n["zh"], n["field"]))
        merged.append({**s, "zh": n["zh"], "field": n["field"], "note": n["note"]})

    added = skipped = 0
    for s in cleaned:
        k = (s["zh"], s["field"])
        if k in seen:          # 幂等：已存在就跳过，保留原 src
            skipped += 1
            continue
        seen.add(k)
        merged.append({**s, "src": "api"})
        added += 1

    preserved = sum(1 for s in merged if s.get("src") == "curated")
    return merged, {"added": added, "skipped": skipped, "total": len(merged),
                    "curated_preserved": preserved}


def remember(glossary: dict, term: str, senses: list[Any]) -> dict:
    """修正后的 remember：合并而非覆盖。

    与旧实现的差异：
      · 旧：glossary['terms'][k] = senses          ← 覆盖，丢 curated
      · 新：glossary['terms'][k] = merge(existing) ← 合并，保 curated
    另修一个索引连带缺陷：若 k 是 loose 命中别名，必须把义项写到规范 key 上，
    否则 resolveKey('back-pressure') 会给出一个 lookup 查不到的 key。
    """
    k = norm_term(term)
    if not k:
        raise Reject("term 归一化后为空")
    if k != norm_term(k):
        raise Reject(f"key 非规范形：{k!r}")

    terms = glossary.setdefault("terms", {})
    canonical = k
    if k not in terms:                      # loose 别名 → 归到规范 key
        for key in terms:
            if loose_key(key) == loose_key(k):
                canonical = key
                break

    merged, stats = merge_senses(terms.get(canonical), senses)
    terms[canonical] = merged
    return {"key": canonical, **stats}


# ============================ 测试 ============================
if __name__ == "__main__":
    import json
    from collections import Counter
    from pathlib import Path

    print("=" * 66)
    print(" 1. 修复前后行为对比（用真实词库里的 'bind'）")
    print("=" * 66)
    gl = json.loads(Path("xlate/glossary.json").read_text(encoding="utf-8"))
    bind_before = gl["terms"]["bind"]
    print("修复前 bind：", len(bind_before), "条",
          Counter(s["src"] for s in bind_before))
    for s in bind_before:
        print(f"   [{field_label(s)}] {s['zh']}  src={s['src']}")

    # 模拟 LLM 只返回 1 条（网络语境的"绑定"）
    llm = [{"field": "网络", "zh": "绑定", "note": "模型给的重复义项"}]

    # --- 旧实现 ---
    old_terms = {"bind": [dict(s) for s in bind_before]}
    old_terms["bind"] = [{**s, "src": "api"} for s in llm]
    print(f"\n旧实现 remember 后：{len(old_terms['bind'])} 条 "
          f"{Counter(s['src'] for s in old_terms['bind'])}  ← ❌ 3 条 curated 全丢")

    # --- 新实现 ---
    new_gl = {"terms": {"bind": [dict(s) for s in bind_before]}}
    r = remember(new_gl, "bind", llm)
    after = new_gl["terms"]["bind"]
    print(f"新实现 remember 后：{len(after)} 条 {Counter(s['src'] for s in after)}  ← ✅")
    print("  统计:", r)

    print("\n" + "=" * 66)
    print(" 2. 幂等性（重复调 LLM 不应产生重复义项）")
    print("=" * 66)
    for i in range(3):
        rr = remember(new_gl, "bind", llm)
        print(f"  第 {i+1} 次重复: added={rr['added']} skipped={rr['skipped']} "
              f"total={rr['total']} curated={rr['curated_preserved']}")
    assert len(new_gl["terms"]["bind"]) == 3, "不应增长"
    print("  义项数稳定在 3 ✅")

    print("\n" + "=" * 66)
    print(" 3. 追加新领域义项（多义性应被保留）")
    print("=" * 66)
    r = remember(new_gl, "bind", [{"field": "数据库", "zh": "绑定", "note": "绑定参数"}])
    print("  新增数据库义项:", r)
    print("  bind 现在:", [(field_label(s), s["zh"], s["src"]) for s in new_gl["terms"]["bind"]])

    print("\n" + "=" * 66)
    print(" 4. loose 别名写入（修掉索引连带缺陷）")
    print("=" * 66)
    g2 = {"terms": {"back pressure": [{"field": "网络", "zh": "背压", "note": "x", "src": "curated"}]}}
    r = remember(g2, "back-pressure", [{"field": "系统", "zh": "背压", "note": "y"}])
    print("  term='back-pressure' -> key:", r["key"])
    print("  terms:", list(g2["terms"].keys()))
    assert r["key"] == "back pressure"
    assert len(g2["terms"]["back pressure"]) == 2
    print("  ✅ 写入规范 key，lookup 能查到")

    print("\n" + "=" * 66)
    print(" 5. 校验失败必须整批拒绝（最坏情况=没改动）")
    print("=" * 66)
    g3 = {"terms": {"foo": [{"field": "通用", "zh": "原值", "note": "", "src": "curated"}]}}
    snapshot = json.dumps(g3, ensure_ascii=False, sort_keys=True)
    bad_cases = [
        ("缺 zh", [{"field": "通用", "note": "没有译法"}]),
        ("zh 过长", [{"zh": "超" * 60}]),
        ("src 非法", [{"zh": "译", "src": "hacked"}]),
        ("非对象", ["just a string"]),
        ("空数组", []),
        ("部分合法部分非法", [{"zh": "合法"}, {"note": "缺 zh"}]),
    ]
    for name, payload in bad_cases:
        try:
            remember(g3, "foo", payload)
            print(f"  ❌ {name}: 未拦截！")
            raise SystemExit(1)
        except Reject as e:
            print(f"  ✅ {name}: 已拒绝 -> {e}")
    unchanged = json.dumps(g3, ensure_ascii=False, sort_keys=True) == snapshot
    print("  词库未被改动:", "✅" if unchanged else "❌")
    assert unchanged

    print("\n" + "=" * 66)
    print(" 6. 边界用例：库里 zh 带前导空格（真实数据 ' thunk（延迟计算的包装）'）")
    print("=" * 66)
    dirty = {"terms": {"thunk": [{"field": "语言",
                                  "zh": " thunk（延迟计算的包装）",
                                  "note": "包一层函数把计算推迟", "src": "curated"}]}}
    r = remember(dirty, "thunk", [{"field": "语言", "zh": "thunk（延迟计算的包装）",
                                   "note": "模型版本"}])
    n = len(dirty["terms"]["thunk"])
    print(f"  合并后义项数: {n} (added={r['added']} skipped={r['skipped']})")
    assert n == 1, "带前导空格的既有义项被判成新义项，发生重复追加"
    print("  ✅ 已归一化比较，未重复追加")

    print("\n" + "=" * 66)
    print(" 7. 义项超量应拒绝而非静默截断")
    print("=" * 66)
    g4 = {"terms": {}}
    try:
        remember(g4, "spam", [{"zh": f"译{i}"} for i in range(MAX_SENSES + 1)])
        print("  ❌ 未拦截超量义项")
        raise SystemExit(1)
    except Reject as e:
        print(f"  ✅ 已拒绝 -> {e}")
    assert "spam" not in g4["terms"], "拒绝后不应落盘"
    print("  ✅ 且未落盘")

    print("\n" + "=" * 66)
    print(" 8. 全量真实词库回归：不变量 + 模拟合并 480 词")
    print("=" * 66)
    terms = gl["terms"]
    assert all(k == norm_term(k) for k in terms), "存在非规范形 key"
    print("  normTerm(key) === key 对全部", len(terms), "个 key 成立 ✅")

    test_gl = {"terms": json.loads(json.dumps(terms))}
    before_counts = Counter(s["src"] for v in test_gl["terms"].values() for s in v)
    changed = 0
    for k in list(terms):
        llm_like = [{"field": field_label(s) or "通用", "zh": s["zh"], "note": "模型版本"} for s in terms[k]]
        before = len(test_gl["terms"][k])
        rr = remember(test_gl, k, llm_like)
        if rr["added"] or rr["skipped"]:
            changed += 1
        assert len(test_gl["terms"][k]) == before, f"{k} 义项数变了"
    after_counts = Counter(s["src"] for v in test_gl["terms"].values() for s in v)
    print(f"  模拟对全部 480 词调用 remember：{changed} 个词被处理")
    print(f"  src 分布 前: {dict(before_counts)}")
    print(f"  src 分布 后: {dict(after_counts)}")
    assert before_counts == after_counts, "curated 被降级了！"
    assert all(len(test_gl['terms'][k]) == len(terms[k]) for k in terms)
    print("  ✅ curated 义项零丢失、零降级、零重复")

    print("\n" + "=" * 66)
    print(" 全部通过 ✅  修正后的 remember 语义正确")
    print("=" * 66)
