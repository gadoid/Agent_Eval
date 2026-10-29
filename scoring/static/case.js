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

/* 计分配置（doc/17 §5.2）：条目用途/权重表单。「保存到用例」走保存管线
 * （/api/case/{id}/edit：md 校验 → 落盘 → build 新版本 → 导入 → rescore）；
 * 临时权重只覆盖权重、只重算本用例；转正 = 写进 md + 删临时。 */
;(function () {
  var box = document.getElementById('scoring-editor');
  if (!box) return;
  var caseId = box.dataset.case || '';
  var msg = box.querySelector('[data-sc-msg]');
  var tempInUse = box.dataset.tempInUse === '1';

  function rows() {
    return Array.prototype.slice.call(box.querySelectorAll('tr[data-row]')).map(function (tr) {
      var useSel = tr.querySelector('.sc-use');
      var w = tr.querySelector('.sc-w');
      var tw = tr.querySelector('.sc-tw');
      var use = useSel ? useSel.value : 'score';
      return {
        id: tr.dataset.row,
        use: use,
        weight: (use === 'score' && w && w.value !== '') ? parseFloat(w.value) : 0,
        temp: (use === 'score' && tw && tw.value !== '') ? parseFloat(tw.value) : 0
      };
    });
  }
  function syncSums() {
    var rs = rows();
    var md = rs.reduce(function (a, r) { return a + (r.use === 'score' ? r.weight : 0); }, 0);
    var el = box.querySelector('[data-md-sum]');
    if (el) el.textContent = Math.round(md * 100) / 100 + '%';
    var tEl = box.querySelector('[data-temp-sum]');
    if (tEl) {
      var t = rs.reduce(function (a, r) { return a + (r.use === 'score' ? r.temp : 0); }, 0);
      tEl.textContent = Math.round(t * 100) / 100 + '%';
    }
    box.querySelectorAll('tr[data-row]').forEach(function (tr) {
      var use = tr.querySelector('.sc-use');
      var isScore = !use || use.value === 'score';
      var w = tr.querySelector('.sc-w');
      if (w) w.disabled = !isScore;
      var tw = tr.querySelector('.sc-tw');
      if (tw) tw.disabled = !isScore;
    });
  }
  box.addEventListener('input', syncSums);
  box.addEventListener('change', syncSums);

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
    post('/api/case/' + encodeURIComponent(caseId) + '/edit', {
      case_id: caseId,
      items: rows().map(function (r) { return { id: r.id, use: r.use, weight: r.weight }; })
    }).then(function (res) { saveMd.disabled = false; done(res, '保存到用例'); });
  });

  var saveTemp = document.getElementById('sc-save-temp');
  if (saveTemp) saveTemp.addEventListener('click', function () {
    var weights = {};
    rows().forEach(function (r) { if (r.use === 'score') weights[r.id] = r.temp || r.weight; });
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

  // ---------------- 条目详情（沿用：列表点「详情」或下钻）
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

  syncSums();
})();
