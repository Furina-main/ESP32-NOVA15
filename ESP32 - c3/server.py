# server.py - C3 适配版：asyncio 导入统一 + 外挂覆盖空间预检修正
import network
import time
import gc
import os
import ujson
import random

try:
    import asyncio
except ImportError:
    import uasyncio as asyncio

from microdot import Microdot, Response, Request
from hardware import (led, btn, epaper, WIFI_DURATION,
                      AP_SSID, AP_PASSWORD, SPACE_RESERVE_KB,
                      EXT_CS, EXT_SIZE_MB)

SPACE_RESERVE = SPACE_RESERVE_KB * 1024
EXT_RESERVE = 4096
STATIC_CHUNK = 4096
EXT_DL_CHUNK = 8192
EDITOR_HTML_PATH = 'www/editor.html'

HEARTBEAT_TIMEOUT_S = 15

for d in ('www', 'img', 'sys', 'res'):
    try:
        os.mkdir(d)
    except OSError:
        pass

app = Microdot()
PROTECTED = (
    'boot.py', 'main.py', 'microdot.py', 'hardware.py', 'server.py',
    'extmem.py', 'webedit.py',
    'www/screen_config.json', 'playlist.json',
    'www/index.html', 'www/main.js', 'www/main.css',
    'www/gallery.html', 'www/list.html', 'www/fm.html',
    'www/editor.html', 'www/appearance.html',
    'www/htmx.min.js', 'www/alpine.min.js',
)
PLAYLIST = 'playlist.json'
CONFIG_PATH = 'www/screen_config.json'

STATIC_FORBIDDEN = ('www/screen_config.json', 'www/playlist.json')

_upload_file = None
_upload_last_ms = 0
_upload_remaining_budget = 0
UPLOAD_TIMEOUT_S = 120
_upload_in_progress = False
_files_cache = {'data': None}
_last_activity = 0
_wifi_manual_off = False
_click_ts = []
CLICK_WINDOW_S = 1.5

_ext_store = None
_ext_fail_count = 0
EXT_MAX_RETRY = 3
_upload_ext_mode = False
_upload_ext_name = None
_upload_ext_size = 0
_upload_ext_written = 0
_upload_real_name = ""


def _json_body(request):
    body = request.body
    if isinstance(body, (bytes, bytearray)):
        try:
            body = bytes(body).decode('utf-8')
        except Exception:
            return None
    try:
        return ujson.loads(body)
    except Exception:
        return None


def _safe_filename(name):
    out = []
    for c in name:
        o = ord(c)
        if ((0x30 <= o <= 0x39) or (0x41 <= o <= 0x5A)
                or (0x61 <= o <= 0x7A)
                or c == '.' or c == '_' or c == '-'):
            out.append(c)
        else:
            out.append('_')
    s = ''.join(out)
    return s if s else 'file.bin'


def _url_encode_utf8(s):
    out = []
    for c in s:
        o = ord(c)
        if ((0x30 <= o <= 0x39) or (0x41 <= o <= 0x5A)
                or (0x61 <= o <= 0x7A) or c in '-._~'):
            out.append(c)
        else:
            try:
                b = c.encode('utf-8')
            except Exception:
                out.append('%3F')
                continue
            for byte in b:
                out.append('%%%02X' % byte)
    return ''.join(out)


def _content_disposition(name):
    return ('attachment; filename="%s"; filename*=UTF-8\'\'%s'
            % (_safe_filename(name), _url_encode_utf8(name)))


def _get_ext_store():
    global _ext_store, _ext_fail_count
    if _ext_store is not None:
        return _ext_store
    if _ext_fail_count >= EXT_MAX_RETRY:
        return None
    try:
        import extmem
        _ext_spi = getattr(epaper, 'spi', None)
        if _ext_spi is None:
            raise OSError("epaper.spi 未初始化")
        inst = extmem.get_instance(spi=_ext_spi, cs=EXT_CS,
                                   size_mb=EXT_SIZE_MB)
        if not getattr(inst, 'jedec_ok', False):
            _ext_fail_count += 1
            return None
        if not inst.is_formatted():
            inst.format()
        _ext_store = inst
        _ext_fail_count = 0
        print("[外挂] 就绪, free=%d/%d" % (inst.free(), inst.total()))
    except Exception as e:
        print("[外挂] 初始化失败:", e)
        _ext_fail_count += 1
        _ext_store = None
    return _ext_store


def _ext_info():
    if _ext_store is not None:
        try:
            return {'free': _ext_store.free(), 'total': _ext_store.total()}
        except Exception:
            pass
    return None


def _start_ap():
    gc.collect()
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    time.sleep_ms(100)
    for attempt in range(3):
        try:
            if AP_PASSWORD and len(AP_PASSWORD) >= 8:
                ap.config(essid=AP_SSID, password=AP_PASSWORD,
                          authmode=4, txpower=20)
            else:
                ap.config(essid=AP_SSID, authmode=0, txpower=20)
            return ap
        except (OSError, ValueError, RuntimeError):
            time.sleep_ms(200)
    return ap


def _close_upload_sync():
    global _upload_file, _upload_in_progress, _upload_remaining_budget
    if _upload_file:
        try:
            _upload_file.close()
        except Exception:
            pass
        _upload_file = None
    _upload_in_progress = False
    _upload_remaining_budget = 0


def _free_space():
    try:
        st = os.statvfs('/')
        return st[0] * st[3]
    except Exception:
        return 0


def _total_space():
    try:
        st = os.statvfs('/')
        return st[0] * st[2]
    except Exception:
        return 0


def _is_dir(st):
    try:
        return (st[0] & 0x4000) != 0
    except Exception:
        return False


def _is_protected(path):
    p = path.lstrip('/')
    if p in PROTECTED:
        return True
    if p.split('/')[-1] in PROTECTED:
        return True
    return False


def load_playlist():
    try:
        with open(PLAYLIST) as f:
            return ujson.load(f)
    except Exception:
        return {"order": [], "interval": 3600, "mode": "fixed",
                "current": 0, "last_play": 0}


def _save_playlist_now(p):
    try:
        with open(PLAYLIST, 'w') as f:
            ujson.dump(p, f)
        return True
    except Exception:
        return False


def _stat_item(parent, name):
    path = (parent.rstrip('/') + '/' + name) if parent != '/' else name
    try:
        st = os.stat(path)
        return {'name': name, 'size': -1 if _is_dir(st) else st[6]}
    except OSError:
        return None


def build_files():
    result = {}
    for d in ['/img', '/res']:
        result[d] = []
        try:
            items = os.listdir(d)
            for n in items:
                if n.endswith('.bak'):
                    continue
                if len(result[d]) >= 30:
                    break
                item = _stat_item(d, n)
                if item:
                    result[d].append(item)
        except OSError:
            pass

    result['/'] = []
    try:
        for n in os.listdir('/'):
            if n in ('img', 'res', 'www', 'sys'):
                continue
            if n.endswith('.bak'):
                continue
            item = _stat_item('/', n)
            if item:
                result['/'].append(item)
    except OSError:
        pass

    if _ext_store is not None:
        try:
            items = _ext_store.list_dir()
            result['/ext'] = [{'name': it['name'], 'size': it['size']}
                              for it in items]
        except Exception:
            pass
    return result


def refresh_files_now():
    _files_cache['data'] = build_files()
    return _files_cache['data']


def _load_cfg_file():
    try:
        with open(CONFIG_PATH) as f:
            return ujson.load(f)
    except Exception:
        return None


def _save_cfg_file(cfg):
    tmp = 'www/_cfg.tmp'
    try:
        with open(tmp, 'w') as f:
            ujson.dump(cfg, f)
        try:
            os.remove(CONFIG_PATH)
        except OSError:
            pass
        os.rename(tmp, CONFIG_PATH)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _clear_edit_password():
    cfg = _load_cfg_file()
    if cfg is None:
        return False
    cfg.setdefault('hardware', {})['edit_password'] = ''
    return _save_cfg_file(cfg)


@app.before_request
async def touch(request):
    global _last_activity
    if _wifi_manual_off:
        return
    _last_activity = time.time()


def _guess_content_type(path):
    if path.endswith('.json'):
        return 'application/json'
    if path.endswith('.html'):
        return 'text/html; charset=utf-8'
    if path.endswith('.css'):
        return 'text/css; charset=utf-8'
    if path.endswith('.js'):
        return 'application/javascript; charset=utf-8'
    return 'application/octet-stream'


def _serve_static(path):
    if '?' in path:
        path = path.split('?', 1)[0]
    if '..' in path:
        return 'bad path', 400
    full_path = 'www/' + path
    if full_path in STATIC_FORBIDDEN:
        return 'forbidden', 403
    try:
        st = os.stat(full_path)
        if _is_dir(st):
            return 'not found', 404
        size = st[6]
    except OSError:
        return 'not found', 404
    ct = _guess_content_type(path)

    def stream_file():
        with open(full_path, 'rb') as f:
            while True:
                chunk = f.read(STATIC_CHUNK)
                if not chunk:
                    break
                yield chunk

    return Response(stream_file(), 200, {
        'Content-Type': ct,
        'Content-Length': str(size),
        'Cache-Control': 'no-cache'
    })


@app.route('/')
async def index(request):
    return _serve_static('index.html')


@app.route('/www/<path:path>')
async def static_www(request, path):
    return _serve_static(path)


@app.route('/editor', methods=['GET'])
@app.route('/editor/', methods=['GET'])
async def _editor_entry(request):
    try:
        import webedit
        if not getattr(webedit, '_registered', False):
            webedit.register(app)
            webedit._registered = True
    except Exception as e:
        return '编辑器加载失败: ' + str(e), 500

    try:
        st = os.stat(EDITOR_HTML_PATH)
        size = st[6]
    except OSError:
        return ('<h1>editor.html 缺失</h1>'
                '<p>请把编辑器 HTML 放到 www/editor.html</p>'), 500

    def stream_editor():
        with open(EDITOR_HTML_PATH, 'rb') as f:
            while True:
                chunk = f.read(STATIC_CHUNK)
                if not chunk:
                    break
                yield chunk

    return Response(stream_editor(), 200, {
        'Content-Type': 'text/html; charset=utf-8',
        'Content-Length': str(size),
    })


# ---------------- 流式 HTML 片段渲染 ----------------

def _html_escape(s):
    s = str(s)
    s = s.replace('&', '&amp;')
    s = s.replace('<', '&lt;')
    s = s.replace('>', '&gt;')
    s = s.replace('"', '&quot;')
    s = s.replace("'", '&#39;')
    return s


def _fmt_kb(b):
    try:
        return '%.1f KB' % (b / 1024.0)
    except Exception:
        return str(b)


@app.route('/frag/fm')
async def frag_fm(request):
    files = refresh_files_now()

    def generate_html():
        for d, title in (('/img', '📷 内置图片'),
                         ('/res', '📁 内置资源'),
                         ('/ext', '💾 外挂')):
            items = files.get(d, [])
            yield '<div class="fm-group" style="margin:10px 0;">'
            yield '<h4 style="margin:6px 0;font-size:14px;">%s</h4>' % title

            if not items:
                yield '<div class="placeholder" style="padding:6px 8px;">（空）</div>'
            else:
                d_clean = d.strip('/')
                for it in items:
                    path = (d_clean + '/' + it['name']) if d_clean else it['name']
                    p = _html_escape(path)
                    n = _html_escape(it['name'])
                    is_dir = it['size'] < 0
                    sz = '目录' if is_dir else _fmt_kb(it['size'])
                    icon = '📁' if is_dir else '📎'
                    url_path = _html_escape(_url_encode_utf8(path))

                    yield (
                        '<div class="fm-row" style="display:flex;align-items:center;gap:8px;'
                        'padding:6px 8px;border-bottom:1px solid rgba(0,0,0,0.05);">'
                        '<span>%s</span>'
                        '<span style="flex:1;word-break:break-all;">%s</span>'
                        '<span style="color:#888;font-size:12px;">%s</span>'
                        '<button class="del" hx-post="/api/delete?path=%s" '
                        'hx-target="closest .fm-row" '
                        'hx-swap="outerHTML" hx-confirm="确定删除 %s ?">删除</button>'
                        '</div>' % (icon, n, sz, url_path, n)
                    )
            yield '</div>'

    return Response(generate_html(), 200, {'Content-Type': 'text/html; charset=utf-8'})


# ---------------- 业务 API ----------------

@app.route('/api/heartbeat')
async def api_heartbeat(request):
    return {'ok': True}


@app.route('/api/all')
async def api_all(request):
    try:
        with open(CONFIG_PATH) as f:
            cfg = ujson.load(f)
    except Exception:
        cfg = {}
    if _files_cache['data'] is None:
        try:
            refresh_files_now()
        except Exception:
            pass
    return {'free': _free_space(), 'total': _total_space(),
            'reserve': SPACE_RESERVE, 'ext': _ext_info(),
            'ram': gc.mem_free(),
            'files': _files_cache['data'] or {},
            'playlist': load_playlist(), 'config': cfg}


@app.route('/api/status')
async def status(request):
    return {'free': _free_space(), 'total': _total_space(),
            'reserve': SPACE_RESERVE, 'ram': gc.mem_free(),
            'ext': _ext_info()}


@app.route('/api/space')
async def space(request):
    return {'free': _free_space(), 'total': _total_space(),
            'reserve': SPACE_RESERVE}


@app.route('/api/config/ui_bg', methods=['POST'])
async def set_ui_bg(request):
    data = _json_body(request)
    if data is None:
        return {'ok': False, 'err': 'bad json'}, 400
    path = data.get('path', 'res/bg.jpg')
    bg_type = data.get('type', 'pc')

    if not path.startswith('res/') and not path.startswith('ext/'):
        return {'ok': False, 'err': 'invalid path'}, 400

    cfg = _load_cfg_file()
    if cfg is None:
        cfg = {}
    cfg['ui'] = cfg.get('ui', {})

    base_name = 'bg_pc.jpg' if bg_type == 'pc' else 'bg_mobile.jpg'
    if path.startswith('ext/'):
        cfg['ui']['bg_path_' + bg_type] = path
        try:
            os.remove('res/' + base_name)
        except OSError:
            pass
    else:
        cfg['ui']['bg_path_' + bg_type] = path
        ext_store = _get_ext_store()
        if ext_store and ext_store.exists(base_name):
            try:
                ext_store.delete(base_name)
            except Exception:
                pass

    _save_cfg_file(cfg)
    return {'ok': True, 'path': path}


@app.route('/api/config/ui_bg', methods=['DELETE'])
async def del_ui_bg(request):
    bg_type = request.args.get('type', 'all')
    cfg = _load_cfg_file()
    if cfg and 'ui' in cfg:
        if bg_type in ('all', 'pc'):
            cfg['ui'].pop('bg_path_pc', None)
        if bg_type in ('all', 'mobile'):
            cfg['ui'].pop('bg_path_mobile', None)
        _save_cfg_file(cfg)
    return {'ok': True}


@app.route('/api/ext/init', methods=['POST'])
async def ext_init(request):
    global _ext_fail_count, _ext_store
    _ext_fail_count = 0
    _ext_store = None
    try:
        import extmem
        try:
            extmem.release_instance()
        except Exception:
            pass
        _ext_spi = getattr(epaper, 'spi', None)
        if _ext_spi is None:
            return {'ok': False, 'err': 'spi 未初始化'}, 503
        inst = extmem.get_instance(spi=_ext_spi, cs=EXT_CS,
                                   size_mb=EXT_SIZE_MB)
        if not getattr(inst, 'jedec_ok', False):
            return {'ok': False,
                    'err': 'JEDEC 自检失败，检查 CS/MISO/供电'}, 503
        if not inst.is_formatted():
            inst.format()
        _ext_store = inst
        _ext_fail_count = 0
        return {'ok': True, 'free': inst.free(), 'total': inst.total()}
    except Exception as e:
        print("[外挂] init 异常:", e)
        return {'ok': False, 'err': str(e)}, 503


@app.route('/api/files/refresh', methods=['POST'])
async def refresh_files(request):
    gc.collect()
    return {'ok': True, 'files': refresh_files_now(), 'ext': _ext_info()}


@app.route('/api/config/ap_ssid')
async def get_ap_ssid(request):
    cfg = _load_cfg_file()
    if cfg is None:
        return {'ok': False, 'err': 'read fail'}, 500
    v = cfg.get('hardware', {}).get('ap_ssid', AP_SSID)
    return {'ok': True, 'value': str(v)}


@app.route('/api/config/ap_ssid', methods=['POST'])
async def set_ap_ssid(request):
    data = _json_body(request)
    if data is None:
        return {'ok': False, 'err': 'bad json'}, 400
    v = str(data.get('value', '')).strip()
    if not v or len(v) > 32:
        return {'ok': False, 'err': 'SSID 长度须在 1-32 字符'}, 400
    cfg = _load_cfg_file()
    if cfg is None:
        return {'ok': False, 'err': 'read fail'}, 500
    cfg.setdefault('hardware', {})['ap_ssid'] = v
    if not _save_cfg_file(cfg):
        return {'ok': False, 'err': 'write fail'}, 500
    return {'ok': True, 'value': v}


@app.route('/api/config/deep_sleep')
async def get_deep_sleep(request):
    cfg = _load_cfg_file()
    if cfg is None:
        return {'ok': False, 'err': 'read fail'}, 500
    v = cfg.get('hardware', {}).get('deep_sleep', 'OFF')
    if isinstance(v, bool):
        v = 'ON' if v else 'OFF'
    elif isinstance(v, int):
        v = 'ON' if v else 'OFF'
    else:
        v = str(v).strip().upper()
        if v not in ('ON', 'OFF'):
            v = 'OFF'
    return {'ok': True, 'value': v}


@app.route('/api/config/deep_sleep', methods=['POST'])
async def set_deep_sleep(request):
    data = _json_body(request)
    if data is None:
        return {'ok': False, 'err': 'bad json'}, 400
    raw = data.get('value', '')
    if isinstance(raw, bool):
        v = 'ON' if raw else 'OFF'
    elif isinstance(raw, int):
        v = 'ON' if raw else 'OFF'
    else:
        v = str(raw).strip().upper()
        if v not in ('ON', 'OFF'):
            return {'ok': False, 'err': 'value must be ON or OFF'}, 400
    cfg = _load_cfg_file()
    if cfg is None:
        return {'ok': False, 'err': 'read fail'}, 500
    cfg.setdefault('hardware', {})['deep_sleep'] = v
    if not _save_cfg_file(cfg):
        return {'ok': False, 'err': 'write fail'}, 500
    return {'ok': True, 'value': v}


@app.route('/api/epaper/display_now', methods=['POST'])
async def display_now(request):
    global _last_activity
    try:
        data = _json_body(request)
        if data is None:
            return {'ok': False, 'err': 'bad json'}, 400
        path = data.get('path', '')
        if not path or '..' in path:
            return {'ok': False, 'err': '非法路径'}, 400
        gc.collect()
        _files_cache['data'] = None
        gc.collect()

        if path.startswith('ext/'):
            ext = _get_ext_store()
            if not ext:
                return {'ok': False, 'err': '外挂不可用'}, 503
            name = path[4:]
            if not ext.exists(name):
                return {'ok': False, 'err': '文件不存在'}, 404
            print("[display_now] 开始外挂刷屏:", name)
            
            # C3 刷屏前刷新活跃时间
            _last_activity = time.time() 
            ok = epaper.show_ext_file(ext, name)
            
            print("[display_now] ext 刷屏结果:", ok)
            if not ok:
                return {'ok': False, 'err': '刷屏失败'}, 500
        else:
            if not path.startswith('img/'):
                return {'ok': False, 'err': '非法路径'}, 400
            print("[display_now] 开始内置刷屏:", path)
            
            # C3 刷屏前刷新活跃时间
            _last_activity = time.time() 
            ok = epaper.show_file(path)
            
            print("[display_now] 内置刷屏结果:", ok)
            if not ok:
                return {'ok': False, 'err': '刷屏失败'}, 500

        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'err': str(e)}, 500
    finally:
        _last_activity = time.time()
        try:
            refresh_files_now()
        except Exception:
            pass

@app.route('/api/upload/start', methods=['POST'])
async def upload_start(request):
    global _upload_file, _upload_last_ms, _upload_in_progress
    global _upload_ext_mode, _upload_ext_name, _upload_ext_size
    global _upload_remaining_budget, _upload_ext_written, _upload_real_name

    gc.collect()
    data = _json_body(request)
    if data is None:
        return {'ok': False, 'err': 'bad json'}, 400
    fname = (data.get('name') or '').strip()
    try:
        size = int(data.get('size', 0))
    except Exception:
        size = 0

    if not fname or '..' in fname or fname.startswith('/'):
        return {'ok': False, 'err': 'bad name'}, 400
    if size <= 0:
        return {'ok': False, 'err': 'bad size'}, 400

    if _upload_in_progress:
        _close_upload_sync()
        if _upload_ext_mode and _ext_store:
            try:
                _ext_store.abort_write()
            except Exception:
                pass
        _upload_ext_mode = False
        print("[上传] 发现旧锁，已强制清理重置")

    if _is_protected(fname):
        return {'ok': False, 'err': 'protected'}, 403

    is_ext_target = fname.startswith('ext/')

    if not is_ext_target:
        if fname.startswith('res/bg_') or fname.startswith('res/bg'):
            base_name = fname.split('/')[-1]
            fname = 'ext/' + base_name
            is_ext_target = True
            print("[上传] 背景图强制转存外挂:", fname)
        else:
            free = _free_space()
            if free - size < SPACE_RESERVE:
                ext = _get_ext_store()
                if ext and ext.free() - size >= EXT_RESERVE:
                    base_name = fname.split('/')[-1]
                    fname = 'ext/' + base_name
                    is_ext_target = True
                    print("[上传] 内置空间不足，自动转存外挂:", fname)
                else:
                    return {'ok': False, 'err': '内置和外挂空间均不足'}, 507

    _upload_real_name = fname

    if is_ext_target:
        ext = _get_ext_store()
        if not ext:
            return {'ok': False, 'err': '外挂不可用'}, 503
        name = fname[4:]
        try:
            name_bytes = len(name.encode('utf-8'))
        except Exception:
            return {'ok': False, 'err': 'bad name encoding'}, 400
        if name_bytes > 31:
            return {'ok': False, 'err': '外挂文件名超过 31 字节'}, 400

        # C3 修正：排除同名旧文件占用，避免覆盖大文件时误报空间不足
        free = ext.free(exclude_name=name)
        if free - size < EXT_RESERVE:
            return {'ok': False, 'err': '外挂空间不足'}, 507

        try:
            ext.begin_write(name, size)
        except Exception as e:
            return {'ok': False, 'err': 'begin: ' + str(e)}, 500

        _upload_ext_name = name
        _upload_ext_mode = True
        _upload_ext_size = size
        _upload_ext_written = 0
        _upload_in_progress = True
        _upload_last_ms = time.time()
        return {'ok': True, 'free': free, 'path': fname}

    _close_upload_sync()
    try:
        _upload_file = open(fname, 'wb')
    except OSError as e:
        return {'ok': False, 'err': 'open: ' + str(e)}, 500

    _upload_in_progress = True
    _upload_last_ms = time.time()
    _upload_remaining_budget = _free_space() - SPACE_RESERVE
    return {'ok': True, 'path': fname}


@app.route('/api/upload/chunk', methods=['POST'])
async def upload_chunk(request):
    global _upload_last_ms, _upload_ext_mode, _upload_remaining_budget
    global _upload_in_progress, _upload_ext_written
    body = request.body
    if not body:
        return {'ok': False, 'err': 'empty'}, 400

    try:
        if _upload_ext_mode:
            ext = _ext_store
            if not ext:
                _upload_ext_mode = False
                _upload_in_progress = False
                _upload_remaining_budget = 0
                _upload_ext_written = 0
                return {'ok': False, 'err': '外挂失效'}, 503

            try:
                ext.write_chunk(body)
            except Exception as e:
                try:
                    ext.abort_write()
                except Exception:
                    pass
                _upload_ext_mode = False
                _upload_in_progress = False
                _upload_remaining_budget = 0
                _upload_ext_written = 0
                return {'ok': False, 'err': 'ext write: ' + str(e)}, 500

            _upload_ext_written += len(body)
            _upload_last_ms = time.time()
            return {'ok': True}

        if _upload_file is None:
            return {'ok': False, 'err': 'no start'}, 400

        _upload_remaining_budget -= len(body)
        if _upload_remaining_budget < 0:
            _close_upload_sync()
            return {'ok': False, 'err': 'space'}, 507

        try:
            _upload_file.write(body)
        except OSError as e:
            _close_upload_sync()
            return {'ok': False, 'err': 'write: ' + str(e)}, 500

        _upload_last_ms = time.time()
        return {'ok': True}
    except Exception as e:
        print("[上传异常]", e)
        return {'ok': False, 'err': 'chunk ex: ' + str(e)}, 500


@app.route('/api/upload/end', methods=['POST'])
async def upload_end(request):
    global _upload_ext_mode, _upload_ext_name, _upload_ext_size
    global _upload_ext_written, _upload_real_name
    if _upload_ext_mode:
        ext = _ext_store
        try:
            real_size = ext.end_write()
        except Exception as e:
            try:
                ext.abort_write()
            except Exception:
                pass
            _upload_ext_mode = False
            _upload_ext_written = 0
            return {'ok': False, 'err': str(e)}, 500
        expected = _upload_ext_size
        _upload_ext_mode = False
        _upload_ext_written = 0
        refresh_files_now()
        warn = ''
        if real_size != expected:
            warn = 'size mismatch: %d != %d' % (real_size, expected)
            print('[上传] 外挂文件大小不符:', warn)
        return {'ok': True, 'size': real_size, 'expected': expected,
                'warn': warn, 'path': _upload_real_name}

    _close_upload_sync()
    refresh_files_now()
    return {'ok': True, 'free': _free_space(), 'path': _upload_real_name}


@app.route('/api/mkdir', methods=['POST'])
async def mkdir(request):
    try:
        data = _json_body(request)
        if data is None:
            return {'ok': False, 'err': 'bad json'}, 400
        path = (data.get('path') or '').strip()
        if not path or '..' in path:
            return {'ok': False, 'err': 'bad path'}, 400
        if _is_protected(path):
            return {'ok': False, 'err': 'protected'}, 403
        try:
            os.mkdir(path)
        except OSError:
            pass
        refresh_files_now()
        return {'ok': True, 'path': path}
    except Exception as e:
        return {'ok': False, 'err': str(e)}, 500


@app.route('/api/rename', methods=['POST'])
async def rename_file(request):
    data = _json_body(request)
    if data is None:
        return {'ok': False, 'err': 'bad json'}, 400
    old = (data.get('old') or '').strip().lstrip('/')
    new = (data.get('new') or '').strip().lstrip('/')
    if not old or not new:
        return {'ok': False, 'err': '缺少参数'}, 400
    if '..' in old or '..' in new:
        return {'ok': False, 'err': 'bad path'}, 400
    if old == new:
        return {'ok': True, 'old': old, 'new': new, 'playlist_updated': False}
    if _is_protected(old) or _is_protected(new):
        return {'ok': False, 'err': '受保护文件不可重命名'}, 403

    old_dir = old.rsplit('/', 1)[0] if '/' in old else ''
    new_dir = new.rsplit('/', 1)[0] if '/' in new else ''
    if old_dir != new_dir:
        return {'ok': False, 'err': '不支持跨目录重命名'}, 400
    new_base = new.split('/')[-1]
    if not new_base or '\\' in new_base:
        return {'ok': False, 'err': '文件名非法'}, 400

    p = load_playlist()
    order = list(p.get('order', []))
    in_playlist = old in order
    if in_playlist and not new.lower().endswith('.bin'):
        return {'ok': False, 'err': '播放列表中的图片必须保持 .bin 扩展名'}, 400

    if old.startswith('ext/'):
        ext = _get_ext_store()
        if not ext:
            return {'ok': False, 'err': '外挂不可用'}, 503
        old_name = old[4:]
        new_name = new[4:]
        try:
            nb = len(new_name.encode('utf-8'))
        except Exception:
            return {'ok': False, 'err': 'bad name encoding'}, 400
        if nb > 31:
            return {'ok': False, 'err': '外挂文件名超过 31 字节'}, 400
        if not ext.exists(old_name):
            return {'ok': False, 'err': '源文件不存在'}, 404
        if ext.exists(new_name):
            return {'ok': False, 'err': '目标文件已存在'}, 409
        try:
            ext.rename(old_name, new_name)
        except Exception as e:
            return {'ok': False, 'err': 'rename: ' + str(e)}, 500
    else:
        try:
            os.stat(old)
        except OSError:
            return {'ok': False, 'err': '源文件不存在'}, 404
        try:
            os.stat(new)
            return {'ok': False, 'err': '目标文件已存在'}, 409
        except OSError:
            pass
        try:
            os.rename(old, new)
        except OSError as e:
            return {'ok': False, 'err': 'rename: ' + str(e)}, 500

    playlist_updated = False
    if in_playlist:
        try:
            p2 = load_playlist()
            order2 = p2.get('order', [])
            changed = False
            new_order = []
            for it in order2:
                if it == old:
                    new_order.append(new)
                    changed = True
                else:
                    new_order.append(it)
            if changed:
                p2['order'] = new_order
                if _save_playlist_now(p2):
                    playlist_updated = True
        except Exception as e:
            print('[改名] playlist 更新失败:', e)

    refresh_files_now()
    return {'ok': True, 'old': old, 'new': new,
            'playlist_updated': playlist_updated}


@app.route('/api/delete', methods=['POST'])
async def delete_file(request):
    is_htmx = request.headers.get('hx-request') == 'true'
    try:
        path = request.args.get('path', '')
        if not path:
            data = _json_body(request)
            if data:
                path = data.get('path', '')

        if not path or '..' in path:
            return ('', 400) if is_htmx else ({'ok': False, 'err': 'bad path'}, 400)
        if _is_protected(path):
            return ('', 403) if is_htmx else ({'ok': False, 'err': 'protected'}, 403)

        if path.startswith('ext/'):
            ext = _get_ext_store()
            if not ext:
                return ('', 503) if is_htmx else ({'ok': False, 'err': '外挂不可用'}, 503)
            name = path[4:]
            if not ext.delete(name):
                return ('', 404) if is_htmx else ({'ok': False, 'err': 'not found'}, 404)
            refresh_files_now()
            return ('', 200) if is_htmx else {'ok': True}

        try:
            os.remove(path)
        except OSError:
            os.rmdir(path)
        refresh_files_now()
        return ('', 200) if is_htmx else {'ok': True}
    except Exception as e:
        print("[删除异常]", e)
        return ('', 500) if is_htmx else ({'ok': False, 'err': str(e)}, 500)


@app.route('/api/download')
async def download_file(request):
    path = request.args.get('path', '')
    if not path or '..' in path:
        return 'bad path', 400
    if _is_protected(path):
        return 'forbidden', 403

    if path.startswith('ext/'):
        ext = _get_ext_store()
        if not ext:
            return '外挂不可用', 503
        name = path[4:]
        e = ext._find(name)
        if not e:
            return 'not found', 404
        size = e['size']
        if size < 0:
            return 'not found', 404

        def stream_ext():
            for ch in ext.read_chunks(name, EXT_DL_CHUNK):
                if not ch:
                    break
                yield ch

        fname = name.split('/')[-1]
        return Response(stream_ext(), 200, {
            'Content-Type': 'application/octet-stream',
            'Content-Disposition': _content_disposition(fname),
            'Content-Length': str(size),
            'X-Content-Type-Options': 'nosniff',
            'Cache-Control': 'max-age=86400'
        })

    try:
        st = os.stat(path)
        if _is_dir(st):
            return 'not found', 404
        size = st[6]
    except OSError:
        return 'not found', 404

    def stream_builtin():
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(STATIC_CHUNK)
                if not chunk:
                    break
                yield chunk

    fname = path.split('/')[-1]
    return Response(stream_builtin(), 200, {
        'Content-Type': 'application/octet-stream',
        'Content-Disposition': _content_disposition(fname),
        'Content-Length': str(size),
        'X-Content-Type-Options': 'nosniff',
        'Cache-Control': 'max-age=86400'
    })


@app.route('/api/playlist')
async def get_playlist(request):
    return load_playlist()


@app.route('/api/playlist', methods=['POST'])
async def set_playlist(request):
    try:
        data = _json_body(request)
        if data is None:
            return {'ok': False, 'err': 'bad json'}, 400
        p = load_playlist()
        if 'order' in data:
            if data['order'] != p.get('order'):
                n_new = len(data['order'])
                p['current'] = max(0, n_new - 1)
                p['last_play'] = 0
            p['order'] = data['order']
        if 'interval' in data:
            p['interval'] = max(60, int(data['interval']))
        if 'mode' in data and data['mode'] in ('fixed', 'random', 'single'):
            p['mode'] = data['mode']
        _save_playlist_now(p)
        return {'ok': True, 'playlist': p}
    except Exception as e:
        return {'ok': False, 'err': str(e)}, 500


@app.route('/api/wifi/off', methods=['POST'])
async def manual_wifi_off(request):
    global _wifi_manual_off
    _wifi_manual_off = True
    return 'ok'


async def _upload_timeout_watcher():
    global _upload_ext_mode, _upload_ext_written
    while True:
        await asyncio.sleep(5)
        if _upload_in_progress and time.time() - _upload_last_ms > UPLOAD_TIMEOUT_S:
            _close_upload_sync()
        if _upload_ext_mode and time.time() - _upload_last_ms > UPLOAD_TIMEOUT_S:
            if _ext_store:
                try:
                    _ext_store.abort_write()
                except Exception:
                    pass
            _upload_ext_mode = False
            _upload_ext_written = 0


async def _wifi_timeout_watcher(app):
    while True:
        await asyncio.sleep(1)
        idle = time.time() - _last_activity
        if _wifi_manual_off or (not _upload_in_progress and idle >= HEARTBEAT_TIMEOUT_S):
            try:
                app.shutdown()
            except Exception:
                pass
            return


async def _button_watcher():
    global _wifi_manual_off, _click_ts
    while True:
        await asyncio.sleep(0.05)
        evt = btn.poll()
        if evt == 'short':
            now = time.time()
            _click_ts.append(now)
            _click_ts = [t for t in _click_ts if now - t < CLICK_WINDOW_S]
            if len(_click_ts) >= 3:
                _click_ts = []
                _clear_edit_password()
        elif evt == 'long':
            _wifi_manual_off = True
            return


async def _server_heartbeat():
    while True:
        await asyncio.sleep(5)
        gc.collect()


async def _gc_task():
    while True:
        await asyncio.sleep(15)
        gc.collect()


async def _run():
    global _last_activity, _wifi_manual_off, _click_ts
    _wifi_manual_off = False
    _last_activity = time.time()
    _click_ts = []

    ap = _start_ap()
    led.on()

    try:
        _get_ext_store()
    except Exception as e:
        print("[外挂] 默认激活失败:", e)

    try:
        refresh_files_now()
    except Exception as e:
        print("[服务器] 初始文件列表失败:", e)

    asyncio.create_task(_upload_timeout_watcher())
    asyncio.create_task(_wifi_timeout_watcher(app))
    asyncio.create_task(_button_watcher())
    asyncio.create_task(_server_heartbeat())
    asyncio.create_task(_gc_task())
    try:
        await app.start_server('0.0.0.0', 80)
    except Exception as e:
        print("[服务器] 错误:", e)
    finally:
        try:
            ap.active(False)
        except Exception:
            pass
        led.off()
        _close_upload_sync()


def run():
    asyncio.run(_run())