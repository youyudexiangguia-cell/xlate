# tips.md —— xlate 升级踩坑记录

> 从「Tampermonkey 用户脚本 + 静态文件服务」升级到「FastAPI 后端 + 网页 + 薄客户端」
> 的过程中，实际踩到并修掉的坑。按**症状**组织，便于回头查。
>
> 每条都给：**症状 → 原因 → 对策**。凡是标 🩸 的，都是**真的踩过**（含我自己造成的返工）。
>
> 环境：Windows + DSH 沙箱（workspace-write）· Python 3.12.14 · FastAPI 0.142.2 ·
> starlette 1.7.0 · Node v24.21.0 · uv 0.12.15

---

## 0. 三条最贵的教训

如果只看三条，看这三条：

| # | 教训 | 代价 |
|---|---|---|
| 🩸1 | **测试必须写临时副本，永远不要碰生产数据** | 我用真实服务做 smoke test，把一条乱码词条写进了真实词库 |
| 🩸2 | **同一个逻辑不要写两遍**（JS 一份 + Python 一份） | 两侧对空 field 的默认值不同，同一条义项算出两个去重键，幂等静默失效 |
| 🩸3 | **删文件时过滤条件要显式排除 `.venv`** | 我一条 `-Filter "__pycache__"` 递归删掉了 venv 里所有字节码缓存 |

---

## 1. DSH 沙箱边界（最容易反复撞的一类）

### 1.1 uv 默认缓存目录不可写 🩸

**症状**
```
$ uv python list --only-installed
error: Failed to initialize cache at `C:\Users\14386\AppData\Local\uv\cache`
  cause: failed to open file `...\sdists-v9\.git`: 拒绝访问。 (os error 5)
```

**原因**：`%LOCALAPPDATA%\uv\cache` 与 `%USERPROFILE%\.cache\uv` 在沙箱下**不可写**。

**对策**：把缓存指到工作区。
```powershell
$env:UV_CACHE_DIR = (Join-Path $PWD ".uv-cache")
```
> ⚠️ **`uv --version` 不碰缓存**，所以它能跑通**不代表** uv 可用于装包。
> 验证 uv 是否真可用，要跑一条会碰缓存的命令（如 `uv python list`）。

### 1.2 `%TEMP%` 的**子目录**不可写 🩸

**症状**
```
PermissionError: [Errno 13] Permission denied:
  'C:\Users\...\Temp\dsh-gxonDo\tips-a-esnmnw80\x.txt'
```

**实测边界**（很反直觉，值得记死）：

| 操作 | 结果 |
|---|---|
| 在 `%TEMP%` **根**下新建文件 | ✅ 可以 |
| `tempfile.NamedTemporaryFile()` | ✅ 可以 |
| `tempfile.mkdtemp()` → 往这个**新子目录**里写 | ❌ **Permission denied** |

**原因**：沙箱只给 temp 根目录写权限，新建的子目录不继承。

**对策**：测试的临时目录放**工作区内**，用完即清。
```python
tmp = Path(__file__).resolve().parent / ".tmp-test"   # 不要用 tempfile.mkdtemp()
```

### 1.3 Node 不能通过管道 spawn 子进程 🩸

**症状**
```
Error: spawnSync .venv/Scripts/python.exe EPERM
    code: 'EPERM', syscall: 'spawnSync .venv/Scripts/python.exe'
```

**原因**：沙箱禁止程序打开命名管道。`child_process` 默认 `stdio:'pipe'` 就是管道。

**对策**（不要重试，要改造）：
- **用文件交换**：A 侧写 `samples.json` → B 侧读并写 `truth.json` → A 侧读回比对
- 或 `stdio:'inherit'` / `'ignore'`（但那样就拿不到返回值）

本项目的做法就是两步式：`merge_test.mjs --samples` → `norm_check.py` → `merge_test.mjs`。

### 1.4 matplotlib 缓存

**症状**：`Permission denied: 'C:\Users\14386\.matplotlib\fontlist-*.json.matplotlib-lock'`（仅告警）

**对策**
```powershell
$env:MPLCONFIGDIR = (Join-Path $PWD ".mplconfig")
```

### 1.5 静态工具的 `Get-NetTCPConnection` 不可用

`Get-NetTCPConnection` 在沙箱里返回空，**不能**用来判断服务是否在监听。
要探活就直接请求：`Invoke-WebRequest http://127.0.0.1:PORT/api/health`。

---

## 2. 编码 / 转义（Windows 上的经典陷阱）

### 2.1 内联 PowerShell 会把中文变成 `??` 🩸

**症状**：`$py -c "... '性能/性能' ..."` 传到 Python 变成 `'??/??'`，
于是校验"通过"了但写进去的是垃圾，`zh` 也成了 `??`。

**原因**：pwsh 内联代码块经过一次编码转换，非 ASCII 字符被吃掉。

**对策**：**任何含中文的代码都写成 `.py` / `.mjs` 文件再执行**。
这正是 AGENT.md §5.2 已有的规则 —— 我又违反了一次，代价是往真实词库里写了一条乱码。

### 2.2 控制台里中文"乱码"不一定是真的乱码 🩸

**症状**：调试输出显示 `æ§è½`、`ç­­ç¹`，看起来 JSON 编码坏了。

**真相**：PowerShell 控制台按 ANSI(GBK) 解码 UTF-8 字节流**显示**成乱码，数据本身没问题。
用第三方客户端取**原始字节**验证才是可靠的：
```python
r = await httpx.AsyncClient().get(url)
print(r.content[:60])          # b'{"term":"hotspot",...'
print(r.json()["senses"][0]["zh"])   # 热点  ← 完全正常
```

**对策**：判编码问题要看原始字节，别信控制台渲染。
另外给 JSON 加显式 `charset=utf-8`（旧式客户端按 ANSI 解码时才不会出错）：
```python
if ctype.startswith("application/json") and "charset" not in ctype.lower():
    response.headers["content-type"] = "application/json; charset=utf-8"
```

### 2.3 pwsh 报 `NativeCommandError` 不等于命令失败 🩸

**症状**
```
uv : Checked 1 package in 5ms
    + CategoryInfo : NotSpecified: ...
    + FullyQualifiedErrorId : NativeCommandError
```
看起来像失败，其实成功了。

**原因**：uv 把进度写 stderr，`2>&1 | Out-String` 让 pwsh 把 stderr 当错误包了一层。

**对策**：**只看 `$LASTEXITCODE`**，不要看 pwsh 的报错包装。

### 2.4 pwsh 的 `if` 是语句不是表达式

```powershell
"{0}" -f $p, (if ($x) { "a" } else { "b" })   # ❌ 报 'if' is not recognized
"{0}" -f $p, $(if ($x) { "a" } else { "b" })  # ✅ 用 $() 包起来
```

---

## 3. Python / 依赖

### 3.1 starlette 1.x 的 TestClient 要 `httpx2`（不是 `httpx`）🩸

**症状**
```
RuntimeError: The starlette.testclient module requires the httpx2 package to be installed.
You can install this with:
    $ pip install httpx2
```

**坑点**：直觉上会去装 `httpx`（FastAPI 生态一贯的依赖），但 starlette 1.7 的
TestClient 认的是 **`httpx2>=2.0.0`**。两个包同时存在用途不同：

| 包 | 版本 | 用途 |
|---|---|---|
| `httpx` | 0.28.1 | 应用代码里的 async HTTP 客户端（调 LLM） |
| `httpx2` | 2.13.1 | **只**给 starlette TestClient 用 |

### 3.2 `.env` 解析：**先去引号，再剥行尾注释** 🩸

**症状**：`XLATE_LLM_MODEL="deepseek-reasoner"   # 注释` 解析出来是 `'"deepseek-reasoner"'`
（带引号），模型名就错了。

**原因**：先剥注释 → 值以 `#` 结尾、不以引号结尾 → 引号分支不触发。

**对策**：两步都做，顺序是**先去引号，再剥注释**；且 `#` 只有在引号未闭合时才算值的一部分。
```ini
K="v"   # 注释   -> v        ✅
K=v # 注释       -> v        ✅
K="a#b"          -> a#b      ✅  (# 在引号里，保留)
K=abc#def        -> abc#def  ✅  (# 前无空白，不是注释)
```
> 顺带一提：只用标准库写 40 行就能覆盖够用的 `.env` 语法，不必为此加 `python-dotenv`。

### 3.3 进程内单例 + 环境变量改路径 = 测试隔离失效 🩸

**症状**：测试明明把依赖覆盖成了临时 store，真实词库却被改了。

**原因**：两种不同的"注入"语义要分清：

| 方式 | 生效范围 |
|---|---|
| `app.dependency_overrides[...]` | **只对 `TestClient`** 生效 |
| `os.environ["XLATE_GLOSSARY"]` | 对**真实启动的进程**生效 |

我用 `Start-Process` 起了真服务做 smoke test，依赖覆盖**完全不参与**，
请求走了真 store → 真词库被写。

**对策**：真实服务器测试必须用环境变量指到副本。
```python
os.environ["XLATE_GLOSSARY"] = str(临时副本)   # 起真服务前设好
...
assert "临时副本" in health["glossary_path"]   # 断言服务确实用的是副本
```

---

## 4. 并发 / async

### 4.1 用 `asyncio.Lock` 做跨请求去重会静默失效 🩸

**症状**：6 个并发请求查同一个新词 → 模型被调用 **6 次**（本意是只调 1 次）。
偶发报错：`got Future attached to a different loop` / `is bound to a different event loop`。

**原因**：`asyncio.Lock` 与创建它的事件循环绑定。
`TestClient` 每个请求可能在**不同线程/事件循环**里跑，锁的行为就不确定了。

**对策**：跨线程的共享状态用**线程原语**：
```python
_inflight: dict[str, concurrent.futures.Future] = {}
_inflight_lock = threading.Lock()          # 不是 asyncio.Lock

# 等待方
await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(fut)), timeout=90)
```
`asyncio.shield` 还有个好处：某个等待者被取消（浏览器断开）**不会**影响真正在干活的那个请求。

### 4.2 `Future` 设了异常却没人取 → 噪音报错

**症状**：日志刷 `Future exception was never retrieved`。

**对策**：设完异常后**自己也要取一次**，保证异常被标记为已检索。
```python
fut.set_exception(err)
try:
    fut.result()
except BaseException as exc:
    raise exc from None
```

### 4.3 「异步」的真正动机不是并发，是响应性

单人使用也**必须**用 async：LLM 一次 1~25 秒，同步视图会占满 worker，
**连续查两个生词就要排队**。这不是并发问题，是响应性问题。

---

## 5. 接口语义（这类坑最隐蔽）

### 5.1 「合并」和「替换」是两种语义，用错就出错 🩸

**症状**：在编辑器里改一条义项的译法，保存后**多出一条义项**；越编辑越多。

**原因**：去重键是 `zh + fields + aspect`。改了 `zh` → 去重键变了 → 合并逻辑认为这是**新义项**。

**对策**：拆成两个接口，各司其职。

| 接口 | 语义 | 适用 |
|---|---|---|
| `POST /api/glossary` | **合并**：已存在跳过、其余追加 | LLM 学到新词后入库 |
| `POST /api/glossary/<key>/replace` | **替换**：以提交列表为准 | 人在编辑器里改 |

替换接口要**如实报告差异**，否则用户不知道删掉了什么：
```json
{"created": false, "total": 1, "kept": 0, "added": 1, "removed": 1,
 "removed_zh": ["旧译法"]}
```

### 5.2 别名入参必须归到规范 key 🩸

**症状**：`POST {"key": "hotspot"}` 返回成功，但 `GET /api/glossary/hotspot` 查不到；
反而多了一个 `hotspot` 词条，而 `back-pressure` 明明能查到 `backpressure`。

**原因**：`merge_term` 只做了 `validate_key`（归一化 + 不变量检查），
**没有过两级索引解析别名**。于是 `hotspot` 被当成新 key 写进去，
而查询走索引仍解析到 `hot spot` → 新写的词**永远查不到**。

**对策**：写入前先解析到已存在的规范 key。
```python
def canonicalize(self, key) -> str:
    k, _ = core.validate_key(key)
    with self._lock:
        if k in self.terms: return k
        return self._index.get(k, k)     # 别名 → 规范 key
```
> 我在 JS 版里修过这个 bug，在 Python 版里又犯了一次。**同一个教训换语言会重犯。**

### 5.3 归并分类会丢信息，得留一个"细分"字段 🩸

**症状**：把原词库 51 个自由文本 `field` 归并成受控领域后，
`bind` 的三条义项 `网络`/`JS`/`事件` 里，`JS` 和 `事件` 都并成「前端」
→ 两条义项去重键相同，**语义区分丢失**。

**对策**：领域归并只影响**筛选维度**，另留 `aspect` 承载细分标签，去重键用
`zh + fields + aspect`。信息不丢，去重也不撞车。

### 5.4 不要用模型自由生成的字段当去重键的一部分 🩸

**症状**：真实查询 `global` 时，模型把 `aspect` 填成
`"编程语言/变量作用域"`、`"版本控制/Git"` 这种长串——
那是 `note` 该干的活。塞进 `aspect` 会让去重键极不稳定（换个说法就是新义项）。

**对策**（两层）：
1. **prompt 里写明分工**：`field` 只填一个短领域名，长描述去 `note`
2. **代码兜底收敛**：`coerce_aspect()` 对长串取有意义的一段

> 教训：**不要让模型的自由输出进入身份标识**（主键/去重键）。要么约束它，要么在服务端收敛。

### 5.5 归一化函数必须幂等 🩸

**症状**：`Web` 和 `web` 产出不同的 `aspect` → 同一含义算出两个去重键。

**对策**：所有会被比较/入库的字段都归一到一个稳定形式（本例：aspect 一律小写），
并**把幂等性写成测试**：
```python
fl, asp = split_field(x)
assert (fl, asp) == split_field("/".join(fl + ([asp] if asp else [])))
```

---

## 6. 数据一致性

### 6.1 整体赋值 = 静默数据丢失 🩸（本项目最严重的缺陷）

**症状**：不需要症状 —— 它**静默**发生。模型对 `bind` 返回任何结果，
该词原有的 3 条 `curated` 义项被替换成 1 条 `api`。

**原因**
```js
glossary.terms[k] = senses.map(s => ({...s, src:'api'}));   // ❌ 整体覆盖
```

**对策**：逐条合并，去重键已存在则**跳过并保留原 `src`**；`curated` 永不删除、永不降级。
```js
for (const s of cleaned) {
  const k = dedupeKey(s);
  if (seen.has(k)) { skipped++; continue; }   // 幂等，保留原 src
  seen.add(k); merged.push({...s, src:'api'}); added++;
}
```

**验收方式**：对已有词跑一次合并，断言 **`curated` 义项数量不变**。

### 6.2 直接写目标文件 = 最坏情况是"改坏了"

**对症**：§4.2 要求最坏情况必须是"**没改动**"。

**对策**：临时文件 + `fsync` + `os.replace` 原子替换；失败则内存回滚。
Windows 上目标文件可能被杀软/编辑器短暂占用，`os.replace` 要**带重试**。

### 6.3 别把"校验"寄托在 `Model.save()` / 构造上

Django 的 `save()` 不自动跑 `full_clean()`；Pydantic 只保证**结构**，
业务不变量（如"去重键不得重复"）仍要在服务层显式校验，且**全有或全无**
（任一条不合格 → 整批拒绝、不落盘）。

---

## 7. 测试纪律

### 7.1 三条铁律

1. **写操作只发生在临时副本上**，绝不碰生产数据（🩸 我违反过一次）
2. **期望值从数据推导，不要硬编码**（🩸 见 7.2）
3. **临时文件和进程，用完即清**

### 7.2 硬编码期望值 = 反复假失败 🩸

**症状**：测试报"义项数仍为 3（未增加）实际 4"，看起来是代码 bug。

**真相**：我在测试里写 `{"field": "工程", "zh": "配置文件"}`，
而库里实际是 `zh: "配置文件、配置组"`、`field: "工程/配置"`。
**提交的确实是一条新义项，代码行为完全正确**，是我的期望写错了。

**对策**：期望值从被测数据里取。
```python
first = before[0]                      # 从库里读真实值
senses = [{"field": first["aspect"] or first["fields"][0], "zh": first["zh"]}]
```

### 7.3 不要断言固定条数——词库是"活"的 🩸

**症状**：`断言 480 词条` 突然失败，实际 481。

**真相**：你自己查了一次 `global`，LLM 结果入库了 —— **这是功能正常工作**，不是 bug。

**对策**：断言**不变量**（全部小写、是不动点、无撞车），而不是具体数字。
真要断言数量，就跟 `meta.counts` 比、或断言 `>= 基线`。

### 7.4 用脚本校验前端 JS，而不是人眼读

模板里的 JS 也能交给 `node --check`：先把占位符按渲染规则替换掉，抽出 `<script>` 再检查。
顺手还能扫 TT 违规：
```python
bad = re.findall(r"\.(?:inner|outer)HTML\s*=|insertAdjacentHTML\s*\(|document\.write\s*\(", js)
```
> 注意：**检查原始模板会误报**，因为模板里本来就该有 `{{...}}`；要先替换再查。

### 7.5 端到端测试要断言"用的是副本"

起真实服务做 e2e 时，第一件事就是断言它没在生产文件上工作：
```python
health = (await c.get(f"{BASE}/api/health")).json()
assert str(TMPDIR) in health["glossary_path"]
```
最后再复查一次真实文件未被改动。

---

## 8. 安全

### 8.1 API Key 的位置

| 放法 | 评价 |
|---|---|
| 前端 / 用户脚本里 | ❌ 一旦分享就泄露 |
| **硬编码进源码** | ❌ 截图/上传/`git init` 都会泄露，且 git 历史删不干净 |
| **`.env`（推荐）** | ✅ 同样"直接写进文件"的手感，但不进源码、被 gitignore 排除 |

仓库只留 `.env.example`（占位符），`.env` 进 `.gitignore`。
环境变量优先级高于 `.env`，临时覆盖不用改文件。

### 8.2 旧版残留在浏览器里的机密要主动清

脚本升到薄客户端后，Key 不再需要，但旧版存在 `GM_setValue('apiKey')` 里的还在。
启动时主动删掉：
```js
if (typeof GM_deleteValue === 'function' && GM_getValue('apiKey', null) !== null) {
  GM_deleteValue('apiKey');
}
```

### 8.3 不要把凭证/密钥写进注释或日志

用正则扫一遍仓库确认没有真实 Key 字样（`sk-[A-Za-z0-9]{8,}`），
顺手也能扫出被硬编码的测试数据。

---

## 9. 我犯的操作失误（坦白）

这些不是技术难点，是**该避免的粗心**，写下来提醒自己：

| # | 失误 | 应该怎么做 |
|---|---|---|
| 🩸1 | 用 `Start-Process` 起真实服务做 smoke test，**没设 `XLATE_GLOSSARY`** → 真词库被写入乱码条目 | 起真服务前必须把数据路径指向副本 |
| 🩸2 | 递归删 `__pycache__` 时过滤条件把 `.venv` 也算进去，删光了 venv 的字节码缓存 | 路径过滤必须显式 `-notlike "*\.venv\*"`，且删除前 `Resolve-Path` 核对 |
| 🩸3 | 在 pwsh 内联脚本里写中文，被转成 `??` | 含非 ASCII 的代码一律写成文件 |
| 🩸4 | 递归删 `__pycache__` 时**忘了 `.venv` 也在递归范围里**，把 site-packages 的缓存也删了 | 递归操作前先看清范围：`Get-ChildItem -Recurse` 会**一路走进 `.venv`**；过滤条件必须显式 `-notlike "*\.venv\*"` |
| 🩸5 | 大改 `xlate.user.js` 删掉函数后，`merge_test.mjs` 还在引用 → 测试崩 | 删实现后**立刻检查测试是否还引用它** |
| 🩸6 | 多次想当然地写测试期望值，导致"代码对、测试错"的假失败反复出现 | 期望值从被测数据推导（见 §7.2） |

---

## 10. 快速自查清单

改完之后按这个顺序过一遍：

```powershell
$py   = ".venv\Scripts\python.exe"
$node = "C:\Users\14386\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"

# 1) 后端验收
& $py xlate\test_api.py
# 2) 合并语义 + 边界
& $py xlate\merge_senses.py
# 3) 词库不变量回归
& $node xlate\verify.mjs
# 4) 客户端/服务端契约（两侧 norm 必须逐字一致）
& $node xlate\merge_test.mjs --samples
& $py xlate\norm_check.py
& $node xlate\merge_test.mjs
# 5) 语法
& $node --check xlate\xlate.user.js
& $py -m compileall -q xlate
```

外加三条人工确认：

- [ ] 真实 `glossary.json` **未被测试改动**（条数 + 无测试词残留）
- [ ] 仓库里**没有真实 Key**（`sk-...` 正则扫描）
- [ ] 临时文件/目录/进程**已清理**，工作区只剩该有的东西

---

## 附：一句话备忘

| 场景 | 记住这句 |
|---|---|
| 要装包 | `UV_CACHE_DIR` 指到工作区；`uv --version` 成功**不代表**能装包 |
| 要临时目录 | 放**工作区内**，别用 `mkdtemp()` |
| 要跨语言 | 用**文件交换**，别用管道 spawn |
| 要写中文代码 | 写成**文件**，别内联 pwsh |
| 要判命令成败 | 只看 `$LASTEXITCODE` |
| 要编辑已有数据 | 用 **replace**，不是 merge |
| 要写义项 | **逐条合并**，别整体赋值 |
| 要判编码 | 看**原始字节**，别信控制台 |
| 要测真实服务 | 环境变量指**副本**，并断言副本生效 |
| 要断言数量 | 断言**不变量**，别写死数字 |
