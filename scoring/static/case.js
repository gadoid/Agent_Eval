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

/* 权重分配（饼图 v4）：调整只走下方滑杠——拖圆钮（减少→进未分配，有未分配才能
 * 增加，未分配杠与最大条目互转，粒度 1%）；双击百分比值可直接输入（失焦/回车
 * 后回退为展示态），上限 = 当前项 + 未分配。饼图仅作可视化与点击下钻详情。 */
;(function () {
  var box = document.getElementById('weight-editor');
  var pieSvg = document.getElementById('w-pie');
  if (!box || !pieSvg) return;
  pieSvg.setAttribute('viewBox', '-14 -14 228 228');
  var NS = 'http://www.w3.org/2000/svg';
  var COLORS = ['#2350a0', '#0e6c77', '#8c6c00', '#6e4a8c', '#b5440f', '#0a5a63', '#5f5a52'];
  var GRAY = '#a8a297';
  var R = 90, CX = 100, CY = 100;

  var items = [];
  document.querySelectorAll('#item-list .rv-item').forEach(function (li) {
    var chip = li.querySelector('.rv-wpct-chip');
    if (!chip) return;
    items.push({ id: li.dataset.id, pct: parseInt(chip.textContent, 10) || 0,
                 color: COLORS[items.length % COLORS.length] });
  });
  if (!items.length) return;
  var rest = { id: '@rest', pct: 0, color: GRAY };
  function total() { return items.reduce(function (a, it) { return a + it.pct; }, 0); }
  function item(id) { for (var i = 0; i < items.length; i++) if (items[i].id === id) return items[i]; return null; }
  function largest() { return items.slice().sort(function (a, b) { return b.pct - a.pct; })[0]; }

  function polar(deg, r) { var a = deg * Math.PI / 180; return [CX + r * Math.cos(a), CY + r * Math.sin(a)]; }
  function arcPath(a0, a1) {
    var p0 = polar(a0, R), p1 = polar(a1, R);
    var large = (a1 - a0) > 180 ? 1 : 0;
    return 'M ' + CX + ' ' + CY + ' L ' + p0[0].toFixed(2) + ' ' + p0[1].toFixed(2) +
           ' A ' + R + ' ' + R + ' 0 ' + large + ' 1 ' + p1[0].toFixed(2) + ' ' + p1[1].toFixed(2) + ' Z';
  }

  function drawSlices() {
    rest.pct = Math.max(0, 100 - total());
    var slices = items.filter(function (s) { return s.pct > 0; })
      .concat(items.filter(function (s) { return s.pct <= 0; }), [rest]);
    var a = -90;
    pieSvg.innerHTML = '';
    slices.forEach(function (s) {
      var span = s.pct * 3.6;
      s._start = a;
      if (s.pct > 0) {
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
          t.textContent = s.pct + '%';
          t.style.pointerEvents = 'none';
          pieSvg.appendChild(t);
        }
      }
      a += span;
    });
  }

  // ---------------- 滑杠（判定条目 + 未分配，统一样式）：拖圆钮调整
  var barBox = document.getElementById('w-legend');
  function barHTML(s) {
    var label = s === rest ? '未分配' : s.id;
    var op = s === rest ? ';opacity:.5' : '';
    return '<i class="rv-lg-dot" style="background:' + s.color + op + '"></i>' +
      '<span class="rv-lg-label" title="' + label + '">' + label + '</span>' +
      '<div class="rv-wbar" data-bar="' + s.id + '"><div class="rv-wbar-fill" style="background:' + s.color + op + '"></div>' +
      '<button class="rv-wbar-knob" type="button" aria-label="' + label + ' 比例"></button></div>' +
      '<b class="rv-mono rv-bar-pct" data-pct="' + s.id + '" title="双击直接输入百分比">' + s.pct + '%</b>';
  }
  function buildBars() {
    barBox.innerHTML = '';
    items.concat([rest]).forEach(function (s) {
      var row = document.createElement('div');
      row.className = 'rv-wbar-row';
      row.innerHTML = barHTML(s);
      barBox.appendChild(row);
    });
    syncBars();
    // 拖圆钮：减少 → 进未分配；增加只能从未分配来（未分配杠与最大条目互转），粒度 1%
    barBox.querySelectorAll('.rv-wbar').forEach(function (bar) {
      var knob = bar.querySelector('.rv-wbar-knob');
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
    barBox.querySelectorAll('.rv-bar-pct').forEach(function (el) {
      el.addEventListener('dblclick', function () { editPct(el); });
    });
  }
  function applyWant(isRest, target, want) {
    if (isRest) {
      var big = largest();
      big.pct = Math.max(0, Math.min(100, big.pct - (want - rest.pct)));
    } else {
      if (want > target.pct) want = Math.min(want, target.pct + rest.pct);   // 增加只能从未分配来
      target.pct = want;
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
    input.step = '1';
    input.value = target.pct;
    input.className = 'rv-bar-edit';
    input.setAttribute('aria-label', (isRest ? '未分配' : id) + ' 百分比（0–' + maxItem + '）');
    el.innerHTML = '';
    el.appendChild(input);
    input.focus();
    input.select();
    function commit() {
      if (!el.contains(input)) return;
      var v = parseInt(input.value, 10);
      if (!isNaN(v)) applyWant(isRest, target, Math.max(0, Math.min(maxItem, v)));
      el.textContent = target.pct + '%';                     // 结束编辑，回退为展示态
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
      bar.querySelector('.rv-wbar-knob').style.left = s.pct + '%';
      var pct = barBox.querySelector('[data-pct="' + s.id + '"]');
      if (pct && !pct.querySelector('input')) pct.textContent = s.pct + '%';
    });
    document.getElementById('w-sum').textContent = total() + '%';
    document.getElementById('w-left').textContent = rest.pct + '%';
    items.forEach(function (it) {
      var chip = document.querySelector('[data-wchip="' + it.id + '"]');
      if (chip) chip.textContent = it.pct + '%';
    });
  }

  // 点扇区 = 下钻详情
  pieSvg.addEventListener('click', function (e) {
    var p = e.target.closest('path');
    if (!p || !p.dataset.slice || p.dataset.slice === '@rest') return;
    showDetail(p.dataset.slice);
  });

  // ---------------- 条目详情
  function showDetail(id) {
    var panel = document.getElementById('item-detail');
    var card = panel.querySelector('[data-detail="' + id + '"]');
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

  // ---------------- 保存（服务端再校验 ≤100%；同值幂等）
  document.getElementById('w-save').addEventListener('click', function () {
    var weights = {};
    items.forEach(function (it) { weights[it.id] = it.pct; });
    fetch(box.dataset.saveUrl, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ qkey: box.dataset.qkey, judge_fp: box.dataset.fp, weights: weights })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) {
          document.querySelector('#weight-editor .rv-wsum').innerHTML =
            '<b style="color:var(--fail-ink)">保存失败：' + (res.j.error || '未知错误') + '</b>';
          return;
        }
        var u = new URL(location);
        if (res.j.changed) u.searchParams.set('saved', res.j.judge_fp.slice(0, 8));
        else u.searchParams.delete('saved');
        u.searchParams.delete('judge_fp');
        location.href = u;
      });
  });
  buildBars();
  drawSlices();
})();
