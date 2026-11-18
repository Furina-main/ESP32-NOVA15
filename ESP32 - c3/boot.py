# boot.py - 上电/唤醒时最先执行
import machine
import time
import gc
import os

# ============================================================
# 启动回滚机制
# ============================================================
ROLLBACK_FILES = ('main.py', 'server.py', 'hardware.py',
                  'extmem.py', 'microdot.py', 'webedit.py')
FLAG_FILE = 'boot_flag.txt'


def _do_rollback():
    restored = []
    for fname in ROLLBACK_FILES:
        bak = fname + '.bak'
        try:
            with open(bak, 'rb') as f:
                data = f.read()
            with open(fname, 'wb') as f:
                f.write(data)
            restored.append(fname)
        except OSError:
            pass
    try:
        os.sync()
    except (AttributeError, OSError):
        pass
    return restored


def _check_boot_flag():
    flag = None
    try:
        with open(FLAG_FILE) as f:
            flag = f.read().strip()
    except OSError:
        flag = None

    if flag == 'testing':
        print("[回滚] 检测到上次启动失败，恢复备份")
        restored = _do_rollback()
        print("[回滚] 已恢复:", restored)
        try:
            os.remove(FLAG_FILE)
        except OSError:
            pass
        print("[回滚] 1 秒后重启...")
        time.sleep(1)
        machine.reset()

    if flag == 'pending':
        print("[回滚] 检测到新代码，本次为测试启动")
        try:
            with open(FLAG_FILE, 'w') as f:
                f.write('testing')
        except OSError:
            pass


try:
    _check_boot_flag()
except Exception as e:
    print("[回滚] 异常:", e)


# ---------- 唤醒原因 ----------
try:
    _r = machine.wake_reason()
    if isinstance(_r, tuple):
        _r = _r[0]
    print("唤醒原因:", _r)
except Exception as e:
    print("wake_reason 读取失败:", e)


# ---------- 关闭 IDF 调试输出 ----------
try:
    import esp
    esp.osdebug(None)
except Exception:
    pass


# ---------- 预加载 WiFi 驱动 ----------
try:
    gc.collect()
    import network
    _wlan = network.WLAN(network.AP_IF)
    _wlan.active(False)
    del _wlan
    gc.collect()
    try:
        # C3：400KB SRAM，阈值降到 4096，更早 GC 减少碎片
        gc.threshold(4096)
    except Exception:
        pass
    print("WiFi 预加载完成, free:", gc.mem_free())
except Exception as e:
    print("WiFi 预加载失败:", e)