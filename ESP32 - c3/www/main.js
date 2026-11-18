var _screenCfg = null;
var _bmpCache = {};
var _bmpQueue = [];
var _bmpRunning = false;
var _BMP_MAX_CACHE = 24;
var _heartbeatTimer = null;
var _heartbeatFailCount = 0;
// C3 单核刷屏 20~40 秒，心跳会短暂超时；连续失败 6 次（≈30 秒）才认为服务器真的关闭
var _HEARTBEAT_MAX_FAIL = 6;

function fmtKB(b) { return (b / 1024).toFixed(1) + ' KB'; }
function fmtMB(b) { return (b / 1024 / 1024).toFixed(1) + ' MB'; }

window.__endsWith = function (s, suf) {
  if (typeof s !== 'string') s = String(s);
  if (s.length < suf.length) return false;
  return s.slice(s.length - suf.length) === suf;
};

window.applyAppearance = function () {
  var bgLayer = document.getElementById('bg-layer');
  if (!bgLayer) return;

  var theme = localStorage.getItem('ui_theme') || 'blue-pink';
  var blur = localStorage.getItem('ui_bg_blur') || '0';
  var cardBlur = localStorage.getItem('ui_card_blur') || '20';

  document.body.classList.remove('theme-dark');

  var isMobile = window.innerWidth < 768 || ('ontouchstart' in window);
  var bgPath = null;

  if (_screenCfg && _screenCfg.ui) {
      bgPath = isMobile ? _screenCfg.ui.bg_path_mobile : _screenCfg.ui.bg_path_pc;
  }
  if (!bgPath) {
      bgPath = isMobile ? 'ext/bg_mobile.jpg' : 'ext/bg_pc.jpg';
  }

  var styleEl = document.getElementById('dynamic-bg-style');
  if (!styleEl) {
      styleEl = document.createElement('style');
      styleEl.id = 'dynamic-bg-style';
      document.head.appendChild(styleEl);
  }

  if (bgPath) {
      var url = '/api/download?path=' + encodeURIComponent(bgPath);
      styleEl.innerHTML = `
        #bg-layer {
          background-image: url("${url}") !important;
          background-size: cover !important;
          background-position: center center !important;
          background-repeat: no-repeat !important;
          filter: blur(${blur}px) !important;
        }
      `;
      window.__uiBgPathActive = url;
  } else {
      var themes = {
        'blue-pink': 'linear-gradient(135deg, #a1c4fd 0%, #c2e9fb 100%)',
        'sunset': 'linear-gradient(135deg, #f6d365 0%, #fda085 100%)',
        'green': 'linear-gradient(135deg, #d4fc79 0%, #96e6a1 100%)',
        'purple': 'linear-gradient(135deg, #e0c3fc 0%, #8ec5fc 100%)',
        'dark': 'linear-gradient(135deg, #232526 0%, #414345 100%)'
      };
      styleEl.innerHTML = `
        #bg-layer {
          background-image: ${themes[theme] || themes['blue-pink']} !important;
          filter: none !important;
        }
      `;
      window.__uiBgPathActive = null;
      if (theme === 'dark') document.body.classList.add('theme-dark');
  }

  var cardStyleEl = document.getElementById('dynamic-card-blur');
  if (!cardStyleEl) {
    cardStyleEl = document.createElement('style');
    cardStyleEl.id = 'dynamic-card-blur';
    document.head.appendChild(cardStyleEl);
  }
  cardStyleEl.innerHTML = '.card, #panel, .fm-tool, .modal-body { backdrop-filter: blur(' + cardBlur + 'px) !important; -webkit-backdrop-filter: blur(' + cardBlur + 'px) !important; }';
};

async function loadStatus() {
  try {
    var r = await fetch('/api/all');
    var j = await r.json();
    _screenCfg = j.config || null;
    var s = document.getElementById('status');
    if (s) {
      var intFlash = fmtMB(j.free);
      var extFlash = j.ext ? fmtMB(j.ext.free) : '未激活';
      var ram = fmtKB(j.ram);
      s.innerHTML = '内置: ' + intFlash + ' | 外挂: ' + extFlash + ' | RAM: ' + ram;
    }
    window.applyAppearance();
  } catch (e) {
    var s = document.getElementById('status');
    if (s) s.textContent = '加载失败: ' + e;
  }
}

function injectHtmlWithScripts(container, html) {
  if (container._injectedScripts) {
    for (var k = 0; k < container._injectedScripts.length; k++) {
      try { container._injectedScripts[k].remove(); } catch (e) {}
    }
  }
  container._injectedScripts = [];
  var doc = new DOMParser().parseFromString(html, 'text/html');
  var scripts = doc.querySelectorAll('script');
  var list = [];
  for (var i = 0; i < scripts.length; i++) {
    var sc = scripts[i];
    if (sc.src) list.push({ src: sc.src });
    else list.push({ code: sc.textContent });
    sc.parentNode.removeChild(sc);
  }
  container.innerHTML = doc.body.innerHTML;
  for (var j = 0; j < list.length; j++) {
    var s = document.createElement('script');
    if (list[j].src) s.src = list[j].src;
    else s.textContent = list[j].code;
    document.head.appendChild(s);
    container._injectedScripts.push(s);
  }
}

async function loadGallery() {
  var box = document.getElementById('galleryContent');
  if (!box) return;
  box.innerHTML = '<div class="placeholder">加载中...</div>';
  try {
    var r = await fetch('/www/gallery.html?t=' + Date.now());
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var html = await r.text();
    injectHtmlWithScripts(box, html);
  } catch (e) {
    box.innerHTML = '<div class="placeholder">加载失败: ' + e + '</div>';
  }
}

async function loadApSsid() {
  var inp = document.getElementById('apSsid');
  if (!inp) return;
  try {
    var r = await fetch('/api/config/ap_ssid');
    var j = await r.json();
    if (j.ok) inp.value = j.value;
  } catch (e) {}
}

async function saveApSsid() {
  var inp = document.getElementById('apSsid');
  var hint = document.getElementById('apSsidHint');
  if (!inp) return;
  var v = inp.value.trim();
  if (!v) { alert('请输入 WiFi 名称'); return; }
  if (v.length > 32) { alert('名字不能超过 32 字符'); return; }
  try {
    var r = await fetch('/api/config/ap_ssid', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: v })
    });
    var j = await r.json();
    if (j.ok) {
      if (hint) {
        hint.textContent = '已保存 ✓ 下次生效';
        hint.style.color = '#34c759';
        setTimeout(function () { if (hint) { hint.textContent = '修改后下次进入服务器模式生效'; hint.style.color = '#888'; } }, 3000);
      }
    } else alert('保存失败: ' + (j.err || ''));
  } catch (e) { alert('请求失败: ' + e); }
}

async function loadDeepSleep() {
  var btn = document.getElementById('dsToggle');
  if (!btn) return;
  try {
    var r = await fetch('/api/config/deep_sleep');
    var j = await r.json();
    if (!j.ok) { btn.textContent = '读取失败'; btn.disabled = true; return; }
    updateDS(j.value);
  } catch (e) { btn.textContent = '读取失败: ' + e; btn.disabled = true; }
}

function updateDS(v) {
  var btn = document.getElementById('dsToggle');
  var hint = document.getElementById('dsHint');
  if (!btn) return;
  btn.dataset.value = v;
  if (v === 'ON') {
    btn.textContent = '💤 开启（深睡省电）';
    btn.classList.remove('active');
    if (hint) { hint.textContent = '定时唤醒刷图，功耗极低。切换后生效。'; hint.style.color = '#34c759'; }
  } else {
    btn.textContent = '⚡ 关闭（常驻轮询）';
    btn.classList.add('active');
    if (hint) { hint.textContent = '随时按 BOOT 进服务器。切换后生效。'; hint.style.color = '#ff3b30'; }
  }
}

async function toggleDeepSleep() {
  var btn = document.getElementById('dsToggle');
  if (!btn) return;
  var next = (btn.dataset.value === 'ON') ? 'OFF' : 'ON';
  btn.disabled = true;
  try {
    var r = await fetch('/api/config/deep_sleep', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: next })
    });
    var j = await r.json();
    if (!j.ok) { alert('切换失败: ' + (j.err || '')); return; }
    updateDS(j.value);
    alert(j.value === 'ON' ? '已切换为【深睡】。退出服务器后生效。' : '已切换为【常驻】。退出服务器后生效。');
  } catch (e) { alert('请求失败: ' + e); }
  finally { btn.disabled = false; }
}

async function loadPlaylistCfg() {
  try {
    var r = await fetch('/api/playlist');
    var p = await r.json();
    var iv = document.getElementById('interval');
    var md = document.getElementById('mode');
    if (iv) iv.value = p.interval || 3600;
    if (md) md.value = p.mode || 'fixed';
  } catch (e) {}
}

async function savePlaylistCfg() {
  var iv = document.getElementById('interval');
  var md = document.getElementById('mode');
  var hint = document.getElementById('playlistHint');
  if (!iv || !md) return;
  var interval = parseInt(iv.value) || 3600;
  if (interval < 60) interval = 60;
  var mode = md.value;
  try {
    var r = await fetch('/api/playlist', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ interval: interval, mode: mode })
    });
    var j = await r.json();
    if (j.ok) {
      if (hint) {
        hint.textContent = '已保存 ✓'; hint.style.color = '#34c759';
        setTimeout(function () { if (hint) { hint.textContent = '保存后立即生效；间隔 ≥ 60 秒'; hint.style.color = '#888'; } }, 2500);
      }
    } else alert('保存失败: ' + (j.err || ''));
  } catch (e) { alert('请求失败: ' + e); }
}

window.__getScreenCfg = async function () {
  if (_screenCfg && _screenCfg.screen) return _screenCfg.screen;
  if (_screenCfg && _screenCfg.width) return _screenCfg;

  try {
    var r = await fetch('/api/all');
    var j = await r.json();
    _screenCfg = j.config || {};
    if (_screenCfg.screen) return _screenCfg.screen;
  } catch (e) { _screenCfg = null; }

  return {
    "width": 768, "height": 552, "bits_per_pixel": 2, "byte_order": "msb_first",
    "palette": [{"code": 0, "rgb": [0,0,0]}, {"code": 1, "rgb": [255,255,255]}, {"code": 2, "rgb": [255,255,0]}, {"code": 3, "rgb": [255,0,0]}]
  };
};

function _normKey(path) { return path ? String(path).replace(/\/+/g, '/').replace(/^\/+/, '') : ''; }

window.__decodeBitmap = function (path, targetW) {
  if (!targetW) targetW = 384;

  if (!path.toLowerCase().endsWith('.bin')) {
    return new Promise(function(resolve, reject) {
      var img = new Image();
      img.onload = function() {
        var scale = Math.min(targetW / img.width, 1);
        var cw = Math.max(1, Math.floor(img.width * scale));
        var ch = Math.max(1, Math.floor(img.height * scale));
        var canvas = document.createElement('canvas');
        canvas.width = cw;
        canvas.height = ch;
        var ctx = canvas.getContext('2d');
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(img, 0, 0, cw, ch);
        resolve(canvas);
      };
      img.onerror = function() { reject(new Error('图片加载失败')); };
      img.src = '/api/download?path=' + encodeURIComponent(path);
    });
  }

  var npath = _normKey(path);
  var key = npath + '|' + targetW;
  if (_bmpCache[key]) return Promise.resolve(_bmpCache[key]);
  return _enqueue(npath, targetW, key);
};

function _enqueue(path, targetW, key) {
  return new Promise(function (res, rej) {
    _bmpQueue.push({ path: path, targetW: targetW, key: key, resolve: res, reject: rej });
    _runBmpQueue();
  });
}

async function _runBmpQueue() {
  if (_bmpRunning) return;
  _bmpRunning = true;

  while (_bmpQueue.length > 0) {
    var t = _bmpQueue.shift();
    if (_bmpCache[t.key]) { t.resolve(_bmpCache[t.key]); continue; }
    try {
      var c = await _decodeOnce(t.path, t.targetW);
      var keys = Object.keys(_bmpCache);
      if (keys.length >= _BMP_MAX_CACHE) {
        for (var i = 0; i < 6 && i < keys.length; i++) delete _bmpCache[keys[i]];
      }
      _bmpCache[t.key] = c;
      t.resolve(c);
    } catch (e) { t.reject(e); }

    await new Promise(function (r) { setTimeout(r, 150); });
  }
  _bmpRunning = false;
}

async function _decodeOnce(path, targetW) {
  var sc = await window.__getScreenCfg();

  var resp = await fetch('/api/download?path=' + encodeURIComponent(path));
  if (!resp.ok) throw new Error('下载失败 HTTP ' + resp.status);

  var buf = new Uint8Array(await resp.arrayBuffer());
  if (buf.length === 0) throw new Error('收到空数据，文件可能已损坏');

  var W = sc.width || 768, H = sc.height || 552, bpp = sc.bits_per_pixel || 2;
  var msbFirst = (sc.byte_order === 'msb_first');
  var bpr = Math.ceil(W * bpp / 8);

  if (buf.length !== bpr * H) throw new Error('位图大小错误 (收到 ' + buf.length + ' 字节，预期 ' + (bpr * H) + ' 字节)');

  var c2r = {};
  var pal = sc.palette || [];
  for (var k = 0; k < pal.length; k++) c2r[pal[k].code] = pal[k].rgb;
  var fb = c2r[1] || [255, 255, 255];
  var mask = (1 << bpp) - 1;

  var fullCanvas = document.createElement('canvas');
  fullCanvas.width = W; fullCanvas.height = H;
  var fullCtx = fullCanvas.getContext('2d');
  var fullImg = fullCtx.createImageData(W, H);

  for (var y = 0; y < H; y++) {
    var rowBase = y * bpr;
    for (var x = 0; x < W; x++) {
      var bp = x * bpp; var bi = rowBase + (bp >> 3); var bo = bp & 7; var code;
      if (bi < buf.length) {
        if (msbFirst) code = (buf[bi] >> (8 - bpp - bo)) & mask;
        else code = (buf[bi] >> bo) & mask;
      } else code = 1;
      var rgb = c2r[code] || fb;
      var di = (y * W + x) * 4;
      fullImg.data[di] = rgb[0]; fullImg.data[di + 1] = rgb[1]; fullImg.data[di + 2] = rgb[2]; fullImg.data[di + 3] = 255;
    }
  }
  fullCtx.putImageData(fullImg, 0, 0);

  var scale = Math.min(targetW / W, 1);
  var cw = Math.max(1, Math.floor(W * scale)); var ch = Math.max(1, Math.floor(H * scale));
  var canvas = document.createElement('canvas');
  canvas.width = cw; canvas.height = ch;
  var ctx = canvas.getContext('2d');
  ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(fullCanvas, 0, 0, W, H, 0, 0, cw, ch);
  return canvas;
}

window.__downloadAsPng = function (path, filename) {
  if (!filename) filename = path.split('/').pop().replace(/\.bin$/i, '');
  window.__decodeBitmap(path, 768).then(function (c) {
    c.toBlob(function (blob) {
      if (!blob) { alert('生成 PNG 失败'); return; }
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = filename + '.png';
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1500);
    }, 'image/png');
  }).catch(function (e) { alert('下载失败: ' + e); });
};

// 心跳：失败不立即停止。C3 单核刷屏期间会短暂阻塞，连续失败 _HEARTBEAT_MAX_FAIL 次才停。
function startHeartbeat() {
    if (_heartbeatTimer) clearInterval(_heartbeatTimer);
    _heartbeatFailCount = 0;
    _heartbeatTimer = setInterval(async () => {
        try {
            await fetch('/api/heartbeat');
            _heartbeatFailCount = 0;
        } catch (e) {
            _heartbeatFailCount++;
            if (_heartbeatFailCount >= _HEARTBEAT_MAX_FAIL) {
                console.log('心跳连续失败 ' + _heartbeatFailCount + ' 次，服务器可能已关闭');
                clearInterval(_heartbeatTimer);
                _heartbeatTimer = null;
            } else {
                console.log('心跳失败 (' + _heartbeatFailCount + '/' + _HEARTBEAT_MAX_FAIL + ')，继续重试');
            }
        }
    }, 5000);
}

var _as = document.getElementById('saveApSsid'); if (_as) _as.addEventListener('click', saveApSsid);
var _ds = document.getElementById('dsToggle'); if (_ds) _ds.addEventListener('click', toggleDeepSleep);
var _sp = document.getElementById('savePlaylistCfg'); if (_sp) _sp.addEventListener('click', savePlaylistCfg);

Promise.all([loadStatus(), loadApSsid(), loadDeepSleep(), loadPlaylistCfg()]).then(() => {
    loadGallery();
    startHeartbeat();
});
window.__injectPanel = injectHtmlWithScripts;