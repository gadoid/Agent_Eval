/* 配色方案：白 / 系统 / 黑。「系统」不写 data-theme，交给 prefers-color-scheme。 */
(function () {
  var KEY = 'rv-theme';
  var root = document.documentElement;
  var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-theme-opt]'));

  function read() {
    try {
      var v = localStorage.getItem(KEY);
      if (v === 'light' || v === 'dark') return v;
    } catch (e) { /* 隐私模式 */ }
    return 'system';
  }
  function paint(mode) {
    if (mode === 'light' || mode === 'dark') root.dataset.theme = mode;
    else delete root.dataset.theme;
    buttons.forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.themeOpt === mode));
    });
  }

  paint(read());
  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      paint(b.dataset.themeOpt);
      try { localStorage.setItem(KEY, b.dataset.themeOpt); } catch (e) { /* 隐私模式 */ }
    });
  });
})();
