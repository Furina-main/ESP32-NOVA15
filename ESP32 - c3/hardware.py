import machine
import time
import ujson
import gc
import os

try:
    with open('www/screen_config.json') as f:
        _cfg = ujson.load(f)
    _hw = _cfg.get('hardware', {})
    _sc = _cfg.get('screen', {})
except Exception:
    _hw, _sc = {}, {}

LED_PIN          = _hw.get('led_pin', 8)
LED_ACTIVE_LOW   = _hw.get('led_active_low', True)
WAKE_PIN         = _hw.get('wake_pin', 9)
WAKE_PULL        = _hw.get('wake_pull', 'up')
LONG_PRESS_MS    = _hw.get('long_press_ms', 3000)
WIFI_DURATION    = _hw.get('wifi_duration', 300)
AP_SSID          = _hw.get('ap_ssid', 'ESP32-Badge')
AP_PASSWORD      = _hw.get('ap_password', '')
SPACE_RESERVE_KB = _hw.get('space_reserve_kb', 64)

SPI_SCK          = _hw.get('spi_sck', 4)
SPI_MOSI         = _hw.get('spi_mosi', 6)
SPI_MISO         = _hw.get('spi_miso', 5)
SPI_BAUD         = int(_hw.get('spi_baud', 2000000))
EPD_CS           = _hw.get('epd_cs', 10)
EPD_DC           = _hw.get('epd_dc', 3)
EPD_RST          = _hw.get('epd_rst', 20)
EPD_BUSY         = _hw.get('epd_busy', 21)

EXT_CS           = _hw.get('ext_cs', 7)
EXT_SIZE_MB      = _hw.get('ext_size_mb', 8)

SCREEN_WIDTH     = 768
SCREEN_HEIGHT    = 552
GATE_HEIGHT      = 600
ROW_BYTES        = 192
FRAME_BYTES      = ROW_BYTES * SCREEN_HEIGHT

DEEP_SLEEP_ON = str(_hw.get('deep_sleep', 'OFF')).strip().upper() == 'ON'

def _make_rev():
    rev = bytearray(256)
    for i in range(256):
        p0 = (i >> 6) & 0x03
        p1 = (i >> 4) & 0x03
        p2 = (i >> 2) & 0x03
        p3 = i & 0x03
        rev[i] = (p3 << 6) | (p2 << 4) | (p1 << 2) | p0
    return bytes(rev)

_REV = _make_rev()

class StatusLED:
    def __init__(self, pin=8, active_low=True):
        self.active_low = active_low
        self.led = machine.Pin(pin, machine.Pin.OUT)
        self.off()

    def on(self):
        self.led.value(0 if self.active_low else 1)

    def off(self):
        self.led.value(1 if self.active_low else 0)

    def blink(self, times=3, interval=0.1):
        for _ in range(times):
            self.on()
            time.sleep(interval)
            self.off()
            time.sleep(interval)

class BootButton:
    def __init__(self, pin=9, pull='up', long_press_ms=3000):
        self.long_press_ms = long_press_ms
        self._press_start = None
        self._long_fired = False
        p = machine.Pin.PULL_UP if pull == 'up' else (
            machine.Pin.PULL_DOWN if pull == 'down' else None)
        self.btn = machine.Pin(pin, machine.Pin.IN, p) if p is not None \
            else machine.Pin(pin, machine.Pin.IN)

    def is_pressed(self):
        return self.btn.value() == 0

    def poll(self):
        pressed = self.is_pressed()
        now = time.ticks_ms()
        if pressed:
            if self._press_start is None:
                self._press_start = now
                self._long_fired = False
            elif (not self._long_fired and
                  time.ticks_diff(now, self._press_start) >= self.long_press_ms):
                self._long_fired = True
                return 'long'
        else:
            if self._press_start is not None:
                dt = time.ticks_diff(now, self._press_start)
                was_long = self._long_fired
                self._press_start = None
                self._long_fired = False
                if not was_long and dt >= 50:
                    return 'short'
        return None

class EPaper:
    def __init__(self):
        print("EPaper 初始化...")
        try:
            self.spi = machine.SPI(1, baudrate=SPI_BAUD, polarity=0, phase=0,
                                   sck=machine.Pin(SPI_SCK),
                                   mosi=machine.Pin(SPI_MOSI),
                                   miso=machine.Pin(SPI_MISO))
        except Exception as e:
            print("SPI 初始化失败，回退 SoftSPI:", e)
            self.spi = machine.SoftSPI(baudrate=1000000, polarity=0, phase=0,
                                       sck=machine.Pin(SPI_SCK),
                                       mosi=machine.Pin(SPI_MOSI),
                                       miso=machine.Pin(SPI_MISO))

        self.cs   = machine.Pin(EPD_CS, machine.Pin.OUT, value=1)
        self.dc   = machine.Pin(EPD_DC, machine.Pin.OUT, value=0)
        self.rst  = machine.Pin(EPD_RST, machine.Pin.OUT, value=1)
        self.busy = machine.Pin(EPD_BUSY, machine.Pin.IN)
        self.initialized = False

    def _cmd(self, cmd):
        self.dc.value(0)
        self.cs.value(0)
        self.spi.write(bytes([cmd]))
        self.cs.value(1)

    def _data(self, data):
        self.dc.value(1)
        self.cs.value(0)
        if isinstance(data, int):
            self.spi.write(bytes([data]))
        else:
            self.spi.write(data)
        self.cs.value(1)

    def _wait_ready(self, timeout_ms=10000):
        t0 = time.ticks_ms()
        while self.busy.value() == 0:
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                print("[EPaper] busy 超时 (%d ms)" % timeout_ms)
                return False
            time.sleep_ms(10)
        return True

    def _reset(self):
        self.rst.value(1)
        time.sleep_ms(20)
        self.rst.value(0)
        time.sleep_ms(40)
        self.rst.value(1)
        time.sleep_ms(50)

    def _gate_row(self, logical_y):
        # 完全对齐原厂 C 代码：A1 版本
        if logical_y < 300:
            return logical_y * 2
        else:
            return logical_y * 2 - 599

    def _set_window(self, y0, y1):
        x1 = SCREEN_WIDTH - 1
        bytes_data = [
            0x00, 0x00, (x1 >> 8) & 0xFF, x1 & 0xFF,
            (y0 >> 8) & 0xFF, y0 & 0xFF, (y1 >> 8) & 0xFF, y1 & 0xFF,
            0x01
        ]
        self._cmd(0x83)
        self._data(bytes(bytes_data))

    def init_controller(self):
        self.cs.value(1)
        time.sleep_ms(10)
        self._reset()
        if not self._wait_ready(): return False
        time.sleep_ms(30)

        self._cmd(0x00)
        self._data(0x0B)
        self._cmd(0x61)
        self._data((SCREEN_WIDTH >> 8) & 0xFF)
        self._data(SCREEN_WIDTH & 0xFF)
        self._data((GATE_HEIGHT >> 8) & 0xFF)
        self._data(GATE_HEIGHT & 0xFF)

        for _ in range(2):
            self._cmd(0x04)
            time.sleep_ms(2)
            if not self._wait_ready(): return False

        self.initialized = True
        return True

    def _finalize_refresh(self):
        self._set_window(0, GATE_HEIGHT - 1)
        self._cmd(0x12)
        self._data(0x01)
        time.sleep_ms(5)
        if not self._wait_ready(180000):
            self.initialized = False
            return False
        self._cmd(0x02)
        self._data(0x00)
        time.sleep_ms(2)
        self._wait_ready()
        self.initialized = False
        return True

    def _show_file(self, path):
        st = os.stat(path)
        if st[6] != FRAME_BYTES: return False
        if not self.initialized:
            if not self.init_controller(): return False

        with open(path, 'rb') as f:
            for y in range(SCREEN_HEIGHT):
                gate_y = self._gate_row(y)
                self._set_window(gate_y, gate_y)
                self._cmd(0x10)

                src_y = SCREEN_HEIGHT - 1 - y
                f.seek(src_y * ROW_BYTES)
                row = f.read(ROW_BYTES)
                if len(row) < ROW_BYTES:
                    row = row + b'\x55' * (ROW_BYTES - len(row))

                out_buf = bytearray(ROW_BYTES)
                for x in range(ROW_BYTES):
                    out_buf[x] = _REV[row[ROW_BYTES - 1 - x]]

                self._data(out_buf)
                if (y & 7) == 7:
                    time.sleep_ms(1)
        return self._finalize_refresh()

    def _show_ext_file(self, ext, name):
        e = ext._find(name)
        if not e or e['size'] != FRAME_BYTES: return False
        if not self.initialized:
            if not self.init_controller(): return False

        start = e['offset']
        for y in range(SCREEN_HEIGHT):
            gate_y = self._gate_row(y)
            self._set_window(gate_y, gate_y)
            self._cmd(0x10)

            src_y = SCREEN_HEIGHT - 1 - y
            row = ext.read(start + src_y * ROW_BYTES, ROW_BYTES)
            if len(row) < ROW_BYTES:
                row = row + b'\x55' * (ROW_BYTES - len(row))

            out_buf = bytearray(ROW_BYTES)
            for x in range(ROW_BYTES):
                out_buf[x] = _REV[row[ROW_BYTES - 1 - x]]

            self._data(out_buf)
            if (y & 7) == 7:
                time.sleep_ms(1)
        return self._finalize_refresh()

    def show_file(self, path):
        gc.collect()
        try:
            return self._show_file(path)
        except Exception as e:
            print("刷屏异常:", e)
            self.initialized = False
            return False

    def show_ext_file(self, ext, name):
        gc.collect()
        try:
            return self._show_ext_file(ext, name)
        except Exception as e:
            print("外挂刷屏异常:", e)
            self.initialized = False
            return False

    def sleep(self):
        try:
            self._cmd(0x07)
            self._data(0xA5)
            time.sleep_ms(200)
        except Exception as e:
            print("EPaper sleep 失败:", e)
        finally:
            self.initialized = False

led = StatusLED(pin=LED_PIN, active_low=LED_ACTIVE_LOW)
btn = BootButton(pin=WAKE_PIN, pull=WAKE_PULL, long_press_ms=LONG_PRESS_MS)
epaper = EPaper()