# main.py - 修复内存泄漏与按键抖动版
import gc
import sys
import time
import os
import machine
import random
import ujson
from hardware import led, btn, epaper, DEEP_SLEEP_ON, WAKE_PIN

PLAYLIST = 'playlist.json'
FLAG_FILE = 'boot_flag.txt'
BUTTON_WINDOW_S = 5
MIN_INTERVAL_S = 60

try:
    DEEPSLEEP_RESET = machine.DEEPSLEEP_RESET
except AttributeError:
    DEEPSLEEP_RESET = 4

_playlist_cache = None

try:
    _wr = machine.wake_reason()
    if isinstance(_wr, tuple):
        _wr = _wr[0]
    WAKE_REASON = _wr
except Exception:
    WAKE_REASON = None

def is_deep_wake():
    return WAKE_REASON == DEEPSLEEP_RESET

def _confirm_boot():
    try:
        os.remove(FLAG_FILE)
    except OSError:
        pass

def load_playlist():
    global _playlist_cache
    try:
        with open(PLAYLIST) as f:
            _playlist_cache = ujson.load(f)
            return _playlist_cache
    except Exception:
        _playlist_cache = {"order": [], "interval": 3600,
                           "mode": "fixed", "current": 0, "last_play": 0}
        return _playlist_cache

def save_playlist(p):
    global _playlist_cache
    _playlist_cache = p
    try:
        with open(PLAYLIST, 'w') as f:
            ujson.dump(p, f)
    except Exception as e:
        print("playlist 保存失败:", e)

def _get_cached_playlist():
    global _playlist_cache
    if _playlist_cache is None:
        return load_playlist()
    return _playlist_cache

def _pick_random_index(p):
    n = len(p['order'])
    if n <= 1:
        return 0
    cur = p.get('current', 0)
    if cur < 0 or cur >= n:
        return random.randint(0, n - 1)
    idx = random.randint(0, n - 2)
    if idx >= cur:
        idx += 1
    return idx

def _interval_seconds():
    p = _get_cached_playlist()
    try:
        v = int(p.get('interval', 3600))
    except Exception:
        v = 3600
    return max(MIN_INTERVAL_S, v)

def _reload_deep_sleep():
    try:
        with open('www/screen_config.json') as f:
            cfg = ujson.load(f)
        v = cfg.get('hardware', {}).get('deep_sleep', 'OFF')
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v != 0
        return str(v).strip().upper() == 'ON'
    except Exception as e:
        print("[配置] 重读失败:", e)
        return False

def run_slideshow_once(force=False, is_wake=False):
    p = load_playlist()
    order = p.get('order', [])
    if not order:
        print("[刷屏] 播放列表为空")
        return False

    order = [x for x in order if (x.startswith('img/') or x.startswith('ext/')) and x.lower().endswith('.bin')]
    if not order:
        print("[刷屏] 列表里没有可刷的图片")
        return False

    mode = p.get('mode', 'fixed')
    current = p.get('current', 0) % len(order)

    if force:
        if is_wake:
            if mode == 'single':
                idx = current
            elif mode == 'random':
                idx = _pick_random_index({'order': order, 'current': current})
            else:
                idx = (current + 1) % len(order)
        else:
            idx = current
    else:
        try:
            interval = max(MIN_INTERVAL_S, int(p.get('interval', 3600)))
        except Exception:
            interval = 3600
        last_play = p.get('last_play', 0)
        now = time.time()
        if last_play > now:
            last_play = 0
        if last_play > 0 and now - last_play < interval:
            return False
        if mode == 'single':
            idx = current
        elif mode == 'random':
            idx = _pick_random_index({'order': order, 'current': current})
        else:
            idx = (current + 1) % len(order)

    path = order[idx]
    print("[刷屏] 播放:", path, "(%d/%d, %s)" % (idx + 1, len(order), mode))

    ok = False
    try:
        if path.startswith('ext/'):
            import server
            ext = server._get_ext_store()
            if not ext:
                print("[刷屏] 外挂不可用，跳过")
            else:
                ok = epaper.show_ext_file(ext, path[4:])
                print("[刷屏] ext 结果:", ok)
        else:
            ok = epaper.show_file(path)
            print("[刷屏] 内置结果:", ok)
    except Exception as e:
        print("[刷屏] 失败:", e)
        ok = False

    if ok:
        p['order'] = order
        p['current'] = idx
        p['last_play'] = time.time()
        save_playlist(p)

    del p
    del order
    gc.collect()
    return ok

def deep_sleep(seconds):
    seconds = max(MIN_INTERVAL_S, int(seconds))
    print("[省电] 深睡", seconds, "秒")
    try:
        epaper.sleep()
    except Exception:
        pass
    try:
        import network
        network.WLAN(network.AP_IF).active(False)
        network.WLAN(network.STA_IF).active(False)
    except Exception:
        pass
    led.off()
    gc.collect()
    time.sleep_ms(100)

    try:
        machine.pin_deepsleep_wakeup(
            [WAKE_PIN], machine.PIN_WAKEUP_LOW_LEVEL, True)
    except AttributeError:
        pass
    except Exception as e:
        print("[省电] 配置引脚唤醒失败:", e)

    machine.deepsleep(seconds * 1000)

def run_server_mode():
    print("[服务器] 进入服务器模式")
    gc.collect()
    try:
        import server
        server.run()
    except Exception as e:
        print("[服务器] 错误:", e)
    finally:
        load_playlist()
        gc.collect()

def wait_button_press(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if btn.is_pressed():
            time.sleep_ms(30)
            if btn.is_pressed():
                while btn.is_pressed():
                    time.sleep_ms(20)
                return True
        time.sleep_ms(50)
    return False

def deep_cold_boot_loop():
    print("[主循环] 冷启动 (深睡模式)")
    led.blink(1, 0.1)
    if wait_button_press(BUTTON_WINDOW_S):
        led.blink(2, 0.05)
        run_server_mode()
        new_deep = _reload_deep_sleep()
        if not new_deep:
            wait_loop()
            return
    run_slideshow_once(force=True, is_wake=False)
    _confirm_boot()
    deep_sleep(_interval_seconds())

def deep_wake_loop():
    print("[主循环] 深睡唤醒")
    led.blink(1, 0.03)
    if wait_button_press(BUTTON_WINDOW_S):
        led.blink(2, 0.05)
        run_server_mode()
        new_deep = _reload_deep_sleep()
        if not new_deep:
            wait_loop()
            return

    ok = run_slideshow_once(force=True, is_wake=True)
    if not ok:
        print("[主循环] 深睡唤醒刷图失败")
    _confirm_boot()
    deep_sleep(_interval_seconds())

def wait_loop():
    print("[主循环] 常驻轮询 (深睡 OFF)")
    led.off()
    last_slide_check = time.time()
    last_beat = 0
    confirmed = False
    cached_p = load_playlist()

    while True:
        evt = btn.poll()
        if evt == 'short':
            print("[事件] 短按 BOOT")
            led.blink(2, 0.05)
            # 防抖：确保用户彻底松开按键后再进服务器
            while btn.is_pressed():
                time.sleep_ms(20)

            run_server_mode()

            cached_p = _get_cached_playlist()
            new_deep = _reload_deep_sleep()
            if new_deep:
                ok = run_slideshow_once(force=True, is_wake=False)
                if ok:
                    _confirm_boot()
                deep_sleep(_interval_seconds())
                return
            last_slide_check = time.time()
            last_beat = time.time()
            continue
        elif evt == 'long':
            print("[事件] 长按 BOOT")
            led.blink(1, 0.1)
            continue

        now = time.time()
        if now - last_beat >= 5:
            n = len(cached_p.get('order', []))
            last_play = cached_p.get('last_play', 0)
            since = int(now - last_play) if last_play <= now else -1
            gc.collect()
            print("[心跳] free:", gc.mem_free(),
                  "| 列表:", n, "| 距上次刷屏:", since, "秒")
            last_beat = now
            if not confirmed:
                _confirm_boot()
                confirmed = True
            if n == 0:
                last_slide_check = now - 55

        if now - last_slide_check >= 5:
            ok = run_slideshow_once(force=False)
            if ok and not confirmed:
                _confirm_boot()
                confirmed = True
            cached_p = _get_cached_playlist()
            last_slide_check = now
        time.sleep_ms(50)

def _run_by_mode():
    deep = _reload_deep_sleep()
    print("[模式] deep_sleep =", "ON" if deep else "OFF")
    if deep:
        if is_deep_wake():
            deep_wake_loop()
        else:
            deep_cold_boot_loop()
    else:
        wait_loop()

def main():
    print("===== 启动 =====")
    try:
        _run_by_mode()
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print("[异常]", e)
        raise

main()