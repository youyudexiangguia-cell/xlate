# xlate —— 技术术语译查

> 一个面向中文母语开发者的**术语查询工具**。
> 当前实现：**FastAPI 后端 + 网页前端**（`xlate/app/`），可选保留一个薄用户脚本。

---

## 0. 文档修订记录（v1.0）

后端落地时按实测数据校正了旧版文档里的几处说法。**以本节为准**：

| 位置 | 旧说法 | 实测事实 |
|---|---|---|
| §5 文件表 / 用户脚本头注释 | 「覆盖 18 个领域」 | 词库里 `field` 有 **51 个不同取值**；出现 ≥10 次的有 **17 个**，24 个是 `A/B` 复合标签，25 个只出现 1 次。"18" 对不上任何口径 |
| §4.4 举例 | `read-only` 因 norm 无法合并规范形而漏查 | 词库里**根本没有 `read-only` / `readonly`**（`read only` 也不是 key）。norm 对连字符的处理本身没问题 —— 真实存在的 `thread safe`（由 `thread-safe` 归并而来）、`dirty read`、`spread` 都能正常命中。原举例只是举错了词 |
| §4.4 两级索引举例 | `key = "backpressure"`，`hotspot` 漏查（key 是 `hot spot`） | `hotspot`→`hot spot` 正确；但 `backpressure` / `hot spot` **本身就是 key**。两级索引的机制描述正确，只有 key 写法写反了 |
| §4.1 数据模型 | `"<规范化 key>": [ { field, zh, note, src } ]` | v1.0 起 `field` 改成 **`fields`（数组）+ `aspect`** 两个字段，见 §4.6。去重键也从 `zh + field` 变为 `zh + fields + aspect` |
| §6 目标架构 | 「Python 后端（FastAPI 推荐；Flask 也行）」 | 已实现，见 §6.2。数据库仍未引入（JSON + 内存索引足够） |

---

## 1. 一句话定义

**选中英文技术词 → 先查本地词库 → 未命中才调 LLM → 结果自动入库。**

不是翻译器，是**术语表 + LLM 兜底**。

---

## 2. 问题陈述（最重要的一节，别跳过）

### 2.1 真正的痛点不是"看不懂英文"

`profile` 被通用翻译器译成"个人资料"，不是因为翻译引擎差，而是因为**它不知道你在读技术文档**。同理：

| 词 | 通用翻译 | 技术语境 |
|---|---|---|
| `deprecate` | 反对、谴责 | **弃用** |
| `resolve`（Promise） | 解决 | **决议、兑现** |
| `handle`（名词） | 处理 | **句柄** |
| `truncate`（SQL） | 截断 | **清空整张表** |
| `orphan` | 孤儿 | **孤儿进程 / 孤儿分支** |
| `naive` | 幼稚的 | **朴素的（未经优化），中性词** |

### 2.2 这个问题的性质决定了架构

| 维度 | 通用翻译 | 本工具 |
|---|---|---|
| 输入单位 | 句子 | **词 / 短语** |
| 目标 | 通顺 | **圈内惯用译法** |
| 答案数量 | 可以有多个 | 通常**只有一个默认** |
| **可缓存性** | 差（组合无限） | **极好（术语是有限集合）** |

> **核心推论：技术术语是一个有限集合（几百到几千个）。**
> 所以：**不该每次都调 API。应该先查本词典，查不到才调 API，然后把答案存进词典。**

这条推论是整个项目的基石。任何后续实现都必须保留它。

---

## 3. 核心设计决策（承重墙，不要改）

### 3.1 词库优先，API 兜底

```
第 1 次查 profile → 调 LLM（~1s，花钱）
第 2 次起          → 本地命中（<1ms，免费）
```

这不是优化，是**架构前提**。去掉它，项目就退化成一个普通的翻译器。

### 3.2 自增长词库

```
用 → LLM 学到新词 → 写回词库 → 越用越快越省钱
```

### 3.3 强制结构化输出

不能要一段散文。必须拿到可直接入库的结构：

```json
{ "term": "profile",
  "senses": [ { "field": "工程/配置", "zh": "配置文件、配置组", "note": "…" } ] }
```

DeepSeek 用 `response_format: { type: "json_object" }`（prompt 里必须出现 "json" 字样）。

### 3.4 `curated` / `api` 双轨标记

| src | 含义 | 处理 |
|---|---|---|
| `curated` | **人工确认过** | 长期保留 |
| `api` | 模型生成、未复核 | 可一键清空重来 |

**没有这个区分，词库会慢慢被模型幻觉污染，而且无法分辨。**

> ⚠️ **写入必须是"合并"，不是"覆盖"**（v1.0 修复的严重缺陷）
>
> 旧实现是 `glossary.terms[k] = senses.map(s => ({...s, src:'api'}))` —— 整体赋值。
> 后果：`bind` 原有 3 条 `curated` 义项（网络/JS/事件），一旦模型对它返回任何结果，
> **3 条全被替换成 1 条 `api`**，人工确认的内容直接丢失并降级，而且无法察觉。
>
> 这条同时违反了本节的"curated 长期保留"和 §4.2 #5 的去重键约定
> —— **文档写了规则，代码没实现**。
>
> 正确语义（见 §4.2 #7）：
> ```
> 对 incoming 的每条义项：
>   去重键已存在 → 跳过，【保留原 src】
>   去重键不存在 → 追加，src='api'
> 既有 curated 义项永不删除、永不降级
> ```

### 3.5 prompt 设计（真正的秘方）

```
你在帮一位中文母语的软件开发者读英文技术文档。
用户给的词是「{term}」，它出现在计算机语境中。请：
1. 给出该词在中国开发者社区里的惯用译法（禁止直译）
2. 一词多义时按领域分列
3. 每个义项用 5~15 字说明它出现在什么场景
4. 不解释背景，不客套
只输出 JSON：{"term":"…","senses":[{"field":"…","zh":"…","note":"…"}]}
```

要点：**禁止直译 + 分领域 + 结构化 + 不要客套**。少任何一条，产出质量都会明显下降。

---

## 4. 数据模型（精确规格）

### 4.1 `glossary.json`

```json
{
  "meta": {
    "version": 2,
    "updated": "2026-10-04",
    "counts": { "terms": 480, "senses": 493, "polysemous": 10 }
  },
  "terms": {
    "<规范化 key>": [
      { "fields": ["工程"], "aspect": "配置", "zh": "配置文件、配置组",
        "note": "一套命名好的配置，如 VS Code profile", "src": "curated" }
    ]
  }
}
```

字段语义：

| 字段 | 必填 | 说明 |
|---|---|---|
| `fields` | **必填** | **规范领域数组**。`"系统/并发"` → `["系统","并发"]`。用于筛选与统计 |
| `aspect` | 建议 | **细分标签**。领域归并会丢信息，原词保留在这里。见 §4.6 |
| `zh` | **必填** | 惯用译法，要短（能直接用） |
| `note` | 建议 | 「什么时候用哪个」——**这才是真正的价值**，比译法本身更重要 |
| `src` | 必填 | `curated` / `api` |

> **为什么把 `field` 拆成 `fields` + `aspect`**：实测 `bind` 的三条义项原为
> `网络` / `JS` / `事件`。若把 `JS` 和 `事件` 都归并成「前端」，这两条就成了同一个
> 去重键，**语义区分丢失、幂等去重也会误判**。拆开后互不重复：
> `(["网络"], "")`、`(["前端"], "JS")`、`(["前端"], "事件")`。

### 4.2 硬性不变量（前六条 + v1.0 新增两条）⚠️

这些是**用血换来的**，任何新实现都必须遵守：

| # | 不变量 | 违反后果 |
|---|---|---|
| 1 | **key 必须全小写** | 查询会转小写 → 大写 key（`CORS`/`ACID`/`JWT`）**永远命中不到** |
| 2 | key 必须是**规范形**：空格/下划线/连字符统一成单个空格 | 同 #1 |
| 3 | key 必须是查询归一化函数的**不动点**（`norm(key) === key`） | 保证「选区 → 命中」不漏 |
| 4 | 归一化后**不能撞车**（两个 key 归一后相同） | 其中一个永远查不到 |
| 5 | 去重键是 **`zh` + `fields` + `aspect`**，不是单独的 `zh` | `bind` 在多个语境下都译「绑定」，只用 `zh` 会丢掉义项 |
| 6 | **先校验，后写入** | 最坏情况必须是"没改动"，而不是"改坏了" |
| 7 | **写入是合并，不是覆盖**：去重键已存在则跳过并**保留原 `src`**；既有 `curated` 永不删除、永不降级 | 模型一回答就把人工确认的义项冲掉，且无法分辨（v1.0 修复的真实缺陷，见 §3.4） |
| 8 | **写入路径必须断言 `norm(key) === key`** | 别名入参会写进一个 `lookup` 查不到的 key（例：`back-pressure` 曾写入别名 key 而非规范 key） |

> #7 和 #8 是 v1.0 新增的。旧实现两条都违反：`remember()` 用整体赋值覆盖，
> 且不校验就落盘 —— 详见 §3.4 与 §7.2。

### 4.3 查询归一化（必须两侧一致）

```js
function normTerm(s) {
  return String(s).trim().toLowerCase()
    .replace(/^[\s"'`(\[]+/, '')          // 剥开头引号括号
    .replace(/[\s"'`)\].,;:!?]+$/, '')    // 剥结尾标点
    .replace(/[\s_\-]+/g, ' ')            // 分隔符归一
    .trim();
}
```

**顺序要紧：先剥标点，再归一化分隔符**（`"tree-shaking."` → `tree shaking`）。

### 4.4 两级索引（解决「连写 vs 分写」）

`normTerm` 只能把分隔符**统一成空格**，合不了连写/分写的差异：

```
key = "back pressure"          ← 注意：规范形带空格
选中 "back-pressure" → normTerm → "back pressure" → ✅ 命中
选中 "backpressure"  → normTerm → "backpressure"  → ❌ 一级索引漏查
                                   ↓ 去分隔符后 "backpressure" → ✅ 命中
选中 "hotspot"       → normTerm → "hotspot"       → ❌ 漏查（key 是 "hot spot"）
                                   ↓ 去分隔符后 "hotspot" → ✅ 命中
```

所以需要**两级索引**：

```js
const INDEX = new Map();
for (const k of keys) {
  if (!INDEX.has(k)) INDEX.set(k, k);                 // ① 规范形（优先）
  const loose = k.replace(/[\s_\-]/g, '');
  if (!INDEX.has(loose)) INDEX.set(loose, k);         // ② 去分隔符形
}
```

### 4.5 词形变化只能显式收录

`norm` 处理不了 `bikeshed ↔ bikeshedding`、`shard ↔ sharding`、`memoize ↔ memoization`。这类必须**单独收录条目**。

### 4.6 领域归并：`fields` + `aspect`

原词库的 `field` 是自由文本，实测有 **51 个取值**：17 个主领域（出现 ≥10 次）、
24 个 `A/B` 复合标签、25 个只出现 1 次的单例（`JS`/`React`/`事件`/`兼容层`…）。
自由文本没法可靠地筛选和统计，所以 v1.0 引入**受控领域表**（见 `app/core.py` 的
`CANONICAL_FIELDS` / `FIELD_ALIASES`）。

归并规则：

| 原始写法 | 归并结果 |
|---|---|
| `系统/并发` | `fields=["系统","并发"]`，`aspect=""` |
| `工程/配置` | `fields=["工程"]`，`aspect="配置"` |
| `JS`、`React` | `fields=["前端"]`，`aspect=""`（明确别名，无信息损失） |
| `事件` | `fields=["前端"]`，`aspect="事件"`（归并型，保留原词） |
| `缓存/状态` | `fields=["数据"]`，`aspect="缓存/状态"`（都不认识 → 整体保留） |

**关键点**：归并只影响**筛选维度**（`fields`），不影响**语义区分**（`aspect` 兜底），
所以不会像"粗暴归并"那样让两条不同义项撞成一个去重键。

> 注意 `Web` 已并入 `前端`（原库 `Web` 11 条 / `前端` 28 条，语义重叠）。

---

## 5. 实现现状（v1.0：FastAPI 后端 + 网页）

```
                    ┌─ 网页前端 / 薄用户脚本 ─┐
                    │  /  /glossary  /stats   │
                    └───────────┬─────────────┘
                                │ HTTP + JSON
┌───────────────────────────────▼───────────────────────────────┐
│  FastAPI（xlate/app/）                                         │
│                                                                │
│   main.py      路由 / 并发去重 / 异常映射                       │
│   ├ core.py    纯函数：norm / 两级索引键 / 不变量 / 领域归并      │
│   ├ models.py  Pydantic 契约（入口防线）                        │
│   ├ glossary.py 两级索引 + **原子落盘** + 合并写入              │
│   └ llm.py     DeepSeek 适配层（**Key 只在服务端**）             │
└───────────────────────────────┬───────────────────────────────┘
                                │
                    glossary.json（唯一真相源，纯文本可 diff）
```

| 文件 | 作用 |
|---|---|
| `glossary.json` | **词库主文件，核心资产**（480 词 / 493 义项） |
| `app/main.py` | FastAPI 应用与全部路由 |
| `app/core.py` | 归一化、不变量、领域归并（纯函数，易测） |
| `app/glossary.py` | 词库存储：两级索引 + 原子写入 + 合并 + 别名解析 |
| `app/llm.py` | LLM 适配层 + `.env` 加载（**Key 只在服务端**） |
| `app/templates/` | 查词页 / 词库页 / **词条编辑页** / 统计页（**零 `innerHTML`**） |
| `run.py` | 启动脚本（自动读 `.env`） |
| `.env.example` | 配置模板；复制成 `.env` 填 Key（`.env` 已被 gitignore） |
| `xlate.user.js` | **薄客户端**（v1.0.0）：选中 → 调后端 → 显示。不含数据/prompt/Key |
| `test_api.py` | **后端验收测试（75 项）** |
| `merge_test.mjs` + `norm_check.py` | **客户端/服务端契约测试**：两侧归一化必须逐字一致 |
| `merge_senses.py` | 合并语义的参考实现 + 边界测试 |
| `verify.mjs` | 词库回归检查（不动点 / 撞车 / 选区模拟） |
| `legacy/` | 旧架构脚本（`build.mjs` / `e2e.mjs` / `serve.py` / `start-server.cmd` / `_render-test.html`），**已不再使用**，留作回溯 |

### 5.1 跑起来

```powershell
# 1) 配置 LLM Key —— 写进 .env 即可（推荐，不用每次设环境变量）
Copy-Item .env.example .env
notepad .env          # 填 DEEPSEEK_API_KEY=sk-...
#    .env 已被 .gitignore 排除，不会进版本库

# 2) 启动
& .venv\Scripts\python.exe xlate\run.py

# 查词页 http://127.0.0.1:8765/   词库页 /glossary   统计 /stats   接口文档 /docs
```

> 不填 Key 也能跑：词库查询照常，只有遇到库里没有的词才会返回 **503**
> （前端会明确显示"后端未配置 LLM Key"）。
>
> 也可以用环境变量，且**环境变量优先于 `.env`**：
> ```powershell
> $env:DEEPSEEK_API_KEY = "sk-..."
> ```

### 5.2 与旧实现的差异（为什么换了）

| 旧（用户脚本 + serve.py） | 新（FastAPI + 薄脚本） |
|---|---|
| 学到的词只存在浏览器 `GM_setValue` 里，清一次就没了 | 落盘到 `glossary.json`，**单一真相源** |
| 每人在各自浏览器里长出一个分叉词库 | 一份词库 |
| API Key 在浏览器扩展里（`GM_setValue`） | **只在服务端**（`.env` / 环境变量） |
| 业务逻辑 JS 一份、Python 一份，**两套并存容易漂移** | 逻辑只在后端一处；脚本只做「选中→请求→显示」 |
| 脚本自带 prompt、词库种子、合并实现 | 脚本里**没有**这些，所以永远不用重贴 |
| `remember()` 覆盖既有义项（数据丢失） | **合并语义**，`curated` 受保护 |

---

## 6. 目标架构：Python + 网页

### 6.1 为什么要迁

| 现有痛点 | Python + Web 后 |
|---|---|
| 改词库要重启服务 / 刷新页面 | 后端热重载，或提供编辑界面 |
| 只能自己用 | 局域网 / 多人可用 |
| 词库没法方便地增删改 | 可以做管理界面 |
| API Key 存在浏览器扩展里 | 挪到服务端，**不暴露** |
| 无法加缓存/限流/统计 | 后端随便加 |
| 换模型要改用户脚本 | 后端一个适配层搞定 |

### 6.2 架构

```
┌─ 前端 ────────────────────────────────────────────
│  ① 查词网页   输入 / 粘贴 / 历史 / 词库浏览编辑
│  ② 薄用户脚本  只做「选中 → 调本地 API → 显示」
│                 ← 不含任何数据，所以【永远不用重贴】
└───────────────────┬───────────────────────────────
                    │ HTTP / JSON
┌───────────────────▼───────────────────────────────
│  Python 后端（FastAPI 推荐；Flask 也行）
│
│  · 词库层     读 glossary.json（可保留原文件，零迁移成本）
│               或迁到 SQLite（需要更复杂查询时再说）
│  · 查询层     归一化 → 两级索引 → 命中即返回
│               未命中 → LLM 适配层 → 写回词库
│  · LLM 适配层  统一接口，可切 DeepSeek / OpenAI / 本地模型
│               负责：prompt、结构化输出解析、重试、超时
│  · 校验层     写入前跑六条不变量
└───────────────────────────────────────────────────
```

### 6.3 API（v1.0 已实现，以代码为准）

```
GET  /api/health                 → { ok, glossary_path, terms, llm_configured }

GET  /api/lookup?term=<text>
     → 200 { term, raw, source:"glossary"|"llm", cached, matched_key, senses[], stats? }
     · source 必须如实返回，前端据此显示「本地词库 / API · 已入库」标签
     · 未命中才调 LLM；结果经校验后**合并**入库（不是覆盖）
     · 词库为空/过短 → 400；超长 → 422

GET  /api/glossary?q=&field=&limit=&offset=
     → { counts:{terms,senses,polysemous}, terms:{ "<key>": [sense...] } }
GET  /api/glossary/<key>         → [sense...] | 404

POST /api/glossary               body: { key, senses:[{field|fields, aspect, zh, note, src}] }
     · **合并**语义：已存在的义项跳过并保留原 src，其余追加
POST /api/glossary/<key>/replace body: { key, senses:[...] }
     · **替换**语义（编辑器用）：以提交的列表为准，不在里面的义项被删除
     · 响应 stats 报告 {created, total, kept, added, removed, removed_zh}
     · 路径 key 与 body key 不一致 → 400
POST /api/glossary/bulk          body: { terms: { "<key>": [sense...] } }
     · **全有或全无**：任一词不合格 → 422 且词库不动
POST /api/import                 body: { terms: {...}, meta? }   → 合并式、幂等
DELETE /api/glossary/<key>       → { ok, key, stats:{removed} }
GET  /api/export                 → 完整 glossary.json（可直接进 Git）
GET  /api/stats                  → counts + 领域分布 + src 分布 + 未归并领域

网页：/ 查词   /glossary 词库   /edit/<key> 编辑   /new 新建   /stats 统计   /docs OpenAPI
```

**合并 vs 替换，别用错**：

| | 什么时候用 | 改一条义项的 `zh`/`aspect` 会怎样 |
|---|---|---|
| `POST /api/glossary` | LLM 学到新词后入库 | 去重键变了 → **追加成新义项** |
| `POST /api/glossary/<key>/replace` | 人在编辑器里改 | **替换那一条**（这才是编辑的期望） |

> 这个区别是实测踩出来的：编辑器最初用合并接口，改一下译法就多出一条义项。

**关键约束**：`/api/lookup` **必须**实现「词库优先」，且 `source` 要如实返回，
让用户看得见缓存生效。写入失败时**词库必须完全不变**（原子落盘 + 校验前置）。

### 6.4 前端形态

**别把「选中即查」弄丢。** 网页很方便，但日常阅读时切标签页很烦。所以保留一个**薄用户脚本**：

- **主界面（网页）**：查词、词库浏览、批量导入、统计
- **薄用户脚本（v1.0.0）**：选中文本 → `GET /api/lookup` → 就地显示面板

薄脚本里**不含数据、不含 prompt、不含 key**，只做「选中 → 归一化 → 请求 → 渲染」四件事，
所以**永远不需要重贴**。

> **v0.5.x 曾经是"两套并存"**：脚本自己内嵌词库种子、自己维护两级索引、自己拼 prompt、
> 自己直连 DeepSeek、自己合并入库 —— 和 FastAPI 后端各写了一遍同样的业务逻辑。
> 后果是改了一边忘了另一边，行为就会不一致（实测已经发生过：JS 与 Python 对空 field
> 的默认值不同，导致同一条义项在两侧算出不同的去重键）。
> **v1.0.0 把逻辑全部收敛到后端，脚本退化为纯客户端。**

⚠️ 因此 `build.mjs`（往脚本里注入种子）已作废并移入 `legacy/`。

配置：Tampermonkey 菜单 → **⚙️ 设置后端地址**（默认 `http://127.0.0.1:8765`）。
脚本还会自动**清除旧版残留在浏览器里的 API Key**（`GM_deleteValue('apiKey')`）。

### 6.5 迁移路径 vs 实际完成度

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P-1** | 修 `remember()` 覆盖缺陷 + 合并语义 | ✅ 完成（JS + Python 双实现，各有测试） |
| **P0** | 后端读同一份 `glossary.json`，`/api/lookup` + `/api/glossary` | ✅ 完成，词库零迁移 |
| **P1** | LLM 适配层搬进 Python，用户脚本变薄客户端 | ✅ 完成（v1.0.0 薄客户端，逻辑已全部收敛到后端） |
| **P2** | 网页前端：查词页 + 词库浏览 | ✅ 完成（含统计页） |
| **P3** | 统计、批量导入导出 | ✅ 完成；历史记录、配额限流**未做** |

**未做且建议后续补的**：

| 项 | 为什么 |
|---|---|
| **配额 / 限流** | LLM 兜底目前无预算保护；单人本机风险低，但一旦 `XLATE_HOST=0.0.0.0` 暴露到局域网就有 |
| **词库变更历史** | 单人场景用 Git 就够 —— 建议 `git init` 并把 `glossary.json` 纳入版本控制（`.env` 已被 gitignore） |
| **批量编辑 / 撤销** | 编辑器现在一次只处理一个词条；误删只能靠 Git 或重新查词 |
| **`aspect` 的受控词表** | `fields` 有 `CANONICAL_FIELDS`，`aspect` 目前是自由文本 + 长度收敛，没有建议列表 |

---

## 7. 已踩过的坑（不要重踩）

### 7.1 浏览器侧（如果保留用户脚本）

| 坑 | 后果 | 对策 |
|---|---|---|
| **Trusted Types 拦 `innerHTML`** | 严格站点（GitHub/Google/银行）上面板**只剩标题、正文空白** | 全程用 `createElement`/`textContent`/`replaceChildren`。**注意 `innerHTML = ''` 也算 sink** |
| 宿主元素 `width:0;height:0` + `max-height:60vh` | 视口高 0 时面板塌成 **1px** | 宿主用 `inset:0` 铺满 + `pointer-events:none`；`max-height` 由 JS 按视口算并带兜底 |
| `@match *://*/*` 在所有 frame 跑 | 菜单命令在**隐藏 iframe** 里响应，视口 370×0 | 加 `@noframes` |
| Shadow DOM 以为能隔离一切 | **它隔离 CSS，不隔离 CSP** | 记住这条 |
| `innerHTML` 异常被吞 | 静默失败，界面毫无反馈 | try/catch + **console 面包屑** + 错误显示在面板里 |

> **排查经验**：现象是「面板能出来、tag 也在、就是正文空」→ 一定是**渲染中途抛了异常**（tag 在抛出点之前添加成功了）。
> 加日志定位，别靠读代码推理。

### 7.2 后端 / 数据侧

| 坑 | 后果 | 对策 |
|---|---|---|
| key 存成大写 | 缩略语（CORS/ACID/JWT）**永远命中不到** | 全小写 + 归一化 |
| 去重只用 `zh` | 同词不同领域同译时**丢义项** | 去重键用 `zh` + `field` |
| 连写/分写不统一 | `back-pressure` / `hotspot` 漏查 | 两级索引 |
| 不校验就写入 | 词库被写坏，且难排查 | 先校验后写，失败不落盘 |
| **用整体赋值写入义项** | 模型一回答就把 `curated` 义项全冲掉、还降级成 `api`，**静默且无法追溯** | **逐条合并**（§4.2 #7）。这是 v1.0 修掉的真实缺陷 |
| **用合并接口做编辑** | 改一条义项的 `zh`/`aspect` → 去重键变了 → **又多出一条义项**，越编辑越多 | 编辑用 **replace** 接口（`/api/glossary/<key>/replace`），语义是"照我提交的来" |
| **`aspect` 交给模型自由填** | 实测填成 `"编程语言/变量作用域"`、`"版本控制/Git"` 这种长串 —— 那是 `note` 该干的活；塞进 `aspect` 会让去重键极不稳定 | ① prompt 里写明 field 只填一个短领域名 ② `core.coerce_aspect` 兜底收敛 |
| **`split_field` 不幂等** | `Web` 和 `web` 产出不同 `aspect` → 同一含义算出两个去重键 | aspect 一律小写化，并加幂等断言（已经在测试里覆盖） |
| **别名入参没归到规范 key** | 写入的词 `lookup` 查不到（`back-pressure` 曾写进别名 key） | 写入前断言 `norm(key)===key`，并把别名归到规范 key |
| **直接写目标文件** | 中途失败留下半个 JSON = 词库被写坏 | **原子落盘**：临时文件 + `fsync` + `os.replace` |
| 服务不声明 `charset=utf-8` | 中文乱码 | 显式声明（`python -m http.server` 不声明，别直接用） |
| 服务不设 `no-store` | 改完刷新拿不到新版 | 加 `Cache-Control: no-store` |
| API key 放在前端 | 一旦分享就泄露 | 挪到服务端（`.env` / 环境变量），脚本里不得出现 |
| **把 Key 硬编码进源码** | 分享/上传/截图时**直接泄露**，且 git 历史里删不干净 | 写进 **`.env`**（同样"直接写在文件里"的体验），并把 `.env` 加进 `.gitignore`；仓库只保留 `.env.example` |
| **Key 残留在浏览器存储** | 旧版把 Key 存在 `GM_setValue`，换版本后仍留着 | v1.0.0 脚本启动时主动 `GM_deleteValue('apiKey')` |
| **后台任务存 `%TEMP%`** | 沙箱/安全策略下 `%TEMP%` 常不可写 | 临时目录放工作区内，用完即清 |
| **用 `asyncio.Lock` 做跨请求去重** | 锁绑定事件循环，`TestClient`/多线程下抛 `bound to a different event loop`，去重**静默失效** | 用 `threading.Lock` + `concurrent.futures.Future` |

### 7.3 验证纪律

**每次改完必须跑**（都已写好）：

```powershell
$py   = ".venv\Scripts\python.exe"
$node = "C:\Users\14386\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"

& $py xlate\test_api.py          # 后端验收测试（111 项）
& $py xlate\merge_senses.py      # 合并语义 + 边界（8 组）
& $node xlate\verify.mjs         # 词库回归：不动点 / 撞车 / 选区模拟

# 客户端/服务端契约（两步，因为沙箱禁止 Node 通过管道 spawn 子进程）
& $node xlate\merge_test.mjs --samples
& $py xlate\norm_check.py
& $node xlate\merge_test.mjs     # 读回真值给最终判定
```

**最有价值的三条检查**：

1. 对每个 key 断言 `norm(key) === key` —— 成立就保证「选中任意文本 → 命中」不漏。
2. 对已有词跑一次合并，断言 **`curated` 义项数量不变** —— 成立就保证模型不会污染人工成果。
3. **JS 与 Python 的 `norm` 结果逐字相等** —— 这是唯一的两侧边界，一旦漂移就会出现
   「选中的词明明在库里却查不到」这种最难查的 bug。

**测试纪律**：所有写操作只发生在**临时副本**上，绝不碰真实 `glossary.json`。

---

## 8. 验收标准

### 8.1 基础 8 项

| # | 验收项 |
|---|---|
| 1 | `normTerm(key) === key` 对全部 key 成立 |
| 2 | 归一化后无撞车 |
| 3 | 以下选区全部命中：`CORS` / `tree-shaking` / `tree_shaking` / `(ACID)` / `back-pressure` / `backpressure` / `hotspot` / `profile.` / `DevDependency` |
| 4 | 不存在的词（如 `foobar`）落到 LLM，且结果**自动入库** |
| 5 | 第二次查同一个词**不再调 LLM**（`source` 从 `llm` 变 `glossary`） |
| 6 | 词库写入前校验失败时**文件不被修改** |
| 7 | 在强制 Trusted Types 的站点上功能正常（用 `legacy/_render-test.html` 验证；脚本与网页均零 `innerHTML`） |
| 8 | 改 `glossary.json` 后，**不重装任何东西**就能生效 |

### 8.2 网站化新增（v1.0）

| # | 验收项 | 为什么 |
|---|---|---|
| 9 | 对已有 `curated` 义项的词跑合并 → **`curated` 数量与内容不变**，新义项才追加为 `api` | §4.2 #7 的核心，旧实现必然失败 |
| 10 | 校验失败时 **`glossary.json` 字节级不变**（不只是"没报错"） | 原子落盘 + 校验前置 |
| 11 | 重复导入同一份词库 → **不产生重复义项**（幂等） | 导入必须可重放 |
| 12 | 并发查同一个新词 → **只调一次 LLM、只产生一份义项** | 省费用、防重复入库 |
| 13 | 全部 `field` 都能归一或明确列为未知；`fields`/`aspect` 拆分后**无去重键撞车** | §4.6 领域归并的正确性 |
| 14 | 前端模板**无 `innerHTML` / `insertAdjacentHTML` sink** | TT 纪律，防严格站点失效 |
| 15 | 进程重启后从磁盘重建，词条数、`curated` 数量、新增词**全部保持** | 持久化真的生效 |
| 16 | 编辑器「载入 → 直接保存」**逐字往返、零变化**（`kept=n added=0 removed=0`） | 保证编辑不会悄悄改写数据 |
| 17 | 编辑器改一条义项的 `zh`/`aspect` → **替换那一条**，不是追加 | 合并接口做不到这件事（实测踩过） |
| 18 | 编辑器删掉一条义项卡片并保存 → 该义项被移除，且响应如实报告 `removed_zh` | 替换语义要可见、可核对 |
| 19 | `split_field` **幂等**：`f(f(x)) == f(x)`，且 `Web`/`web` 归一结果相同 | 否则同一含义会算出两个去重键 |
| 20 | 全部模板渲染后 **`node --check` 通过**，且无 `innerHTML`/`insertAdjacentHTML` sink | 页面 JS 也得守住 TT 纪律 |

---

## 9. 术语

| 词 | 在本项目里的含义 |
|---|---|
| **sense / 义项** | 一个词条下的一条译法（含 `fields`/`aspect`/`zh`/`note`） |
| **fields** | 规范领域数组（`["系统","并发"]`），用于筛选与统计 |
| **aspect** | 细分标签（`"事件"`、`"容器"`），领域归并后保留原信息、支撑去重 |
| **curated** | 人工确认过、可长期保留的义项 |
| **api 义项** | LLM 生成、未经复核的义项 |
| **合并（merge）** | 逐义项按去重键并入，已存在则跳过并保留原 `src`；**不是覆盖** |
| **原子落盘** | 临时文件 + `fsync` + `os.replace`，保证失败时文件不变 |
| **热加载** | （旧机制）从词库地址拉取最新词库并合并进运行时 |
| **种子（seed）** | 构建时内嵌进用户脚本的兜底词条（8 个，建议废弃） |
| **两级索引** | 规范形 + 去分隔符形，解决连写/分写差异 |
