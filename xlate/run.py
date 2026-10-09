"""启动 xlate 后端。

    & .venv\\Scripts\\python.exe xlate\\run.py
    # 或
    & .venv\\Scripts\\python.exe -m uvicorn xlate.app.main:app --reload

环境变量：
    DEEPSEEK_API_KEY   LLM 的 Key（不设则 /api/lookup 的兜底返回 503，词库查询照常）
    XLATE_HOST         默认 127.0.0.1
    XLATE_PORT         默认 8765
    XLATE_GLOSSARY     词库文件路径（默认 xlate/glossary.json）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许直接 `python xlate/run.py` 运行（把仓库根目录放进 sys.path）
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    try:
        import uvicorn
    except ImportError:
        print("缺少 uvicorn，请让用户执行：uv add 'uvicorn[standard]'")
        return 1

    from xlate.app import llm
    from xlate.app.glossary import get_store

    # 先读 .env —— 这样 LLM 的 Key 可以直接写在文件里，不用每次设环境变量
    env_used = llm.load_env_file()

    host = os.environ.get("XLATE_HOST", "127.0.0.1")
    port = int(os.environ.get("XLATE_PORT", "8765"))

    try:
        store = get_store()
    except Exception as exc:                      # noqa: BLE001
        print(f"❌ 词库加载失败：{exc}")
        return 1

    counts = store.counts()
    print("=" * 62)
    print("  xlate —— 技术术语译查（FastAPI）")
    print("=" * 62)
    print(f"  词库    : {store.path}")
    print(f"  规模    : {counts['terms']} 词 / {counts['senses']} 义项"
          f"（多义 {counts['polysemous']}）")
    print(f"  LLM     : {'已配置 ' + llm.API_KEY_ENV if llm.is_configured() else '未配置（词库查询仍可用）'}")
    print(f"  配置来源: {env_used if env_used else '未找到 .env，仅用环境变量'}")
    print()
    print(f"  查词页  : http://{host}:{port}/")
    print(f"  词库页  : http://{host}:{port}/glossary")
    print(f"  统计    : http://{host}:{port}/stats")
    print(f"  接口文档: http://{host}:{port}/docs")
    print()
    print("  停止    : Ctrl+C")
    print("=" * 62)

    uvicorn.run("xlate.app.main:app", host=host, port=port, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
