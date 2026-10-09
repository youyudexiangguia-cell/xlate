/**
 * e2e.mjs —— 端到端验证：HTTP 服务 → 词库 → 用户脚本的查找逻辑
 *
 * 这里的 normTerm / 索引 / resolveKey 必须和 xlate.user.js 里的实现逐字一致。
 */
const URL = process.env.GLOSSARY_URL || 'http://127.0.0.1:8765/glossary.json';

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

const res = await fetch(URL);
console.log('  HTTP         :', res.status);
console.log('  Content-Type :', res.headers.get('content-type'));
const text = await res.text();
console.log('  字节数       :', Buffer.byteLength(text, 'utf8'));

const g = JSON.parse(text);
const terms = g.terms;
const keys = Object.keys(terms);
const senses = keys.reduce((n, k) => n + terms[k].length, 0);
console.log(`  词条/义项     : ${keys.length} / ${senses}`);
console.log(`  meta         : ${JSON.stringify(g.meta?.counts || {})}`);

/* 建索引（与用户脚本一致） */
const IDX = new Map();
for (const k of keys) {
  if (!IDX.has(k)) IDX.set(k, k);
  const l = k.replace(/[\s_\-]/g, '');
  if (!IDX.has(l)) IDX.set(l, k);
}
const resolveKey = (t) => {
  const n = normTerm(t);
  if (IDX.has(n)) return IDX.get(n);
  const l = n.replace(/[\s_\-]/g, '');
  return IDX.has(l) ? IDX.get(l) : null;
};

/* 抽查 */
const CASES = [
  ['CORS', true], ['tree-shaking', true], ['tree shaking', true],
  ['back-pressure', true], ['backpressure', true], ['hotspot', true],
  ['profile', true], ['bikeshedding', true], ['bikeshed', true],
  ['self-signed', true], ['(ACID)', true], ['N+1 query', true],
  ['memoization', true], ['iteration', true], ['spinlock', true],
  ['LGTM', true], ['MTLS', true], ['DevDependency', true],
  ['foobar', false], ['read-only', false],
];

console.log('\n  抽查：');
let bad = 0;
for (const [t, expect] of CASES) {
  const k = resolveKey(t);
  const ok = !!k === expect;
  if (!ok) bad++;
  const detail = k ? `key="${k}"  ${terms[k].length} 条` : '未命中 → 走 API';
  console.log(`   ${ok ? '✅' : '❌'} ${JSON.stringify(t).padEnd(18)} → ${detail}`);
}

console.log(bad ? `\n  ❌ ${bad} 项不符` : '\n  ✅ 端到端链路正常：HTTP → 词库 → 查找');
process.exit(bad ? 1 : 0);
