import json
import struct
from pathlib import Path

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def _get_chain(data, fat, start, sector_size):
    out = bytearray()
    sid = start
    seen = set()
    while sid not in (FREESECT, ENDOFCHAIN) and sid < len(fat) and sid not in seen:
        seen.add(sid)
        pos = (sid + 1) * sector_size
        out.extend(data[pos:pos + sector_size])
        sid = fat[sid]
    return bytes(out)


def extract_workbook_stream(path):
    data = Path(path).read_bytes()
    if data[:8] != bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1"):
        raise ValueError("not an OLE compound file")

    sector_size = 1 << _u16(data, 30)
    mini_sector_size = 1 << _u16(data, 32)
    first_dir_sector = _u32(data, 48)
    mini_cutoff = _u32(data, 56)
    first_mini_fat_sector = _u32(data, 60)
    num_mini_fat_sectors = _u32(data, 64)
    difat = [_u32(data, 76 + i * 4) for i in range(109)]
    fat_sectors = [x for x in difat if x not in (FREESECT, ENDOFCHAIN)]

    fat = []
    for fsid in fat_sectors:
        pos = (fsid + 1) * sector_size
        fat.extend(_u32(data, pos + i * 4) for i in range(sector_size // 4))

    directory = _get_chain(data, fat, first_dir_sector, sector_size)
    entries = []
    for off in range(0, len(directory), 128):
        ent = directory[off:off + 128]
        if len(ent) < 128:
            continue
        name_len = _u16(ent, 64)
        name = ent[:max(0, name_len - 2)].decode("utf-16le", errors="ignore")
        obj_type = ent[66]
        start = _u32(ent, 116)
        size = _u32(ent, 120)
        entries.append({"name": name, "type": obj_type, "start": start, "size": size})

    root = next((e for e in entries if e["type"] == 5), None)
    workbook = next((e for e in entries if e["name"] in ("Workbook", "Book")), None)
    if not workbook:
        raise ValueError("Workbook stream not found")

    if workbook["size"] >= mini_cutoff:
        return _get_chain(data, fat, workbook["start"], sector_size)[:workbook["size"]]

    mini_fat = []
    sid = first_mini_fat_sector
    for _ in range(num_mini_fat_sectors):
        pos = (sid + 1) * sector_size
        mini_fat.extend(_u32(data, pos + i * 4) for i in range(sector_size // 4))
        sid = fat[sid]

    mini_stream = _get_chain(data, fat, root["start"], sector_size)[:root["size"]]
    out = bytearray()
    sid = workbook["start"]
    seen = set()
    while sid not in (FREESECT, ENDOFCHAIN) and sid < len(mini_fat) and sid not in seen:
        seen.add(sid)
        pos = sid * mini_sector_size
        out.extend(mini_stream[pos:pos + mini_sector_size])
        sid = mini_fat[sid]
    return bytes(out[:workbook["size"]])


def biff_records(stream):
    pos = 0
    while pos + 4 <= len(stream):
        rt, size = struct.unpack_from("<HH", stream, pos)
        payload = stream[pos + 4:pos + 4 + size]
        yield pos, rt, payload
        pos += 4 + size


def split_record_payloads(stream, target_rt):
    chunks = []
    collecting = False
    for _, rt, payload in biff_records(stream):
        if rt == target_rt:
            chunks.append(payload)
            collecting = True
        elif collecting and rt == 0x003C:
            chunks.append(payload)
        elif collecting:
            break
    return chunks


class ChunkReader:
    def __init__(self, chunks):
        self.chunks = chunks
        self.ci = 0
        self.pos = 0

    def read(self, n):
        out = bytearray()
        while n > 0 and self.ci < len(self.chunks):
            chunk = self.chunks[self.ci]
            take = min(n, len(chunk) - self.pos)
            out.extend(chunk[self.pos:self.pos + take])
            self.pos += take
            n -= take
            if self.pos >= len(chunk):
                self.ci += 1
                self.pos = 0
        if n:
            raise EOFError("chunk reader exhausted")
        return bytes(out)

    def at_chunk_start(self):
        return self.pos == 0


def read_biff8_string(reader):
    cch = struct.unpack("<H", reader.read(2))[0]
    flags = reader.read(1)[0]
    rich_runs = struct.unpack("<H", reader.read(2))[0] if flags & 0x08 else 0
    ext_size = struct.unpack("<I", reader.read(4))[0] if flags & 0x04 else 0

    chars = []
    remaining = cch
    compressed = not (flags & 0x01)
    while remaining:
        if reader.at_chunk_start():
            flags = reader.read(1)[0]
            compressed = not (flags & 0x01)
        width = 1 if compressed else 2
        available = len(reader.chunks[reader.ci]) - reader.pos if reader.ci < len(reader.chunks) else 0
        count = min(remaining, available // width)
        raw = reader.read(count * width)
        chars.append(raw.decode("latin1" if compressed else "utf-16le", errors="ignore"))
        remaining -= count
    if rich_runs:
        reader.read(4 * rich_runs)
    if ext_size:
        reader.read(ext_size)
    return "".join(chars)


def parse_sst(stream):
    chunks = split_record_payloads(stream, 0x00FC)
    if not chunks:
        return []
    reader = ChunkReader(chunks)
    _total = struct.unpack("<I", reader.read(4))[0]
    unique = struct.unpack("<I", reader.read(4))[0]
    strings = []
    for _ in range(unique):
        strings.append(read_biff8_string(reader))
    return strings


def rk_value(raw):
    mult100 = raw & 1
    is_int = raw & 2
    valbits = raw & 0xFFFFFFFC
    if is_int:
        val = struct.unpack("<i", struct.pack("<I", valbits))[0] >> 2
    else:
        packed = struct.pack("<II", 0, valbits)
        val = struct.unpack("<d", packed)[0]
    return val / 100 if mult100 else val


def parse_workbook(path):
    stream = extract_workbook_stream(path)
    sst = parse_sst(stream)
    sheets = []
    for _, rt, payload in biff_records(stream):
        if rt == 0x0085:
            offset = _u32(payload, 0)
            name_len = payload[6]
            flags = payload[7]
            raw = payload[8:8 + name_len * (2 if flags & 1 else 1)]
            name = raw.decode("utf-16le" if flags & 1 else "latin1", errors="ignore")
            sheets.append({"name": name, "offset": offset})

    results = []
    for i, sheet in enumerate(sheets):
        start = sheet["offset"]
        end = sheets[i + 1]["offset"] if i + 1 < len(sheets) else len(stream)
        cells = {}
        for _, rt, payload in biff_records(stream[start:end]):
            if rt == 0x00FD and len(payload) >= 10:
                row, col = _u16(payload, 0), _u16(payload, 2)
                idx = _u32(payload, 6)
                cells[(row, col)] = sst[idx] if idx < len(sst) else ""
            elif rt == 0x0204 and len(payload) >= 8:
                row, col = _u16(payload, 0), _u16(payload, 2)
                ln = _u16(payload, 6)
                cells[(row, col)] = payload[8:8 + ln].decode("latin1", errors="ignore")
            elif rt == 0x0203 and len(payload) >= 14:
                row, col = _u16(payload, 0), _u16(payload, 2)
                cells[(row, col)] = struct.unpack_from("<d", payload, 6)[0]
            elif rt == 0x027E and len(payload) >= 10:
                row, col = _u16(payload, 0), _u16(payload, 2)
                cells[(row, col)] = rk_value(_u32(payload, 6))
            elif rt == 0x00BD and len(payload) >= 6:
                row = _u16(payload, 0)
                first_col = _u16(payload, 2)
                last_col = _u16(payload, len(payload) - 2)
                p = 4
                for col in range(first_col, last_col + 1):
                    if p + 6 > len(payload) - 2:
                        break
                    cells[(row, col)] = rk_value(_u32(payload, p + 2))
                    p += 6
        max_row = max((r for r, _ in cells), default=-1)
        max_col = max((c for _, c in cells), default=-1)
        matrix = []
        for r in range(min(max_row + 1, 15)):
            matrix.append([cells.get((r, c)) for c in range(min(max_col + 1, 20))])
        results.append({"name": sheet["name"], "max_row": max_row + 1, "max_col": max_col + 1, "preview": matrix})
    return results


if __name__ == "__main__":
    path = r"C:\Users\lee-y\Downloads\阿里国际站后台导出的原始回复明细表.xls"
    print(json.dumps(parse_workbook(path), ensure_ascii=False, default=str, indent=2))
