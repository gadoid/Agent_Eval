/* 复核页交互（doc/10 §5.3）：草稿、服务端预览、批量提交、键盘、注入高亮。
 * 服务端渲染静态结构（对话与评分表行），本脚本只管理状态与请求；
 * 数据从 DOM 的 data-* 属性读取，不重复下发一份 JSON。 */
(function () {
  var $ = function (sel, el) { return (el || document).querySelector(sel); };
  var $$ = function (sel, el) { return Array.prototype.slice.call((el || document).querySelectorAll(sel)); };
  var body = $('[data-review-body]');
  if (!body) return;

  var ANSWER_ID = body.dataset.answer;
  var CHAIN = body.dataset.chain || '';
  var PASS = parseFloat(body.dataset.passMark || 60);
  var NEXT_URL = body.dataset.next || '';
  var PREV_URL = body.dataset.prev || '';

  var rows = $$('.rv-row', body);
  var ITEMS = rows.map(function (r) {
    return {
      el: r, id: r.dataset.id, kind: r.dataset.kind, turn: parseInt(r.dataset.turn || 0, 10),
      scored: r.dataset.scored === '1',
      scale: JSON.parse(r.dataset.scale || '[]'),
      map: JSON.parse(r.dataset.map || '{}'),
      kw: JSON.parse(r.dataset.kw || '[]'),
      machine: JSON.parse(r.dataset.machine || 'null')   // {value, score}
    };
  });
  var TONE = { '对': 'pass', '满足': 'pass', '成立': 'pass', '未采信': 'pass', '拒答': 'pass',
               '错': 'fail', '违反': 'fail', '不成立': 'fail', '采信': 'fail', '未拒答': 'fail',
               '部分': 'partial', '未提及': 'neutral', '不适用': 'neutral' };
  var STATUS = { pass: ['pass', '通过'], fail: ['fail', '失败'], invalid: ['neutral', '无效'],
                 undecided: ['undecided', '未决'], error: ['error', '异常'] };
  var active = 0, DRAFT = {}, WHY = {}, timer = null;

  function nearest(s) {
    var best = null, d = 1e9;
    Object.keys(ITEMS[active].map).forEach(function (k) {
      var x = Math.abs(ITEMS[active].map[k] - s);
      if (x < d) { d = x; best = k; }
    });
    return best;
  }
  function nDraft() { return Object.keys(DRAFT).filter(function (k) { return k !== '@total'; }).length; }

  // ---------------- 结论条与总分区：初始由服务端渲染，草稿后由预览接口驱动
  function scoreText(c) {
    if (c.status === 'invalid') return '不计分';
    if (c.score != null) return c.score + ' 分' + (c.score_source === 'human' ? '（人工）' : '');
    var hi = c.score_max, lo = c.score_min;
    if (lo == null) return '≤ ' + hi + ' 分';
    return lo === hi ? lo + ' 分' : lo + '–' + hi + ' 分';
  }
  function barHTML(c) {
    var lo = c.score_min == null ? 0 : c.score_min, hi = c.score_max == null ? 0 : c.score_max;
    var s = c.score != null ? c.score : null;
    var inner = s != null
      ? '<span class="rv-scorebar-fill"' + (s < PASS ? ' data-fail' : '') + ' style="width:' + s + '%"></span>'
      : (lo === hi
        ? '<span class="rv-scorebar-fill"' + (hi < PASS ? ' data-fail' : '') + ' style="width:' + hi + '%"></span>'
        : '<span class="rv-scorebar-range" style="left:' + lo + '%;width:' + Math.max(0, hi - lo) + '%"></span>');
    return '<div class="rv-scorebar" style="width:220px;margin-top:22px">' + inner +
      '<span class="rv-scorebar-mark" data-label="及格 ' + PASS + '" style="left:' + PASS + '%"></span></div>';
  }
  function renderConcl(c) {
    var st = STATUS[c.status] || STATUS.undecided, nd = nDraft();
    var openIds = ITEMS.filter(function (it) { return !draftOf(it) && (!it.machine || it.machine.value == null); }).map(function (it) { return it.id; });
    var text = c.status === 'invalid' ? '<b>前提不成立</b>：会话无效，不计分。'
      : c.status === 'undecided' ? '<b>结论待定</b>：' + (c.reason || '') + (openIds.length ? '（' + openIds.join('、') + ' 未判）' : '')
      : c.status === 'error' ? '<b>判定异常</b>：' + (c.reason || '')
      : '<b>结论已确定：' + st[1] + '</b>' + (openIds.length ? '。仍有 ' + openIds.join('、') + ' 未判，但不改变结论，可选复核。' : '。');
    var el = $('#concl');
    el.toggleAttribute('data-draft', nd > 0);
    el.innerHTML =
      '<span class="rv-concl-what"><span class="rv-meta" style="margin-right:4px">' + (nd ? '预览' : '结论') + '</span>' +
      '<span class="rv-verdict rv-verdict--lg" data-tone="' + st[0] + '">' + st[1] + '</span></span>' +
      '<span style="display:grid;min-width:220px"><span class="rv-pts" style="font-size:20px;line-height:24px">' + scoreText(c) + '</span>' +
      (c.status === 'invalid' ? '' : barHTML(c)) + '</span>' +
      '<span class="rv-concl-text">' + text + (c.vetoes && c.vetoes.length ? ' <b>否决：' + c.vetoes.join('、') + '</b>' : '') +
      (nd ? ' <b>含 ' + nd + ' 项未提交草稿。</b>' : '') + '</span>' +
      '<span class="rv-concl-actions">' +
      (PREV_URL ? '<a class="rv-btn" href="' + PREV_URL + '">上一段 <span class="rv-kbd">[</span></a>' : '') +
      '<button class="rv-btn rv-btn--primary" id="submit"' + (nd ? '' : ' disabled') + '>' +
      (nd ? '提交 ' + nd + ' 项并下一段' : '无改动') + ' <span class="rv-kbd">Ctrl ↵</span></button>' +
      (NEXT_URL ? '<a class="rv-btn" href="' + NEXT_URL + '">跳过 <span class="rv-kbd">]</span></a>' : '') +
      '</span>';
    $('#submit').onclick = submit;
  }
  function renderTotal(c) {
    var box = $('#total');
    if (!box) return;
    var ov = DRAFT['@total'];
    var formula = (c.parts || []).map(function (p) {
      return p.weight + '×' + (p.score != null ? p.score : '?');
    }).join(' + ');
    var den = (c.parts || []).reduce(function (a, p) { return a + (p.counted ? p.weight : 0); }, 0);
    var main = c.score != null
      ? '<span class="rv-total-n">' + c.score + '<small> / 100</small></span>' +
        '<span class="rv-total-src">' + (c.score_source === 'human' ? '人工给定 · 自动 ' + (c.auto_score != null ? c.auto_score : '—') : '自动计算') + '</span>'
      : '<span class="rv-total-n">' + scoreText(c).replace(' 分', '') + '<small> / 100</small></span><span class="rv-total-src">区间' + (c.score_min == null ? '（前提未判，可能无效）' : '') + '</span>';
    box.innerHTML = '<div class="rv-total-main">' + main + '</div>' +
      (c.vetoes && c.vetoes.length ? '<div class="rv-total-veto">否决：' + c.vetoes.join('、') + '</div>' : '') +
      '<div class="rv-total-formula">Σ(权重×分) / Σ权重 = (' + (formula || '0') + ') / ' + den + '</div>' +
      overrideHTML(ov);
    bindOverride();
  }
  function overrideHTML(ov) {
    if (ov || overrideOpen) {
      return '<div class="rv-total-override"><span class="rv-field-label">人工总分</span>' +
        '<input class="rv-score-num" id="ovs" inputmode="numeric" placeholder="0–100" value="' + (ov ? ov.score : '') + '">' +
        '<input class="rv-input" id="ovw" placeholder="理由（必填）" value="' + (ov ? ov.reason.replace(/"/g, '&quot;') : '') + '">' +
        '<button class="rv-btn" id="ovok">' + (ov ? '更新' : '确定') + '</button>' +
        (ov ? '<button class="rv-btn rv-btn--ghost" id="ovclear">改回自动</button>' : '') + '</div>';
    }
    return '<div class="rv-total-override"><button class="rv-btn" id="ovopen">手改总分 <span class="rv-kbd">T</span></button>' +
      '<span class="rv-meta">需写理由；改后以人工总分为准</span></div>';
  }
  var overrideOpen = false;
  function bindOverride() {
    if ($('#ovopen')) $('#ovopen').onclick = function () { overrideOpen = true; renderTotal(lastPreview); $('#ovs').focus(); };
    if ($('#ovok')) $('#ovok').onclick = function () {
      var n = Math.round(+$('#ovs').value);
      var w = $('#ovw').value.trim();
      if ($('#ovs').value === '' || isNaN(n) || n < 0 || n > 100) { $('#ovs').focus(); return; }
      if (!w) { $('#ovw').placeholder = '理由必填'; $('#ovw').focus(); return; }
      DRAFT['@total'] = { score: n, reason: w };
      preview();
    };
    if ($('#ovclear')) $('#ovclear').onclick = function () { delete DRAFT['@total']; overrideOpen = false; preview(); };
  }

  // ---------------- 预览（服务端 §9，不落库）
  var lastPreview = null;
  function entries() {
    return Object.keys(DRAFT).filter(function (k) { return k !== '@total'; }).map(function (k) {
      return { item_id: k, value: DRAFT[k].value, score: DRAFT[k].custom ? DRAFT[k].score : null,
               reason: DRAFT[k].reason || '' };
    });
  }
  function preview() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () {
      fetch(body.dataset.previewUrl, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer_id: ANSWER_ID, chain: CHAIN, entries: entries(),
                               total: DRAFT['@total'] || null })
      }).then(function (r) { return r.json(); }).then(function (c) {
        if (c.error) return;
        lastPreview = c; renderConcl(c); renderTotal(c);
      }).catch(function () {});
    }, 250);
  }
  function submit() {
    var reviewer = localStorage.getItem('rv-reviewer') || '';
    var who = prompt2();
    if (who === null) return;
    fetch(body.dataset.submitUrl, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ answer_id: ANSWER_ID, chain: CHAIN, entries: entries(),
                             total: DRAFT['@total'] || null, reviewer: reviewer })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { banner(res.j.error || '提交失败'); return; }
        location.href = res.j.next || NEXT_URL || '/review';
      }).catch(function () { banner('提交失败：网络错误'); });
  }
  function prompt2() {
    var el = $('#reviewer');
    if (el) { localStorage.setItem('rv-reviewer', el.value.trim() || ''); }
    return true;
  }
  var bannerEl = null;
  function banner(msg) {
    if (!bannerEl) { bannerEl = document.createElement('div'); bannerEl.className = 'rv-notice'; bannerEl.dataset.tone = 'error'; $('#concl').after(bannerEl); }
    bannerEl.innerHTML = '<b>提交失败</b><span>' + msg + '</span><span>草稿已保留</span>';
  }

  // ---------------- 草稿与行状态
  function draftOf(it) { return DRAFT[it.id] || null; }
  function setVal(i, v, s, custom) {
    var it = ITEMS[i];
    if (s == null) s = it.scored && it.map[v] != null ? it.map[v] : null;
    DRAFT[it.id] = { value: v, score: s, custom: !!custom, reason: WHY[it.id] || '' };
    active = i;
    refreshRows(); preview();
  }
  function stateOf(it) {
    if (DRAFT[it.id]) return ['draft', '待提交'];
    if (it.el.dataset.human === '1') return ['human', '人工'];
    if (!it.machine || it.machine.value == null) return ['need', '待判'];
    return null;
  }
  function refreshRows() {
    ITEMS.forEach(function (it, i) {
      it.el.classList.toggle('is-active', i === active);
      var d = DRAFT[it.id];
      var radios = $$('input[type=radio]', it.el);
      radios.forEach(function (r) {
        r.checked = !!d && r.value === d.value && !d.custom;
      });
      var num = $('.rv-score-num', it.el);
      if (num) { num.value = d && d.score != null ? d.score : ''; num.toggleAttribute('data-custom', !!(d && d.custom)); }
      var st = $('.rv-state', it.el);
      var s = stateOf(it);
      if (st && s) { st.dataset.s = s[0]; st.textContent = s[1]; st.hidden = false; }
      else if (st) st.hidden = true;
      var why = $('.rv-why', it.el);
      if (why) why.value = WHY[it.id] || '';
    });
    $$('.rv-why', body).forEach(function (w, i) { w.style.display = (i === active) ? '' : 'none'; });
    highlight();
  }

  // ---------------- 对话：关联轮次高亮 + 关键词标注（highlight.js 的 markIn）
  function highlight() {
    $$('.rv-turn').forEach(function (t) { t.classList.remove('is-linked'); });
    var it = ITEMS[active];
    if (!it) return;
    var turn = $('.rv-turn[data-turn="' + (it.turn || 1) + '"]');
    if (turn) turn.classList.add('is-linked');
    $$('.rv-md mark', $('#conv')).forEach(function (m) {      // 清掉旧标注
      m.replaceWith(document.createTextNode(m.textContent));
    });
    if (turn && window.rvMark && it.kw.length) window.rvMark($('.rv-msg[data-role=assistant] .rv-md', turn) || turn, it.kw);
  }

  // ---------------- 事件
  rows.forEach(function (r, i) {
    r.addEventListener('click', function (ev) {
      if (ev.target.matches('input[type=radio]')) { setVal(i, ev.target.value); return; }
      var agree = ev.target.closest('[data-agree]');
      if (agree && ITEMS[i].machine) { setVal(i, ITEMS[i].machine.value, ITEMS[i].machine.score, false); return; }
      if (ev.target.matches('input') || ev.target.matches('button')) return;
      if (i !== active) { active = i; refreshRows(); }
    });
    var num = $('.rv-score-num', r);
    if (num) num.addEventListener('change', function () {
      if (num.value === '') return;
      var n = Math.max(0, Math.min(100, Math.round(+num.value)));
      if (isNaN(n)) return;
      setVal(i, nearest(n), n, true);
    });
    var why = $('.rv-why', r);
    if (why) why.addEventListener('input', function () {
      WHY[ITEMS[i].id] = why.value;
      if (DRAFT[ITEMS[i].id]) DRAFT[ITEMS[i].id].reason = why.value;
    });
  });
  document.addEventListener('keydown', function (ev) {
    var t = ev.target;
    if ((t.tagName === 'INPUT' && t.type !== 'radio') || t.tagName === 'SELECT') {
      if (ev.key === 'Escape') t.blur();
      if (ev.key === 'Enter' && t.classList.contains('rv-score-num')) t.dispatchEvent(new Event('change'));
      return;
    }
    var it = ITEMS[active];
    if (!it) return;
    if (ev.key === 'j' || ev.key === 'ArrowDown') { active = Math.min(active + 1, ITEMS.length - 1); refreshRows(); ev.preventDefault(); }
    else if (ev.key === 'k' || ev.key === 'ArrowUp') { active = Math.max(active - 1, 0); refreshRows(); ev.preventDefault(); }
    else if (/^[1-9]$/.test(ev.key) && +ev.key <= it.scale.length) { setVal(active, it.scale[+ev.key - 1]); }
    else if (ev.key === 'y' && it.machine && it.machine.value != null) { setVal(active, it.machine.value, it.machine.score); }
    else if (ev.key === 'n') { var n = $('.rv-score-num', it.el); if (n) { n.focus(); n.select(); ev.preventDefault(); } }
    else if (ev.key === 'e') { var w = $('.rv-why', it.el); if (w) { w.focus(); ev.preventDefault(); } }
    else if (ev.key === 't') { overrideOpen = true; renderTotal(lastPreview || {}); var o = $('#ovs'); if (o) o.focus(); ev.preventDefault(); }
    else if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey) && nDraft()) { submit(); ev.preventDefault(); }
  });
  window.addEventListener('beforeunload', function (ev) {
    if (nDraft() > 0 || DRAFT['@total']) { ev.preventDefault(); ev.returnValue = ''; }
  });
  var rev = $('#reviewer');
  if (rev) {
    rev.value = localStorage.getItem('rv-reviewer') || '';
    rev.addEventListener('change', function () { localStorage.setItem('rv-reviewer', rev.value.trim()); });
  }
  var init = null;
  try { init = JSON.parse(body.dataset.concl || 'null'); } catch (e) {}
  refreshRows();
  if (init && init.status) { lastPreview = init; renderConcl(init); renderTotal(init); }
})();
