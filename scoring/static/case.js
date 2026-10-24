/* 用例详情页交互（doc/10）：显示密度、变体/对话筛选、并排勾选计数、分区滚动高亮。
 * 服务端渲染全部内容，本脚本只切换展示方式，不请求数据：
 * - 必要信息（提问、判定条目、体检）始终完整显示，不折叠；
 * - 可选信息（参考答案、变体列表、对话列表）通过筛选/折叠/密度调整。 */
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

  // ---------------- 变体筛选：全部 / 含待定 / 有对话
  var vBody = $('[data-variants-body]');
  var vButtons = $$('[data-vfilter]', page);
  var vNote = $('[data-variant-note]');
  var vRows = vBody ? $$('tr[data-uid]', vBody) : [];
  var vEmpty = vBody ? $('tr[data-empty-row]', vBody) : null;
  function vMatch(row, f) {
    if (f === 'undecided') return row.dataset.undecided === '1';
    if (f === 'with_convs') return parseInt(row.dataset.convs || '0', 10) > 0;
    return true;
  }
  function applyVariantFilter(f) {
    var shown = 0;
    vRows.forEach(function (row) {
      var ok = vMatch(row, f);
      row.hidden = !ok;
      if (ok) shown += 1;
    });
    if (vEmpty) vEmpty.hidden = shown !== 0;
    if (vNote) vNote.textContent = '显示 ' + shown + ' / ' + vRows.length;
    vButtons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.vfilter === f)); });
  }
  vButtons.forEach(function (b) {
    b.addEventListener('click', function () { applyVariantFilter(b.dataset.vfilter); });
  });

  // ---------------- 对话筛选：全部 / 待定 / 异常；组内全隐时组头一起隐藏
  var cButtons = $$('[data-cfilter]', page);
  var cNote = $('[data-conv-note]');
  var cItems = $$('#convs li[data-status]');
  var cGroups = $$('[data-cq-group]', page);
  var cEmpty = $('[data-empty="convs"]', page);
  function cMatch(st, f) {
    if (f === 'undecided') return st === 'undecided';
    if (f === 'failed') return st === 'invalid' || st === 'error';
    return true;
  }
  function applyConvFilter(f) {
    var shown = 0;
    cItems.forEach(function (li) {
      var ok = cMatch(li.dataset.status, f);
      li.hidden = !ok;
      if (ok) shown += 1;
    });
    cGroups.forEach(function (g) {
      var any = $$('li[data-status]', g).some(function (li) { return !li.hidden; });
      g.hidden = !any;
    });
    if (cEmpty) cEmpty.hidden = shown !== 0 || !cItems.length;
    if (cNote) cNote.textContent = '显示 ' + shown + ' / ' + cItems.length;
    cButtons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.cfilter === f)); });
  }
  cButtons.forEach(function (b) {
    b.addEventListener('click', function () { applyConvFilter(b.dataset.cfilter); });
  });

  // ---------------- 并排对照：最多两段，选中即计数，满两段才可提交
  var form = $('#convs form');
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
  paintPair();

  // ---------------- 分区导航：滚动时高亮当前区块
  var links = $$('[data-spy]', page);
  var sections = links.map(function (a) { return document.getElementById(a.dataset.spy); })
                      .filter(Boolean);
  function spy() {
    var top = 52 + 44 + 12;   // 应用头 + 子导航高度，取当前滚过线
    var current = sections[0];
    sections.forEach(function (s) { if (s.getBoundingClientRect().top <= top) current = s; });
    // 末节不足一屏时滚到底也应高亮它
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
      current = sections[sections.length - 1];
    }
    links.forEach(function (a) {
      if (current && a.dataset.spy === current.id) a.setAttribute('aria-current', 'true');
      else a.removeAttribute('aria-current');
    });
  }
  var ticking = false;
  window.addEventListener('scroll', function () {
    if (ticking) return;
    ticking = true;
    setTimeout(function () { spy(); ticking = false; }, 50);   // 后台标签页 rAF 会被节流
  }, { passive: true });
  spy();
})();
