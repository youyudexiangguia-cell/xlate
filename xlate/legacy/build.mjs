/**
 * build.mjs —— 把 glossary.json 内嵌进 xlate.user.js（仅作兜底种子）
 *
 * 用法（Windows）：
 *   & "C:\Users\14386\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe" build.mjs
 *   & "...\node.exe" build.mjs --seed 8      # 只内嵌 8 词，脚本更小（推荐）
 *   & "...\node.exe" build.mjs --seed 0      # 全部内嵌
 *
 * 作用：
 *   读 glossary.json → 生成 `const SEED_TERMS = {...}` → 替换 xlate.user.js 中
 *   两条标记之间的内容。标记之间的内容请勿手改，会被覆盖。
 *
 *   有了「📁 设置词库地址」之后，完整词库由本地服务提供，
 *   内嵌部分只需保留少量种子词兜底（服务没开时也能查几个高频词）。
 *   --seed N 就是控制这个数量：优先挑「一词多义」的词，信息量最大。
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DIR = path.dirname(fileURLToPath(import.meta.url));
const GLOSSARY = path.join(DIR, 'glossary.json');
const SCRIPT = path.join(DIR, 'xlate.user.js');

const BEGIN = '/* ===== GLOSSARY:BEGIN';
const END = '/* ===== GLOSSARY:END ===== */';

/* ---------- 解析参数 ---------- */
const argv = process.argv.slice(2);
if (argv.includes('--help') || argv.includes('-h')) {
  console.log('用法: node build.mjs [--seed N]');
  console.log('  --seed N  只内嵌 N 个词条作为兜底种子（0 = 全部内嵌，默认 0）');
  process.exit(0);
}

let seedLimit = 0;
const si = argv.indexOf('--seed');
if (si >= 0) {
  const v = parseInt(argv[si + 1], 10);
  if (!Number.isFinite(v) || v < 0) {
    console.error('❌ --seed 需要一个非负整数，例如 --seed 8');
    process.exit(1);
  }
  seedLimit = v;
}

/* ---------- 读词库 ---------- */
if (!fs.existsSync(GLOSSARY)) {
  console.error(`❌ 找不到 ${GLOSSARY}`);
  process.exit(1);
}

let glossary;
try {
  glossary = JSON.parse(fs.readFileSync(GLOSSARY, 'utf8'));
} catch (e) {
  console.error('❌ glossary.json 不是合法 JSON：' + e.message);
  process.exit(1);
}

const allTerms = glossary.terms;
if (!allTerms || typeof allTerms !== 'object' || Array.isArray(allTerms)) {
  console.error('❌ glossary.json 缺少合法的 terms 对象');
  process.exit(1);
}

/* ---------- 校验 ---------- */
const problems = [];
let senseCount = 0;
const allKeys = Object.keys(allTerms);

for (const k of allKeys) {
  const arr = allTerms[k];
  if (!Array.isArray(arr) || arr.length === 0) {
    problems.push(`  "${k}" 的值不是非空数组`);
    continue;
  }
  if (k !== k.toLowerCase()) problems.push(`  "${k}" 不是小写（查询会转小写，建议统一）`);
  for (const [i, s] of arr.entries()) {
    if (!s || typeof s !== 'object') { problems.push(`  "${k}"[${i}] 不是对象`); continue; }
    if (!s.zh && !s.translation) problems.push(`  "${k}"[${i}] 缺少 zh`);
    senseCount++;
  }
}

if (problems.length) {
  console.error('❌ 词库校验失败：');
  problems.forEach((p) => console.error(p));
  process.exit(1);
}

/* ---------- 决定内嵌哪些 ---------- */
// 优先"一词多义"的词（信息量最大），再补单义词，保证种子有代表性
let embedKeys = allKeys;
if (seedLimit > 0 && seedLimit < allKeys.length) {
  const multi = allKeys.filter((k) => allTerms[k].length > 1);
  const single = allKeys.filter((k) => allTerms[k].length === 1);
  embedKeys = [...multi, ...single].slice(0, seedLimit);
}

const terms = {};
for (const k of embedKeys) terms[k] = allTerms[k];

/* ---------- 生成内嵌代码 ---------- */
const literal = JSON.stringify(terms, null, 2)
  .split('\n')
  .map((line, i) => (i === 0 ? line : '  ' + line))
  .join('\n');

const note = embedKeys.length < allKeys.length
  ? `仅 ${embedKeys.length} 个兜底种子（词库共 ${allKeys.length} 词，完整内容走「📁 设置词库地址」）`
  : `全部 ${allKeys.length} 个词条`;

const block = `${BEGIN} —— 由 build.mjs 生成（${note}），请勿手改 ===== */
  const SEED_TERMS = ${literal};
  ${END}`;

/* ---------- 替换 ---------- */
const src = fs.readFileSync(SCRIPT, 'utf8');
const b = src.indexOf(BEGIN);
const e = src.indexOf(END);

if (b < 0 || e < 0 || e < b) {
  console.error(`❌ 在 ${path.basename(SCRIPT)} 中找不到 GLOSSARY 标记`);
  console.error(`   需要同时存在:\n     ${BEGIN} ... */\n     ${END}`);
  process.exit(1);
}

const out = src.slice(0, b) + block + src.slice(e + END.length);
fs.writeFileSync(SCRIPT, out, 'utf8');

/* ---------- 报告 ---------- */
const multiAll = allKeys.filter((k) => allTerms[k].length > 1);
console.log('✅ 构建完成');
console.log(`   词库总览 : ${allKeys.length} 词 / ${senseCount} 义项 / ${multiAll.length} 个一词多义`);
console.log(`   本次内嵌 : ${embedKeys.length} 词${embedKeys.length < allKeys.length ? '   (--seed ' + seedLimit + ')' : '   (全部)'}`);
console.log(`   内嵌词   : ${embedKeys.join(', ')}`);
console.log(`   脚本大小 : ${(out.length / 1024).toFixed(1)} KB`);
