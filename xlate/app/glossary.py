"""glossary —— 词库存储层。

单一真相源是 `glossary.json`（保留纯文本、可 diff、进 Git 的历史资产）。
运行时在内存里建**两级索引**（§4.4），写入走**原子落盘**。

并发策略：本项目按「一个人使用」定位（见 README §6.6），
所以用一把可重入锁串行化写入即可，不做数据库级别的并发控制。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import core

log = logging.getLogger("xlate.glossary")

_HERE = Path(__file__).resolve().parent


def data_path() -> Path:
    """词库文件位置。可用环境变量 XLATE_GLOSSARY 覆盖（测试用）。"""
    env = os.environ.get("XLATE_GLOSSARY")
    if env:
        return Path(env).resolve()
    return (_HERE.parent / "glossary.json").resolve()


class GlossaryError(RuntimeError):
    pass


class GlossaryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else data_path()
        self._lock = threading.RLock()
        self.terms: dict[str, list[dict]] = {}
        self.meta: dict[str, Any] = {}
        self._index: dict[str, str] = {}        # 规范形 + 去分隔符形 → 规范 key
        self._loose_collisions: list[tuple[str, str]] = []
        self.load()

    # -------------------------------------------------- 载入
    def load(self) -> None:
        if not self.path.is_file():
            raise GlossaryError(f"词库文件不存在：{self.path}")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("terms"), dict):
            raise GlossaryError("词库格式非法：缺少 terms 对象")

        terms: dict[str, list[dict]] = {}
        problems: list[str] = []
        for raw_key, senses in raw["terms"].items():
            try:
                key, _loose = core.validate_key(raw_key)
            except core.InvariantError as exc:
                problems.append(f"{raw_key!r}: {exc}")
                continue
            if key in terms:
                problems.append(f"{raw_key!r}: 归一化后与 {key!r} 撞车（不变量 #4）")
                continue
            cleaned: list[dict] = []
            for s in (senses or []):
                try:
                    cleaned.append(core.normalize_sense(s))
                except core.InvariantError as exc:
                    problems.append(f"{key!r}: {exc}")
            if cleaned:
                terms[key] = cleaned

        if problems:
            # 不静默吞掉：既有词库有问题必须让人看见
            log.warning("载入词库时有 %d 处问题：%s", len(problems), "; ".join(problems[:5]))

        with self._lock:
            self.terms = terms
            self.meta = dict(raw.get("meta") or {})
            self._rebuild_index()
        log.info("词库已载入：%d 词条 / %d 义项（%s）",
                 len(self.terms), self.sense_count(), self.path)

    def _rebuild_index(self) -> None:
        """两级索引（§4.4）：规范形优先，去分隔符形兜底。"""
        index: dict[str, str] = {}
        loose_owner: dict[str, str] = {}
        collisions: list[tuple[str, str]] = []
        for key in self.terms:
            index.setdefault(key, key)                    # ① 规范形（优先）
            loose = core.loose_key(key)
            if not loose:
                continue
            owner = loose_owner.get(loose)
            if owner is None:
                loose_owner[loose] = key
                index.setdefault(loose, key)              # ② 去分隔符形
            elif owner != key:
                collisions.append((loose, f"{owner} / {key}"))
        self._index = index
        self._loose_collisions = collisions
        if collisions:
            log.warning("宽松索引撞车 %d 处（前者优先）：%s", len(collisions), collisions[:3])

    # -------------------------------------------------- 查询
    def resolve_key(self, term: Any) -> str | None:
        """把任意选区文本解析成词库里的规范 key；解析不到返回 None。"""
        n = core.norm_term(term)
        if not n:
            return None
        with self._lock:
            hit = self._index.get(n)
            if hit is not None:
                return hit
            return self._index.get(core.loose_key(n))

    def lookup(self, term: Any) -> tuple[str | None, list[dict]]:
        key = self.resolve_key(term)
        if key is None:
            return None, []
        with self._lock:
            return key, [dict(s) for s in self.terms.get(key, [])]

    def get(self, key: Any) -> list[dict] | None:
        k = core.norm_term(key)
        with self._lock:
            senses = self.terms.get(k)
            return [dict(s) for s in senses] if senses else None

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self.terms)

    def sense_count(self) -> int:
        with self._lock:
            return sum(len(v) for v in self.terms.values())

    def counts(self) -> dict[str, int]:
        with self._lock:
            terms = len(self.terms)
            senses = sum(len(v) for v in self.terms.values())
            polysemous = sum(1 for v in self.terms.values() if len(v) > 1)
        return {"terms": terms, "senses": senses, "polysemous": polysemous}

    def field_stats(self) -> list[tuple[str, int]]:
        tally: dict[str, int] = {}
        with self._lock:
            for senses in self.terms.values():
                for s in senses:
                    for f in (s.get("fields") or ["(未标注)"]):
                        tally[f] = tally.get(f, 0) + 1
        return sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))

    def unknown_fields(self) -> list[str]:
        known = set(core.CANONICAL_FIELDS)
        out: set[str] = set()
        with self._lock:
            for senses in self.terms.values():
                for s in senses:
                    for f in (s.get("fields") or []):
                        if f not in known:
                            out.add(f)
        return sorted(out)

    def src_distribution(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        with self._lock:
            for senses in self.terms.values():
                for s in senses:
                    src = s.get("src") or "api"
                    tally[src] = tally.get(src, 0) + 1
        return tally

    def search(self, q: str = "", field: str = "") -> dict[str, list[dict]]:
        """按 key 子串 / 译法子串 + 领域筛选。"""
        qn = core.norm_term(q) if q else ""
        fq = field.strip() if field else ""
        out: dict[str, list[dict]] = {}
        with self._lock:
            for key, senses in self.terms.items():
                if qn:
                    hay = key + " " + " ".join(s.get("zh", "") for s in senses)
                    if qn not in hay.lower():
                        continue
                picked = senses
                if fq:
                    picked = [s for s in senses if fq in (s.get("fields") or [])]
                    if not picked:
                        continue
                out[key] = [dict(s) for s in picked]
        return dict(sorted(out.items()))

    # -------------------------------------------------- 写入
    def canonicalize(self, key: Any) -> str:
        """把任意写法解析到**已存在的规范 key**；不存在则返回其规范形。

        必须走这一步：`hotspot` 是 `hot spot` 的去分隔符别名，
        若不解析就写入，会新建一个 `hotspot` 条目 —— 而 `resolveKey('hotspot')`
        命中的仍是 `hot spot`，于是新写的词**永远查不到**（不变量 #8）。
        """
        k, _loose = core.validate_key(key)
        with self._lock:
            if k in self.terms:
                return k
            existing = self._index.get(k)
            if existing is not None:
                return existing
        return k

    def merge_term(self, key: str, senses: Iterable[Any],
                   *, default_src: str = "api") -> dict:
        """把一个词的义项**合并**进词库（不是覆盖），然后原子落盘。

        这是旧的 remember() 覆盖缺陷的修正落点，语义见 core.merge_senses。
        """
        k = self.canonicalize(key)
        prepared = [core.normalize_sense(s, default_src=default_src) for s in senses]
        with self._lock:
            existing = self.terms.get(k, [])
            merged, stats = core.merge_senses(existing, prepared)
            before = list(existing)
            self.terms[k] = merged
            self._rebuild_index()
            try:
                self._save()
            except Exception:
                self.terms[k] = before        # 落盘失败 → 回滚内存，保证一致
                self._rebuild_index()
                raise
        return {"key": k, **stats}

    def replace_term(self, key: str, senses: Iterable[Any],
                     *, default_src: str = "curated") -> dict:
        """把某个词的义项**整体替换**为给定列表（编辑器专用）。

        为什么需要它：合并语义下，改一条义项的 zh/aspect 会改变去重键，
        于是变成「追加一条新的」而不是「更新那一条」（实测踩过）。
        编辑器要的是"照我提交的来"，所以这里显式提供替换语义，并返回差异。

        校验仍然是**全有或全无**：任一条不合格就整批拒绝、词库不动。
        """
        k = self.canonicalize(key)
        prepared = [core.normalize_sense(s, default_src=default_src) for s in senses]
        if not prepared:
            raise core.InvariantError("至少要有一条义项")
        if len(prepared) > core.MAX_SENSES_PER_TERM:
            raise core.InvariantError(
                f"义项过多（{len(prepared)} > {core.MAX_SENSES_PER_TERM}）")
        # 同一批内不许重复
        seen = {(s["zh"], tuple(s["fields"]), s["aspect"]) for s in prepared}
        if len(seen) != len(prepared):
            raise core.InvariantError("提交的义项里有重复（去重键 zh+fields+aspect）")

        with self._lock:
            before = [dict(s) for s in self.terms.get(k, [])]
            created = not before

            def ident(s: dict):
                return (s.get("zh", ""), tuple(s.get("fields") or []),
                        s.get("aspect", "") or "")

            before_ids = {ident(s) for s in before}
            after_ids = {ident(s) for s in prepared}
            removed = [s for s in before if ident(s) not in after_ids]
            added = [s for s in prepared if ident(s) not in before_ids]

            self.terms[k] = prepared
            self._rebuild_index()
            try:
                self._save()
            except Exception:
                self.terms[k] = before if before else self.terms.pop(k, None)
                self._rebuild_index()
                raise

        kept = sum(1 for s in prepared if ident(s) in before_ids)
        src_count: dict[str, int] = {}
        for s in prepared:
            src_count[s["src"]] = src_count.get(s["src"], 0) + 1
        return {
            "key": k,
            "created": created,
            "total": len(prepared),
            "kept": kept,
            "added": len(added),
            "removed": len(removed),
            "removed_zh": [s.get("zh", "") for s in removed],
            "src_distribution": src_count,
        }

    def delete(self, key: str) -> dict:
        k = core.norm_term(key)
        with self._lock:
            if k not in self.terms:
                return {"ok": False, "removed": 0, "key": k}
            removed = len(self.terms.pop(k))
            self._rebuild_index()
            try:
                self._save()
            except Exception:
                self.load()                   # 落盘失败 → 从磁盘重载
                raise
        return {"ok": True, "removed": removed, "key": k}

    def replace_all(self, terms: dict[str, list[dict]], meta: dict | None = None) -> dict:
        """整体替换（导入用）。先校验，全部合格才落盘。"""
        clean: dict[str, list[dict]] = {}
        rejected: list[str] = []
        for raw_key, senses in terms.items():
            try:
                k, _ = core.validate_key(raw_key)
                if k in clean:
                    rejected.append(f"{raw_key!r}: 归一化后撞车")
                    continue
                cleaned = [core.normalize_sense(s) for s in senses]
                if not cleaned:
                    rejected.append(f"{raw_key!r}: 没有有效义项")
                    continue
                clean[k] = cleaned
            except core.InvariantError as exc:
                rejected.append(f"{raw_key!r}: {exc}")
        if rejected:
            raise core.InvariantError("；".join(rejected[:8]))

        with self._lock:
            backup_terms, backup_meta = self.terms, self.meta
            self.terms = clean
            if meta is not None:
                self.meta = dict(meta)
            self._rebuild_index()
            try:
                self._save()
            except Exception:
                self.terms, self.meta = backup_terms, backup_meta
                self._rebuild_index()
                raise
        return {"ok": True, "terms": len(clean),
                "senses": sum(len(v) for v in clean.values())}

    def import_merge(self, terms: dict[str, list[dict]], *, default_src: str = "api") -> dict:
        """合并式导入：逐个词走 merge（幂等，不会产生重复义项）。"""
        added = skipped = merged_keys = 0
        rejected: list[str] = []
        for raw_key, senses in terms.items():
            try:
                r = self.merge_term(raw_key, senses, default_src=default_src)
            except core.InvariantError as exc:
                rejected.append(f"{raw_key!r}: {exc}")
                continue
            added += r["added"]
            skipped += r["skipped"]
            merged_keys += 1
        return {"ok": not rejected, "added": added, "skipped": skipped,
                "merged": merged_keys, "rejected": rejected}

    def bulk_merge(self, terms: dict[str, list[Any]], *, default_src: str = "api",
                   strict: bool = True) -> dict:
        """批量合并写入。

        strict=True 时是**全有或全无**：先把所有词都试算一遍，
        任何一个不合格就整批拒绝、一个字都不落盘（§4.2 #6「最坏情况=没改动」）。

        试算用纯函数，不碰 self.terms，所以天然安全。
        """
        plan: list[tuple[str, list[dict]]] = []
        rejected: list[str] = []
        for raw_key, senses in terms.items():
            try:
                key = self.canonicalize(raw_key)
                prepared = [core.normalize_sense(s, default_src=default_src)
                            for s in senses]
            except core.InvariantError as exc:
                rejected.append(f"{raw_key!r}: {exc}")
                continue
            # 在同一批里也不能出现重复的 (zh, fields, aspect)
            keys = {(s["zh"], tuple(s["fields"]), s["aspect"]) for s in prepared}
            if len(keys) != len(prepared):
                rejected.append(f"{raw_key!r}: 同一批内出现重复义项"
                                f"（去重键 zh+fields+aspect）")
                continue
            plan.append((key, prepared))

        if rejected and strict:
            raise core.InvariantError("；".join(rejected[:8]))
        if not plan:
            return {"ok": False, "added": 0, "skipped": 0, "merged": 0,
                    "rejected": rejected}

        added = skipped = 0
        with self._lock:
            backup_terms = {k: list(v) for k, v in self.terms.items()}
            try:
                for key, prepared in plan:
                    merged, stats = core.merge_senses(self.terms.get(key, []), prepared)
                    self.terms[key] = merged
                    added += stats["added"]
                    skipped += stats["skipped"]
                self._rebuild_index()
                self._save()
            except Exception:
                self.terms = backup_terms          # 落盘失败 → 整批回滚
                self._rebuild_index()
                raise
        return {"ok": not rejected, "added": added, "skipped": skipped,
                "merged": len(plan), "rejected": rejected}

    # -------------------------------------------------- 序列化
    def snapshot(self) -> dict:
        with self._lock:
            counts = self.counts()
            meta = dict(self.meta)
            terms = {k: [dict(s) for s in v] for k, v in self.terms.items()}
        meta.update({"version": 2, "counts": counts,
                     "updated": time.strftime("%Y-%m-%d")})
        meta.setdefault("note", "key 用小写规范形；fields 是领域列表；"
                                "src: curated=人工确认 / api=模型生成待复核")
        return {"meta": meta, "terms": terms}

    def _save(self) -> None:
        """原子写入：先写临时文件 + fsync，再 os.replace 覆盖。

        直接写目标文件的话，中途失败会留下半个 JSON（「改坏了」）；
        §4.2 #6 要求最坏情况必须是「没改动」。
        """
        payload = json.dumps(self.snapshot(), ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp, self.path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _replace_with_retry(src: Path, dst: Path, attempts: int = 5) -> None:
    """Windows 上目标可能被杀毒/编辑器短暂占用，重试几次。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:          # WinError 5/32
            last = exc
            time.sleep(0.05 * (i + 1))
    if last:
        raise last


# -------------------------------------------------- 全局单例
_store: GlossaryStore | None = None
_store_lock = threading.Lock()


def get_store() -> GlossaryStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = GlossaryStore()
        return _store


def reset_store(path: Path | None = None) -> GlossaryStore:
    """测试用：丢弃单例并重新载入。"""
    global _store
    with _store_lock:
        _store = GlossaryStore(path)
        return _store
