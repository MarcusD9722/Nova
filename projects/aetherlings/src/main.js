/* Aetherlings — boot. */
(function (AE) {
  'use strict';

  function fitCanvas(canvas) {
    var stage = document.getElementById('stage');
    var availW = stage.clientWidth;
    var availH = stage.clientHeight;
    if (!availW || !availH) return;

    var scale = Math.min(availW / AE.W, availH / AE.H);
    /* Prefer whole-pixel scaling so the art stays crisp; fall back when it
       would waste more than a fifth of the screen. */
    var whole = Math.floor(scale);
    if (whole >= 1 && (scale - whole) / scale < 0.2) scale = whole;

    canvas.style.width = Math.round(AE.W * scale) + 'px';
    canvas.style.height = Math.round(AE.H * scale) + 'px';
  }

  function boot() {
    var canvas = document.getElementById('screen');
    canvas.width = AE.W;
    canvas.height = AE.H;
    var ctx = canvas.getContext('2d', { alpha: false });
    ctx.imageSmoothingEnabled = false;

    AE.bindInput(document.body);
    fitCanvas(canvas);
    window.addEventListener('resize', function () { fitCanvas(canvas); });
    window.addEventListener('orientationchange', function () {
      setTimeout(function () { fitCanvas(canvas); }, 120);
    });

    /* Stop iOS from scrolling or zooming the page while you're playing. */
    document.addEventListener('touchmove', function (e) {
      if (e.touches.length > 1) e.preventDefault();
    }, { passive: false });
    document.addEventListener('gesturestart', function (e) { e.preventDefault(); });

    if (/[?&]test=1/.test(location.search)) {
      var out = AE.runTests();
      showTestPanel(out);
    }

    AE.push(AE.TitleScene());
    AE.start(ctx);
  }

  function showTestPanel(out) {
    var el = document.getElementById('tests');
    if (!el) return;
    el.style.display = 'block';
    var failed = out.results.filter(function (r) { return !r.pass; });
    var html = '<h2 id="test-summary">' +
      (out.failed ? out.failed + ' of ' + out.total + ' FAILED' : 'all ' + out.total + ' passed') +
      '</h2>';
    if (failed.length) {
      html += '<ul>' + failed.map(function (r) {
        return '<li><b>[' + r.group + ']</b> ' + r.name +
          (r.detail ? '<br><span class="d">' + r.detail + '</span>' : '') + '</li>';
      }).join('') + '</ul>';
    }
    el.innerHTML = html;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

})(window.AE = window.AE || {});
