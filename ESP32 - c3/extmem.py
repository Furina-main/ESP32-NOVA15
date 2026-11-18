import machine
import time
import struct
import gc

CMD_READ_ID      = 0x9F
CMD_READ_STATUS  = 0x05
CMD_WRITE_ENABLE = 0x06
CMD_READ_DATA    = 0x03
CMD_PAGE_PROGRAM = 0x02
CMD_SECTOR_ERASE = 0x20
CMD_BLOCK_ERASE  = 0xD8
CMD_CHIP_ERASE   = 0xC7

MAGIC = b'EXS1'
DIR_ADDR = 0x000000
DATA_START = 0x001000
MAX_FILES = 50
NAME_LEN = 32
ENTRY_SIZE = 40
DIR_SIZE = 4 * 1024

PAGE_SIZE = 256
SECTOR_SIZE = 4096
CHUNK = 4096

SIZE_DIR = 0xFFFFFFFF

JEDEC_VENDORS = {
    0x01: 'Spansion/Cypress', 0x0B: 'XTX', 0x1F: 'Adesto/Atmel',
    0x20: 'Micron/ST', 0x5E: 'Zbit', 0x9D: 'ISSI',
    0xBF: 'SST/Microchip', 0xC8: 'GigaDevice', 0xEF: 'Winbond',
}

JEDEC_SIZES = {
    0x11: 0.0625, 0x12: 0.125, 0x13: 0.25, 0x14: 0.5,
    0x15: 1, 0x16: 2, 0x17: 4, 0x18: 8, 0x19: 16,
    0x1A: 32, 0x1B: 64, 0x1C: 128,
}


def decode_jedec(id_bytes):
    if not id_bytes or len(id_bytes) < 3:
        return None
    mfr, mem_type, cap = id_bytes[0], id_bytes[1], id_bytes[2]
    if mfr in (0x00, 0xFF):
        return None
    return {
        'manufacturer': JEDEC_VENDORS.get(mfr, 'unknown'),
        'mfr_id': mfr, 'mem_type': mem_type, 'capacity_code': cap,
        'size_mb': JEDEC_SIZES.get(cap, 0),
        'raw': bytes(id_bytes),
    }


class ExtMem:
    def __init__(self, spi, cs=7, size_mb=8, baudrate=1_000_000):
        self.spi = spi
        self._own_spi = False
        self.cs_pin_num = cs

        try:
            self.cs = machine.Pin(cs, machine.Pin.OUT)
            self.cs.value(1)
        except ValueError as e:
            raise ValueError("外挂 CS 引脚非法 (cs=%d): %s" % (cs, e))

        # 硬件自检：读 JEDEC ID
        self.jedec_ok = False
        self.jedec_info = None
        try:
            raw = self.read_id()
            self.jedec_info = decode_jedec(raw)
            if self.jedec_info:
                self.jedec_ok = True
                print("[extmem] JEDEC OK:", self.jedec_info['manufacturer'],
                      "%.1fMB" % self.jedec_info['size_mb'])
            else:
                print("[extmem] JEDEC 读取异常，raw=%s" % raw)
        except Exception as e:
            print("[extmem] JEDEC 自检异常:", e)

        self.size = size_mb * 1024 * 1024
        self.entries = []
        self._formatted = False
        self._write_start = 0
        self._write_name = None
        self._write_pos = 0
        self._write_end = 0
        self._write_last_sector = -1
        self._write_old = None   # 暂存被覆盖的旧条目，abort 时恢复
        self._load_dir()

    @staticmethod
    def _align(n):
        if n <= 0:
            return SECTOR_SIZE
        return ((n + SECTOR_SIZE - 1) // SECTOR_SIZE) * SECTOR_SIZE

    def _cmd(self, cmd):
        self.cs.value(0)
        self.spi.write(bytes([cmd]))

    def _deselect(self):
        self.cs.value(1)

    def _wait_ready(self, timeout_ms=2000):
        t0 = time.ticks_ms()
        self.cs.value(0)
        self.spi.write(bytes([CMD_READ_STATUS]))
        try:
            while True:
                st = self.spi.read(1)[0]
                if (st & 0x01) == 0:
                    break
                if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
                    raise OSError("flash timeout")
                time.sleep_ms(1)
        finally:
            self._deselect()

    def _write_enable(self):
        self._cmd(CMD_WRITE_ENABLE)
        self._deselect()

    def read_id(self):
        self._cmd(CMD_READ_ID)
        idbuf = self.spi.read(3)
        self._deselect()
        return idbuf

    def identify(self):
        raw = self.read_id()
        info = decode_jedec(raw)
        if info:
            info['raw'] = raw
        return info

    def read(self, addr, n):
        if n <= 0:
            return b''
        self.cs.value(0)
        self.spi.write(bytes([
            CMD_READ_DATA,
            (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
        ]))
        data = self.spi.read(n)
        self._deselect()
        return data

    def erase_sector(self, addr):
        self._write_enable()
        self.cs.value(0)
        self.spi.write(bytes([
            CMD_SECTOR_ERASE,
            (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
        ]))
        self._deselect()
        self._wait_ready()

    def write_page(self, addr, data):
        if len(data) > PAGE_SIZE:
            raise ValueError("page > 256")
        if (addr & 0xFF) + len(data) > PAGE_SIZE:
            raise ValueError("cross page")
        self._write_enable()
        self.cs.value(0)
        self.spi.write(bytes([
            CMD_PAGE_PROGRAM,
            (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
        ]))
        self.spi.write(data)
        self._deselect()
        self._wait_ready()

    def write(self, addr, data):
        pos = 0
        n = len(data)
        while pos < n:
            cur = addr + pos
            offset = cur & 0xFF
            take = min(PAGE_SIZE - offset, n - pos)
            self.write_page(cur, data[pos:pos + take])
            pos += take

    def _load_dir(self):
        try:
            raw = self.read(DIR_ADDR, MAX_FILES * ENTRY_SIZE + 8)
        except Exception as e:
            print("[extmem] 读 DIR 失败:", e)
            self.entries = []
            self._formatted = False
            return
        if len(raw) < 8 or raw[:4] != MAGIC:
            print("extmem: 未格式化")
            self.entries = []
            self._formatted = False
            return
        count = raw[4] | (raw[5] << 8)
        if count > MAX_FILES:
            count = MAX_FILES
        self._formatted = True
        self.entries = []
        base = 8
        raw_len = len(raw)
        for i in range(count):
            off = base + i * ENTRY_SIZE
            if off + ENTRY_SIZE > raw_len:
                break
            try:
                name_b = raw[off:off + NAME_LEN]
                name = name_b.split(b'\x00')[0].decode()
                if not name:
                    break
                offset, size = struct.unpack(
                    '<II', raw[off + NAME_LEN:off + ENTRY_SIZE])
                if size == SIZE_DIR:
                    size = -1
                self.entries.append({
                    'name': name, 'offset': offset, 'size': size})
            except Exception as e:
                print("[extmem] 条目 %d 解析失败: %s" % (i, e))
                break

    def _encode_entry(self, buf, off, e):
        name_b = e['name'].encode()[:NAME_LEN - 1]
        buf[off:off + len(name_b)] = name_b
        for k in range(len(name_b), NAME_LEN):
            buf[off + k] = 0
        o = e['offset']
        s = e['size']
        if s < 0:
            s = SIZE_DIR
        buf[off + NAME_LEN]     = o & 0xFF
        buf[off + NAME_LEN + 1] = (o >> 8) & 0xFF
        buf[off + NAME_LEN + 2] = (o >> 16) & 0xFF
        buf[off + NAME_LEN + 3] = (o >> 24) & 0xFF
        buf[off + NAME_LEN + 4] = s & 0xFF
        buf[off + NAME_LEN + 5] = (s >> 8) & 0xFF
        buf[off + NAME_LEN + 6] = (s >> 16) & 0xFF
        buf[off + NAME_LEN + 7] = (s >> 24) & 0xFF

    def _save_dir(self):
        buf = bytearray(MAX_FILES * ENTRY_SIZE + 8)
        buf[0:4] = MAGIC
        count = len(self.entries)
        buf[4] = count & 0xFF
        buf[5] = (count >> 8) & 0xFF
        for i, e in enumerate(self.entries):
            self._encode_entry(buf, 8 + i * ENTRY_SIZE, e)
        for sector in range(0, DIR_SIZE, SECTOR_SIZE):
            self.erase_sector(DIR_ADDR + sector)
        self.write(DIR_ADDR, buf)

    def format(self):
        self.entries = []
        self._save_dir()
        self._formatted = True
        print("extmem: 格式化完成")

    def is_formatted(self):
        return self._formatted

    def _find(self, name):
        for e in self.entries:
            if e['name'] == name:
                return e
        return None

    def exists(self, name):
        return self._find(name) is not None

    def list_dir(self):
        return [{'name': e['name'], 'size': e['size']} for e in self.entries]

    def _alloc(self, size):
        need = self._align(size)
        used = []
        for e in self.entries:
            if e['size'] < 0:
                continue
            used.append((e['offset'], e['offset'] + self._align(e['size'])))
        used.sort()
        cursor = DATA_START
        for start, end in used:
            if start - cursor >= need:
                return cursor
            if end > cursor:
                cursor = end
        return cursor

    def delete(self, name):
        e = self._find(name)
        if not e:
            return False
        self.entries.remove(e)
        self._save_dir()
        return True

    def mkdir(self, name):
        try:
            nb = len(name.encode('utf-8'))
        except Exception:
            raise ValueError("bad name encoding")
        if nb > NAME_LEN - 1:
            raise ValueError("name too long (%d bytes, max %d)"
                             % (nb, NAME_LEN - 1))
        if self.exists(name):
            return False
        self.entries.append({'name': name, 'offset': 0, 'size': -1})
        self._save_dir()
        return True

    def rename(self, old_name, new_name):
        if old_name == new_name:
            return True
        e = self._find(old_name)
        if not e:
            raise OSError("no file: " + old_name)
        if e['size'] < 0:
            raise OSError("cannot rename a directory")
        if self.exists(new_name):
            raise OSError("target exists: " + new_name)
        try:
            nb = len(new_name.encode('utf-8'))
        except Exception:
            raise ValueError("bad name encoding")
        if nb > NAME_LEN - 1:
            raise ValueError("name too long (%d bytes, max %d)"
                             % (nb, NAME_LEN - 1))
        e['name'] = new_name
        self._save_dir()
        return True

    def begin_write(self, name, size):
        try:
            nb = len(name.encode('utf-8'))
        except Exception:
            raise ValueError("bad name encoding")
        if nb > NAME_LEN - 1:
            raise ValueError("name too long (%d bytes, max %d)"
                             % (nb, NAME_LEN - 1))
        if size <= 0:
            raise ValueError("size must be > 0")

        # 同名校验：不允许把目录当文件覆盖
        old = self._find(name)
        if old is not None and old['size'] < 0:
            raise OSError("存在同名目录: " + name)

        # 暂存旧条目以便 abort 恢复；begin_write 不立即 _save_dir，
        # 所以磁盘目录在 end_write 前仍保持旧状态，掉电可回到旧目录
        self._write_old = old
        if old is not None:
            self.entries.remove(old)

        start = self._alloc(size)
        if start + self._align(size) > self.size:
            # 空间不足，恢复旧条目，保持内存/磁盘一致
            if old is not None:
                self.entries.append(old)
            self._write_old = None
            raise OSError("ext flash full")

        self._write_start = start
        self._write_name = name
        self._write_pos = start
        self._write_end = start + self._align(size)
        self._write_last_sector = -1
        return start

    def write_chunk(self, chunk):
        if self._write_name is None:
            raise OSError("no begin_write in progress")
        pos = self._write_pos
        n = len(chunk)
        if pos + n > self._write_end:
            raise OSError("write overflow")
        i = 0
        while i < n:
            sector = (pos // SECTOR_SIZE) * SECTOR_SIZE
            if sector != self._write_last_sector:
                self.erase_sector(sector)
                self._write_last_sector = sector
            page_off = pos % PAGE_SIZE
            take = min(PAGE_SIZE - page_off, n - i)
            self.write_page(pos, chunk[i:i + take])
            pos += take
            i += take
        self._write_pos = pos

    def end_write(self):
        if self._write_name is None:
            raise OSError("no begin_write in progress")
        size = self._write_pos - self._write_start
        self.entries.append({
            'name': self._write_name,
            'offset': self._write_start,
            'size': size,
        })
        self._write_old = None
        self._write_name = None
        self._save_dir()
        return size

    def abort_write(self):
        # 恢复被 begin_write 临时摘掉的旧条目，保证内存/磁盘目录一致
        if self._write_name is None:
            return
        if self._write_old is not None:
            self.entries.append(self._write_old)
        self._write_old = None
        self._write_name = None

    def read_all(self, name):
        e = self._find(name)
        if not e:
            raise OSError("no file: " + name)
        if e['size'] < 0:
            raise OSError("is a directory: " + name)
        return self.read(e['offset'], e['size'])

    def read_chunks(self, name, chunk_size=CHUNK):
        e = self._find(name)
        if not e:
            raise OSError("no file: " + name)
        if e['size'] < 0:
            return
        offset = e['offset']
        remain = e['size']
        counter = 0
        while remain > 0:
            n = min(chunk_size, remain)
            yield self.read(offset, n)
            offset += n
            remain -= n
            counter += 1
            if counter % 8 == 0:
                gc.collect()

    def free(self, exclude_name=None):
        # exclude_name: 覆盖同名文件时可用来忽略旧文件占用来做空间预检
        used = 0
        for e in self.entries:
            if e['size'] < 0:
                continue
            if exclude_name is not None and e['name'] == exclude_name:
                continue
            used += self._align(e['size'])
        return max(0, self.size - DATA_START - used)

    def total(self):
        return self.size - DATA_START

    def close(self):
        self.entries = []


# ============================================================
# 单例
# ============================================================
_instance = None

def get_instance(spi, cs=7, size_mb=8):
    global _instance
    if _instance is None:
        if spi is None:
            raise ValueError("必须传入外挂屏已创建的 spi 对象！")
        _instance = ExtMem(spi=spi, cs=cs, size_mb=size_mb)
    return _instance

def release_instance():
    global _instance
    if _instance is not None:
        try:
            _instance.close()
        except Exception:
            pass
        _instance = None
    gc.collect()