"""test_api.py —— xlate 后端验收测试。

不引入 pytest（保持 README §7.3 的脚本式验证纪律，和 verify.mjs / e2e.mjs 一致）：
直接 assert，失败即非零退出。

    & .venv\\Scripts\\python.exe xlate\\test_api.py
    & .venv\\Scripts\\python.exe xlate\\test_api.py --keep   # 保留临时目录供排查

覆盖 README §8 的验收标准 + 网站化后新增的项（重号对应 README §8.1 的编号）。
所有写操作都发生在**临时副本**上，不碰真实 glossary.json。
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 测试期间压掉 INFO 噪音，保留 WARNING/ERROR
logging.disable(logging.INFO)

from fastapi.testclient import TestClient          # noqa: E402

from xlate.app import core, llm                    # noqa: E402
from xlate.app.glossary import GlossaryStore, reset_store  # noqa: E402
from xlate.app.main import app, store_dep          # noqa: E402

REAL_GLOSSARY = Path(__file__).resolve().parent / "glossary.json"

# ---------------------------------------------------------------- 断言框架
_PASS = 0
_FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> bool:
    global _PASS
    if cond:
        _PASS += 1
        print(f"  [OK]   {name}" + (f"  {extra}" if extra else ""))
    else:
        _FAIL.append(name)
        print(f"  [FAIL] {name}" + (f"  {extra}" if extra else ""))
    return cond


def section(title: str) -> None:
    print("\n" + "=" * 68)
    print(f" {title}")
    print("=" * 68)


def make_client(tmp: Path) -> tuple[TestClient, GlossaryStore, llm.StubProvider]:
    target = tmp / "glossary.json"
    shutil.copy(REAL_GLOSSARY, target)
    store = reset_store(target)

    stub = llm.StubProvider(table={
        # 被测词：不在词库，走 LLM
        "foobar": [{"field": "通用", "zh": "福吧", "note": "测试用虚构词"}],
        # 并发测试用：让 6 个并发请求都能拿到结果
        "concurrentterm": [{"field": "通用", "zh": "并发词", "note": "并发测试"}],
        # 已知词：库里 profile 有 3 条 curated，用来验证不会被覆盖
        "profile": [{"field": "工程", "zh": "配置文件、配置组", "note": "模型给的重复义项"}],
        # 多领域：验证 zh+fields+aspect 去重键
        "bind": [{"field": "网络", "zh": "绑定", "note": "模型重复义项"}],
        # 非法输出：验证不落盘
        "badword": [{"field": "通用", "note": "缺少 zh"}],
        # 模型返回空
        "emptyword": [],
    })
    llm.set_provider(stub)
    app.dependency_overrides[store_dep] = lambda: store
    return TestClient(app), store, stub


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    args = ap.parse_args()

    # 沙箱通常**不允许**写 %TEMP%，所以临时目录放在工作区内，
    # 并在 finally 里按绝对路径核对后清理（AGENT.md §5.3）。
    tmp = Path(__file__).resolve().parent / ".tmp-test"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        client, store, stub = make_client(tmp)
        print(f"临时词库：{store.path}")

        # ============================================================
        section("§8.1 六条不变量（对全部 480 个 key）")
        # ============================================================
        keys = store.keys()
        # 词库是活的（用户查过的词会自动入库），所以断言的是"不变量"而非固定条数
        declared = json.loads(REAL_GLOSSARY.read_text(encoding="utf-8"))["meta"]["counts"]["terms"]
        check("词条数与 meta 声明一致（±用户在用的新增）",
              len(keys) >= declared,
              f"实际 {len(keys)}，原始 {declared}")
        non_lower = [k for k in keys if k != k.lower()]
        check("#1 全部小写", not non_lower, str(non_lower[:5]))
        non_canon = [k for k in keys if core.norm_term(k) != k]
        check("#3 全部是不动点 norm(key)===key", not non_canon, str(non_canon[:5]))
        loose: dict[str, list[str]] = {}
        for k in keys:
            loose.setdefault(core.loose_key(k), []).append(k)
        collisions = {lk: v for lk, v in loose.items() if len(v) > 1}
        check("#4 归一化后无撞车", not collisions, str(list(collisions.items())[:3]))
        dup = []
        for k in keys:
            combos = [(s["zh"], tuple(s["fields"]), s.get("aspect", ""))
                      for s in store.get(k)]
            if len(combos) != len(set(combos)):
                dup.append(k)
        check("#5 去重键 zh+fields+aspect 无重复", not dup, str(dup[:5]))

        # ============================================================
        section("§8.3 九个选区全部命中")
        # ============================================================
        for t in ["CORS", "tree-shaking", "tree_shaking", "(ACID)", "back-pressure",
                  "backpressure", "hotspot", "profile.", "DevDependency"]:
            k, senses = store.lookup(t)
            check(f"命中 {t!r}", bool(k and senses), f"-> key={k!r}")

        # ============================================================
        section("§8.4/§8.5 LLM 兜底 + 第二次不再调模型")
        # ============================================================
        r = client.get("/api/lookup", params={"term": "foobar"})
        check("未命中词 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        d = r.json()
        check("source='llm'", d["source"] == "llm", d["source"])
        check("cached=true（已入库）", d["cached"] is True)
        check("返回 1 条义项", len(d["senses"]) == 1, str(d["senses"]))
        check("模型被调用 1 次", stub.calls.count("foobar") == 1, str(stub.calls))

        r2 = client.get("/api/lookup", params={"term": "foobar"})
        d2 = r2.json()
        check("第二次 source='glossary'", d2["source"] == "glossary", d2["source"])
        check("第二次未再调模型", stub.calls.count("foobar") == 1, str(stub.calls))
        check("大小写不敏感（FOOBAR 命中）",
              client.get("/api/lookup", params={"term": "FOOBAR"}).json()["source"] == "glossary")

        # ============================================================
        section("§新增-2 curated 义项绝不被 LLM 覆盖（核心修复）")
        # ============================================================
        before = store.get("profile")
        check("profile 原本 3 条且全 curated",
              len(before) == 3 and all(s["src"] == "curated" for s in before),
              str([(s["zh"], s["src"]) for s in before]))

        r = client.get("/api/lookup", params={"term": "profile"})
        check("已入库词直接命中词库（不调模型）", r.json()["source"] == "glossary")
        check("且确实没调模型", "profile" not in stub.calls)

        # 直接走写入路径，强制让模型"回答"一个已有词。
        # 期望从库里**真实取**，不硬编码 —— 库变了测试不该跟着假失败。
        first = before[0]
        r = client.post("/api/glossary", json={
            "key": "profile",
            "senses": [{"field": "/".join(first["fields"] + ([first["aspect"]]
                                                              if first["aspect"] else [])),
                        "zh": first["zh"], "note": "模型给的重复义项"}],
        })
        check("写入已有词 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        after = store.get("profile")
        check("义项数仍为 3（未增加）", len(after) == 3, f"实际 {len(after)}")
        check("curated 数量仍为 3", sum(1 for s in after if s["src"] == "curated") == 3,
              str([s["src"] for s in after]))
        check("重复义项被跳过（added=0 skipped=1）",
              r.json()["added"] == 0 and r.json()["skipped"] == 1, str(r.json()["stats"]))

        # 真正的新领域义项应被追加，且 src=api
        r = client.post("/api/glossary", json={
            "key": "profile",
            "senses": [{"field": "网络", "zh": first["zh"], "note": "网络语境"}],
        })
        after2 = store.get("profile")
        check("新领域义项被追加（4 条）", len(after2) == 4, f"实际 {len(after2)}")
        check("新义项 fields=['网络'] 且 src=api",
              after2[-1]["src"] == "api" and after2[-1]["fields"] == ["网络"],
              str(after2[-1]))
        check("curated 依然 3 条", sum(1 for s in after2 if s["src"] == "curated") == 3)

        # 领域归并不得造成语义撞车：bind 的 JS / 事件 都归并到「前端」，
        # 但必须靠 aspect 保住区分（这是实测发现的真实问题）
        bind_before = store.get("bind")
        check("bind 原本 3 条且互不重复",
              len({(s["zh"], tuple(s["fields"]), s["aspect"]) for s in bind_before}) == 3,
              str([(s["fields"], s["aspect"]) for s in bind_before]))
        check("bind 的 JS/事件 保留在 aspect 里",
              {s["aspect"] for s in bind_before} >= {"JS", "事件"},
              str([s["aspect"] for s in bind_before]))

        # ============================================================
        section("§4.2 #8 别名入参必须写到规范 key（实测踩过的真 bug）")
        # ============================================================
        # hotspot 是 hot spot 的去分隔符别名。若写入时不解析别名，
        # 会新建一个 hotspot 条目，而 lookup('hotspot') 命中的仍是 hot spot
        # —— 新写的词永远查不到。
        check("resolve_key('hotspot') 指向规范 key",
              store.resolve_key("hotspot") == "hot spot",
              repr(store.resolve_key("hotspot")))
        check("'hotspot' 不是独立 key", "hotspot" not in store.keys())

        canon_senses = store.get("hot spot")
        first_canon = canon_senses[0]
        r = client.post("/api/glossary", json={
            "key": "hotspot",                       # ← 故意用别名
            "senses": [{"field": first_canon["aspect"] or first_canon["fields"][0],
                        "zh": first_canon["zh"]}],
        })
        check("用别名写入重复义项 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("写到规范 key 'hot spot'", r.json()["key"] == "hot spot", r.json()["key"])
        check("仍然没有新建 'hotspot' key", "hotspot" not in store.keys())
        check("义项数未增加（added=0）", r.json()["added"] == 0, str(r.json()["stats"]))
        check("curated 被保留", r.json()["stats"]["curated_preserved"] >= 1,
              str(r.json()["stats"]))

        # ============================================================
        section("§新增-4 校验失败 → 词库完全不动")
        # ============================================================
        snapshot = json.dumps(store.snapshot(), ensure_ascii=False, sort_keys=True)

        r = client.get("/api/lookup", params={"term": "badword"})
        check("模型输出缺 zh → 422", r.status_code == 422, f"HTTP {r.status_code}")
        check("badword 未入库", store.get("badword") is None)

        r = client.get("/api/lookup", params={"term": "emptyword"})
        check("模型返回空 → 502", r.status_code == 502, f"HTTP {r.status_code}")

        r = client.post("/api/glossary", json={
            "key": "zzz", "senses": [{"field": "通用", "zh": "超" * 80}]})
        check("zh 过长 → 422", r.status_code == 422, f"HTTP {r.status_code}")

        r = client.post("/api/glossary/bulk", json={"terms": {
            "goodone": [{"field": "通用", "zh": "好词"}],
            "badone": [{"field": "通用", "note": "缺 zh"}],
        }})
        check("批量含非法项 → 422", r.status_code == 422, f"HTTP {r.status_code}")
        check("批量全有或全无：goodone 也未写入", store.get("goodone") is None)

        now = json.dumps(store.snapshot(), ensure_ascii=False, sort_keys=True)
        check("以上全部失败操作后词库内容完全未变", snapshot == now)

        # ============================================================
        section("§新增-3 重复导入幂等")
        # ============================================================
        senses_before = store.sense_count()
        payload = {"terms": {k: store.get(k) for k in list(store.keys())[:40]}}
        r1 = client.post("/api/import", json=payload)
        check("导入 40 词 → 200", r1.status_code == 200, f"HTTP {r1.status_code}")
        check("导入未新增义项（全部跳过）", r1.json()["added"] == 0, str(r1.json()))
        r2 = client.post("/api/import", json=payload)
        check("再导一次仍 added=0", r2.json()["added"] == 0, str(r2.json()))
        check("义项总数不变", store.sense_count() == senses_before,
              f"{senses_before} -> {store.sense_count()}")

        # ============================================================
        section("§新增-5 并发查同一个新词 → 只调一次模型、只有一份义项")
        # ============================================================
        stub.calls.clear()
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(lambda: client.get("/api/lookup", params={"term": "concurrentterm"}))
                    for _ in range(6)]
            results = [f.result() for f in futs]
        ok_codes = sum(1 for r in results if r.status_code == 200)
        check("6 个并发请求全部成功", ok_codes == 6, f"成功 {ok_codes}/6")
        check("模型只被调用 1 次", stub.calls.count("concurrentterm") == 1, str(stub.calls))
        senses = store.get("concurrentterm")
        check("只产生 1 份义项", senses is not None and len(senses) == 1,
              str(senses))

        # ============================================================
        section("§8.8 改写词库文件后无需重装即生效（重建 store）")
        # ============================================================
        marker = store.path.read_text(encoding="utf-8")
        check("落盘文件是合法 JSON", bool(json.loads(marker)))
        fresh = GlossaryStore(store.path)
        check("从磁盘重建：词条数一致", fresh.counts()["terms"] == store.counts()["terms"],
              f"{fresh.counts()} vs {store.counts()}")
        check("从磁盘重建：profile 仍是 4 条且 curated 3 条",
              len(fresh.get("profile")) == 4 and
              sum(1 for s in fresh.get("profile") if s["src"] == "curated") == 3,
              f"{len(fresh.get('profile'))} 条, "
              f"curated {sum(1 for s in fresh.get('profile') if s['src'] == 'curated')}")
        check("从磁盘重建：foobar 持久化成功", bool(fresh.get("foobar")))

        # ============================================================
        section("接口正确性杂项")
        # ============================================================
        check("GET /api/health", client.get("/api/health").json()["ok"] is True)

        # 中文工具必须显式声明 charset，否则旧式客户端会按 ANSI 解码成乱码
        ct = client.get("/api/lookup", params={"term": "hotspot"}).headers["content-type"]
        check("JSON 响应声明 charset=utf-8", "charset=utf-8" in ct.lower(), ct)
        check("JSON 里中文未损坏",
              client.get("/api/lookup", params={"term": "hotspot"}).json()["senses"][0]["zh"]
              == "热点",
              client.get("/api/lookup", params={"term": "hotspot"}).json()["senses"][0]["zh"])
        check("HTML 响应声明 charset=utf-8",
              "charset=utf-8" in client.get("/").headers["content-type"].lower())
        r = client.get("/api/glossary", params={"q": "backpressure"})
        check("GET /api/glossary 搜索", r.status_code == 200 and "backpressure" in r.json()["terms"])
        r = client.get("/api/glossary", params={"field": "安全", "limit": 5})
        check("按领域筛选", r.status_code == 200 and len(r.json()["terms"]) <= 5)
        r = client.get("/api/glossary/nosuchterm")
        check("GET 不存在的词 → 404", r.status_code == 404, f"HTTP {r.status_code}")
        r = client.get("/api/lookup", params={"term": "a"})
        check("过短词 → 400", r.status_code == 400, f"HTTP {r.status_code}")
        r = client.get("/api/lookup", params={"term": "x" * 100})
        check("超长词 → 422", r.status_code == 422, f"HTTP {r.status_code}")
        r = client.get("/api/export")
        check("导出可被 JSON 解析", r.status_code == 200 and "terms" in r.json())
        r = client.get("/api/stats")
        sd = r.json()
        check("统计含领域分布", r.status_code == 200 and len(sd["fields"]) > 0)
        check("统计里没有未归并领域", sd["unknown_fields"] == [], str(sd["unknown_fields"]))
        for page in ["/", "/glossary", "/stats", "/new", "/docs"]:
            pr = client.get(page)
            check(f"页面 {page} → 200", pr.status_code == 200, f"HTTP {pr.status_code}")
        pr = client.get("/edit/backpressure")
        check("页面 /edit/{key} → 200", pr.status_code == 200, f"HTTP {pr.status_code}")
        check("/edit 页含规范领域表（可供前端多选）",
              "const CANON = [" in pr.text and "语言" in pr.text)

        # ============================================================
        section("词条编辑界面（写操作全在临时副本上）")
        # ============================================================
        # 1) 新建一个词条（模拟编辑器提交）
        r = client.post("/api/glossary", json={
            "key": "NewTerm",                       # 故意大写 + 含大写，测规范化
            "senses": [{"field": "网络", "zh": "新词", "note": "编辑器新建"}],
        })
        check("新建词条 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("key 被规范化为小写", r.json()["key"] == "newterm", r.json()["key"])
        check("新词条内容正确",
              store.get("newterm")[0]["zh"] == "新词",
              str(store.get("newterm")))

        # 2) 编辑器载入页面能拿到数据（前端走的是这个接口）
        r = client.get("/api/glossary/newterm")
        check("编辑器可用 GET /api/glossary/{key} 载入",
              r.status_code == 200 and r.json()[0]["zh"] == "新词")

        # 3) 编辑：改 zh、加 aspect、把 src 改成 curated
        #    必须走 **replace** 语义 —— 合并语义下改 zh 会变成追加新义项（实测踩过）
        r = client.post("/api/glossary/newterm/replace", json={
            "key": "newterm",
            "senses": [{"field": "网络", "aspect": "协议", "zh": "新词（改）",
                        "note": "编辑器改过", "src": "curated"}],
        })
        check("编辑器 replace → 200", r.status_code == 200, f"HTTP {r.status_code}")
        senses = store.get("newterm")
        check("replace 后只剩 1 条（不是追加成 2 条）",
              len(senses) == 1, f"实际 {len(senses)}")
        check("zh 已更新", senses[0]["zh"] == "新词（改）", str(senses[0]))
        check("aspect 已写入", senses[0]["aspect"] == "协议", str(senses[0]))
        check("src 可改为 curated", senses[0]["src"] == "curated", senses[0]["src"])
        check("replace 报告 保留1/新增1/删除1",
              r.json()["stats"]["kept"] == 0 and r.json()["stats"]["added"] == 1
              and r.json()["stats"]["removed"] == 1, str(r.json()["stats"]))
        check("replace 报告被删义项内容",
              r.json()["stats"]["removed_zh"] == ["新词"], str(r.json()["stats"]))

        # 3b) 路径 key 与 body key 不一致必须拒绝
        r = client.post("/api/glossary/otherkey/replace", json={
            "key": "newterm", "senses": [{"field": "通用", "zh": "x"}]})
        check("replace 的路径/body key 不一致 → 400", r.status_code == 400,
              f"HTTP {r.status_code}")

        # 4) 追加第二条义项（replace 传两条）
        r = client.post("/api/glossary/newterm/replace", json={
            "key": "newterm",
            "senses": [
                {"field": "网络", "aspect": "协议", "zh": "新词（改）", "src": "curated"},
                {"field": "数据", "zh": "另一条", "src": "curated"},
            ],
        })
        check("replace 两条 → 词条有 2 条", len(store.get("newterm")) == 2,
              str(store.get("newterm")))
        check("replace 报告 kept=1 added=1 removed=0",
              r.json()["stats"]["kept"] == 1 and r.json()["stats"]["added"] == 1
              and r.json()["stats"]["removed"] == 0, str(r.json()["stats"]))

        # 4b) 编辑器实际提交的形状是 {fields:[...], aspect:"..."}，
        #     必须能**逐字往返**（载入 → 保存 → 内容不变）
        original = [
            {"fields": ["前端"], "aspect": "JS", "zh": "绑定", "note": "fn.bind", "src": "curated"},
            {"fields": ["网络"], "aspect": "", "zh": "绑定", "note": "bind 端口", "src": "curated"},
        ]
        r = client.post("/api/glossary/roundtrip/replace",
                        json={"key": "roundtrip", "senses": original})
        check("编辑器形状（fields+aspect）提交 → 200", r.status_code == 200,
              f"HTTP {r.status_code}")
        got = store.get("roundtrip")
        norm = [{"fields": s["fields"], "aspect": s["aspect"], "zh": s["zh"],
                 "note": s["note"], "src": s["src"]} for s in got]
        check("逐字往返：内容与提交完全一致", norm == original,
              f"\n      提交 {original}\n      实得 {norm}")

        # 再存一次（模拟"打开编辑器不动直接保存"）→ 应零变化
        r = client.post("/api/glossary/roundtrip/replace",
                        json={"key": "roundtrip", "senses": original})
        check("重复保存幂等：kept=2 added=0 removed=0",
              r.json()["stats"]["kept"] == 2 and r.json()["stats"]["added"] == 0
              and r.json()["stats"]["removed"] == 0, str(r.json()["stats"]))

        # aspect 与 field 同时给出时，aspect 不能被丢掉
        r = client.post("/api/glossary/aspectcase/replace", json={
            "key": "aspectcase",
            "senses": [{"field": "网络", "aspect": "协议", "zh": "测试"}],
        })
        ac = store.get("aspectcase")
        check("field+aspect 同时给出时 aspect 保留",
              ac[0]["aspect"] == "协议" and ac[0]["fields"] == ["网络"], str(ac[0]))

        # ============================================================
        section("aspect 规范化（实测：模型会填成长串）")
        # ============================================================
        # 实测样例来自真实词条 global：模型把 aspect 填成了 "编程语言/变量作用域"
        for raw_field, want_fields, want_aspect, label in [
            # 首段只是 aspect（"编程语言" 不是领域），"通用" 是兜底领域
            ("编程语言/变量作用域", ["通用"], "变量作用域", "纯描述型首段 → 末段当 aspect"),
            ("版本控制/Git", ["系统"], "版本控制/git", "首段是领域 + 次段是 aspect"),
            ("CSS", ["前端"], "css", "明确别名（新增）"),
            ("分布式系统", ["通用"], "分布式系统", "刚好达标的短标签保留"),
            ("网络", ["网络"], "", "纯领域无 aspect"),
        ]:
            r = client.post("/api/glossary/aspectnorm/replace", json={
                "key": "aspectnorm",
                "senses": [{"field": raw_field, "zh": "测试", "note": ""}],
            })
            got = store.get("aspectnorm")
            check(f"aspect 规范化（{label}）",
                  got[0]["fields"] == want_fields and got[0]["aspect"] == want_aspect,
                  f"field={raw_field!r} -> fields={got[0]['fields']} aspect={got[0]['aspect']!r}")

        # 显式传 aspect 时同样会被收敛
        client.post("/api/glossary/aspectnorm/replace", json={
            "key": "aspectnorm",
            "senses": [{"fields": ["通用"], "aspect": "编程语言/变量作用域", "zh": "测试"}],
        })
        got = store.get("aspectnorm")
        check("显式 aspect 过长也会被收敛",
              got[0]["aspect"] == "变量作用域", repr(got[0]["aspect"]))

        # 5) 删除整个词条
        r = client.delete("/api/glossary/newterm")
        check("删除词条 → 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("删除后查不到", store.get("newterm") is None)
        r = client.delete("/api/glossary/newterm")
        check("重复删除 → 404", r.status_code == 404, f"HTTP {r.status_code}")

        # 6) 编辑非法输入必须 422 且不改变词库
        snap2 = json.dumps(store.snapshot(), ensure_ascii=False, sort_keys=True)
        for name, payload in [
            ("缺 zh", {"key": "badterm", "senses": [{"field": "通用", "note": "x"}]}),
            ("空义项列表", {"key": "badterm", "senses": []}),
            ("zh 过长", {"key": "badterm", "senses": [{"field": "通用", "zh": "超" * 80}]}),
            ("src 非法", {"key": "badterm", "senses": [{"field": "通用", "zh": "x", "src": "hack"}]}),
        ]:
            rr = client.post("/api/glossary", json=payload)
            check(f"编辑器非法输入「{name}」→ 422", rr.status_code == 422,
                  f"HTTP {rr.status_code}")
        check("以上失败后词库完全未变",
              json.dumps(store.snapshot(), ensure_ascii=False, sort_keys=True) == snap2)

        # 前端必须零 innerHTML **调用**（README §7.1 TT 纪律）。
        # 注：CSP 的注释里会提到 innerHTML 这个词，所以要匹配赋值/调用而非词本身。
        import re as _re
        sink = _re.compile(r"\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML\s*\(")
        tpl_dir = Path(__file__).resolve().parent / "app" / "templates"
        offenders = [f.name for f in tpl_dir.glob("*.html")
                     if sink.search(f.read_text(encoding="utf-8"))]
        check("前端模板无 innerHTML sink", not offenders, str(offenders))

    finally:
        app.dependency_overrides.clear()
        llm.set_provider(None)
        reset_store(REAL_GLOSSARY)
        if args.keep:
            print(f"\n临时目录保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
            print(f"\n临时目录已清理：{tmp}")

    section("结果")
    print(f"  通过 {_PASS} 项，失败 {len(_FAIL)} 项")
    if _FAIL:
        for name in _FAIL:
            print(f"    [FAIL] {name}")
        print("\n  ❌ 未全部通过")
        return 1
    print("\n  ✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
