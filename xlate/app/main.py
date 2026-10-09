"""xlate 后端 —— FastAPI 应用。

架构（README §6.2）：
    前端(网页 / 薄用户脚本)
        │ HTTP + JSON
    本模块 ─ 查询层（词库优先 → LLM 兜底 → 校验 → 合并入库）
            ├ core      归一化 / 六条不变量 / 领域归并
            ├ glossary  两级索引 + 原子落盘
            └ llm       DeepSeek 适配层（Key 只在服务端）

设计取舍：本项目定位「一个人使用」（README §6.6），所以
  · 不做多用户/鉴权（单人本机，无越权面）
  · 不做数据库（480 词，JSON + 内存索引更快更简单，且保留可 diff 的纯文本）
  · 但**保留**「LLM 调用不阻塞事件循环」和「重复请求合并」两件事 ——
    那不是并发问题，是响应性和省 API 费用的问题。
"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from . import core, llm
from .glossary import GlossaryError, GlossaryStore, get_store
from .models import (
    Counts,
    FieldStat,
    GlossaryIn,
    GlossaryOut,
    HealthOut,
    ImportIn,
    LookupOut,
    MutationOut,
    ReplacementIn,
    SenseOut,
    StatsOut,
    TermIn,
)

log = logging.getLogger("xlate")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

MIN_TERM_LEN = 2
LEARN_TIMEOUT_S = 90.0

# 同一时刻对同一个词的未完成查询 → 共用一个 Future，
# 避免「刷新/双击/同时查同一个新词」触发多次 LLM 调用（白花钱）。
#
# 这里用 threading.Lock + concurrent.futures.Future，**不用 asyncio.Lock**：
# asyncio.Lock 绑定事件循环，跨线程/跨事件循环复用会抛
# "is bound to a different event loop"，反而静默失去去重作用（实测踩过）。
_inflight: dict[str, _cf.Future] = {}
_inflight_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = get_store()                      # 启动即载入，fail fast
    log.info("xlate 就绪：%s（%s）", store.counts(), store.path)
    if not llm.is_configured():
        log.warning("未设置 %s —— /api/lookup 的 LLM 兜底将返回 503", llm.API_KEY_ENV)
    yield


app = FastAPI(
    title="xlate —— 技术术语译查",
    description="选中英文技术词 → 先查本地词库 → 未命中才调 LLM → 结果自动入库",
    version="1.0.0",
    lifespan=lifespan,
)


def store_dep() -> GlossaryStore:
    """依赖注入，测试里可以覆盖。"""
    return get_store()


@app.middleware("http")
async def _declare_json_charset(request: Request, call_next):
    """给 JSON 响应显式补上 charset=utf-8。

    JSON 按 RFC 8259 必须是 UTF-8，浏览器不会错；但旧式客户端/工具
    （curl、部分 .NET/PowerShell 的默认解码）会按系统 ANSI 解码，
    中文就成乱码。显式声明能避免这类无谓的排查。
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("application/json") and "charset" not in ctype.lower():
        response.headers["content-type"] = "application/json; charset=utf-8"
    return response


def provider_dep() -> llm.LLMProvider:
    return llm.get_provider()


# ============================================================
# 探活
# ============================================================
@app.get("/api/health", response_model=HealthOut)
async def health(store: GlossaryStore = Depends(store_dep)) -> HealthOut:
    return HealthOut(glossary_path=str(store.path),
                     terms=len(store.keys()),
                     llm_configured=llm.is_configured())


# ============================================================
# 核心：查词（**词库优先**）
# ============================================================
@app.get("/api/lookup", response_model=LookupOut)
async def lookup(
    term: str = Query(..., min_length=1, max_length=llm.MAX_TERM_LEN),
    store: GlossaryStore = Depends(store_dep),
    provider: llm.LLMProvider = Depends(provider_dep),
) -> LookupOut:
    """词库优先，未命中才调 LLM，然后把结果**合并**入库。

    `source` 如实返回，让前端看得见缓存是否生效（README §6.3 关键约束）。
    """
    raw = term.strip()
    normalized = core.norm_term(raw)
    if len(normalized) < MIN_TERM_LEN:
        raise HTTPException(400, f"词太短（<{MIN_TERM_LEN} 字符）")

    hit_key, senses = store.lookup(normalized)
    if hit_key and senses:
        return LookupOut(term=normalized, raw=raw, source="glossary",
                         cached=True, matched_key=hit_key,
                         senses=[SenseOut.of(s) for s in senses])

    # ---- 未命中：调 LLM（事务外），并合并同一词的并发请求 ----
    try:
        senses_out, stats = await _learn(normalized, store, provider)
    finally:
        _clear_inflight(normalized)
    return LookupOut(term=normalized, raw=raw, source="llm", cached=True,
                     matched_key=normalized, stats=stats,
                     senses=[SenseOut.of(s) for s in senses_out])


async def _learn(term: str, store: GlossaryStore, provider: llm.LLMProvider
                 ) -> tuple[list[dict], dict]:
    """调 LLM 学习新词，然后校验 + 合并入库。

    同一词的并发请求会共用同一个 Future：只有第一个真正调模型，
    其余的等它的结果 —— 省 API 费用，也避免重复入库。
    """
    with _inflight_lock:
        fut = _inflight.get(term)
        if fut is None:
            fut = _cf.Future()
            _inflight[term] = fut
            owner = True
        else:
            owner = False

    if not owner:
        log.info("合并并发查询：%s", term)
        return await _await_future(fut)

    try:
        raw_senses = await provider.senses(term)
        if not raw_senses:
            raise llm.LLMError("模型没给出任何义项", 502)
        # 校验 + 合并 + 原子落盘（core 保证任一条不合格就整批拒绝）
        result = store.merge_term(term, raw_senses, default_src="api")
        key = result["key"]
        _key, merged = store.lookup(key)
        payload = (merged, result)
        fut.set_result(payload)
        return payload
    except core.InvariantError as exc:
        # 校验失败 → 词库未改动（§4.2 #6）
        err = HTTPException(422, f"模型输出未通过不变量校验：{exc}")
    except llm.LLMError as exc:
        err = HTTPException(exc.status, str(exc))
    except Exception as exc:                          # noqa: BLE001
        err = HTTPException(500, f"入库失败：{exc}")

    fut.set_exception(err)
    try:
        # 一定要取一次 fut 的结果（哪怕自己就是拥有者），否则 asyncio 会报
        # "Future exception was never retrieved"
        fut.result()
    except BaseException as exc:                      # noqa: BLE001
        raise exc from None
    raise err                                         # pragma: no cover
    # 注意：Future 的清理放在 _finally_cleanup，避免等待者拿到前就被摘掉


async def _await_future(fut: _cf.Future) -> tuple[list[dict], dict]:
    """把一个 concurrent.futures.Future 等成 asyncio 结果。

    用 shield：某个等待者被取消（例如浏览器断开）不应影响真正在干活的那个请求。
    """
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)),
                                     timeout=LEARN_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, f"等待同一词的入库结果超时（{LEARN_TIMEOUT_S:g}s）") from exc


def _clear_inflight(term: str) -> None:
    with _inflight_lock:
        _inflight.pop(term, None)


# ============================================================
# 词库：读
# ============================================================
def _subset(store: GlossaryStore, terms: dict[str, list[dict]],
            limit: int, offset: int) -> GlossaryOut:
    keys = list(terms)[offset:offset + limit]
    return GlossaryOut(
        counts=Counts(**store.counts()),
        terms={k: [SenseOut.of(s) for s in terms[k]] for k in keys},
    )


@app.get("/api/glossary", response_model=GlossaryOut)
async def list_glossary(
    q: str = Query("", description="key 或译法子串"),
    field: str = Query("", description="按领域筛选"),
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    store: GlossaryStore = Depends(store_dep),
) -> GlossaryOut:
    return _subset(store, store.search(q, field), limit, offset)


@app.get("/api/glossary/{key}", response_model=list[SenseOut])
async def get_term(key: str, store: GlossaryStore = Depends(store_dep)) -> list[SenseOut]:
    senses = store.get(key)
    if not senses:
        raise HTTPException(404, f"词库中没有 {core.norm_term(key)!r}")
    return [SenseOut.of(s) for s in senses]


# ============================================================
# 词库：写
# ============================================================
@app.post("/api/glossary", response_model=MutationOut)
async def put_term(body: TermIn, store: GlossaryStore = Depends(store_dep)) -> MutationOut:
    """写入/合并单个词。服务端先校验六条不变量，不合法就 422 并说明哪条不过。"""
    try:
        stats = store.merge_term(
            body.key, [s.to_payload() for s in body.senses], default_src="api")
    except core.InvariantError as exc:
        raise HTTPException(422, str(exc)) from exc
    return MutationOut(ok=True, added=stats["added"], skipped=stats["skipped"],
                       key=stats["key"], stats=stats)


@app.post("/api/glossary/bulk", response_model=MutationOut)
async def put_bulk(body: GlossaryIn, store: GlossaryStore = Depends(store_dep)) -> MutationOut:
    """批量写入，**全有或全无**：任一词不合格则整批拒绝、词库不动。"""
    payload = {k: [s.to_payload() for s in v] for k, v in body.terms.items()}
    try:
        result = store.bulk_merge(payload, default_src="api", strict=True)
    except core.InvariantError as exc:
        raise HTTPException(422, str(exc)) from exc
    return MutationOut(ok=True, added=result["added"], skipped=result["skipped"],
                       merged=result["merged"])


@app.post("/api/glossary/{key}/replace", response_model=MutationOut)
async def replace_term_endpoint(key: str, body: ReplacementIn,
                                store: GlossaryStore = Depends(store_dep)) -> MutationOut:
    """**整体替换**某个词的义项（编辑器保存用）。

    与 `POST /api/glossary` 的区别：那个是合并（改 zh 会变成追加新义项），
    这个是"照提交的来"。校验同样全有或全无。
    """
    path_key = core.norm_term(key)
    body_key = core.norm_term(body.key)
    if path_key != body_key:
        raise HTTPException(400, f"路径 key（{path_key!r}）与 body key（{body_key!r}）不一致")
    try:
        stats = store.replace_term(path_key, [s.to_payload() for s in body.senses])
    except core.InvariantError as exc:
        raise HTTPException(422, str(exc)) from exc
    return MutationOut(ok=True, added=stats["added"], key=stats["key"], stats=stats)


@app.delete("/api/glossary/{key}", response_model=MutationOut)
async def delete_term(key: str, store: GlossaryStore = Depends(store_dep)) -> MutationOut:
    result = store.delete(key)
    if not result["ok"]:
        raise HTTPException(404, f"词库中没有 {result['key']!r}")
    return MutationOut(ok=True, key=result["key"], stats={"removed": result["removed"]})


# ============================================================
# 导入 / 导出 / 统计
# ============================================================
@app.get("/api/export")
async def export_glossary(store: GlossaryStore = Depends(store_dep)) -> JSONResponse:
    """完整词库，可直接存成 glossary.json 进 Git。"""
    snap = store.snapshot()
    return JSONResponse(snap, headers={
        "Content-Disposition": 'attachment; filename="glossary.json"'})


@app.post("/api/import", response_model=MutationOut)
async def import_glossary(body: ImportIn, store: GlossaryStore = Depends(store_dep)) -> MutationOut:
    """合并式导入（幂等，不产生重复义项）。"""
    try:
        result = store.import_merge(body.terms, default_src="curated")
    except core.InvariantError as exc:
        raise HTTPException(422, str(exc)) from exc
    if result["rejected"]:
        raise HTTPException(422, {"rejected": result["rejected"]})
    return MutationOut(ok=True, added=result["added"], skipped=result["skipped"],
                       merged=result["merged"])


@app.get("/api/stats", response_model=StatsOut)
async def stats(store: GlossaryStore = Depends(store_dep)) -> StatsOut:
    return StatsOut(
        counts=Counts(**store.counts()),
        fields=[FieldStat(field=f, senses=n) for f, n in store.field_stats()],
        src_distribution=store.src_distribution(),
        canonical_fields=list(core.CANONICAL_FIELDS),
        unknown_fields=store.unknown_fields(),
    )


# ============================================================
# 异常处理：词库坏了要明确报，不能 500 一片黑
# ============================================================
@app.exception_handler(GlossaryError)
async def _glossary_error(_: Request, exc: GlossaryError) -> JSONResponse:
    return JSONResponse({"detail": f"词库错误：{exc}"}, status_code=500)


# ============================================================
# 前端页面
# ============================================================
_TEMPLATES = Path(__file__).resolve().parent / "templates"


def _render(name: str, **ctx) -> HTMLResponse:
    """极简模板渲染：用 str.format，避免为了两个页面引入 Jinja2。

    页面里所有 HTML 都是静态的，动态内容一律走前端 JS 的
    createElement/textContent（**零 innerHTML**，遵守 README §7.1 的 TT 纪律）。
    """
    html = (_TEMPLATES / name).read_text(encoding="utf-8")
    for k, v in ctx.items():
        html = html.replace("{{" + k + "}}", str(v))
    return HTMLResponse(html)


@app.get("/", response_class=HTMLResponse)
async def page_lookup() -> HTMLResponse:
    return _render("lookup.html", title="xlate · 查词", nav="lookup")


@app.get("/glossary", response_class=HTMLResponse)
async def page_glossary() -> HTMLResponse:
    return _render("glossary.html", title="xlate · 词库", nav="glossary")


@app.get("/edit/{key}", response_class=HTMLResponse)
async def page_edit(key: str, store: GlossaryStore = Depends(store_dep)) -> HTMLResponse:
    """编辑已有词条。key 不存在也照样渲染（由前端报错），避免刷新时丢页面。"""
    return _render("edit.html", title=f"xlate · 编辑 {core.norm_term(key)}",
                   nav="glossary", mode="edit",
                   canonical_fields=_canonical_fields_json())


@app.get("/new", response_class=HTMLResponse)
async def page_new() -> HTMLResponse:
    return _render("edit.html", title="xlate · 新建词条", nav="glossary",
                   mode="new", canonical_fields=_canonical_fields_json())


@app.get("/stats", response_class=HTMLResponse)
async def page_stats() -> HTMLResponse:
    return _render("stats.html", title="xlate · 统计", nav="stats")


def _canonical_fields_json() -> str:
    """把规范领域表渲染成 JS 数组字面量。

    用 json.dumps 保证引号/转义正确（领域名都是中文，不能手工拼字符串）。
    """
    return json.dumps(list(core.CANONICAL_FIELDS), ensure_ascii=False)
