/**
 * verify.mjs —— 词库查找链路验证
 *
 * 两级查找（必须和 xlate.user.js 完全一致）：
 *   ① 规范形    normTerm(text)            处理 大小写 / 首尾标点 / 分隔符差异
 *   ② 去分隔符形 normTerm(text) 去掉空格   处理 backpressure ↔ back-pressure ↔ back pressure
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = path.dirname(fileURLToPath(import.meta.url));
const g = JSON.parse(fs.readFileSync(path.join(DIR, 'glossary.json'), 'utf8'));
const terms = g.terms;
const keys = Object.keys(terms);

/* ---- 与 xlate.user.js 完全一致 ---- */
function normTerm(s) {
  return String(s)
    .trim()
    .toLowerCase()
    .replace(/^[\s"'`(\[]+/, '')
    .replace(/[\s"'`)\].,;:!?]+$/, '')
    .replace(/[\s_\-]+/g, ' ')
    .trim();
}
const loose = (s) => s.replace(/[\s_\-]/g, '');

/* ---- 建索引 ---- */
const INDEX = new Map();
for (const k of keys) {
  if (!INDEX.has(k)) INDEX.set(k, k);
  const l = loose(k);
  if (!INDEX.has(l)) INDEX.set(l, k);
}
function resolveKey(text) {
  const n = normTerm(text);
  if (INDEX.has(n)) return INDEX.get(n);
  const l = loose(n);
  if (INDEX.has(l)) return INDEX.get(l);
  return null;
}
function lookup(text) {
  const k = resolveKey(text);
  return k ? terms[k] : null;
}

let fail = 0;

/* ---- 1) 不动点 ---- */
console.log('=== 1) 不动点检查 ===');
const notFixed = keys.filter((k) => normTerm(k) !== k);
if (notFixed.length) { fail++; console.log(`  ❌ ${notFixed.length} 个 key 不规范：`); notFixed.slice(0, 10).forEach((k) => console.log(`     "${k}" → "${normTerm(k)}"`)); }
else console.log(`  ✅ 全部 ${keys.length} 个 key 都是规范形`);

/* ---- 2) 严格撞车（规范形相同的不同 key）---- */
console.log('\n=== 2) 严格撞车检查 ===');
const byNorm = new Map();
for (const k of keys) { const n = normTerm(k); if (!byNorm.has(n)) byNorm.set(n, []); byNorm.get(n).push(k); }
const clash = [...byNorm.entries()].filter(([, v]) => v.length > 1);
if (clash.length) { fail++; console.log(`  ❌ ${clash.length} 组`); clash.slice(0, 10).forEach(([n, v]) => console.log(`     "${n}" ← ${v.join(' | ')}`)); }
else console.log('  ✅ 无');

/* ---- 3) 宽松索引撞车（只警告，不判失败：规范形优先）---- */
console.log('\n=== 3) 宽松索引撞车（仅警告）===');
const byLoose = new Map();
for (const k of keys) { const l = loose(normTerm(k)); if (!byLoose.has(l)) byLoose.set(l, []); byLoose.get(l).push(k); }
const looseClash = [...byLoose.entries()].filter(([, v]) => v.length > 1);
if (looseClash.length) { console.log(`  ⚠️ ${looseClash.length} 组（规范形优先，后者可能查不到）`); looseClash.slice(0, 10).forEach(([l, v]) => console.log(`     "${l}" ← ${v.join(' | ')}`)); }
else console.log('  ✅ 无');

/* ---- 4) 模拟真实选区 ---- */
console.log('\n=== 4) 模拟"用户在页面上选中这些文本" ===');
const SAMPLES = [
  ['CORS', true], ['cors', true], ['CORS.', true], ['(ACID)', true],
  ['tree-shaking', true], ['tree_shaking', true], ['Tree Shaking', true],
  ['self-signed', true], ['multi-tenant', true], ['at-least-once', true],
  ['back-pressure', true], ['backpressure', true], ['back pressure', true],
  ['hotspot', true], ['hot spot', true], ['auto-scaling', true], ['autoscaling', true],
  ['profile', true], ['"profile"', true], ['  mutex  ', true],
  ['DevDependency', true], ['N+1 query', true], ['HEAD-OF-LINE BLOCKING', true],
  ['bikeshedding', true], ['bikeshed', true], ['LGTM', true], ['nit:', true],
  ['virtual DOM', true], ['optimistic UI', true], ['keep-alive', true],
  ['zero-day', true], ['blue-green', true], ['MTLS', true], ['rbac', true],
  ['trace', true], ['tracing', false], ['memoize', true], ['memoization', true],
  ['iteration', true], ['hang', true], ['spinlock', true], ['shard', true],
  // 应当漏到 API
  ['read-only', false], ['foobar', false], ['xyzzy', false],
];
let hit = 0, miss = 0, wrong = 0;
for (const [text, expect] of SAMPLES) {
  const k = resolveKey(text);
  const got = !!k;
  const ok = got === expect;
  if (!ok) { wrong++; fail++; }
  if (got) hit++; else miss++;
  const shown = got ? `命中 ${terms[k].length} 条  (key="${k}")` : '未命中（走 API）';
  console.log(`  ${ok ? '✅' : '❌'} ${JSON.stringify(text).padEnd(24)} → ${JSON.stringify(normTerm(text)).padEnd(24)} ${shown}`);
}
console.log(`\n  命中 ${hit} / 未命中 ${miss} / 判定错误 ${wrong}`);

/* ---- 5) 概览 ---- */
console.log('\n=== 5) 词库概览 ===');
const senses = keys.reduce((n, k) => n + terms[k].length, 0);
const multi = keys.filter((k) => terms[k].length > 1);
const byField = {};
for (const k of keys) for (const s of terms[k]) byField[s.field || '(无)'] = (byField[s.field || '(无)'] || 0) + 1;
console.log(`  词条 ${keys.length} / 义项 ${senses} / 一词多义 ${multi.length}`);
console.log(`  多义词: ${multi.map((k) => `${k}(${terms[k].length})`).join(', ')}`);
console.log('  义项最多的领域:');
Object.entries(byField).sort((a, b) => b[1] - a[1]).slice(0, 12).forEach(([f, n]) => console.log(`     ${String(n).padStart(4)}  ${f}`));
const fromApi = keys.filter((k) => terms[k].every((s) => s.src === 'api'));
console.log(`  纯 API 来源: ${fromApi.length}`);

console.log(fail ? `\n❌ ${fail} 项未通过` : '\n✅ 全部通过');
process.exit(fail ? 1 : 0);
