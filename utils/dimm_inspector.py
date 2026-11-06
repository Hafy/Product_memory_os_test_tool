import os
import re
from datetime import datetime


_INVALID_TOKENS = {
    "", "none", "null", "nil", "n/a", "na", "unknown", "undefined",
    "not specified", "not provided", "not applicable",
    "to be filled by o.e.m.", "to be filled by oem",
    "no module installed", "no dimm", "not installed",
    "0", "00000000", "0000000000000000", "0000", "-", "--", "???",
}


def _is_invalid(value):
    if value is None:
        return True
    v = value.strip().lower()
    if v in _INVALID_TOKENS:
        return True
    if v and set(v) <= {"0", " "}:
        return True
    if v and set(v) <= {"0", "f", " "}:
        return True
    return False


def _norm(value):
    if _is_invalid(value):
        return ""
    return value.strip()


_KEY_VALUE_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9\- ]*?):\s*(.*?)\s*$')
_SIZE_RE = re.compile(r'^\s*Size:\s*(\d+)\s*(GB|MB|TB)', re.IGNORECASE)
_NUM_RE = re.compile(r'(\d+)')


def _parse_block_to_dict(block):
    info = {}
    for line in block.splitlines():
        m = _KEY_VALUE_RE.match(line)
        if not m:
            continue
        key = m.group(1).strip()
        value = m.group(2).strip()
        if key not in info:
            info[key] = value
    return info


def _size_to_gb(size_str):
    m = _SIZE_RE.search("Size: " + size_str)
    if not m:
        return 0
    num = int(m.group(1))
    unit = m.group(2).upper()
    if unit == "GB":
        return num
    if unit == "TB":
        return num * 1024
    if unit == "MB":
        return num / 1024.0
    return 0


def _speed_to_int(speed_str):
    if not speed_str:
        return 0
    m = _NUM_RE.search(speed_str)
    return int(m.group(1)) if m else 0


class DIMMInfo:
    def __init__(self, raw):
        self.raw = raw
        self.locator = _norm(raw.get("Locator", ""))
        self.size_gb = _size_to_gb(raw.get("Size", ""))
        self.speed_mts = _speed_to_int(raw.get("Speed", ""))
        self.mem_type = _norm(raw.get("Type", ""))

    def short(self):
        parts = [
            f"locator:{self.locator}",
            f"size:{float(self.size_gb * 1024):.1f}",
            f"frequency:{self.speed_mts}",
        ]
        rank = _norm(self.raw.get("Rank", ""))
        if rank:
            parts.append(f"rank:{rank}")
        if self.mem_type:
            parts.append(f"type:{self.mem_type}")
        sn = _norm(self.raw.get("Serial Number", ""))
        if sn:
            parts.append(f"SN:{sn}")
        manu = _norm(self.raw.get("Manufacturer", ""))
        if manu:
            parts.append(f"manufacturer:{manu}")
        return ", ".join(parts)


class DIMMInspector:
    @classmethod
    def parse(cls, dmidecode_output):
        dimms = []
        if not dmidecode_output:
            return dimms
        parts = re.split(r'^\s*Memory Device\s*$', dmidecode_output,
                         flags=re.MULTILINE | re.IGNORECASE)
        for block in parts[1:]:
            info = _parse_block_to_dict(block)
            size_gb = _size_to_gb(info.get("Size", ""))
            if size_gb == 0:
                continue
            dimms.append(DIMMInfo(raw=info))
        return dimms


class DIMMComparator:
    @staticmethod
    def compare(base_dimms, current_dimms):
        """
        返回 (has_error, target_count, actual_count, per_dimm_lines, error_lines)
        """
        target_count = len(base_dimms)
        actual_count = len(current_dimms)

        base_total = sum(d.size_gb for d in base_dimms)
        curr_total = sum(d.size_gb for d in current_dimms)

        base_by_loc = {d.locator: d for d in base_dimms}
        curr_by_loc = {d.locator: d for d in current_dimms}
        base_locs = set(base_by_loc.keys())
        curr_locs = set(curr_by_loc.keys())

        per_dimm_lines = []
        error_lines = []

        if target_count != actual_count:
            error_lines.append(
                f"实际内存与目标配置不符,目标数量:{target_count}, 实际数量:{actual_count}"
            )
        if base_total != curr_total:
            error_lines.append(
                f"总容量不一致,基准:{base_total} GB, 当前:{curr_total} GB"
            )

        for loc in sorted(base_locs - curr_locs):
            d = base_by_loc[loc]
            per_dimm_lines.append(f"内存丢失, {d.short()}")
            error_lines.append(
                f"实际内存与目标配置不符, 丢失内存 locator:{d.locator}, "
                f"size:{float(d.size_gb * 1024):.1f}, frequency:{d.speed_mts}"
            )

        for loc in sorted(curr_locs - base_locs):
            d = curr_by_loc[loc]
            per_dimm_lines.append(f"内存新增, {d.short()}")
            error_lines.append(
                f"实际内存与目标配置不符, 新增内存 locator:{d.locator}, "
                f"size:{float(d.size_gb * 1024):.1f}, frequency:{d.speed_mts}"
            )

        for loc in sorted(base_locs & curr_locs):
            b = base_by_loc[loc]
            c = curr_by_loc[loc]
            diffs = []
            if b.speed_mts and c.speed_mts and b.speed_mts != c.speed_mts:
                diffs.append(f"频率变化: 基准 {b.speed_mts} MT/s, 当前 {c.speed_mts} MT/s")
            if b.size_gb != c.size_gb:
                diffs.append(f"容量变化: 基准 {b.size_gb} GB, 当前 {c.size_gb} GB")
            if diffs:
                per_dimm_lines.append(f"内存检查异常, locator:{loc}, " + "; ".join(diffs))
                for dd in diffs:
                    error_lines.append(f"实际内存与目标配置不符, locator:{loc}, {dd}")
            else:
                per_dimm_lines.append(f"内存检查正常, {c.short()}")

        has_error = bool(error_lines)
        if has_error:
            error_lines.append("内存配置校验失败")
        return has_error, target_count, actual_count, per_dimm_lines, error_lines