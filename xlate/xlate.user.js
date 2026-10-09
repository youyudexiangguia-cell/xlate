// ==UserScript==
// @name         技术术语译查 (xlate) — v1.0.0 薄客户端
// @namespace    local.xlate
// @version      1.0.0
// @description  选中英文技术词 → 调 xlate 后端 → 就地显示面板。不含数据、不含 prompt、不含 Key。
// @author       local
// @match        *://*/*
// @run-at       document-idle
// @noframes
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @grant        GM_deleteValue
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

/* ============================================================
 * v1.0.0 —— 从「自带数据与模型调用」改为「后端薄客户端」
 *
 * 【为什么要改】旧版（v0.5.x）是一个**独立实现**：它自己内嵌词库种子、
 * 自己维护两级索引、自己拼 prompt、自己直连 DeepSeek、自己合并入库。
 * 于是同一套业务逻辑在两个地方各写一遍（JS 一份、FastAPI 一份），
 * 谁改了另一边不跟着改就行为不一致 —— 这就是"两套并存"的问题。
 *
 * 【现在】脚本只做三件事：
 *   1. 选中 → 归一化（与后端 core.norm_term 同一套规则）
 *   2. GET 后端 /api/lookup
 *   3. 把结果显示出来
 *
 * 由此带来的好处：
 *   · 脚本里**没有**词库、prompt、API Key —— 所以永远不用重贴
 *   · 词库优先、LLM 兜底、校验、合并入库全部由后端一处负责
 *   · 换模型、改 prompt、修 bug 只改后端，脚本不用动
 *
 * 【删除的东西】内嵌种子、热加载、本地索引、remember/mergeSenses/
 * validateSense、buildPrompt、askApi、getApiKey、导入导出菜单。
 * 这些能力全在后端，脚本不该有第二份。
 * ============================================================ */

(() => {
  'use strict';

  const TAG = '[xlate]';
  const log = (...a) => console.log(TAG, ...a);
  const warn = (...a) => console.warn(TAG, ...a);

  const MIN_TERM_LEN = 2;
  const MAX_TERM_LEN = 60;
  const LOOKUP_TIMEOUT_MS = 40000;   // LLM 兜底可能要 20~30s

  /* ============================================================
   * 后端地址
   * ============================================================ */
  const DEFAULT_BACKEND = 'http://127.0.0.1:8765';

  /** 把用户输入统一成可请求的 origin（去掉路径和尾斜杠）。 */
  function normalizeBackend(raw) {
    let s = String(raw == null ? '' : raw).trim();
    if (!s) return '';
    s = s.replace(/^["'`]+|["'`]+$/g, '').trim();
    if (!/^https?:\/\//i.test(s)) {
      // 裸 host:port / localhost / 127.0.0.1 → 补 http://
      s = 'http://' + s;
    }
    // 去掉路径、查询、尾斜杠：只留 protocol://host:port
    const m = s.match(/^(https?:\/\/[^/?#]+)/i);
    return m ? m[1] : s;
  }

  function getBackend() {
    const raw = GM_getValue('backendUrl', '') || DEFAULT_BACKEND;
    return normalizeBackend(raw);
  }
  function setBackend(url) {
    GM_setValue('backendUrl', normalizeBackend(url));
  }

  /* ============================================================
   * 归一化 —— 必须与后端 core.norm_term 完全一致
   *   顺序要紧：先剥标点，再归一化分隔符（"tree-shaking." → "tree shaking"）
   * ============================================================ */
  function normTerm(s) {
    return String(s == null ? '' : s)
      .trim()
      .toLowerCase()
      .replace(/^[\s"'`(\[]+/, '')
      .replace(/[\s"'`)\].,;:!?]+$/, '')
      .replace(/[\s_\-]+/g, ' ')
      .trim();
  }

  /* ============================================================
   * 视口工具
   * ============================================================ */
  function viewportSize() {
    const de = document.documentElement;
    const w = Math.max(window.innerWidth || 0, de ? de.clientWidth || 0 : 0);
    const h = Math.max(window.innerHeight || 0, de ? de.clientHeight || 0 : 0);
    return { w, h };
  }

  /* ============================================================
   * 后端调用
   * ============================================================ */
  function fetchLookup(term, overrideBase) {
    const base = normalizeBackend(overrideBase || getBackend());
    if (!base) {
      return Promise.reject(new Error('还没设置后端地址。\n菜单 → ⚙️ 设置后端地址'));
    }
    const url = base + '/api/lookup?term=' + encodeURIComponent(term);
    log('请求后端:', url);

    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'GET',
        url,
        timeout: LOOKUP_TIMEOUT_MS,
        onload(res) {
          let body = null;
          try { body = JSON.parse(res.responseText || '{}'); } catch (e) { body = null; }

          if (res.status === 200 && body) return resolve(body);

          // 把后端的 detail 原样透出来，排查时最有用
          const detail = body && body.detail
            ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail))
            : String(res.responseText || '').slice(0, 200);

          if (res.status === 503) {
            return reject(new Error('后端未配置 LLM Key。\n\n' + detail +
              '\n\n（词库里已有的词仍可查；新词需要在服务端 .env 里填 DEEPSEEK_API_KEY）'));
          }
          if (res.status === 422) return reject(new Error('模型输出未通过校验：\n' + detail));
          if (res.status === 404) return reject(new Error('后端没有这个接口（版本不匹配？）'));
          reject(new Error('HTTP ' + res.status + '：' + detail));
        },
        onerror: () => reject(new Error(
          '连不上后端：' + base +
          '\n\n检查：\n① 后端是否在运行（xlate\\run.py）\n② 地址/端口对不对（菜单 → ⚙️ 设置后端地址）')),
        ontimeout: () => reject(new Error('请求超时（' + Math.round(LOOKUP_TIMEOUT_MS / 1000) + 's）\n' + base)),
      });
    });
  }

  /* ============================================================
   * CSS
   * ============================================================ */
  const CSS = `
    :host {
      all: initial;
      display: block !important;
      position: fixed !important;
      top: 0 !important; left: 0 !important; right: 0 !important; bottom: 0 !important;
      margin: 0 !important; padding: 0 !important; border: 0 !important;
      background: transparent !important;
      pointer-events: none;
      z-index: 2147483647;
      font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
      font-size: 13px; line-height: normal; color: #111827;
      text-align: left; direction: ltr; visibility: visible; opacity: 1;
    }
    * {
      box-sizing: border-box; margin: 0; padding: 0;
      font-family: inherit; font-size: inherit; font-weight: inherit; font-style: inherit;
      line-height: normal; letter-spacing: normal; word-spacing: normal; text-indent: 0;
      color: inherit; text-align: left; text-transform: none; text-decoration: none;
      white-space: normal; list-style: none; float: none; vertical-align: baseline;
      border: 0; background: transparent; max-width: none; min-width: 0;
    }

    .bubble {
      display: block; position: absolute; pointer-events: auto; cursor: pointer;
      background: #1f2937; color: #ffffff; border-radius: 6px;
      padding: 3px 9px; font-size: 12px; line-height: 1.6;
      box-shadow: 0 2px 8px rgba(0,0,0,.3); user-select: none;
    }
    .bubble:hover { background: #374151; }

    .panel {
      display: block; position: absolute; pointer-events: auto;
      width: 320px; overflow: auto;
      background: #ffffff; color: #111827; border: 1px solid #d1d5db;
      border-radius: 8px; box-shadow: 0 8px 28px rgba(0,0,0,.22); font-size: 13px;
    }
    .hd {
      display: flex; align-items: center; gap: 6px;
      padding: 8px 10px; border-bottom: 1px solid #e5e7eb; background: #ffffff;
    }
    .term { display: block; font-weight: 600; font-size: 14px; flex: 1; word-break: break-all; }
    .tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 10px; white-space: nowrap; }
    .tag.local { background: #dcfce7; color: #166534; }
    .tag.api   { background: #fef3c7; color: #92400e; }
    .tag.err   { background: #fee2e2; color: #991b1b; }
    .close { display: inline-block; cursor: pointer; color: #9ca3af; font-size: 16px; padding: 0 2px; }
    .close:hover { color: #374151; }

    .body { display: block; padding: 4px 0; }
    .sense { display: block; padding: 7px 10px; border-bottom: 1px solid #f3f4f6; }
    .sense:last-child { border-bottom: 0; }
    .row1 { display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
    .field { display: inline-block; font-size: 11px; color: #3730a3;
             background: #e0e7ff; padding: 1px 6px; border-radius: 10px; white-space: nowrap; }
    .zh { display: inline-block; font-weight: 600; color: #1d4ed8; }
    .note { display: block; color: #4b5563; margin-top: 3px; line-height: 1.5; white-space: pre-wrap; }
    .msg { display: block; padding: 14px 10px; color: #6b7280; text-align: center; }
    .msg.err { color: #b91c1c; text-align: left; white-space: pre-wrap; line-height: 1.55; }
    .src { display: block; padding: 5px 10px; color: #9ca3af; font-size: 11px;
           border-top: 1px solid #f3f4f6; }
  `;

  /* ============================================================
   * 挂载
   * ============================================================ */
  let host = null, shadow = null;

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  const clearNode = (node) => { if (node) node.replaceChildren(); };

  function ttStatus() {
    if (!window.trustedTypes) return '不支持（浏览器较旧）';
    try {
      const p = window.trustedTypes.createPolicy('xlate-probe-' + Date.now(), { createHTML: (s) => s });
      const enforced = typeof window.trustedTypes.getPolicyNames === 'function'
        ? window.trustedTypes.getPolicyNames().length > 0 : true;
      return '可用（未必强制）' + (enforced ? '' : '');
    } catch (e) { return '检测异常：' + e.message; }
  }

  function mount() {
    const { w, h } = viewportSize();
    if (w < 200 || h < 120) {
      warn(`视口过小（${w}x${h}），跳过挂载。`);
      return false;
    }
    host = el('div');
    host.setAttribute('data-xlate-host', '1');
    const parent = document.body || document.documentElement;
    if (!parent) { warn('找不到可挂载的父节点'); return false; }
    parent.appendChild(host);
    shadow = host.attachShadow({ mode: 'open' });
    shadow.appendChild(el('style', null, CSS));
    log(`已挂载 | 视口 ${w}x${h} | 后端 ${getBackend()}`);
    return true;
  }

  if (!mount()) return;

  let bubbleEl = null, panelEl = null;
  const removeBubble = () => { if (bubbleEl) { bubbleEl.remove(); bubbleEl = null; } };
  const removePanel = () => { if (panelEl) { panelEl.remove(); panelEl = null; } };

  /* ============================================================
   * 渲染（零 innerHTML）
   * ============================================================ */
  function setMessage(node, text, isErr) {
    clearNode(node);
    node.appendChild(el('div', 'msg' + (isErr ? ' err' : ''), text));
  }

  function renderSenses(bodyEl, senses) {
    if (!bodyEl) throw new Error('内部错误：body 容器为空');
    if (!Array.isArray(senses)) throw new Error('内部错误：senses 不是数组');
    clearNode(bodyEl);
    let n = 0;
    for (const s of senses) {
      if (!s) continue;
      const d = el('div', 'sense');
      const r1 = el('div', 'row1');

      const labels = (Array.isArray(s.fields) && s.fields.length) ? s.fields.slice() : [];
      if (s.aspect) labels.push(s.aspect);
      for (const f of labels) r1.appendChild(el('span', 'field', f));

      r1.appendChild(el('span', 'zh', s.zh || s.translation || ''));
      d.appendChild(r1);

      const noteText = s.note || s.desc || '';
      if (noteText) d.appendChild(el('div', 'note', noteText));
      bodyEl.appendChild(d);
      n++;
    }
    return n;
  }

  function buildPanel(rect, rawText) {
    const p = el('div', 'panel');
    const hd = el('div', 'hd');
    hd.appendChild(el('div', 'term', String(rawText).trim()));

    const closeBtn = el('span', 'close', '✕');
    closeBtn.addEventListener('click', removePanel);
    hd.appendChild(closeBtn);
    p.appendChild(hd);

    const bodyEl = el('div', 'body');
    p.appendChild(bodyEl);

    const { w: vw, h: vh } = viewportSize();
    const safeVw = vw || 1024;
    const safeVh = vh || 800;

    const maxH = vh >= 320 ? Math.min(520, vh - 80) : 520;
    p.style.maxHeight = maxH + 'px';

    const boxW = 320, boxH = 200;
    let top = (rect ? rect.bottom : 60) + 8;
    if (top + boxH > safeVh) top = Math.max(8, (rect ? rect.top : 60) - boxH - 8);
    let left = rect ? rect.left : 60;
    if (left + boxW > safeVw) left = Math.max(8, safeVw - boxW - 8);
    p.style.top = top + 'px';
    p.style.left = left + 'px';

    shadow.appendChild(p);
    panelEl = p;
    return { p, hd, bodyEl, closeBtn };
  }

  function addTag(hd, closeBtn, text, cls) {
    const tag = el('span', 'tag ' + cls, text);
    hd.insertBefore(tag, closeBtn);
    return tag;
  }

  function showError(ui, msg) {
    addTag(ui.hd, ui.closeBtn, '出错', 'err');
    setMessage(ui.bodyEl, msg, true);
    warn('面板错误:', msg);
  }

  /* ============================================================
   * 主流程
   * ============================================================ */
  async function openPanel(rect, rawText) {
    removePanel();
    const term = normTerm(rawText);
    const ui = buildPanel(rect, rawText);
    setMessage(ui.bodyEl, '查询中…');

    try {
      const data = await fetchLookup(term);
      if (!ui.p.isConnected) return;

      clearNode(ui.bodyEl);
      if (data.source === 'glossary') {
        addTag(ui.hd, ui.closeBtn, '本地词库', 'local');
      } else {
        addTag(ui.hd, ui.closeBtn, 'API · 已入库', 'api');
      }
      ui.bodyEl.parentNode.style.maxHeight =
        (viewportSize().h >= 320 ? Math.min(520, viewportSize().h - 80) : 520) + 'px';
      renderSenses(ui.bodyEl, data.senses || []);

      // 来源与命中 key 放进页脚，让「词库优先是否生效」可见
      const bits = [];
      if (data.matched_key) bits.push('key: ' + data.matched_key);
      if (data.source) bits.push('来源: ' + data.source);
      if (data.stats) {
        bits.push('新增 ' + data.stats.added + ' / 跳过 ' + data.stats.skipped);
      }
      if (bits.length) ui.p.appendChild(el('div', 'src', bits.join('　·　')));
    } catch (err) {
      if (!ui.p.isConnected) return;
      showError(ui, err && err.message ? err.message : String(err));
    }
  }

  /* ============================================================
   * 事件
   * ============================================================ */
  function showBubble(rect, text) {
    removeBubble();
    const b = el('div', 'bubble', '译');
    b.addEventListener('mousedown', (e) => e.preventDefault());
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      removeBubble();
      openPanel(rect, text);
    });
    const { w: vw } = viewportSize();
    const safeVw = vw || 1024;
    b.style.top = Math.max(4, rect.top - 30) + 'px';
    b.style.left = Math.min(safeVw - 40, Math.max(4, rect.left + rect.width / 2 - 12)) + 'px';
    shadow.appendChild(b);
    bubbleEl = b;
  }

  const inWidget = (e) => e.target === host || (host && host.contains(e.target));

  document.addEventListener('mouseup', (e) => {
    if (inWidget(e) || e.button !== 0) return;
    setTimeout(() => {
      try {
        const sel = window.getSelection();
        const text = sel ? sel.toString().trim() : '';
        if (!text || text.length < MIN_TERM_LEN || text.length > MAX_TERM_LEN ||
            !/[A-Za-z]/.test(text) || !sel.rangeCount) {
          removeBubble();
          return;
        }
        showBubble(sel.getRangeAt(0).getBoundingClientRect(), text);
      } catch (err) { warn('mouseup 处理异常:', err); }
    }, 10);
  });

  document.addEventListener('mousedown', (e) => { if (!inWidget(e)) removePanel(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { removeBubble(); removePanel(); }
    // Ctrl/Cmd+Shift+X：直接查当前选区，不用等气泡
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'X' || e.key === 'x')) {
      const sel = window.getSelection();
      const text = sel ? sel.toString().trim() : '';
      if (text && sel.rangeCount) {
        e.preventDefault();
        openPanel(sel.getRangeAt(0).getBoundingClientRect(), text);
      }
    }
  });

  /* ============================================================
   * 菜单
   * ============================================================ */
  GM_registerMenuCommand('⚙️ 设置后端地址', () => {
    const cur = getBackend();
    const input = window.prompt(
      'xlate 后端地址（只填基地址，不要带路径）：\n\n' +
      '· 本机     http://127.0.0.1:8765\n' +
      '· 局域网   http://192.168.x.x:8765\n\n' +
      '当前：',
      cur || DEFAULT_BACKEND
    );
    if (input === null) return;
    const norm = normalizeBackend(input);
    if (!norm) return alert('已清空，脚本将无法查询（再设一次即可）。');
    setBackend(norm);

    // 立刻探一次，直接告诉用户行不行
    fetchLookup('profile', norm)
      .then((d) => alert(`✅ 后端可用\n\n${norm}\n词条命中：${d.matched_key || d.term}\n来源：${d.source}`))
      .catch((e) => alert(`⚠️ 已保存，但连接测试失败：\n\n${e && e.message ? e.message : e}`));
  });

  GM_registerMenuCommand('🩺 诊断后端', () => {
    const base = getBackend();
    if (!base) return alert('还没设置后端地址。\n用菜单「⚙️ 设置后端地址」。');
    GM_xmlhttpRequest({
      method: 'GET',
      url: base + '/api/health',
      timeout: 10000,
      onload(res) {
        let extra = '';
        try {
          const h = JSON.parse(res.responseText);
          extra = '\n\n词库：' + h.terms + ' 词\nLLM：' +
            (h.llm_configured ? '✅ 已配置' : '❌ 未配置（新词会 503）') +
            '\n词库文件：' + h.glossary_path;
        } catch (e) { extra = '\n\n（health 返回不是 JSON）'; }
        alert(`地址：${base}\nHTTP：${res.status}${extra}`);
      },
      onerror: () => alert(
        `地址：${base}\n\n❌ 连不上\n\n检查：\n· 后端是否在运行（xlate\\run.py）\n· 端口是否一致\n· 防火墙`),
      ontimeout: () => alert(`❌ 超时 10s\n\n${base}`),
    });
  });

  GM_registerMenuCommand('🩺 自检：渲染一张测试面板', () => {
    try {
      removePanel();
      const ui = buildPanel({ top: 120, left: 120, bottom: 140, width: 60 }, '自检面板');
      addTag(ui.hd, ui.closeBtn, '自检', 'local');
      const demo = [
        { fields: ['网络'], aspect: '', zh: '背压', note: '下游处理不过来时向上游施加的压力' },
        { fields: ['前端'], aspect: 'JS', zh: '绑定', note: 'fn.bind(this)：固定执行上下文' },
      ];
      const n = renderSenses(ui.bodyEl, demo);
      const zhEl = ui.bodyEl.querySelector('.zh');
      const cs = zhEl ? getComputedStyle(zhEl) : null;
      const ps = getComputedStyle(ui.p);
      const hs = getComputedStyle(host);
      const r = (x) => x ? `${Math.round(x.width)}x${Math.round(x.height)} @ ${Math.round(x.left)},${Math.round(x.top)}` : 'null';
      const vp = viewportSize();
      const info = [
        `Trusted Types：${ttStatus()}`,
        `视口：${vp.w} x ${vp.h}   dpr=${window.devicePixelRatio}`,
        `后端：${getBackend() || '(未设置)'}`,
        ``,
        `【结构】渲染义项数 ${n}   body 子节点 ${ui.bodyEl.children.length}`,
        `【尺寸】host ${r(host.getBoundingClientRect())}`,
        `        .panel ${r(ui.p.getBoundingClientRect())}`,
        `        .zh ${r(zhEl ? zhEl.getBoundingClientRect() : null)}`,
        `【computed】host display=${hs.display} position=${hs.position} h=${hs.height}`,
        `        .panel h=${ps.height} maxH=${ps.maxHeight} overflow=${ps.overflow}`,
        `        .zh color=${cs ? cs.color : '?'} fontSize=${cs ? cs.fontSize : '?'}`,
        `【内容】.zh = ${zhEl ? JSON.stringify(zhEl.textContent) : '(找不到)'}`,
      ].join('\n');
      log('自检\n' + info);
      alert('自检完成（也打印在控制台）\n\n' + info);
    } catch (e) {
      alert('自检抛异常：' + (e && e.message ? e.message : e));
      console.error(TAG, e);
    }
  });

  GM_registerMenuCommand('🔬 检测本页 Trusted Types 策略', () => {
    alert('本页 Trusted Types 状态：\n\n' + ttStatus() +
      '\n\n（若为「强制」，则任何用 innerHTML 的脚本在本页都会失效；' +
      '本脚本全程零 innerHTML）');
  });

  /* 清理旧版遗留的机密：v0.5.x 把 DeepSeek Key 存在 GM 存储里，现在不再需要 */
  (function purgeLegacySecret() {
    try {
      if (typeof GM_deleteValue === 'function' && GM_getValue('apiKey', null) !== null) {
        GM_deleteValue('apiKey');
        log('已清除旧版残留的本地 apiKey（Key 现在只存在服务端）');
      }
    } catch (e) { /* 某些管理器不支持，忽略 */ }
  })();

  log(`已加载 v1.0.0 薄客户端 | 后端 ${getBackend() || '(未设置)'} | 本脚本不含词库/Key/prompt`);
})();
