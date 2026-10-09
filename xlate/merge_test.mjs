/**
 * merge_test.mjs —— 客户端薄脚本与后端的**契约测试**
 *
 * 【为什么改名不改文件名】v0.5.x 时这个文件测的是脚本里那份**重复实现**
 * （mergeSenses/validateSense）。v1.0.0 把那套实现删了、收敛到后端，
 * 于是"两份实现会不会漂移"这个风险消失了。
 *
 * 但**新的边界风险**出现了，而且更隐蔽：
 *   · 脚本的 normTerm 必须与后端 core.norm_term 完全一致
 *     否则「选中 → 命中」会在边界上漏（脚本算出的 key 与后端不一致）
 *   · 而且 normTerm 必须是**幂等**的（norm(norm(x)) === norm(x)）
 *   · 脚本里不许再出现 Key / prompt / 内嵌词库（回归防护）
 *
 * 【与 Python 对照的方式】沙箱禁止 Node 通过管道 spawn 子进程
 * （child_process 默认 stdio:'pipe' → EPERM，见 AGENT.md §2.3），
 * 所以这里**用文件交换**，分两步跑（norm_check.py 负责中间那步）：
 *
 *     node  xlate/merge_test.mjs --samples     # 写出待测样本 + JS 结果
 *     py    xlate/norm_check.py                # 算出 Python 真值并比对
 *
 * 不带参数时只跑「不依赖 Python」的那几组检查。
 */
import { readFileSync, mkdirSync, writeFileSync } from 'node:fs';

const SRC = readFileSync('xlate/xlate.user.js', 'utf8');
const GLOSSARY = JSON.parse(readFileSync('xlate/glossary.json', 'utf8'));
const TMP = 'xlate/.tmp-norm';

/* ---------- 从真实脚本里抽出函数（不复制，避免与真实现漂移）---------- */
function extractFn(name) {
  const start = SRC.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`脚本里找不到 ${name}`);
  const brace = SRC.indexOf('{', start);
  let depth = 0, i = brace;
  for (; i < SRC.length; i++) {
    if (SRC[i] === '{') depth++;
    else if (SRC[i] === '}') { depth--; if (depth === 0) break; }
  }
  return SRC.slice(start, i + 1);
}

// eslint-disable-next-line no-new-func
const { normTerm, normalizeBackend } = new Function(
  `${extractFn('normTerm')}\n${extractFn('normalizeBackend')}\n` +
  `return { normTerm, normalizeBackend };`
)();

/* ---------- 样本（含真实词库全部 key，保底覆盖）---------- */
const SAMPLES = [
  'CORS', 'cors', 'CORS.', '(ACID)', 'tree-shaking', 'tree_shaking', 'Tree Shaking',
  'back-pressure', 'backpressure', 'back pressure', 'hotspot', 'hot spot',
  'profile.', 'DevDependency', '"profile"', '  mutex  ', 'nit:', 'N+1 query',
  'HEAD-OF-LINE BLOCKING', 'read-only', 'thread-safe', 'dirty read', 'spread',
  'zzz', 'a', '', '   ', '  中文  ', 'Multi-Tenant', 'at-least-once',
  ...Object.keys(GLOSSARY.terms),
];

let pass = 0, fail = 0;
const check = (name, cond, extra = '') => {
  if (cond) { pass++; console.log(`  [OK]   ${name}${extra ? '  ' + extra : ''}`); }
  else { fail++; console.log(`  [FAIL] ${name}${extra ? '  ' + extra : ''}`); }
};
const section = (t) => console.log(`\n${'='.repeat(66)}\n ${t}\n${'='.repeat(66)}`);

/* ============================================================
 * 模式 A：--samples —— 只写出样本与 JS 结果，交给 norm_check.py 比对
 * ============================================================ */
if (process.argv.includes('--samples')) {
  mkdirSync(TMP, { recursive: true });
  writeFileSync(`${TMP}/samples.json`, JSON.stringify({
    samples: SAMPLES,
    js: SAMPLES.map((s) => normTerm(s)),
    glossaryKeys: Object.keys(GLOSSARY.terms),
  }, null, 2), 'utf8');
  console.log(`已写出 ${SAMPLES.length} 个样本到 ${TMP}/samples.json`);
  console.log('下一步： .venv\\Scripts\\python.exe xlate\\norm_check.py');
  process.exit(0);
}

/* ============================================================
 * 1. 与 Python 对照的结果（若上一步已跑过）
 * ============================================================ */
section('1. 脚本的 normTerm 与后端 core.norm_term 逐字一致');

let pyTruth = null;
try {
  pyTruth = JSON.parse(readFileSync('xlate/.norm-truth.json', 'utf8'));
} catch (e) { /* 还没跑 Python 那步 */ }

if (!pyTruth) {
  console.log('  [SKIP] 尚未生成真值。先跑：');
  console.log('         node xlate/merge_test.mjs --samples');
  console.log('         .venv\\Scripts\\python.exe xlate\\norm_check.py');
} else {
  check('样本数与 Python 一致', pyTruth.norm.length === SAMPLES.length,
    `${SAMPLES.length} 个`);
  const mismatch = [];
  for (let i = 0; i < SAMPLES.length; i++) {
    const js = normTerm(SAMPLES[i]);
    if (js !== pyTruth.norm[i]) {
      mismatch.push({ input: SAMPLES[i], js, py: pyTruth.norm[i] });
    }
  }
  check('JS 与 Python 归一化结果完全一致', mismatch.length === 0,
    mismatch.length ? JSON.stringify(mismatch.slice(0, 5))
                    : `${SAMPLES.length} 个样本全等`);
}

/* ============================================================ */
section('2. normTerm 的工程性质');

check('幂等：norm(norm(x)) === norm(x)',
  SAMPLES.every((s) => normTerm(normTerm(s)) === normTerm(s)));

check('对全部词库 key 是不动点',
  Object.keys(GLOSSARY.terms).every((k) => normTerm(k) === k));

check('确定性：同输入同输出',
  SAMPLES.every((s) => normTerm(s) === normTerm(s)));

check('结果不含首尾空白',
  SAMPLES.every((s) => normTerm(s) === normTerm(s).trim()));

check('结果不含大写字母',
  SAMPLES.every((s) => normTerm(s) === normTerm(s).toLowerCase()));

check('结果不含下划线或连字符（已归一为空格）',
  SAMPLES.every((s) => !/[_\-]/.test(normTerm(s))));

/* ============================================================ */
section('3. 归一化后不撞车（否则有 key 永远查不到）');

const loose = new Map();
for (const k of Object.keys(GLOSSARY.terms)) {
  const l = normTerm(k);
  if (!loose.has(l)) loose.set(l, []);
  loose.get(l).push(k);
}
const collided = [...loose.entries()].filter(([, v]) => v.length > 1);
check('480 个 key 归一化后互不撞车', collided.length === 0,
  collided.length ? JSON.stringify(collided.slice(0, 3)) : '');

/* ============================================================ */
section('4. 后端地址归一化');

const BASE_CASES = [
  ['127.0.0.1:8765', 'http://127.0.0.1:8765'],
  ['http://127.0.0.1:8765', 'http://127.0.0.1:8765'],
  ['http://127.0.0.1:8765/', 'http://127.0.0.1:8765'],
  ['http://127.0.0.1:8765/api/lookup', 'http://127.0.0.1:8765'],
  ['localhost:8765', 'http://localhost:8765'],
  ['http://192.168.1.9:8765/', 'http://192.168.1.9:8765'],
  ['"http://127.0.0.1:8765"', 'http://127.0.0.1:8765'],
];
for (const [input, expect] of BASE_CASES) {
  const got = normalizeBackend(input);
  check(`normalizeBackend(${JSON.stringify(input)})`, got === expect, `-> ${got}`);
}

/* ============================================================ */
section('5. 脚本里不得再有机密 / 重复实现（回归防护）');

const FORBIDDEN = [
  ['直连模型 API', /api\.deepseek\.com/],
  ['Authorization 头', /Authorization/],
  ['prompt 模板', /buildPrompt/],
  ['读本地 API Key', /getApiKey/],
  ['内嵌词库种子', /SEED_TERMS/],
  ['本地合并实现', /function\s+mergeSenses/],
  ['本地校验实现', /function\s+validateSense/],
  ['热加载实现', /hotReload/],
];
for (const [label, re] of FORBIDDEN) {
  // 注释里提到这些名字是允许的（那段是"删除了什么"的说明），
  // 所以只在**去掉注释后**的代码里找。
  const code = SRC
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  check(`不含${label}`, !re.test(code));
}

check('声明了 GM_xmlhttpRequest', /@grant\s+GM_xmlhttpRequest/.test(SRC));
check('有 @noframes（防隐藏 iframe 响应）', /@noframes/.test(SRC));
check('版本为 1.0.0', /@version\s+1\.0\.0/.test(SRC));

/* ---------- 零 innerHTML（TT 纪律）---------- */
const sink = /\.innerHTML\s*=|insertAdjacentHTML\s*\(/.test(SRC);
check('脚本无 innerHTML sink', !sink);

/* ============================================================ */
console.log(`\n${'='.repeat(66)}`);
console.log(fail === 0 ? ` 全部通过 ✅  (${pass} 项)` : ` 有失败 ❌  (通过 ${pass} / 失败 ${fail})`);
console.log('='.repeat(66));
process.exit(fail ? 1 : 0);
