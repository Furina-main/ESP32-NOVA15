import os
import gc
import time
import ujson
import sys

try:
    import asyncio
except ImportError:
    import uasyncio as asyncio

try:
    import machine
    _HAS_MACHINE = True
except ImportError:
    _HAS_MACHINE = False

CONFIG_PATH = 'www/screen_config.json'
EDITOR_HTML_PATH = 'www/editor.html'
FLAG_FILE = 'boot_flag.txt'

HARD_BLOCK = ('boot.py',)

PROTECTED_FILES = (
    'boot.py', 'main.py', 'server.py', 'microdot.py',
    'hardware.py', 'extmem.py', 'webedit.py',
    'playlist.json', 'www/screen_config.json',
)

NO_EDIT_PREFIX = ('img/', '_stage/')

TEXT_EXTS = ('.txt', '.md', '.json', '.html', '.css',
             '.js', '.py', '.log', '.csv', '.ini', '.conf')

_registered = False


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


def _load_config():
    try:
        with open(CONFIG_PATH) as f:
            return ujson.load(f)
    except Exception:
        return None


def _save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w') as f:
            ujson.dump(cfg, f)
        return True
    except Exception as e:
        print("[编辑器] 保存配置失败:", e)
        return False


def get_password():
    cfg = _load_config()
    if cfg is None:
        return ''
    return str(cfg.get('hardware', {}).get('edit_password', '') or '')


def _make_token(pwd):
    s = 'badge_edit_v1:' + pwd
    h = 2166136261
    for c in s:
        h ^= ord(c)
        h = (h * 16777619) & 0xFFFFFFFF
    h2 = (h ^ 0x5BD1E995) & 0xFFFFFFFF
    for c in s:
        h2 ^= ord(c)
        h2 = (h2 * 16777619) & 0xFFFFFFFF
    return '%08x%08x%08x' % (h, h2, (h ^ h2) & 0xFFFFFFFF)


def _token_ok(request):
    pwd = get_password()
    if not pwd:
        return False
    tok = request.headers.get('x-auth-token', '')
    if not tok:
        return False
    return tok == _make_token(pwd)


def is_ext_path(path):
    return path.startswith('ext/')


def needs_auth(path):
    if not get_password():
        return False
    if is_ext_path(path):
        name = path[4:]
        if name.endswith('.py'):
            return True
        return False
    p = path.lstrip('/')
    if p in PROTECTED_FILES:
        return True
    if p.endswith('.py'):
        return True
    return False


def can_edit(path):
    if not path or '..' in path:
        return False
    if is_ext_path(path):
        return True
    p = path.lstrip('/')
    if p in HARD_BLOCK:
        return False
    if p.endswith('.bak'):
        return False
    for pref in NO_EDIT_PREFIX:
        if p.startswith(pref):
            return False
    for c in p:
        if c in '\\:*?"<>|':
            return False
    return True


def _has_text_ext(path):
    for ext in TEXT_EXTS:
        if path.endswith(ext):
            return True
    return False


def _get_ext_store():
    srv = sys.modules.get('server')
    if srv is None:
        return None
    try:
        return getattr(srv, '_ext_store', None)
    except Exception as e:
        print("[编辑器] 外挂获取失败:", e)
        return None


def get_index_html():
    try:
        with open(EDITOR_HTML_PATH) as f:
            return f.read()
    except OSError:
        return ('<h1>editor.html 缺失</h1>'
                '<p>请把编辑器 HTML 放到 www/editor.html</p>')


def register(app):

    @app.route('/editor/api/auth/status', methods=['GET'])
    async def _auth_status(request):
        pwd = get_password()
        has_pwd = bool(pwd)
        ok = _token_ok(request) if has_pwd else False
        return {'ok': True, 'has_password': has_pwd, 'logged_in': ok}

    @app.route('/editor/api/auth/login', methods=['POST'])
    async def _login(request):
        data = _json_body(request)
        if data is None:
            return {'ok': False, 'err': 'bad json'}, 400
        pwd_real = get_password()
        if not pwd_real:
            return {'ok': False, 'err': '未设置密码',
                    'code': 'no_password'}, 403
        pwd_in = str(data.get('password', ''))
        if pwd_in != pwd_real:
            await asyncio.sleep(0.3)
            return {'ok': False, 'err': '密码错误'}, 401
        return {'ok': True, 'token': _make_token(pwd_real)}

    @app.route('/editor/api/auth/set_password', methods=['POST'])
    async def _set_password(request):
        pwd_real = get_password()
        if pwd_real:
            return {'ok': False, 'err': '密码已设置',
                    'code': 'already_set'}, 400
        data = _json_body(request)
        if data is None:
            return {'ok': False, 'err': 'bad json'}, 400
        new_pwd = str(data.get('password', ''))
        if len(new_pwd) < 4:
            return {'ok': False, 'err': '密码至少 4 位'}, 400
        if len(new_pwd) > 32:
            return {'ok': False, 'err': '密码最多 32 位'}, 400
        cfg = _load_config()
        if cfg is None:
            return {'ok': False, 'err': 'config read fail'}, 500
        cfg.setdefault('hardware', {})['edit_password'] = new_pwd
        if not _save_config(cfg):
            return {'ok': False, 'err': 'write fail'}, 500
        return {'ok': True, 'token': _make_token(new_pwd)}

    @app.route('/editor/api/reboot', methods=['POST'])
    async def _reboot(request):
        async def _delayed():
            await asyncio.sleep(1.0)
            machine.reset()
        asyncio.create_task(_delayed())
        return {'ok': True}

    @app.route('/editor/api/file/list', methods=['GET'])
    async def _file_list(request):
        result = {}
        for d in ('', 'res', 'sys', 'www'):
            full = d if d else '.'
            try:
                items = os.listdir(full)
            except OSError:
                continue
            lst = []
            for name in items:
                if name.endswith('.bak'):
                    continue
                path = (d + '/' + name) if d else name
                try:
                    st = os.stat(path)
                    is_dir = (st[0] & 0x4000) != 0
                    lst.append({'name': name, 'size': -1 if is_dir else st[6]})
                except OSError:
                    pass
            result[d] = lst

        try:
            ext = _get_ext_store()
            if ext is not None:
                items = ext.list_dir()
                result['ext'] = [{'name': it['name'], 'size': it['size']} for it in items]
        except Exception as e:
            print("[编辑器] 外挂列表失败:", e)

        return {'ok': True, 'dirs': result}

    @app.route('/editor/api/file/read', methods=['GET'])
    async def _file_read(request):
        path = request.args.get('path', '')
        if not path or '..' in path:
            return {'ok': False, 'err': 'bad path'}, 400
        if not can_edit(path):
            return {'ok': False, 'err': '不允许读取'}, 403
        if needs_auth(path) and not _token_ok(request):
            return {'ok': False, 'err': '需要登录', 'code': 'need_auth'}, 401

        if is_ext_path(path):
            ext = _get_ext_store()
            if ext is None:
                return {'ok': False, 'err': '外挂未激活'}, 503
            name = path[4:]
            if not ext.exists(name):
                return {'ok': False, 'err': '文件不存在'}, 404
            try:
                raw = ext.read_all(name)
            except Exception as e:
                return {'ok': False, 'err': str(e)}, 500
        else:
            try:
                with open(path, 'rb') as f:
                    raw = f.read()
            except OSError as e:
                return {'ok': False, 'err': str(e)}, 404

        content = None
        encoding = 'utf-8'
        try:
            content = raw.decode('utf-8')
        except UnicodeError:
            try:
                content = raw.decode('latin-1')
                encoding = 'latin-1'
            except Exception:
                content = None
        if content is None:
            return {'ok': False, 'err': '文件编码无法识别'}, 400

        return {'ok': True, 'content': content, 'size': len(raw), 'encoding': encoding}

    @app.route('/editor/api/file/write', methods=['POST'])
    async def _file_write(request):
        data = _json_body(request)
        if data is None:
            return {'ok': False, 'err': 'bad json'}, 400
        path = data.get('path', '')
        content = data.get('content', '')
        if not path or '..' in path:
            return {'ok': False, 'err': 'bad path'}, 400
        if not can_edit(path):
            return {'ok': False, 'err': '不允许写入'}, 403
        if needs_auth(path) and not _token_ok(request):
            return {'ok': False, 'err': '需要登录', 'code': 'need_auth'}, 401

        is_py = path.endswith('.py')

        if is_py:
            try:
                compile(content, path, 'exec')
            except SyntaxError as e:
                return {'ok': False, 'err': '语法错误: ' + str(e)}, 400
            except Exception:
                pass

        # 备份：二进制读写，避免非 UTF-8 内容导致 UnicodeError
        backup_ok = False
        if is_py and not is_ext_path(path):
            try:
                with open(path, 'rb') as f:
                    old = f.read()
                with open(path + '.bak', 'wb') as f:
                    f.write(old)
                backup_ok = True
            except Exception:
                pass

        if is_ext_path(path):
            ext = _get_ext_store()
            if ext is None:
                return {'ok': False, 'err': '外挂未激活'}, 503
            name = path[4:]
            try:
                raw = content.encode('utf-8')
                ext.begin_write(name, len(raw) if raw else 1)
                if raw:
                    ext.write_chunk(raw)
                ext.end_write()
            except Exception as e:
                try:
                    ext.abort_write()
                except Exception:
                    pass
                return {'ok': False, 'err': str(e)}, 500
        else:
            try:
                with open(path, 'w') as f:
                    f.write(content)
            except UnicodeError as e:
                return {'ok': False, 'err': '编码错误: ' + str(e)}, 400
            except OSError as e:
                return {'ok': False, 'err': str(e)}, 500

        pending = False
        if is_py:
            try:
                with open(FLAG_FILE, 'w') as f:
                    f.write('pending')
                pending = True
            except OSError as e:
                print("[编辑器] 写回滚标志失败:", e)

        return {'ok': True, 'size': len(content), 'pending_rollback': pending, 'backup': backup_ok}

    @app.route('/editor/api/file/new', methods=['POST'])
    async def _file_new(request):
        data = _json_body(request)
        if data is None:
            return {'ok': False, 'err': 'bad json'}, 400
        path = data.get('path', '').strip()
        content = data.get('content', '')
        if not path or '..' in path:
            return {'ok': False, 'err': 'bad path'}, 400

        last_seg = path.rsplit('/', 1)[-1]
        if '.' not in last_seg:
            path = path + '.txt'

        if not _has_text_ext(path):
            return {'ok': False, 'err': '只能创建文本文件'}, 400
        if not can_edit(path):
            return {'ok': False, 'err': '路径不允许'}, 403
        if path.endswith('.py'):
            if not _token_ok(request):
                return {'ok': False, 'err': '需要登录', 'code': 'need_auth'}, 401

        if is_ext_path(path):
            ext = _get_ext_store()
            if ext is None:
                return {'ok': False, 'err': '外挂未激活'}, 503
            name = path[4:]
            if ext.exists(name):
                return {'ok': False, 'err': '文件已存在'}, 400
            try:
                raw = content.encode('utf-8')
                ext.begin_write(name, len(raw) if raw else 1)
                if raw:
                    ext.write_chunk(raw)
                ext.end_write()
            except Exception as e:
                try:
                    ext.abort_write()
                except Exception:
                    pass
                return {'ok': False, 'err': str(e)}, 500
        else:
            try:
                os.stat(path)
                return {'ok': False, 'err': '文件已存在'}, 400
            except OSError:
                pass
            try:
                with open(path, 'w') as f:
                    f.write(content)
            except UnicodeError as e:
                return {'ok': False, 'err': '编码错误: ' + str(e)}, 400
            except OSError as e:
                return {'ok': False, 'err': str(e)}, 500

        if path.endswith('.py'):
            try:
                with open(FLAG_FILE, 'w') as f:
                    f.write('pending')
            except OSError:
                pass

        return {'ok': True, 'path': path}