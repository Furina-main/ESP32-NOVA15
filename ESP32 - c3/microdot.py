# C3 适配：asyncio 导入统一 + 流式 GC 频率 8KB + 失败清 pending
try:
    import asyncio
except ImportError:
    import uasyncio as asyncio
import gc

try:
    import socket as _socket
except ImportError:
    _socket = None

STATUS_TEXT = {200: 'OK', 302: 'Found', 400: 'Bad Request', 403: 'Forbidden',
               404: 'Not Found', 405: 'Method Not Allowed',
               413: 'Payload Too Large', 500: 'Internal Server Error',
               503: 'Service Unavailable', 504: 'Gateway Timeout',
               507: 'Insufficient Storage'}

REQUEST_TIMEOUT_S = 6.0
HANDLER_TIMEOUT_S = 15.0
BODY_READ_TIMEOUT_S = 5.0
# 缩小流式发送块大小，防 OOM
STREAM_FLUSH_SIZE = 1024
READ_CHUNK = 1024
# C3 上 64KB 单次 POST 的 ujson 中间对象约 150KB，风险较高。
# 如果编辑器要保存 >40KB 的文本文件，务必降到 32768 并同步改前端分块上传。
MAX_BODY_SIZE = 65536

MAX_CONCURRENT = 2
KEEP_ALIVE = False
# C3（400KB SRAM）每 8KB 流式 flush 后 GC 一次，兼顾碎片与 CPU
GC_FLUSH_INTERVAL = 8
GC_REQ_INTERVAL = 8


def _is_generator(obj):
    if obj is None:
        return False
    return hasattr(obj, '__next__') and hasattr(obj, 'send')


def _urldecode(s, plus_is_space=False):
    if '%' not in s and not (plus_is_space and '+' in s):
        return s
    if plus_is_space:
        s = s.replace('+', ' ')
    out = bytearray()
    i, n = 0, len(s)
    while i < n:
        if s[i] == '%' and i + 2 < n:
            try:
                out.append(int(s[i+1:i+3], 16))
                i += 3
                continue
            except ValueError:
                pass
        try:
            out.extend(s[i].encode('utf-8'))
        except Exception:
            out.append(ord(s[i]) & 0xFF)
        i += 1
    try:
        return out.decode('utf-8')
    except Exception:
        return str(out)


class Request:
    def __init__(self, method, path, headers, body, args):
        self.method = method
        self.path = path
        self.headers = headers
        self._body = body
        self.args = args

    @property
    def body(self):
        return self._body


class Response:
    def __init__(self, body=b'', status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        if _is_generator(body):
            self.body = body
            self._is_stream = True
            return
        self._is_stream = False
        if isinstance(body, str):
            body = body.encode()
        elif isinstance(body, (dict, list)):
            import ujson
            body = ujson.dumps(body).encode()
            if 'Content-Type' not in self.headers:
                self.headers['Content-Type'] = 'application/json'
        self.body = body if isinstance(body, (bytes, bytearray)) else b''

    def render_headers(self, keep_alive=False):
        status_text = STATUS_TEXT.get(self.status_code, 'OK')
        out = bytearray()
        out.extend(b'HTTP/1.1 ' + str(self.status_code).encode() + b' '
                   + status_text.encode() + b'\r\n')
        has_cl = False
        for k, v in self.headers.items():
            lk = k.lower()
            if lk == 'connection':
                continue
            if lk == 'content-length' and not self._is_stream:
                continue
            out.extend(k.encode() + b': ' + str(v).encode() + b'\r\n')
            if lk == 'content-length':
                has_cl = True
        if not self._is_stream:
            out.extend(b'Content-Length: ' + str(len(self.body)).encode()
                       + b'\r\n')
            has_cl = True

        if self._is_stream and not has_cl:
            keep_alive = False
        if 'Content-Type' not in self.headers:
            out.extend(b'Content-Type: text/plain\r\n')
        if keep_alive:
            out.extend(b'Connection: keep-alive\r\n\r\n')
        else:
            out.extend(b'Connection: close\r\n\r\n')
        return bytes(out)


class Microdot:
    def __init__(self):
        self.routes = []
        self.before_request_handlers = []
        self._server = None
        self._active = 0
        self._active_lock = None

    def route(self, path_pattern, methods=None):
        def deco(f):
            self.routes.append({
                'pattern': path_pattern,
                'methods': [m.upper() for m in (methods or ['GET'])],
                'func': f,
            })
            return f
        return deco

    def before_request(self, f):
        self.before_request_handlers.append(f)
        return f

    def shutdown(self):
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass

    async def _handle_client(self, reader, writer):
        if self._active_lock is None:
            self._active_lock = asyncio.Lock()
        while True:
            async with self._active_lock:
                if self._active < MAX_CONCURRENT:
                    self._active += 1
                    break
            await asyncio.sleep_ms(10)

        if _socket is not None:
            try:
                s = getattr(writer, 's', None) or getattr(writer, 'socket', None)
                if s is not None:
                    s.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
            except Exception:
                pass

        req_count = 0
        try:
            while True:
                keep = await self._handle_client_inner(reader, writer)
                if not keep or not KEEP_ALIVE:
                    break
                req_count += 1
                if (req_count & (GC_REQ_INTERVAL - 1)) == 0:
                    gc.collect()
        except Exception as e:
            print("[client]", e)
        finally:
            async with self._active_lock:
                self._active -= 1
            try:
                writer.close()
                if hasattr(writer, 'wait_closed'):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:
                pass
            gc.collect()

    async def _handle_client_inner(self, reader, writer):
        try:
            req = await asyncio.wait_for(self._parse_request(reader),
                                         timeout=REQUEST_TIMEOUT_S)
        except asyncio.TimeoutError:
            return False
        if req is None:
            return False

        conn_hdr = req.headers.get('connection', '').lower()
        keep = KEEP_ALIVE and conn_hdr != 'close'

        handler = None
        for route in self.routes:
            params = self._match(route['pattern'], req.path)
            if params is not None and req.method in route['methods']:
                handler = route['func']
                req._params = params
                break

        if handler is None:
            resp = Response('404', 404)
        else:
            for h in self.before_request_handlers:
                try:
                    await asyncio.wait_for(h(req), timeout=2.0)
                except Exception as e:
                    print("[before]", e)
            try:
                if req._params:
                    result = await asyncio.wait_for(
                        handler(req, **req._params), timeout=HANDLER_TIMEOUT_S)
                else:
                    result = await asyncio.wait_for(
                        handler(req), timeout=HANDLER_TIMEOUT_S)
            except asyncio.TimeoutError:
                result = Response('timeout', 504)
                keep = False
            except Exception as e:
                print("[handler]", e)
                import sys
                sys.print_exception(e)
                result = Response('500', 500)
                keep = False
            resp = self._normalize(result)

        if getattr(resp, '_is_stream', False):
            keep = await self._send_stream(writer, resp, keep)
        else:
            try:
                header = resp.render_headers(keep_alive=keep)
                writer.write(header)
                del header
                if resp.body:
                    writer.write(resp.body)
                await asyncio.wait_for(writer.drain(), timeout=8.0)
            except Exception as e:
                print("[client] 发送失败:", e)
                keep = False
        return keep

    async def _send_stream(self, writer, resp, keep_alive=False):
        try:
            header = resp.render_headers(keep_alive=keep_alive)
        except Exception:
            return False
        try:
            writer.write(header)
            del header
            await writer.drain()
        except Exception:
            return False

        gen = resp.body
        pending = bytearray()
        flushes = 0
        while True:
            try:
                chunk = next(gen)
            except StopIteration:
                break
            except Exception as e:
                print("[stream] 发生异常:", e)
                keep_alive = False
                break
            if not chunk:
                continue
            if isinstance(chunk, str):
                chunk = chunk.encode()
            pending.extend(chunk)
            if len(pending) >= STREAM_FLUSH_SIZE:
                try:
                    writer.write(pending)
                    await writer.drain()
                except Exception:
                    keep_alive = False
                    pending = bytearray()  # 失败后立即释放，避免二次写
                    break
                pending = bytearray()
                flushes += 1
                # C3：每 8KB GC 一次
                if (flushes & (GC_FLUSH_INTERVAL - 1)) == 0:
                    gc.collect()
        if pending:
            try:
                writer.write(pending)
                await writer.drain()
            except Exception:
                keep_alive = False
        try:
            if hasattr(gen, 'close'):
                gen.close()
        except Exception:
            pass
        return keep_alive

    def _normalize(self, result):
        if isinstance(result, Response):
            return result
        if isinstance(result, tuple):
            body = result[0]
            status = result[1] if len(result) > 1 else 200
            headers = result[2] if len(result) > 2 else {}
            if isinstance(body, (dict, list)):
                import ujson
                return Response(ujson.dumps(body), status,
                                {'Content-Type': 'application/json'})
            return Response(body, status, headers)
        if isinstance(result, (dict, list)):
            return Response(result, 200)
        if isinstance(result, str):
            return Response(result, 200)
        if isinstance(result, (bytes, bytearray)):
            return Response(result, 200)
        return Response(str(result), 200)

    async def _parse_request(self, reader):
        try:
            line = await reader.readline()
            if not line:
                return None
            parts = line.decode().strip().split()
            if len(parts) < 3:
                return None
            method, url, _ = parts
            if '?' in url:
                path, qs = url.split('?', 1)
                args = {}
                for pair in qs.split('&'):
                    if '=' in pair:
                        k, v = pair.split('=', 1)
                        args[_urldecode(k, True)] = _urldecode(v, True)
            else:
                path = url
                args = {}
            path = _urldecode(path, False)
            headers = {}
            while True:
                line = await reader.readline()
                if not line or line == b'\r\n' or line == b'\n':
                    break
                line = line.decode().strip()
                if ':' in line:
                    k, v = line.split(':', 1)
                    headers[k.strip().lower()] = v.strip()

            try:
                cl = int(headers.get('content-length', 0))
            except ValueError:
                return None
            if cl < 0 or cl > MAX_BODY_SIZE:
                return None

            body = b''
            if cl > 0:
                buf = bytearray()
                remain = cl
                while remain > 0:
                    n = READ_CHUNK if remain > READ_CHUNK else remain
                    try:
                        chunk = await asyncio.wait_for(
                            reader.read(n), timeout=BODY_READ_TIMEOUT_S)
                    except Exception:
                        return None
                    if not chunk:
                        break
                    buf.extend(chunk)
                    remain -= len(chunk)
                body = bytes(buf)
                del buf
            return Request(method, path, headers, body, args)
        except Exception as e:
            print("[parse]", e)
            return None

    def _match(self, pattern, path):
        if pattern == path:
            return {}
        if '<' not in pattern:
            return None
        parts = pattern.split('/')
        path_parts = path.split('/')
        n_parts = len(parts)
        n_path = len(path_parts)
        params = {}
        pi = 0
        si = 0
        while pi < n_parts:
            p = parts[pi]
            if p.startswith('<') and p.endswith('>'):
                inner = p[1:-1]
                greedy = False
                if ':' in inner:
                    key, conv = inner.split(':', 1)
                    if conv == 'path':
                        greedy = True
                else:
                    key = inner
                if greedy:
                    if si >= n_path:
                        return None
                    params[key] = '/'.join(path_parts[si:])
                    si = n_path
                    pi += 1
                    if pi != n_parts:
                        return None
                else:
                    if si >= n_path:
                        return None
                    params[key] = path_parts[si]
                    pi += 1
                    si += 1
            else:
                if si >= n_path or p != path_parts[si]:
                    return None
                pi += 1
                si += 1
        if si != n_path:
            return None
        return params

    async def start_server(self, host='0.0.0.0', port=80):
        self._server = await asyncio.start_server(
            self._handle_client, host, port)
        await self._server.wait_closed()


def abort(status_code, reason=None):
    raise Exception(reason or str(status_code))