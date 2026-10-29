/* 用例详情页交互（doc/10）：变体选择、对话筛选、并排勾选计数、显示密度。
 * 服务端渲染全部变体与对话，本脚本只切换展示：选中变体时聚焦该变体的提问，
 * 并把下方对话收敛到该变体；「全部」显示所有变体。必要信息始终全文，不折叠。 */
(function () {
  var $ = function (sel, el) { return (el || document).querySelector(sel); };
  var $$ = function (sel, el) { return Array.prototype.slice.call((el || document).querySelectorAll(sel)); };
  var page = $('[data-case-page]');
  if (!page) return;

  // ---------------- 变体选择：全部 / 单个变体（按钮切换 + 面板淡入）
  var tabs = $$('[data-vtab]', page);
  var panels = $$('[data-vpanel]', page);
  var variantInput = $('[data-variant-input]', page);
  var selected = tabs.filter(function (t) { return t.getAttribute('aria-selected') === 'true'; })[0] || tabs[0];
  var activeVariant = selected ? selected.dataset.vtab : '__all';

  function paintTabs() {
    tabs.forEach(function (t) {
      var on = t.dataset.vtab === activeVariant;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
    });
  }
  function paintPanels() {
    panels.forEach(function (p) {
      var on = activeVariant === '__all' || p.dataset.vpanel === activeVariant;
      p.hidden = !on;
      if (on) {   // 重新触发淡入
        p.style.animation = 'none';
        void p.offsetWidth;
        p.style.animation = '';
      }
    });
  }
  function syncUrl() {
    try {
      var u = new URL(location.href);
      u.searchParams.set('variant', activeVariant);
      history.replaceState(null, '', u);
    } catch (e) { /* 老浏览器 */ }
  }
  function selectVariant(uid) {
    activeVariant = uid;
    page.dataset.focusVariant = uid === '__all' ? '' : uid;
    page.dataset.showAll = uid === '__all' ? '1' : '0';
    if (variantInput) variantInput.value = uid;
    paintTabs();
    paintPanels();
    applyConvFilter(statusFilter);
    prunePair();
    syncUrl();
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function (ev) {
      ev.preventDefault();
      selectVariant(t.dataset.vtab);
    });
    t.addEventListener('keydown', function (ev) {
      if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
      ev.preventDefault();
      var i = tabs.indexOf(t);
      var n = (i + (ev.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      tabs[n].focus();
      selectVariant(tabs[n].dataset.vtab);
    });
  });
  panels.forEach(function (p, i) { p.style.setProperty('--i', i); });

  // ---------------- 对话筛选：变体范围 ∩ 状态
  var statusFilter = 'all';
  var cButtons = $$('[data-cfilter]', page);
  var convItems = $$('#convs li[data-status]');
  var groups = $$('[data-cq-group]', page);
  var scopeLabel = $('[data-conv-scope]', page);
  var totalLabel = $('[data-conv-total]', page);
  var note = $('[data-conv-note]', page);
  var empty = $('[data-empty="convs"]', page);
  var counts = $$('[data-cfilter-count]', page);

  function inScope(li) { return activeVariant === '__all' || li.dataset.uid === activeVariant; }
  function statusOk(st) {
    if (statusFilter === 'undecided') return st === 'undecided';
    if (statusFilter === 'failed') return st === 'invalid' || st === 'error';
    return true;
  }
  function applyConvFilter(f) {
    statusFilter = f;
    var scope = convItems.filter(inScope);
    var shown = 0;
    convItems.forEach(function (li) {
      var ok = inScope(li) && statusOk(li.dataset.status);
      li.hidden = !ok;
      if (ok) shown += 1;
    });
    groups.forEach(function (g) {
      var vis = $$('li[data-status]', g).filter(function (li) { return !li.hidden; });
      g.hidden = vis.length === 0;
      var gc = $('[data-group-count]', g);
      if (gc) gc.textContent = vis.length + ' 段';
    });
    counts.forEach(function (el) {
      var k = el.dataset.cfilterCount;
      el.textContent = scope.filter(function (li) {
        if (k === 'undecided') return li.dataset.status === 'undecided';
        if (k === 'failed') return li.dataset.status === 'invalid' || li.dataset.status === 'error';
        return true;
      }).length;
    });
    cButtons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.cfilter === f)); });
    if (note) note.textContent = '显示 ' + shown + ' / ' + scope.length;
    if (empty) empty.hidden = !(convItems.length > 0 && shown === 0);
    if (totalLabel) totalLabel.textContent = scope.length + ' 段';
    if (scopeLabel) scopeLabel.textContent = activeVariant === '__all' ? '全部变体' : '只看 ' + activeVariant;
  }
  cButtons.forEach(function (b) {
    b.addEventListener('click', function () { applyConvFilter(b.dataset.cfilter); });
  });
  $$('[data-vfocus-link]', page).forEach(function (a) {
    a.addEventListener('click', function () { if (statusFilter !== 'all') applyConvFilter('all'); });
  });

  // ---------------- 并排对照：最多两段，选中即计数，满两段才可提交
  var form = $('[data-pair-form]', page);
  var pairBoxes = form ? $$('input[name="pair"]', form) : [];
  var pairCount = $('[data-pair-count]', page);
  var pairSubmit = $('[data-pair-submit]', page);
  var pairClear = $('[data-pair-clear]', page);
  var pairOrder = pairBoxes.filter(function (b) { return b.checked; })
                           .map(function (b) { return b.value; });
  function paintPair() {
    var n = pairOrder.length;
    pairBoxes.forEach(function (b) {
      var picked = pairOrder.indexOf(b.value) >= 0;
      b.checked = picked;
      var li = b.closest('li');
      if (li) {
        if (picked) li.setAttribute('data-pair-selected', '');
        else li.removeAttribute('data-pair-selected');
      }
    });
    if (pairCount) pairCount.textContent = '已选 ' + n + ' / 2';
    if (pairSubmit) pairSubmit.disabled = n !== 2;
  }
  function prunePair() {
    var visible = pairBoxes.filter(function (b) {
      var li = b.closest('li');
      return li && !li.hidden;
    }).map(function (b) { return b.value; });
    pairOrder = pairOrder.filter(function (v) { return visible.indexOf(v) >= 0; });
    paintPair();
  }
  pairBoxes.forEach(function (b) {
    b.addEventListener('change', function () {
      var i = pairOrder.indexOf(b.value);
      if (b.checked && i < 0) pairOrder.push(b.value);
      if (!b.checked && i >= 0) pairOrder.splice(i, 1);
      while (pairOrder.length > 2) pairOrder.shift();   // 选第三段时让最早的一段退出
      paintPair();
    });
  });
  if (pairClear) {
    pairClear.addEventListener('click', function () { pairOrder = []; paintPair(); });
  }

  // ---------------- 初始化
  paintTabs();
  applyConvFilter('all');
  prunePair();          // 初始选择变体时，隐藏变体里已勾选的段落退出并排
})();

/* 计分配置（doc/17 §5.2）：饼图 + 滑杠编辑条目用途与权重（样式沿用改造前）。
 * 「保存到用例」走保存管线（/api/case/{id}/edit：md 校验 → 落盘 → build 新版本
 * → 导入 → rescore）；临时权重只覆盖权重、只重算本用例；转正 = 写进 md + 删临时。 */
;(function () {
  var box = document.getElementById('scoring-editor');
  if (!box) return;
  var dataEl = document.getElementById('scoring-data');
  var pieSvg = document.getElementById('w-pie');
  var barBox = document.getElementById('w-legend');
  if (!pieSvg || !barBox || !dataEl) return;
  var caseId = box.dataset.case || '';
  var msg = box.querySelector('[data-sc-msg]');
  var tempInUse = box.dataset.tempInUse === '1';

  var rows = [];
  try { rows = JSON.parse(dataEl.textContent || '[]'); } catch (e) { return; }

  pieSvg.setAttribute('viewBox', '-14 -14 228 228');
  var NS = 'http://www.w3.org/2000/svg';
  var COLORS = ['#2350a0', '#0e6c77', '#8c6c00', '#6e4a8c', '#b5440f', '#0a5a63', '#5f5a52'];
  var GRAY = '#a8a297';
  var R = 90, CX = 100, CY = 100;

  // 服务端给的全部条目（前提/路径/事实/拒答）；拒答权重由 md 拒答权重固定，不可拖
  var items = [];
  rows.forEach(function (r) {
    var w = Math.round((r.weight || 0) * 100) / 100;
    items.push({ id: r.id, kind: r.kind, use: r.use === 'ref' ? 'ref' : 'score',
                 pct: w, last: w, orig: w, fixed: r.kind === 'refusal', hint: r.node_hint || '',
                 label: r.kind === 'path' ? '路径' : (r.kind === 'refusal' ? '拒答' : r.id),
                 color: COLORS[items.length % COLORS.length] });
  });
  if (!items.length) return;
  var rest = { id: '@rest', use: 'score', pct: 0, fixed: false, label: '未分配', color: GRAY };
  function total() { return items.reduce(function (a, it) { return a + (it.use === 'score' ? it.pct : 0); }, 0); }
  function item(id) { for (var i = 0; i < items.length; i++) if (items[i].id === id) return items[i]; return null; }
  function largest() {   // 未分配互转的对象：权重最大的计分条目
    var cand = items.filter(function (s) { return s.use === 'score'; });
    return cand.slice().sort(function (a, b) { return b.pct - a.pct; })[0];
  }
  function fmt(v) { return String(Math.round(v * 100) / 100); }

  function polar(deg, r) { var a = deg * Math.PI / 180; return [CX + r * Math.cos(a), CY + r * Math.sin(a)]; }
  function arcPath(a0, a1) {
    var p0 = polar(a0, R), p1 = polar(a1, R);
    var large = (a1 - a0) > 180 ? 1 : 0;
    return 'M ' + CX + ' ' + CY + ' L ' + p0[0].toFixed(2) + ' ' + p0[1].toFixed(2) +
           ' A ' + R + ' ' + R + ' 0 ' + large + ' 1 ' + p1[0].toFixed(2) + ' ' + p1[1].toFixed(2) + ' Z';
  }

  function drawSlices() {
    rest.pct = Math.max(0, Math.round((100 - total()) * 100) / 100);
    var sc = items.filter(function (s) { return s.use === 'score'; });
    var slices = sc.filter(function (s) { return s.pct > 0; })
      .concat(sc.filter(function (s) { return s.pct <= 0; }), [rest]);
    var a = -90;
    pieSvg.innerHTML = '';
    slices.forEach(function (s) {
      var span = s.pct * 3.6;
      if (s.pct > 0) {
        // 整圆（如唯一条目 0% → 未分配 100%）：起点=终点的弧会被 SVG 丢弃，必须画圆
        if (span >= 359.99) {
          var c = document.createElementNS(NS, 'circle');
          c.setAttribute('cx', CX); c.setAttribute('cy', CY); c.setAttribute('r', R);
          c.setAttribute('fill', s.color);
          c.setAttribute('opacity', s === rest ? '0.5' : '0.92');
          c.setAttribute('stroke', 'var(--bg-surface, #fff)');
          c.setAttribute('stroke-width', '1');
          c.dataset.slice = s.id;
          c.style.cursor = 'pointer';
          pieSvg.appendChild(c);
          var tc = document.createElementNS(NS, 'text');
          tc.setAttribute('x', CX); tc.setAttribute('y', CY + 4);
          tc.setAttribute('text-anchor', 'middle');
          tc.setAttribute('font-size', '12');
          tc.setAttribute('font-weight', '600');
          tc.setAttribute('fill', s === rest ? 'var(--ink, #fff)' : '#fff');
          tc.style.pointerEvents = 'none';
          tc.textContent = fmt(s.pct) + '%';
          pieSvg.appendChild(tc);
          a += span;
          return;
        }
        var path = document.createElementNS(NS, 'path');
        path.setAttribute('d', arcPath(a, a + span));
        path.setAttribute('fill', s.color);
        path.setAttribute('opacity', s === rest ? '0.5' : '0.92');
        path.setAttribute('stroke', 'var(--bg-surface, #fff)');
        path.setAttribute('stroke-width', '1');
        path.dataset.slice = s.id;
        path.style.cursor = 'pointer';
        pieSvg.appendChild(path);
        if (span >= 20) {
          var m = (a + span / 2) * Math.PI / 180;
          var t = document.createElementNS(NS, 'text');
          t.setAttribute('x', CX + R * 0.5 * Math.cos(m));
          t.setAttribute('y', CY + R * 0.5 * Math.sin(m) + 4);
          t.setAttribute('text-anchor', 'middle');
          t.setAttribute('font-size', '12');
          t.setAttribute('font-weight', '600');
          t.setAttribute('fill', '#fff');
          t.textContent = fmt(s.pct) + '%';
          t.style.pointerEvents = 'none';
          pieSvg.appendChild(t);
        }
      }
      a += span;
    });
  }

  // ---------------- 滑杠（判定条目 + 未分配）：拖圆钮调整；行首「计分/参考」切换用途
  function barHTML(s) {
    var isRest = s.id === '@rest';
    var ref = !isRest && s.use === 'ref';
    var pill = '';
    if (!isRest && !s.fixed) {
      pill = '<button class="sc-use-pill" type="button" data-use="' + s.id + '" aria-pressed="' +
             (s.use === 'score') + '" title="切换条目用途：计分参与会话分；参考只判定不计分">' +
             (s.use === 'score' ? '计分' : '参考') + '</button>';
    } else if (s.fixed) {
      pill = '<span class="sc-use-pill" title="拒答条目始终计分；权重回写 md「拒答权重」">计分</span>';
    }
    var op = (isRest || ref) ? ';opacity:.5' : '';
    var knob = ref ? '' : '<button class="rv-wbar-knob" type="button" aria-label="' + s.label + ' 比例"></button>';
    var pctTxt = ref ? '参考' : fmt(s.pct) + '%';
    var pctTitle = ref ? '' : ' title="双击直接输入百分比"';
    return '<i class="rv-lg-dot" style="background:' + (ref ? GRAY : s.color) + op + '"></i>' +
      '<span class="rv-lg-label" title="' + s.label + (s.hint ? ' · ' + s.hint : '') + '">' + s.label + '</span>' +
      pill +
      '<div class="rv-wbar" data-bar="' + s.id + '"><div class="rv-wbar-fill" style="background:' +
      (ref ? GRAY : s.color) + op + '"></div>' + knob + '</div>' +
      '<b class="rv-mono rv-bar-pct" data-pct="' + s.id + '"' + pctTitle + '>' + pctTxt + '</b>';
  }
  function buildBars() {
    barBox.innerHTML = '';
    items.concat([rest]).forEach(function (s) {
      var row = document.createElement('div');
      row.className = 'rv-wbar-row';
      if (s.use === 'ref') row.dataset.ref = '1';
      row.innerHTML = barHTML(s);
      barBox.appendChild(row);
    });
    syncBars();
    // 拖圆钮：减少 → 进未分配；增加只能从未分配来（未分配杠与最大条目互转）
    barBox.querySelectorAll('.rv-wbar').forEach(function (bar) {
      var knob = bar.querySelector('.rv-wbar-knob');
      if (!knob) return;
      knob.addEventListener('pointerdown', function (e) {
        e.preventDefault();
        var isRest = bar.dataset.bar === '@rest';
        var target = isRest ? rest : item(bar.dataset.bar);
        if (!target) return;
        var rect = bar.getBoundingClientRect();
        var x0 = e.clientX, base = target.pct;
        function mv(ev) {
          var step = Math.round((ev.clientX - x0) / (rect.width / 100));
          var want = Math.max(0, Math.min(100, base + step));
          applyWant(isRest, target, want);
        }
        function up() {
          window.removeEventListener('pointermove', mv);
          window.removeEventListener('pointerup', up);
        }
        window.addEventListener('pointermove', mv);
        window.addEventListener('pointerup', up);
      });
    });
    // 双击百分比 → 直接输入（上限 = 当前项 + 未分配）
    barBox.querySelectorAll('.rv-bar-pct[title]').forEach(function (el) {
      el.addEventListener('dblclick', function () { editPct(el); });
    });
  }
  function applyWant(isRest, target, want) {
    if (isRest) {
      var big = largest();
      if (!big) return;
      big.pct = Math.max(0, Math.min(100, Math.round((big.pct - (want - rest.pct)) * 100) / 100));
    } else {
      if (want > target.pct) want = Math.min(want, target.pct + rest.pct);   // 增加只能从未分配来
      target.pct = Math.max(0, Math.min(100, Math.round(want * 100) / 100));
    }
    drawSlices();
    syncBars();
  }
  function editPct(el) {
    var id = el.dataset.pct;
    var isRest = id === '@rest';
    var target = isRest ? rest : item(id);
    if (!target || el.querySelector('input')) return;
    var maxItem = isRest ? 100 : Math.min(100, target.pct + rest.pct);
    var input = document.createElement('input');
    input.type = 'number';
    input.min = '0';
    input.max = String(maxItem);
    input.step = '0.01';
    input.value = target.pct;
    input.className = 'rv-bar-edit';
    input.setAttribute('aria-label', (isRest ? '未分配' : id) + ' 百分比（0–' + maxItem + '）');
    el.innerHTML = '';
    el.appendChild(input);
    input.focus();
    input.select();
    function commit() {
      if (!el.contains(input)) return;
      var v = parseFloat(input.value);
      if (!isNaN(v)) applyWant(isRest, target, Math.max(0, Math.min(maxItem, v)));
      el.textContent = fmt(target.pct) + '%';                   // 结束编辑，回退为展示态
    }
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
      if (ev.key === 'Escape') { input.value = String(target.pct); input.blur(); }
    });
    input.addEventListener('blur', commit);
  }
  function syncBars() {
    items.concat([rest]).forEach(function (s) {
      var bar = barBox.querySelector('[data-bar="' + s.id + '"]');
      if (!bar) return;
      bar.querySelector('.rv-wbar-fill').style.width = s.pct + '%';
      var knob = bar.querySelector('.rv-wbar-knob');
      if (knob) knob.style.left = s.pct + '%';
      var pct = barBox.querySelector('[data-pct="' + s.id + '"]');
      if (pct && !pct.querySelector('input')) pct.textContent = s.use === 'ref' ? '参考' : fmt(s.pct) + '%';
    });
    var sumEl = document.getElementById('w-sum'), leftEl = document.getElementById('w-left');
    if (sumEl) sumEl.textContent = fmt(total()) + '%';
    if (leftEl) leftEl.textContent = fmt(rest.pct) + '%';
    items.forEach(function (it) {
      var chip = document.querySelector('[data-wchip="' + it.id + '"]');
      if (chip) chip.textContent = fmt(it.pct) + '%';
    });
  }

  // 用途切换（doc/17）：转参考即去权重（恢复值从未分配里出，无未分配则压到可得额度）
  barBox.addEventListener('click', function (e) {
    var b = e.target.closest('button.sc-use-pill[data-use]');
    if (!b) return;
    var it = item(b.dataset.use);
    if (!it) return;
    if (it.use === 'score') { it.use = 'ref'; it.last = it.pct; it.pct = 0; }
    else { it.use = 'score'; it.pct = Math.min(it.last, it.pct + rest.pct); }
    drawSlices();          // 先更新未分配（rest.pct），再重建图例（内部 syncBars 读它写合计行）
    buildBars();
  });

  // 点扇区 = 下钻详情（扇区可能是 path 或整圆 circle）
  pieSvg.addEventListener('click', function (e) {
    var p = e.target.closest('[data-slice]');
    if (!p || !p.dataset.slice || p.dataset.slice === '@rest') return;
    showDetail(p.dataset.slice);
  });

  // ---------------- 保存（服务端再校验；同值幂等）
  function payload() {
    return items.filter(function (it) { return it.kind !== 'refusal'; })   // 拒答不在 edit 的条目集里
      .map(function (it) {
        return { id: it.id, use: it.use, weight: it.use === 'score' ? it.pct : 0 };
      });
  }
  function refusalWeight() {          // 拒答权重单独走 refusal_weight 字段（改了才带）
    var rf = items.filter(function (it) { return it.kind === 'refusal'; })[0];
    return (rf && Math.abs(rf.pct - rf.orig) > 1e-9) ? rf.pct : null;
  }
  function say(text, isError) {
    if (msg) {
      msg.textContent = text;
      msg.style.color = isError ? 'var(--fail-ink, #b3261e)' : '';
    } else if (isError) {
      window.alert(text);
    }
  }
  function post(url, body) {
    return fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); });
  }
  function done(res, label) {
    if (!res.ok) {
      say(label + '失败：' + ((res.j.errors || []).join('；') || res.j.error || '未知错误'), true);
      return;
    }
    var bits = [label + '成功'];
    if (res.j.version) bits.push('新快照 ' + res.j.version);
    if (res.j.rescored !== undefined) bits.push('重算 ' + res.j.rescored + ' 组结论');
    if (res.j.pending_rejudge) bits.push('待重判 ' + res.j.pending_rejudge + ' 段（rejudge 调算子，不自动执行）');
    var u = new URL(location);
    u.searchParams.set('saved', '1');
    u.searchParams.delete('standard_fp');
    if (res.j.warnings && res.j.warnings.length) say(bits.join(' · ') + '；提示：' + res.j.warnings.join('；'));
    setTimeout(function () { location.href = u; }, 600);
    say(bits.join(' · '));
  }

  var saveMd = document.getElementById('sc-save-md');
  if (saveMd) saveMd.addEventListener('click', function () {
    if (!window.confirm('保存到用例：回写 md 并生成新快照设为当前（覆盖此前的快照选择与逐题偏离）。继续？')) return;
    saveMd.disabled = true;
    var body = { case_id: caseId, items: payload() };
    if (refusalWeight() !== null) body.refusal_weight = refusalWeight();
    post('/api/case/' + encodeURIComponent(caseId) + '/edit', body)
      .then(function (res) { saveMd.disabled = false; done(res, '保存到用例'); });
  });

  var saveTemp = document.getElementById('sc-save-temp');
  if (saveTemp) saveTemp.addEventListener('click', function () {
    var weights = {};
    items.forEach(function (it) { if (it.use === 'score') weights[it.id] = it.pct; });
    saveTemp.disabled = true;
    post('/api/case/' + encodeURIComponent(caseId) + '/temp-weights', {
      case_id: caseId, action: 'save', weights: weights
    }).then(function (res) { saveTemp.disabled = false; done(res, '保存临时权重'); });
  });

  var toggle = document.getElementById('sc-temp-toggle');
  if (toggle) toggle.addEventListener('click', function () {
    toggle.disabled = true;
    post('/api/case/' + encodeURIComponent(caseId) + '/temp-weights', {
      case_id: caseId, action: 'use', on: !tempInUse
    }).then(function (res) { toggle.disabled = false; done(res, tempInUse ? '切回 md 权重' : '启用临时权重'); });
  });

  var promote = document.getElementById('sc-temp-promote');
  if (promote) promote.addEventListener('click', function () {
    if (!window.confirm('转正：把临时权重写进 md（生成新快照）并删除临时权重。继续？')) return;
    promote.disabled = true;
    post('/api/case/' + encodeURIComponent(caseId) + '/temp-weights', {
      case_id: caseId, action: 'promote'
    }).then(function (res) { promote.disabled = false; done(res, '临时权重转正'); });
  });

  var clearT = document.getElementById('sc-temp-clear');
  if (clearT) clearT.addEventListener('click', function () {
    if (!window.confirm('删除临时权重（口径回 md）？')) return;
    clearT.disabled = true;
    post('/api/case/' + encodeURIComponent(caseId) + '/temp-weights', {
      case_id: caseId, action: 'clear'
    }).then(function (res) { clearT.disabled = false; done(res, '删除临时权重'); });
  });

  // ---------------- 条目详情（沿用：列表点「详情」或点饼图扇区下钻）
  function showDetail(id) {
    var panel = document.getElementById('item-detail');
    var card = panel && panel.querySelector('[data-detail="' + id + '"]');
    if (!card) return;
    panel.hidden = false;
    panel.querySelectorAll('.rv-detail-card').forEach(function (c) { c.hidden = c !== card; });
    document.querySelectorAll('#item-list .rv-item').forEach(function (li) {
      li.classList.toggle('is-active', li.dataset.id === id);
    });
    card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
  document.querySelectorAll('.rv-detail-btn').forEach(function (b) {
    b.addEventListener('click', function () { showDetail(b.dataset.id); });
  });
  document.querySelectorAll('.rv-detail-close').forEach(function (b) {
    b.addEventListener('click', function () {
      document.getElementById('item-detail').hidden = true;
      document.querySelectorAll('#item-list .rv-item').forEach(function (li) { li.classList.remove('is-active'); });
    });
  });

  drawSlices();
  buildBars();
})();
