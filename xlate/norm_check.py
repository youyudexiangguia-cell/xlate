"""norm_check.py —— 算出 Python 侧归一化真值，并与薄脚本的 JS 结果比对。

为什么用文件交换而不是管道：
    沙箱不允许 Node 通过 pipe spawn 子进程（EPERM，AGENT.md §2.3），
    所以由 Node 写 samples.json，这里读它、算真值、写 truth.json 并直接比对。

用法（两步）：
    node xlate/merge_test.mjs --samples
    .venv\\Scripts\\python.exe xlate\\norm_check.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from xlate.app.core import norm_term  # noqa: E402

TMP = Path(__file__).resolve().parent / ".tmp-norm"
SAMPLES = TMP / "samples.json"
# 真值要**留在**这里给第 3 步（merge_test.mjs）读，所以不能放在会被清掉的 TMP 里
TRUTH = Path(__file__).resolve().parent / ".norm-truth.json"


def main() -> int:
    if not SAMPLES.is_file():
        print(f"❌ 找不到 {SAMPLES}")
        print("   先跑： node xlate/merge_test.mjs --samples")
        return 1

    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    samples = data["samples"]
    js_results = data["js"]

    # 真值
    py_norm = [norm_term(s) for s in samples]

    TRUTH.write_text(json.dumps({
        "norm": py_norm,
        "glossary_key_count": len(data.get("glossaryKeys", [])),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 66)
    print(" Python 侧归一化真值（xlate.app.core.norm_term）")
    print("=" * 66)

    ok = fail = 0
    mismatches = []
    for src, js, py in zip(samples, js_results, py_norm):
        if js == py:
            ok += 1
        else:
            fail += 1
            mismatches.append((src, js, py))

    # 只展示前若干条样本，避免刷屏（后面 480 个词库 key 是保底覆盖）
    show = min(24, len(samples))
    for src, js, py in list(zip(samples, js_results, py_norm))[:show]:
        flag = "OK  " if js == py else "FAIL"
        if js == py:
            print(f"  [{flag}] {src!r:34} -> {py!r}")
        else:
            print(f"  [{flag}] {src!r:34} -> JS={js!r}  PY={py!r}")
    if len(samples) > show:
        print(f"  ...（另 {len(samples) - show} 个样本，含词库全部 key）")

    print()
    print(f"  一致 {ok} / 不一致 {fail}")
    if mismatches:
        print("\n  不一致样例（前 10）：")
        for src, js, py in mismatches[:10]:
            print(f"    输入 {src!r}\n      JS = {js!r}\n      PY = {py!r}")

    print(f"\n  真值已写出：{TRUTH}")
    print("  下一步： node xlate/merge_test.mjs   # 读回真值并给最终判定")

    shutil.rmtree(TMP, ignore_errors=True)
    if fail:
        print("\n  ❌ 客户端与服务端归一化不一致 —— 会造成「选中却查不到」")
        return 1
    print("\n  ✅ 客户端 normTerm 与服务端 core.norm_term 完全一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
