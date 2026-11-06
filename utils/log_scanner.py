import os
import re
import time

class LogScanner:
    def __init__(self, sol_log_file, error_patterns, log_callback, error_record_callback):
        self.sol_log_file = sol_log_file
        self.sol_last_offset = 0
        self.log_callback = log_callback
        self.error_record_callback = error_record_callback
        self._sol_error_pattern = re.compile('|'.join(error_patterns), re.IGNORECASE)

    def log(self, level, msg):
        if self.log_callback:
            self.log_callback(level, msg)

    def scan_incremental(self, round_idx, reboot_idx, round_dir):
        if not self.sol_log_file or not os.path.exists(self.sol_log_file):
            return False, []
        self.log("Info", "SOL增量扫描开始")
        time.sleep(0.5)
        try:
            with open(self.sol_log_file, 'r', encoding='utf-8', errors='ignore') as f:
                current_size = os.path.getsize(self.sol_log_file)
                if self.sol_last_offset > current_size:
                    self.sol_last_offset = 0
                f.seek(self.sol_last_offset)
                new_content = f.read()
                #self.log("DEBUG", f"扫描前偏移:{self.sol_last_offset}, 新增内容长度:{len(new_content)}")
                self.sol_last_offset = f.tell()
                #self.log("DEBUG", f"扫描后偏移:{self.sol_last_offset}")
                '''
                不需要保存增量的文件
                if new_content and round_idx is not None and reboot_idx is not None:
                    inc_sol_file = os.path.join(round_dir, f"sol_incremental_round{round_idx+1}_reboot{reboot_idx+1}.txt")
                    with open(inc_sol_file, 'w', encoding="utf-8") as fd:
                        fd.write(new_content)
                        self.log("INFO",f"增量SOL日志写入到{inc_sol_file}中")
                '''
        except PermissionError:
            self.log("WARN", "SOL 日志正被其他进程锁定，本次扫描跳过")
            return False, []
        except Exception as e:
            self.log("WARN", f"读取 SOL 新增日志失败: {e}")
            return False, []
        if not new_content:
            return False, []
        remaining_tail = ""
        if not new_content.endswith('\n'):
            last_newline = new_content.rfind('\n')
            if last_newline != -1:
                remaining_tail = new_content[last_newline + 1:]
                new_content = new_content[:last_newline + 1]
                self.sol_last_offset -= len(remaining_tail.encode('utf-8'))
            else:
                self.sol_last_offset -= len(new_content.encode('utf-8'))
                return False, []
        error_lines_set = set()
        for line in new_content.splitlines():
            line_stripped = line.strip()
            if line_stripped and self._sol_error_pattern.search(line_stripped):
                error_lines_set.add(line_stripped)
        if error_lines_set:
            error_lines = list(error_lines_set)
            self.log("ERROR", f"SOL 日志中发现 {len(error_lines)} 条内存错误")
            if self.error_record_callback:
                self.error_record_callback(round_dir, round_idx, reboot_idx, "SOL", error_lines)
            return True, error_lines
        self.log("INFO", "SOL日志未发现内存错误")
        return False, []

