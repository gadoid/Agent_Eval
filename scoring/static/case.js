/* 用例详情页交互（doc/10）：变体选择、对话筛选、并排勾选计数、显示密度。
 * 服务端渲染全部变体与对话，本脚本只切换展示：选中变体时聚焦该变体的提问，
 * 并把下方对话收敛到该变体；「全部」显示所有变体。必要信息始终全文，不折叠。 */
(function () {
  var $ = function (sel, el) { return (el || document).querySelector(sel); };
  var $$ = function (sel, el) { return Array.prototype.slice.call((el || document).querySelectorAll(sel)); };
  var page = $('[data-case-page]');
  if (!page) return;

  // ---------------- 显示密度：localStorage 记忆；无 JS 时默认舒适
  var DENSITY_KEY = 'rv-case-density';
  var densityButtons = $$('[data-density-opt]', page);
  function setDensity(mode) {
    page.dataset.density = mode === 'compact' ? 'compact' : 'comfortable';
    densityButtons.forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.densityOpt === page.dataset.density));
    });
    try { localStorage.setItem(DENSITY_KEY, page.dataset.density); } catch (e) { /* 隐私模式 */ }
  }
  var savedDensity = null;
  try { savedDensity = localStorage.getItem(DENSITY_KEY); } catch (e) { /* 隐私模式 */ }
  if (savedDensity) setDensity(savedDensity);
  densityButtons.forEach(function (b) {
    b.addEventListener('click', function () { setDensity(b.dataset.densityOpt); });
  });

  // ---------------- 变体选择：全部 / 单个变体（滑动指示条 + 面板淡入）
  var tabsBox = $('[data-vtabs]', page);
  var tabs = $$('[data-vtab]', page);
  var panels = $$('[data-vpanel]', page);
  var indicator = $('[data-vtabs-ind]', page);
  var variantInput = $('[data-variant-input]', page);
  var selected = tabs.filter(function (t) { return t.getAttribute('aria-selected') === 'true'; })[0] || tabs[0];
  var activeVariant = selected ? selected.dataset.vtab : '__all';

  function moveIndicator() {
    if (!indicator || !tabsBox) return;
    var tab = tabs.filter(function (t) { return t.dataset.vtab === activeVariant; })[0];
    if (!tab) { indicator.style.width = '0px'; return; }
    var box = tabsBox.getBoundingClientRect();
    var r = tab.getBoundingClientRect();
    indicator.style.width = r.width + 'px';
    indicator.style.transform = 'translateX(' + (r.left - box.left - tabsBox.clientLeft + tabsBox.scrollLeft) + 'px)';
  }
  function paintTabs() {
    var activeTab = null;
    tabs.forEach(function (t) {
      var on = t.dataset.vtab === activeVariant;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      if (on) activeTab = t;
    });
    if (activeTab && tabsBox) {   // 窄屏横向滚动时把选中项带进视野
      var l = activeTab.offsetLeft - tabsBox.clientLeft;
      var r = l + activeTab.offsetWidth;
      if (l < tabsBox.scrollLeft) tabsBox.scrollLeft = l;
      else if (r > tabsBox.scrollLeft + tabsBox.clientWidth) tabsBox.scrollLeft = r - tabsBox.clientWidth;
    }
    moveIndicator();
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
  window.addEventListener('resize', moveIndicator);
  if (tabsBox) tabsBox.addEventListener('scroll', moveIndicator, { passive: true });
})();
