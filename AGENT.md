---
description: math-model 项目的运行环境、工具链绝对路径与沙箱避坑规则
globs: ["**/*.py", "**/*.mjs", "**/*.js", "**/*.md"]
alwaysApply: true
---

# AGENT.md — math-model 运行环境与开发规则

> 本文件即根目录 `AGENT.md`（单数）。若文档或任务描述中引用 `AGENTS.md`，一律指向本文件。

## 0. 最高优先级铁律：环境配置归用户，Agent 只用不建

> **Python 环境与依赖的一切配置动作，统一由用户亲自完成。Agent 不得擅自操作。**

1. **严禁**执行任何环境创建/变更命令，包括但不限于：
   - `uv venv`、`uv init`、`uv add`、`uv sync`、`uv lock`、`uv python install`
   - `uv pip install` / `uv pip uninstall` / `uv pip sync`
   - `pip install` / `pip uninstall` / `python -m venv` / `virtualenv` / `conda`
   - 任何改动 `.venv` 内容、`pyproject.toml`、`uv.lock`、`requirements.txt` 的命令
2. **缺包时的正确做法**：停下来，告知用户缺哪个包，请其自行安装；**不要**自行补装，
   也**不要**试图绕道 DSH 自带解释器或系统 Python 去找替代实现。
3. **Agent 的职责边界**：只负责**使用**既有环境跑代码、读结果、写分析。
   即 `& .venv\Scripts\python.exe script.py` 以及只读查询（如 `--version`、`import` 自检）。
4. **例外**：仅当用户在**本轮对话中明确要求**"你来装/帮我装"时，才可按第 3 节执行，
   且须先报出将要运行的完整命令再动手。
5. **背景**：此前由 Agent 配置环境时反复触发沙箱拦截与缓存权限报错，成功率低、
   排查成本高。因此将配置权收归用户，是**有意的设计选择**，不是待优化的缺陷。

## 1. 解释器与工具链（一律用绝对路径）

- **项目虚拟环境（Python 任务唯一入口，已由用户配好）**

```powershell
$vpy = ".venv\Scripts\python.exe"     # CPython 3.12.14，由 uv 0.12.15 创建
& $vpy script.py
```

  - 已装 105 个包，科学计算栈齐全：`numpy` / `pandas` / `scipy` / `scikit-learn` /
    `statsmodels` / `seaborn` / `sympy` / `pulp` / `tensorflow` / `keras` / `openpyxl` / `tabulate`
  - **Web 栈（用户已配）**：`fastapi` 0.142.2 / `uvicorn` 0.54.0 / `starlette` 1.7.0 /
    `pydantic` 2.13.5
  - ⚠️ **缺 `httpx` 与 `httpx2`**：这会导致 (a) `fastapi.testclient.TestClient` 不可用
    （starlette 1.7 要求 `httpx2>=2.0.0`），(b) 没有 async HTTP 客户端。
    需要时**请用户安装**，不要自行补装。
  - **禁止**使用裸 `python`：工作区 `python` 是 WindowsApps 存根，执行无输出、不可用
  - **禁止**用 DSH 自带解释器
    `C:\Users\14386\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`
    执行项目代码（其依赖与 `.venv` 不一致，且违反第 0 节"只用用户既有环境"的要求）

- **Java**：`javac` / `java` 已在 PATH，实测版本 **26.0.2.1**。
  以 `java -version` 输出为准，忽略 `D:\Java\jdk-21` 之类过期 shim。

- **Node.js（使用频率低，一般不必处理）**：仅在确有需要时按下述绝对路径调用，
  不主动为其安装依赖。

```powershell
$node = "C:\Users\14386\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"
```

  版本 v24.21.0。`node` / `npm` / `pnpm` **均不在 PATH**，必须走绝对路径。

## 2. 沙箱边界与限制

1. **只限写、不限读**：读权限开放，写操作受限；越界写会报 `[sandbox: file access denied ...]`。
2. **缓存类目录普遍不可写**（这是 Agent 配环境易失败的根因之一）：
   - uv 默认缓存 `%LOCALAPPDATA%\uv\cache` 与 `%USERPROFILE%\.cache\uv` **均被拒绝**
   - matplotlib 默认 `~\.matplotlib` 被拒绝，会告警
     `Permission denied: ...\fontlist-*.json.matplotlib-lock`（仅告警，不致命）
   - 仅**工作区**与 `%TEMP%` 可写
3. **Named Pipe 是硬边界**：不要尝试绕过。
   **替代策略**：引导用户在外部浏览器/终端操作，或改用自检脚本/命令验证。
4. 确需越界写时，**一次性给出完整命令**并申请 `danger-full-access`，不要反复试探。

## 3. 环境配置参考（**仅限用户明确授权时**，Agent 默认不得执行）

> 本节保留技术细节，供用户授权 Agent 操作、或用户自行执行时参考。

1. **必须先指定缓存目录**，否则任何触发 uv 缓存的子命令都会失败：

```powershell
$env:UV_CACHE_DIR = (Join-Path $PWD ".uv-cache")
```

   - 实测失败特征：`error: Failed to initialize cache at ...\uv\cache`，
     `cause: failed to open file ...\sdists-v9\.git: 拒绝访问。 (os error 5)`
   - `%TEMP%` 虽可写但每次进程子目录随机，不适合复用；工作区目录最稳
   - **`uv --version` 不碰缓存**，所以它能跑通**不代表** uv 可用于装包

2. **镜像源必须用 uv 自己的参数**：`pip.ini` 中已配置清华源，但 **uv 不读 `pip.ini`**。

```powershell
uv pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple <pkg>
# 或先设环境变量：$env:UV_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
```

3. **安装到项目环境**：

```powershell
uv pip install --python .venv\Scripts\python.exe <pkg>
```

4. **项目已成为受管 uv 项目**（用户重新配置环境后）：
   - 根目录存在 `pyproject.toml` + `uv.lock`，`project.name = "math-model"`。
   - 因此 `uv add` / `uv sync` / `uv lock` **现在是可用的**，但按第 0 节铁律，
     **仍须由用户执行**，Agent 不得代为运行（它们会改写 `pyproject.toml` / `uv.lock` / `.venv`）。
   - Agent 只做**只读**查询：`uv pip list`、`uv tree`、`uv lock --check` 之类不写盘的命令。

5. **判成功只看 `$LASTEXITCODE`，不要看 PowerShell 报错**：
   uv 把正常进度写到 stderr，配 `2>&1 | Out-String` 时会抛
   `NativeCommandError: ... RemoteException`，这是 pwsh 的包装假象而**非** uv 失败。
   例如 `uv pip install` 已满足依赖时输出 `Checked 1 package` 并 `exit=0`，属于成功。

## 4. 开发与测试准则

1. **先问清再动手**：遇到"访问路径不存在"等模糊报错，优先向用户索取**具体报错原文**，
   不要盲目推测或搭建临时方案。**缺依赖也属于此类**——先问，不要自行安装。
2. **避免 PowerShell 内联转义地狱**：严禁把含复杂正则、单双引号、反引号的
   JS/Python 塞进 `pwsh -Command`。
   **规范做法**：存成 `.py` / `.mjs` 文件后显式调用执行。
   （实测教训：内联 Python 的三引号与 `{}` f-string 会被 pwsh 解析吃掉并抛 `SyntaxError`。）
3. **代码修改必须语法校验**：
   - Python：`& .venv\Scripts\python.exe -m py_compile <file>`
   - JS/MJS：`& $node --check <file>`
4. **长时间运行脚本时重定向 matplotlib 缓存**（否则产生告警噪音）：

```powershell
$env:MPLCONFIGDIR = (Join-Path $PWD ".mplconfig")
# 脚本内并配 matplotlib.use("Agg")
```

5. **测试环境对齐 CSP**：本地 `file://` 默认无 CSP，无法复现 Trusted Types 报错。
   测试页须模拟目标环境策略，例如
   `<meta http-equiv="Content-Security-Policy" content="require-trusted-types-for 'script'">`，
   且诊断脚本自身严禁违规调用 `innerHTML`。

## 5. 文件与进程管理规范

1. **写前重读机制**：若文件会被构建工具（如 `build.mjs`）重写，禁止外部并发编辑；
   必须**重新读取最新内容后再保存**，防止并发写入冲突。
2. **长任务/超时任务**：耗时命令超时后会被移至后台 Job。
   **切勿重复发起命令**，用 `job_output` 查询后台执行结果。
3. **临时文件"留痕即清"**：探测测试建立的临时目录与文件
   （如 `.uv-cache`、`.mplconfig`、`_dsh-probe-*`、`_probe_*`、`_p1.json`）用完**立刻清理**。
   删除前必须用 `Resolve-Path` 核对绝对路径，严禁删除动态拼接未经验证的路径。
4. **不得改动环境清单**：`.venv`、`pyproject.toml`、`uv.lock`、`requirements.txt`
   均属用户管理范围，Agent 只读不写（见第 0 节）。
5. **版本控制现状**：工作区**未初始化 git**；`.venv` 内含 uv 生成的 `.gitignore`（内容为 `*`）。

## 6. 环境自检（只读，可随时跑）

```powershell
& .venv\Scripts\python.exe test.py
```

预期输出：解释器命中 `.venv`，NumPy 版本与基础运算正常。
若报 `ModuleNotFoundError`，**不要自行安装**，改为告知用户缺包名称并请其处理。
