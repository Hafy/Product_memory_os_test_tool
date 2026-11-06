import os
import time
import re
import subprocess
import threading
import paramiko
from datetime import datetime
from utils.constants import (
    SSH_KEY, MEMORY_ERROR_PATTERNS,
    STRESSAPPTEST_ERROR_PATTERNS, LOCAL_LOG_BASE, SCRIPT_DIR
)
from utils.ssh_utils import ssh_run
from utils.helpers import clean_remote_log, clear_remote_logs, scan_single_log_for_ecc
from utils.dimm_inspector import DIMMInspector, DIMMComparator

class SOLMixin:
    def sol_deactivate(self):
        from utils.constants import get_ipmitool_path
        ipmitool_exe = get_ipmitool_path()
        # 硬编码加上 -C 17

        subprocess.run([ipmitool_exe, "-I", "lanplus", "-C", "17", "-H", self.bmc_ip,
                       "-U", self.bmc_user, "-P", self.bmc_pwd, "sol", "deactivate"],
                       creationflags=subprocess.CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        time.sleep(0.8)

    def kill_sol_by_title(self):
        title = f"SOL_SESSION_{self.bmc_ip}"
        subprocess.run(f'taskkill /F /FI "WINDOWTITLE eq {title}" /T',
                       shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)

    def start_sol(self, logfile):
        from utils.constants import get_ipmitool_path
        ipmitool_exe = get_ipmitool_path()
        self.sol_deactivate()
        self.kill_sol_by_title()

        # ========== 创建 bat 目录 ==========
        bat_dir = os.path.join(SCRIPT_DIR, "bat")
        os.makedirs(bat_dir, exist_ok=True)

        bat_content = f'''@echo off
title SOL_SESSION_{self.bmc_ip}
"{ipmitool_exe}" -I lanplus -C 17 -H {self.bmc_ip} -U {self.bmc_user} -P {self.bmc_pwd} sol activate >> "{logfile}" 2>&1
'''
        bat_path = os.path.join(bat_dir, f"temp_sol_{self.bmc_ip}.bat")
        with open(bat_path, "w", encoding="gbk") as f:
            f.write(bat_content)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 6
        proc = subprocess.Popen(["cmd.exe", "/c", "start", "/min", "cmd", "/c", bat_path],
                                creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startupinfo,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc, bat_path

    def stop_sol(self, proc=None, bat_path=None):
        self.sol_deactivate()
        self.kill_sol_by_title()
        if proc:
            proc.terminate()
        if bat_path and os.path.exists(bat_path):
            os.remove(bat_path)

class RebootCheckMixin:
    def _run_ipmitool(self, command, timeout=30):
        from utils.constants import get_ipmitool_path
        ipmitool_exe = get_ipmitool_path()
        
        cmd = [ipmitool_exe, "-I", "lanplus", "-C", "17", "-H", self.bmc_ip,
               "-U", self.bmc_user, "-P", self.bmc_pwd] + command.split()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            return result.stdout.strip()
        except Exception as e:
            #self.log("WARN", f"ipmitool 命令失败: {command} - {e}")
            return ""

    def _check_bmc_alive(self):
        return bool(self._run_ipmitool("lan print", timeout=10))

    def wait_bmc_ready(self, max_wait=300):
        start = time.time()
        self.log("INFO", f"等待BMC就绪，最长{max_wait}秒")
        while time.time() - start < max_wait:
            if hasattr(self, 'stop_event') and self.stop_event and self.stop_event.is_set():
                raise InterruptedError("测试被用户停止")
            if self._check_bmc_alive():
                self.log("INFO", "BMC就绪")
                return
            time.sleep(10)
        raise TimeoutError("BMC等待就绪超时")

    def collect_messages_syslog_only(self, local_log_dir):
        has_error = False
        ssh_ok = False
        for attempt in range(3):
            try:
                self.ssh_run("echo ok", silent=True, timeout=5)
                ssh_ok = True
                break
            except:
                time.sleep(2)
        if not ssh_ok:
            self.log("WARN", "SSH 不可通，跳过 messages/syslog 收集")
            return False
        try:
            messages_file = os.path.join(local_log_dir, "messages.txt")
            out = self.ssh_run("cat /var/log/messages 2>/dev/null", silent=True, timeout=30)
            with open(messages_file, 'w', encoding='utf-8') as f:
                f.write(out)
            if self.scan_single_log_for_errors(messages_file, "messages"):
                has_error = True
            syslog_file = os.path.join(local_log_dir, "syslog.txt")
            out = self.ssh_run("cat /var/log/syslog 2>/dev/null", silent=True, timeout=30)
            with open(syslog_file, 'w', encoding='utf-8') as f:
                f.write(out)
            if self.scan_single_log_for_errors(syslog_file, "syslog"):
                has_error = True
        except Exception as e:
            self.log("WARN", f"收集 messages/syslog 失败: {e}")
        return has_error

    def collect_messages_syslog_dmesg_sel(self, local_log_dir):
        has_error = False
        ssh_ok = False
        #self.log("INFO", "尝试连接服务器做日志收尾工作")
        for attempt in range(3):
            try:
                self.ssh_run("echo ok", silent=True, timeout=5)
                ssh_ok = True
                break
            except:
                time.sleep(2)
        if not ssh_ok:
            self.log("WARN", "SSH 不可通，跳过系统日志收集")
            return False
        self.log("INFO", "收集扫描系统日志：message/syslog/dmesg/sel...")
        # messages
        try:
            messages_file = os.path.join(local_log_dir, "messages.txt")
            out = self.ssh_run("cat /var/log/messages 2>/dev/null", silent=True, timeout=30)
            with open(messages_file, 'w', encoding='utf-8') as f:
                f.write(out)
            if self.scan_single_log_for_errors(messages_file, "messages"):
                has_error = True
                self.log("ERROR", "messages 中发现内存错误")
        except Exception as e:
            self.log("WARN", f"收集 messages 失败: {e}")
        # syslog
        try:
            syslog_file = os.path.join(local_log_dir, "syslog.txt")
            out = self.ssh_run("cat /var/log/syslog 2>/dev/null", silent=True, timeout=30)
            with open(syslog_file, 'w', encoding='utf-8') as f:
                f.write(out)
            if self.scan_single_log_for_errors(syslog_file, "syslog"):
                has_error = True
                self.log("ERROR", "syslog 中发现内存错误")
        except Exception as e:
            self.log("WARN", f"收集 syslog 失败: {e}")
        # dmesg
        
           
        if self.collect_dmesg_single(local_log_dir):
            has_error = True
            self.log("ERROR", "dmesg 中发现内存错误")
      
        # SEL
        if self.check_sel_full(local_log_dir, 0, 0):
            self.log("ERROR", "SEL 中发现内存错误")
            has_error = True
        self.log("INFO", f"所有日志保存在：{local_log_dir}")
        return has_error

    def scan_stressapptest_errors_from_content(self, content, local_log_dir, round_idx=0, reboot_idx=0):
        pattern = re.compile('|'.join(STRESSAPPTEST_ERROR_PATTERNS), re.IGNORECASE)
        errors = []
        for line in content.splitlines():
            if pattern.search(line):
                errors.append(line.strip())
        if errors:
            self.log("ERROR", f"stressapptest 中发现 {len(errors)} 条错误")
            self._record_reboot_error(local_log_dir, round_idx, reboot_idx, "stressapptest", errors)
            return True, errors
        return False, []

    def download_and_scan_stressapptest_log(self, remote_log_path, local_log_dir,
                                            local_filename="stressapptest.log",
                                            round_idx=0, reboot_idx=0,
                                            append=False):
        local_path = os.path.join(local_log_dir, local_filename)
        try:
            content = self.ssh_run(f"cat {remote_log_path}", silent=True, timeout=30)
            if not content:
                self.log("WARN", f"远程日志 {remote_log_path} 为空")
                return False, local_path
            cleaned = clean_remote_log(content)
            mode = 'a' if append else 'w'
            with open(local_path, mode, encoding='utf-8') as f:
                if append:
                    f.write(f"\n========== Round {round_idx+1} ==========\n")
                f.write(cleaned)
                if append:
                    f.write("\n")
            self.log("INFO", f"stressapptest 日志已保存到 {local_path}")
            has_error, _ = self.scan_stressapptest_errors_from_content(cleaned, local_log_dir, round_idx, reboot_idx)
            return has_error, local_path
        except Exception as e:
            self.log("WARN", f"下载 stressapptest 日志失败: {e}")
            return True, local_path

    def wait_system_ssh_ready(self, silent_wait=300, max_wait=3600):
        interval = 60  # 1分钟轮询一次
        self.log("INFO", f"静默等待 {silent_wait//60} 分钟后检测SSH，超时时间 {max_wait//60} 分钟")
       
        # ========== 轮询等待 silent_wait，响应 stop_event ==========
        elapsed = 0
        while elapsed < silent_wait:
            if hasattr(self, 'stop_event') and self.stop_event.is_set():
                raise InterruptedError("测试被用户停止")
            time.sleep(interval)
            elapsed += interval
        
        if hasattr(self, 'stop_event') and self.stop_event.is_set():
            raise InterruptedError("测试被用户停止")
        
        # ========== 检测 SSH 连通性 ==========
        start = time.time()
        while True:
            if hasattr(self, 'stop_event') and self.stop_event.is_set():
                raise InterruptedError("测试被用户停止")
            
            elapsed = time.time() - start
            if elapsed >= max_wait:
                raise TimeoutError("SSH系统启动超时")
            
            try:
                self.ssh_run("uname -r", silent=True)
                self.log("INFO", "系统SSH正常连通")
                break
            except:
                self.log("INFO", f"SSH未就绪，已等待 {int(elapsed//60)} 分钟")
                time.sleep(interval)
                

        # ============ 新增：物理内存条检查 ============

    def _check_dmidecode(self):
        """检查目标机是否安装 dmidecode"""
        try:
            out = self.ssh_run("which dmidecode", silent=True, timeout=10).strip()
            if not out:
                self.log("ERROR", "目标机未安装 dmidecode，无法进行物理内存条检查")
                self.log("ERROR", "请安装：yum install -y dmidecode 或 apt install -y dmidecode")
                return False
            return True
        except Exception as e:
            self.log("ERROR", f"检查 dmidecode 失败: {e}")
            return False

    def get_current_dimm_info(self):
        """
        通过 SSH 执行 dmidecode -t memory，解析出每根内存条信息。
        返回 DIMMInfo 列表；失败返回 None。
        """
        try:
            out = self.ssh_run("dmidecode -t memory 2>/dev/null", silent=True, timeout=30)
            if not out or "Memory Device" not in out:
                out = self.ssh_run("sudo dmidecode -t memory 2>/dev/null",
                                   silent=True, timeout=30)
            if not out or "Memory Device" not in out:
                self.log("ERROR", "dmidecode -t memory 输出为空，无法获取物理内存条信息")
                return None
            dimms = DIMMInspector.parse(out)
            if not dimms:
                self.log("ERROR", "dmidecode 解析后未获得任何内存条，请检查 dmidecode 是否安装")
                return None
            return dimms
        except Exception as e:
            self.log("ERROR", f"获取物理内存条信息失败: {e}")
            return None

    def save_baseline_dimm_snapshot(self, local_log_dir, round_label=None):
        """
        测试开始 / 每轮开始：抓 dmidecode，解析基准，写入 dimm/ 独立目录。
        - 不传 round_label（其他测试）：dimm/dimm_check_<时间戳>.log
        - 传 round_label（RMT）：dimm/dimm_check_<round_label>_<时间戳>.log

        返回 (dimms, log_path)；失败返回 (None, None)
        """
        self.log("INFO", "读取测试前物理内存条信息（dmidecode -t memory）...")
        try:
            raw = self.ssh_run("dmidecode -t memory 2>/dev/null", silent=True, timeout=30)
            if not raw or "Memory Device" not in raw:
                raw = self.ssh_run("sudo dmidecode -t memory 2>/dev/null",
                                   silent=True, timeout=30)
        except Exception as e:
            self.log("ERROR", f"执行 dmidecode 失败: {e}")
            return None, None

        if not raw or "Memory Device" not in raw:
            self.log("ERROR", "无法获取 dmidecode 输出，基准内存条快照保存失败")
            return None, None

        dimms = DIMMInspector.parse(raw)
        if not dimms:
            self.log("ERROR", "解析 dmidecode 输出未获得任何内存条，基准快照保存失败")
            return None, None

        # ========== 独立 dimm/ 目录 ==========
        dimm_dir = os.path.join(local_log_dir, "dimm")
        os.makedirs(dimm_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 文件名：其他测试不带 round_label，RMT 带
        if round_label:
            log_filename = f"dimm_check_{round_label}_{timestamp}.log"
        else:
            log_filename = f"dimm_check_{timestamp}.log"

        log_path = os.path.join(dimm_dir, log_filename)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_gb = sum(d.size_gb for d in dimms)

        with open(log_path, "w", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"[{now_str}] [   INFO] -- 内存条基准快照\n")
            f.write("=" * 80 + "\n")
            f.write(f"[{now_str}] [   INFO] -- 数量: {len(dimms)}\n")
            f.write(f"[{now_str}] [   INFO] -- 总容量: {total_gb} GB\n")
            f.write(f"[{now_str}] [   INFO] -- 基准内存条明细:\n")
            for i, d in enumerate(dimms, 1):
                f.write(f"[{now_str}] [  DEBUG] --   [{i}/{len(dimms)}] {d.short()}\n")

        self.log("INFO", f"基准内存条数量: {len(dimms)}，总容量: {total_gb} GB")
        for d in dimms:
            self.log("INFO", f"  [基准] {d.short()}")
        self.log("INFO", f"基准快照已保存: {log_path}")

        self.baseline_dimms = dimms
        self._dimm_check_log_path = log_path
        return dimms, log_path

    def check_dimms_consistency(self, local_log_dir, round_idx=0, reboot_idx=0,
                                baseline_dimms=None, label=None):
        """
        每次重启后：抓 dmidecode 原始输出 → 比对 → 追加到同一个 log。
        返回 (has_error, current_total_gb, current_dimms)
        """
        if baseline_dimms is None:
            baseline_dimms = getattr(self, "baseline_dimms", None)
        if not baseline_dimms:
            self.log("ERROR", "未找到基准内存条快照，无法进行内存条一致性检查")
            return True, 0, None

        try:
            raw_dmidecode = self.ssh_run("dmidecode -t memory 2>/dev/null",
                                         silent=True, timeout=30)
            if not raw_dmidecode or "Memory Device" not in raw_dmidecode:
                raw_dmidecode = self.ssh_run("sudo dmidecode -t memory 2>/dev/null",
                                             silent=True, timeout=30)
        except Exception as e:
            self.log("ERROR", f"执行 dmidecode 失败: {e}")
            return True, 0, None

        if not raw_dmidecode or "Memory Device" not in raw_dmidecode:
            self.log("ERROR", "无法获取 dmidecode 输出，判定为异常")
            return True, 0, None

        current_dimms = DIMMInspector.parse(raw_dmidecode)
        if not current_dimms:
            self.log("ERROR", "解析 dmidecode 未获得任何内存条，判定为异常")
            return True, 0, None

        total_gb = sum(d.size_gb for d in current_dimms)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        has_error, target_count, actual_count, per_dimm_lines, error_lines = \
            DIMMComparator.compare(baseline_dimms, current_dimms)

        log_path = getattr(self, "_dimm_check_log_path", None)
        if not log_path:
            # 兜底：如果基准还没设置，创建 dimm/ 目录下的比对文件
            dimm_dir = os.path.join(local_log_dir, "dimm")
            os.makedirs(dimm_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(dimm_dir, f"dimm_check_{timestamp}.log")
            self._dimm_check_log_path = log_path

        # 标题：label > 多轮（rmt）> 默认（只有轮次）
        if label:
            check_title = label
        elif round_idx > 0:
            check_title = f"第 {round_idx+1} 轮 第 {reboot_idx+1} 次重启内存检查"
        else:
            check_title = f"第 {reboot_idx+1} 次重启内存检查"

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"[{now_str}] [   INFO] -- {check_title}\n")
            f.write("=" * 80 + "\n")
            f.write(f"[{now_str}] [  DEBUG] -- dmidecode -t memory\n")
            f.write(f"[{now_str}] [  DEBUG] -- Command status is \"0\"\n")
            for line in raw_dmidecode.splitlines():
                f.write(f"[{now_str}] [  DEBUG] -- {line}\n")
            f.write(f"[{now_str}] [   INFO] -- 内存数量，目标值:{target_count},实际值:{actual_count}\n")
            for line in per_dimm_lines:
                level = "  DEBUG" if line.startswith("内存检查正常") else "  ERROR"
                f.write(f"[{now_str}] [{level}] -- {line}\n")

            if not has_error:
                f.write(f"[{now_str}] [   INFO] -- 内存检查正常\n")
            else:
                for line in error_lines:
                    f.write(f"[{now_str}] [  ERROR] -- {line}\n")

        self.log("INFO", f"当前内存数量: {actual_count}，总容量: {total_gb} GB")
        for line in per_dimm_lines:
            if line.startswith("内存检查正常"):
                self.log("DEBUG", line)
            else:
                self.log("ERROR", line)

        if has_error:
            for line in error_lines:
                self.log("ERROR", line)
            self._record_reboot_error(
                local_log_dir, round_idx, reboot_idx,
                "内存条不一致", "\n".join(error_lines)
            )
        else:
            self.log("INFO", f"内存检查正常，共 {actual_count} 根，{total_gb} GB")

        return has_error, total_gb, current_dimms

    def save_system_info_before_test(self, local_log_dir):
        info_file = os.path.join(local_log_dir, "system_info_before_test.txt")
        try:
            # ========== OS 信息 ==========
            os_release = self.ssh_run("cat /etc/os-release 2>/dev/null", silent=True, timeout=10)
            kernel = self.ssh_run("uname -a", silent=True, timeout=5)
            dmi_out = self.ssh_run("dmidecode -t memory 2>/dev/null || dmidecode -t memory", silent=True, timeout=30)
            free_out = self.ssh_run("free -m", silent=True, timeout=10)
            cpu_info = self.ssh_run("dmidecode -t processor", silent=True, timeout=10)
            with open(info_file, "w", encoding="utf-8") as f:
                f.write("========== free -m ==========\n")
                f.write(free_out)

                f.write("========== OS Information ==========\n")
                if os_release:
                    f.write(os_release)
                    f.write("\n")
                else:
                    f.write("(无法获取 /etc/os-release)\n")

                f.write("\n\n========== dmidecode -t memory ==========\n")
                f.write(dmi_out)
                
                if kernel:
                    f.write("\n========== Kernel ==========\n")
                    f.write(kernel)
                    f.write("\n\n")

                if cpu_info:
                    f.write("\n========== CPU ==========\n")
                    f.write(cpu_info)
                    f.write("\n\n")

                
            self.log("INFO", f"系统内存信息已保存到 {info_file}")
        except Exception as e:
            self.log("WARN", f"保存系统内存信息失败: {e}")


    def _parse_l3_cache_output(self, raw_output):
        """
        解析 L3 缓存输出，返回 MB 值
        
        支持格式：
        - "61440K"                → 60 MB
        - "48 MiB"                → 48 MB
        - "512M"                  → 512 MB
        - "16384K"                → 16 MB
        - "32 MiB (2 instances)"  → 32 MB
        - "61440K (4 instances)"  → 60 MB
        - "L3 缓存：32 MiB"        → 32 MB
        - "256"                   → 256 MB
        """
        import re
        
        raw_output = raw_output.strip()
        if not raw_output:
            return 0
        
        # 匹配数字和单位（K/M/G），忽略后面任何字符
        match = re.search(r'(\d+)\s*([KMG]?)(?:i?B)?', raw_output, re.IGNORECASE)
        if not match:
            return 0
        
        num = int(match.group(1))
        unit = match.group(2).upper() if match.group(2) else ""
        
        if unit == "K":
            return num // 1024
        elif unit == "G":
            return num * 1024
        else:
            # M 或没有单位
            return num


    def get_l3_cache_mb(self):
        """
        获取 L3 缓存总大小（所有 CPU 核心总和）
        使用 lscpu 直接读取汇总值
        """
        try:
            # 尝试英文环境
            l3_raw = self.ssh_run("lscpu | grep 'L3 cache' | awk '{print $3, $4}'", silent=True, timeout=10).strip()
            
            if not l3_raw:
                # 备用：中文环境
                l3_raw = self.ssh_run("lscpu | grep 'L3 缓存' | awk '{print $3, $4}'", silent=True, timeout=10).strip()
            
            if not l3_raw:
                self.log("WARN", "lscpu 未查询到 L3 cache 信息")
                return 0
            
            # 调用统一的解析函数
            return self._parse_l3_cache_output(l3_raw)
            
        except Exception as e:
            self.log("WARN", f"从 lscpu 获取 L3 缓存失败: {str(e)}")
            return 0
        
    def get_cpu_sockets(self):
        """
        获取 CPU 物理路数。
        优先 lscpu 解析（中英文），失败则用默认值 2。
        """
        import re
        sockets = 2   # 默认值

        try:
            lscpu_out = self.ssh_run("lscpu 2>/dev/null", silent=True, timeout=10)
            if lscpu_out:
                # 英文："Socket(s):  2"
                m = re.search(r'Socket\(s\)\s*[:：]\s*(\d+)', lscpu_out)
                if not m:
                    # 中文："座：  2"
                    m = re.search(r'座\s*[:：]\s*(\d+)', lscpu_out)
                if m:
                    n = int(m.group(1))
                    if n > 0:
                        sockets = n
                        self.log("INFO", f"CPU 路数: {sockets} (来自 lscpu)")
                        return sockets
        except Exception as e:
            self.log("WARN", f"lscpu 解析失败: {e}")

        self.log("INFO", f"CPU 路数: {sockets} (默认值)")
        return sockets

    def backup_sel_elist_before_test(self, local_log_dir):
        self.sel_log_backup_before_test = os.path.join(local_log_dir, "sel_log_backup_before_test.log")
        #self.sel_backup_file = os.path.join(local_log_dir, "sel_inrement.log")
        #self.sel_last_id_file = os.path.join(local_log_dir, ".last_sel_id")
        self.log("INFO", f"备份测试前的SEL ELIST 数据 到本地文件{self.sel_log_backup_before_test}....")
        sel_elist_output = self._run_ipmitool("sel elist")
        with open(self.sel_log_backup_before_test, 'w', encoding='utf-8') as f:
            f.write(sel_elist_output)
        
        #self.log("INFO", "正在清空 BMC SEL 日志...")
        #self._run_ipmitool("sel clear")
        #with open(self.sel_backup_file, 'w', encoding='utf-8') as f:
        #    f.write(f"# SEL 增量备份日志\n# BMC IP: {self.bmc_ip}\n# 开始时间: {datetime.now()}\n\n")
        #self._save_sel_last_id("0x00000000")
        #self.log("INFO", f"SEL 追踪已初始化，备份文件: {self.sel_backup_file}")


    def _get_sel_last_id(self):
        if os.path.exists(self.sel_last_id_file):
            with open(self.sel_last_id_file, 'r') as f:
                return f.read().strip()
        return "0x00000000"

    def _save_sel_last_id(self, last_id):
        with open(self.sel_last_id_file, 'w') as f:
            f.write(last_id)

    def fetch_and_backup_sel(self, round_idx=None, reboot_idx=None, round_dir=None):
        has_error = False
        sel_output = self._run_ipmitool("sel elist")
        if not sel_output:
            return False, 0
        if round_idx == 0 and "Reboot" in self.test_name:
            full_snapshot_file = os.path.join(round_dir, f"sel_elist.txt")
        else:
            full_snapshot_file = os.path.join(round_dir, f"sel_elist_round{round_idx+1}_reboot{reboot_idx+1}.txt")
        with open(full_snapshot_file, 'w', encoding='utf-8') as f:
            f.write(sel_output)
            self.log("INFO", f"SEL 全量快照保存到{full_snapshot_file}")
        last_id_hex = self._get_sel_last_id()
        last_id_int = int(last_id_hex, 16)
        new_events = []
        max_id_int = last_id_int
        for line in sel_output.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if not parts:
                continue
            current_id_hex = parts[0]
            try:
                current_id_int = int(current_id_hex, 16)
                if current_id_int > max_id_int:
                    new_events.append(line)
                    if current_id_int > max_id_int:
                        max_id_int = current_id_int
            except ValueError:
                continue
        if new_events:
            with open(self.sel_backup_file, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"\n# === {timestamp} 新增 {len(new_events)} 条 ===\n")
                f.write("\n".join(new_events) + "\n")
            new_max_id_hex = hex(max_id_int)
            self._save_sel_last_id(new_max_id_hex)
            self.log("INFO", f"SEL 新增 {len(new_events)} 条，最新 ID: {new_max_id_hex}")
            has_error, critical_errors = self._check_sel_events_for_errors(new_events)
            if has_error and round_idx is not None and reboot_idx is not None and round_dir:
                self._record_reboot_error(round_dir, round_idx, reboot_idx, "SEL", critical_errors)
            return has_error, len(new_events)
        else:
            self.log("INFO", "SEL 无新增日志")
            return False, 0

    def _check_sel_events_for_errors(self, events):
        critical = []
        for line in events:
            lower = line.lower()
            if "presence detected" in lower:
                continue
            for p in MEMORY_ERROR_PATTERNS:
                if re.search(p, lower):
                    critical.append(line)
                    break
        if critical:
            self.log("ERROR", f"SEL 发现 {len(critical)} 条严重错误")
            for err in critical[:5]:
                self.log("ERROR", f"  [严重] {err}")
            return True, critical
        return False, []

    def check_dmesg_and_memory(self, round_dir, round_idx, reboot_idx, prefix=None, label=None):
        """
        获取 dmesg、保存文件、扫描错误，并做物理内存条一致性检查。
        label: 自定义 DIMM 检查标题（可选）
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        dmesg_dir = os.path.join(round_dir, "dmesg")
        os.makedirs(dmesg_dir, exist_ok=True)

        if prefix is not None:
            dmesg_file = os.path.join(dmesg_dir, f"dmesg_{prefix}_{timestamp}.txt")
        else:
            dmesg_file = os.path.join(dmesg_dir, f"dmesg_reboot{reboot_idx+1}_{timestamp}.txt")

        try:
            dmesg_out = self.ssh_run("dmesg -T 2>/dev/null || dmesg", silent=True)
            with open(dmesg_file, 'w', encoding='utf-8') as f:
                f.write(dmesg_out)
            self.log("INFO", f"dmesg 保存到 {dmesg_file}")
        except Exception as e:
            self.log("ERROR", f"获取 dmesg 失败: {e}")
            return True, 0

        has_error = self.scan_log_file_for_errors(dmesg_file, "dmesg", round_dir,
                                                  round_idx, reboot_idx)

        # 物理内存条检查
        dimm_error, current_total_gb, _ = self.check_dimms_consistency(
            round_dir, round_idx, reboot_idx, label=label
        )
        if dimm_error:
            has_error = True

        return has_error, current_total_gb
    
    def scan_log_file_for_errors(self, log_path, log_type, round_dir, round_idx, reboot_idx):
        """循环测试使用：扫描日志文件并记录错误到 reboot_errors.txt"""
        if not os.path.exists(log_path):
            return False
        pattern = re.compile('|'.join(MEMORY_ERROR_PATTERNS), re.IGNORECASE)
        errors = []
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if pattern.search(line):
                        errors.append(line.strip())
                        self.log("ERROR", f"[DEBUG] Line matched by pattern:{pattern}\n Line: {line}")
        except Exception as e:
            self.log("WARN", f"扫描 {log_type} 文件失败: {e}")
            return False
        if errors:
            self.log("ERROR", f"{log_type} 中发现 {len(errors)} 条内存错误")
            self._record_reboot_error(round_dir, round_idx, reboot_idx, log_type, errors)
            return True
        return False

    def scan_single_log_for_errors(self, log_path, log_type):
        """非循环测试使用：扫描日志文件，仅返回是否有错误（不记录到 reboot_errors.txt）"""
        if not os.path.exists(log_path):
            return False
        pattern = re.compile('|'.join(MEMORY_ERROR_PATTERNS), re.IGNORECASE)
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if pattern.search(line):
                        self.log("ERROR", f"{log_type} 中发现内存错误: {line.strip()[:200]}")
                        return True
        except Exception as e:
            self.log("WARN", f"扫描 {log_type} 文件失败: {e}")
        return False

    def _record_reboot_error(self, round_dir, round_idx, reboot_idx, error_type, error_info):
        error_log = os.path.join(round_dir, "reboot_errors.txt")
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(error_log, 'a', encoding='utf-8') as f:
            f.write(f"========== {timestamp} ==========\n")
            if not (round_idx == 0 and reboot_idx == 0):
                f.write(f"轮次: {round_idx+1}, 重启次数: {reboot_idx+1}\n")
            f.write(f"错误类型: {error_type}\n")
            if isinstance(error_info, list):
                f.write("错误详情:\n")
                for err in error_info[:10]:
                    f.write(f"  {err[:300]}\n")
            else:
                f.write(f"错误详情: {error_info}\n")
            f.write("\n")
        self.log("ERROR", f"错误已记录到 {error_log}")

    def check_sel_full(self, round_dir, round_idx, cycle_idx):
        sel_output = self._run_ipmitool("sel elist")
        if not sel_output:
            self.log("WARN", "SEL ELIST收集失败，请检查网络")
            return False
        if round_idx == 0  and cycle_idx == 0:
            sel_file = os.path.join(round_dir, "sel_after_test.log")
        else:
            sel_file = os.path.join(round_dir, f"sel_cycle{cycle_idx}.txt")
        with open(sel_file, 'w', encoding='utf-8') as f:
            f.write(sel_output)
        return self.scan_log_file_for_errors(sel_file, "SEL", round_dir, round_idx, cycle_idx)

    def collect_system_logs(self, local_log_dir):
        try:
            messages_file = os.path.join(local_log_dir, "messages.txt")
            out = self.ssh_run("cat /var/log/messages 2>/dev/null", silent=True, timeout=30)
            with open(messages_file, 'w', encoding='utf-8') as f:
                f.write(out)
            self.scan_log_file_for_errors(messages_file, "messages", local_log_dir, 0, 0)
            syslog_file = os.path.join(local_log_dir, "syslog.txt")
            out = self.ssh_run("cat /var/log/syslog 2>/dev/null", silent=True, timeout=30)
            with open(syslog_file, 'w', encoding='utf-8') as f:
                f.write(out)
            self.scan_log_file_for_errors(syslog_file, "syslog", local_log_dir, 0, 0)
        except Exception as e:
            self.log("WARN", f"收集系统日志失败: {e}")

    def collect_dmesg_single(self, local_log_dir):
        try:
            dmesg_file = os.path.join(local_log_dir, "dmesg.txt")
            dmesg_out = self.ssh_run("dmesg -T 2>/dev/null || dmesg", silent=True, timeout=30)
            with open(dmesg_file, 'w', encoding='utf-8') as f:
                f.write(dmesg_out)
            self.log("INFO", f"dmesg 保存到 {dmesg_file}")
            return self.scan_single_log_for_errors(dmesg_file, "dmesg")
        except Exception as e:
            self.log("WARN", f"收集 dmesg 失败: {e}")
            return False

    def reboot_and_check_dmesg(self, round_dir, round_idx, reboot_idx, skip_reboot=False, type=None, label=None):
        if not skip_reboot:
            self.log("ALERT", f"第{round_idx+1}轮 第{reboot_idx+1}次重启：发送reboot命令")
            try:
                self.ssh_run("reboot", silent=True)
            except:
                pass
            self.wait_bmc_ready()
            self.wait_system_ssh_ready(silent_wait=180)
        else:
            self.log("ALERT", "AC上电后检查系统状态")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if type is not None:
            dmesg_file = os.path.join(round_dir, f"{type}_dmesg_{timestamp}.txt")
        else:
            dmesg_file = os.path.join(round_dir, f"dmesg_round{round_idx+1}_reboot{reboot_idx+1}_{timestamp}.txt")
        try:
            dmesg_output = self.ssh_run("dmesg -T 2>/dev/null || dmesg", silent=True)
            with open(dmesg_file, "w", encoding="utf-8") as f:
                f.write(dmesg_output)
            self.log("INFO", f"dmesg已保存到 {dmesg_file}")
        except Exception as e:
            self.log("ERROR", f"获取dmesg失败: {e}")
            return True, 0

        if scan_single_log_for_ecc(dmesg_file, self.log):
            self.log("ERROR", "dmesg 中发现内存错误")
            return True, 0

        # 物理内存条检查
        dimm_error, current_total_gb, _ = self.check_dimms_consistency(
            round_dir, round_idx, reboot_idx, label=label
        )
        if dimm_error:
            return True, current_total_gb

        return False, current_total_gb

    def download_remote_dir_exclude(self, remote_dir, local_dir, exclude_suffixes=None):
        if exclude_suffixes is None:
            exclude_suffixes = []
        os.makedirs(local_dir, exist_ok=True)
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(hostname=self.os_ip, username=self.os_user, key_filename=self.ssh_key, timeout=30)
            sftp = ssh.open_sftp()
            def download_dir(remote_path, local_path):
                try:
                    for item in sftp.listdir(remote_path):
                        remote_item = f"{remote_path}/{item}"
                        local_item = os.path.join(local_path, item)
                        try:
                            attr = sftp.stat(remote_item)
                            if attr.st_mode & 0o40000:
                                os.makedirs(local_item, exist_ok=True)
                                download_dir(remote_item, local_item)
                            else:
                                if any(item.endswith(suffix) for suffix in exclude_suffixes):
                                    self.log("DEBUG", f"排除文件: {remote_item}")
                                    continue
                                sftp.get(remote_item, local_item)
                                self.log("INFO", f"下载文件: {remote_item} -> {local_item}")
                        except Exception as e:
                            self.log("WARN", f"处理 {remote_item} 失败: {e}")
                except Exception as e:
                    self.log("WARN", f"遍历远程目录 {remote_path} 失败: {e}")
            download_dir(remote_dir, local_dir)
            sftp.close()
            ssh.close()
            self.log("INFO", f"目录下载完成，保存到 {local_dir}")
        except Exception as e:
            self.log("ERROR", f"下载远程目录失败: {e}")

    def check_remote_file_exists(self, remote_path):
        try:
            result = self.ssh_run(f"test -f {remote_path} && echo 'exists'", silent=True).strip()
            return result == 'exists'
        except Exception as e:
            self.log("WARN", f"检查远程文件 {remote_path} 失败: {e}")
            return False

    def download_remote_dir_exclude_sh(self, remote_dir, local_dir):
        self.download_remote_dir_exclude(remote_dir, local_dir, exclude_suffixes=['.sh'])

    def download_remote_log(self, remote_path, local_path, clean=True):
        try:
            content = self.ssh_run(f"cat {remote_path}", silent=True, timeout=30)
            if clean:
                content = clean_remote_log(content)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.log("INFO", f"远程日志已下载到 {local_path}")
            return True
        except Exception as e:
            self.log("WARN", f"下载远程日志失败: {e}")
            return False

    def get_test_info_string(self):
        start_time = getattr(self, "start_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if self.test_name in ("MLC测试", "MLC测试（INTEL专用）"):
            count = self.params.get("测试次数", 5)
            return f"Start:{start_time}, Total:{count}次"
        elif self.test_name == "Stream测试":
            single = self.params.get("单线程测试次数", 3)
            multi = self.params.get("多线程测试次数", 3)
            return f"Start:{start_time}, 单线程:{single}次 | 多线程:{multi}次"
        elif self.test_name == "Stressapptest测试":
            mem = self.params.get("测试容量(MB)", 1024)
            sec = self.params.get("测试时间(秒)", 3600)
            return f"Start:{start_time}, 测试容量:{mem}MB | 测试:{sec}秒"
        elif self.test_name == "Memtester测试":
            mem = self.params.get("测试容量(MB)", 1024)
            loops = self.params.get("测试圈数", 1)
            return f"Start:{start_time}, 测试容量:{mem}MB | {loops}圈"
        elif self.test_name == "RMT测试":
            cycles = self.params.get("每组重启次数", 5)
            offset = self.params.get("内存配置测试轮数", 3)
            return f"Start:{start_time}, 重启:{cycles}次/轮 | {offset}轮"
        elif self.test_name == "Reboot测试":
            cycles = self.params.get("测试次数", 5)
            return f"Start:{start_time}, Total:{cycles}次"
        elif self.test_name == "Sleep测试":
            cycles = self.params.get("测试次数", 1)
            seconds = self.params.get("睡眠秒数", 30)
            return f"Start:{start_time}, Total:{cycles}次 | 休眠{seconds}秒"
        elif self.test_name == "AC电源测试":
            cycles = self.params.get("测试次数", 5)
            return f"Start:{start_time}, Total:{cycles}次"
         # ========== 新增三个测试分支 ==========
        elif self.test_name == "Lmbench测试":
            size = self.params.get("测试内存(MB)", 0)
            stride = self.params.get("测试步长(Bytes)", 64)
            return f"Start:{start_time}, size={size}MB, stride={stride}"
        elif self.test_name == "Memory Burn In测试":
            duration = self.params.get("测试时间(小时)", 0)
            if duration > 0:
                return f"Start:{start_time}, 运行{duration}小时"
            else:
                return f"Start:{start_time}, 默认运行24小时"
        elif self.test_name == "Reboot+SAT测试":
            cycles = self.params.get("测试次数", 5)
            return f"Start:{start_time}, Total:{cycles}次"
        elif self.test_name == "AC电源循环测试":
            cycles = self.params.get("测试次数", 5)
            return f"Start:{start_time}, Total:{cycles}次"
        elif self.test_name == "DCreboot测试":
            cycles = self.params.get("总循环次数", 10)
            os_wait = self.params.get("OS运行等待时间", 60)
            dc_sleep = self.params.get("DC断电休眠时间(秒)", 120)
            return f"Start:{start_time}, Total:{cycles}次 | OS等待{os_wait}s | DC休眠{dc_sleep}s"
        # ==========================================
        else:
            return f"Start:{start_time}, {self.test_name}"
      

class CycleLoggerMixin:
    def init_cycle_log(self, log_name="cycle.log"):
        if not hasattr(self, 'local_log_dir') or not self.local_log_dir:
            return
        log_path = os.path.join(self.local_log_dir, log_name)
        self.cycle_log_file = open(log_path, "a", encoding="utf-8")
        self.log_cycle(f"=== {self.test_name} 测试开始 ===")

        # ========== 新增：打印测试参数 ==========
        if hasattr(self, 'params') and self.params:
            params_str = ", ".join([f"{k}={v}" for k, v in self.params.items()])
            self.log_cycle(f"测试参数: {params_str}")
        else:
            self.log_cycle("测试参数: (无)")
        self.log_cycle("=" * 40)

    def log_cycle(self, msg):
        if hasattr(self, 'cycle_log_file') and self.cycle_log_file:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.cycle_log_file.write(f"{timestamp} - {msg}\n")
            self.cycle_log_file.flush()

    def close_cycle_log(self, test_status=None):
        if hasattr(self, 'cycle_log_file') and self.cycle_log_file:
            self.log_cycle(f"=== {self.test_name} 测试结束, 测试状态：{test_status}===")
            self.cycle_log_file.close()



class PrepareMixin:
    """测试环境准备混入类"""

    def _prepare_environment(self):
        """子类重写此方法实现各自的准备逻辑"""
        raise NotImplementedError("子类必须实现 _prepare_environment 方法")
    
    def _get_remote_arch(self):
        """获取远程服务器架构"""
        try:
            arch = self.ssh_run("uname -m", silent=True).strip()
            return arch
        except:
            return "unknown"
    
    def _upload_and_extract_zip(self, zip_name):
        local_zip = os.path.join(SCRIPT_DIR, "product_test_dir", zip_name)
        if not os.path.exists(local_zip):
            self.log("ERROR", f"本地压缩包不存在: {local_zip}")
            return False

        remote_base = self.remote_dir
        zip_basename = os.path.splitext(zip_name)[0]
        target_dir = os.path.join(remote_base, zip_basename).replace("\\", "/")

        try:
            self.ssh_run(f"mkdir -p {remote_base}")
            remote_zip = f"/tmp/{zip_name}"
            self._scp_upload(local_zip, remote_zip)

            self.ssh_run(f"rm -rf {target_dir}")
            # ========== 修复：unzip 后 chown 成 root ==========
            self.ssh_run(
                f"cd {remote_base} && unzip -o {remote_zip} && "
                f"chown -R root:root {remote_base}/{zip_basename} 2>/dev/null || true"
            )

            self.ssh_run(f"rm -f {remote_zip}")
            self.log("INFO", f"解压 {zip_name} 到 {target_dir} 完成")
            return target_dir
        except Exception as e:
            self.log("ERROR", f"上传解压 {zip_name} 失败: {e}")
            return None

    def _upload_and_extract_tar(self, tar_name):
        """上传 tar.gz 包并解压到 product_test_dir 下"""
        local_tar = os.path.join(SCRIPT_DIR, "product_test_dir", tar_name)
        if not os.path.exists(local_tar):
            self.log("ERROR", f"本地压缩包不存在: {local_tar}")
            return None

        remote_base = self.remote_dir
        tar_basename = os.path.splitext(os.path.splitext(tar_name)[0])[0]
        target_dir = os.path.join(remote_base, tar_basename).replace("\\", "/")

        try:
            self.ssh_run(f"mkdir -p {remote_base}")
            remote_tar = f"/tmp/{tar_name}"
            self._scp_upload(local_tar, remote_tar)

            self.ssh_run(f"rm -rf {target_dir}")
            # ========== 修复：加 --no-same-owner，强制 owner 为当前用户（root） ==========
            self.ssh_run(f"cd {remote_base} && tar -xzf {remote_tar} --no-same-owner")

            self.ssh_run(f"rm -f {remote_tar}")
            self.log("INFO", f"解压 {tar_name} 到 {target_dir} 完成")
            return target_dir
        except Exception as e:
            self.log("ERROR", f"上传解压 {tar_name} 失败: {e}")
            return None
    
    def _scp_upload(self, local_path, remote_path):
        """SCP 上传文件"""
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(hostname=self.os_ip, username=self.os_user, key_filename=SSH_KEY, timeout=30)
        sftp = ssh.open_sftp()
        sftp.put(local_path, remote_path)
        sftp.close()
        ssh.close()
    
    def _check_remote_file(self, remote_path):
        """检查远程文件是否存在"""
        try:
            result = self.ssh_run(f"test -f {remote_path} && echo 'exists' || echo 'not_exists'", silent=True).strip()
            return result == "exists"
        except:
            return False
    
    def _check_remote_dir(self, remote_path):
        """检查远程目录是否存在"""
        try:
            result = self.ssh_run(f"test -d {remote_path} && echo 'exists' || echo 'not_exists'", silent=True).strip()
            return result == "exists"
        except:
            return False

