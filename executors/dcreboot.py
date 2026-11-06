import os
import time
import threading
from datetime import datetime
from .base import SOLMixin, RebootCheckMixin, CycleLoggerMixin, PrepareMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE, MEMORY_ERROR_PATTERNS
from utils.ssh_utils import ssh_run
from utils.helpers import clear_remote_logs
from utils.log_scanner import LogScanner


class DCRebootTestExecutor(SOLMixin, RebootCheckMixin, CycleLoggerMixin, PrepareMixin):
    def __init__(self, server_info, test_name, params, log_callback, status_callback,
                 remote_dir, remote_subdir, check_interval=10, test_info_callback=None):
        self.server_info = server_info
        self.test_name = test_name
        self.params = params
        self.log = log_callback
        self.status_callback = status_callback
        self.test_info_callback = test_info_callback
        self.remote_dir = remote_dir
        self.remote_subdir = remote_subdir
        self.check_interval = check_interval

        # 运行状态标记
        self.running = False
        self.user_stopped = False
        self.stop_event = threading.Event()
        self.thread = None
        self.poll_thread = None
        self._cleanup_started = False

        # 状态回调保护 & 失败标记
        self._status_reported = False
        self._test_failed = False

        self.local_log_dir = None
        self.start_time = ""
        self.last_processed_cycle = 0

        # BMC/SSH信息
        self.bmc_ip = server_info.get("bmc_ip")
        self.bmc_user = server_info.get("bmc_user")
        self.bmc_pwd = server_info.get("bmc_pwd")
        self.os_ip = server_info.get("os_ip")
        self.os_user = server_info.get("os_user")
        self.ssh_key = SSH_KEY

        # 远程DC工作目录
        self.remote_dc_dir = f"{remote_dir}/{remote_subdir}"

        # 远端脚本、状态文件路径
        self.REMOTE_SCRIPT = f"{self.remote_dc_dir}/DCreboot_test.sh"
        self.REMOTE_STATE_FILE = f"{self.remote_dc_dir}/dc_reboot_state"
        self.REMOTE_LOG_FILE = f"{self.remote_dc_dir}/dc_reboot_test.log"
        self.REMOTE_ERR_FILE = f"{self.remote_dc_dir}/dc_reboot.error"
        self.REMOTE_DIMM_FILE = f"{self.remote_dc_dir}/DIMM_CNT.txt"
        self.REMOTE_SERVICE_FILE = "/etc/systemd/system/dc-reboot-test.service"

        # 测试参数
        self.total_cycles = params.get("总循环次数", 10)
        self.os_wait_sec = params.get("OS运行等待时间(秒)", 60)
        self.dc_sleep_sec = params.get("DC断电休眠时间(秒)", 120)

        # SOL对象
        self.sol_proc = None
        self.sol_bat = None
        self.sol_log_path = None
        self.sol_scanner = None

    def ssh_run(self, cmd, silent=False, timeout=30):
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=self.ssh_key, silent=silent, timeout=timeout)

    # ===================== start / stop =====================
    def start(self):
        self.running = True
        self.status_callback("running")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        """用户停止测试 - 等待网络恢复并清理自启动服务"""
        self.user_stopped = True
        self.stop_event.set()
        self.running = False
        self.status_callback("stopping")

        if self.sol_proc or self.sol_bat:
            self.stop_sol(self.sol_proc, self.sol_bat)
            self.sol_proc = None
            self.sol_bat = None

        # 后台线程清理，不等待
        if not self._cleanup_started:
            self._cleanup_started = True
            threading.Thread(target=self._remote_cleanup, args=(True, self._on_cleanup_done), daemon=True).start()

    def _on_cleanup_done(self):
        """清理完成后的回调"""
        self.log("INFO", "远端清理已完成")

    # ===================== 远端清理 =====================
    def _remote_cleanup(self, wait_for_network=True, on_done=None):
        """远端环境清理（与 AC 测试逻辑保持一致）"""
        if wait_for_network:
            self.log("INFO", "开始执行远端环境清理，等待网络恢复...")
        else:
            self.log("INFO", "立即执行远端环境清理...")

        clean_cmd = (
            f"rm -f {self.REMOTE_STATE_FILE} {self.REMOTE_ERR_FILE} {self.REMOTE_DIMM_FILE} {self.remote_dc_dir}/dc_start.log; "
            f"systemctl stop dc-reboot-test.service 2>/dev/null; "
            f"systemctl disable dc-reboot-test.service 2>/dev/null; "
            f"rm -f {self.REMOTE_SERVICE_FILE}; "
            f"systemctl daemon-reload 2>/dev/null; "
            f"sed -i '/DCreboot_test.sh/d' /etc/rc.local 2>/dev/null; "
            f"sed -i '/DCreboot_test.sh/d' /etc/init.d/boot.local 2>/dev/null; "
            f"find {self.remote_dc_dir} -name '*:Zone.Identifier' -type f -delete"
        )

        if wait_for_network:
            # 等待网络恢复，最多 10 分钟
            max_attempts = 20
            for attempt in range(max_attempts):
                try:
                    self.ssh_run("echo ok", silent=True, timeout=5)
                    self.ssh_run(clean_cmd, silent=True, timeout=15)
                    self.log("INFO", "远端清理执行完成")
                    if on_done:
                        on_done()
                    return
                except Exception:
                    remaining = (max_attempts - attempt - 1) * 30
                    self.log("INFO", f"等待网络恢复，第 {attempt+1}/{max_attempts} 次尝试，30 秒后重试... (剩余约 {remaining//60} 分钟)")
                    time.sleep(30)
            self.log("ERROR", "网络超时未恢复（10分钟），未能自动清理远程DC服务！")
            self.log("ERROR", "服务器上电后 root 用户执行整条清理命令：")
            self.log("ERROR", clean_cmd)
            if on_done:
                on_done()
        else:
            try:
                self.ssh_run("echo ok", silent=True, timeout=5)
                self.ssh_run(clean_cmd, silent=True, timeout=15)
                self.log("INFO", "远端清理执行完成")
                if on_done:
                    on_done()
            except Exception as e:
                self.log("WARN", f"立即清理失败（服务器可能离线）：{e}")
                if on_done:
                    on_done()

    # ===================== 环境准备 =====================
    def _prepare_environment(self):
        self.log("INFO", "准备 DC 测试环境...")

                # ========== 清理上次残留状态文件（防止 STATE_FILE 残留导致参数失效） ==========
        self.log("INFO", "清理上次 DC 测试残留状态...")
        cleanup_cmds = [
            f"rm -f {self.REMOTE_STATE_FILE} {self.REMOTE_ERR_FILE} {self.REMOTE_DIMM_FILE} {self.remote_dc_dir}/dc_start.log",
            "systemctl stop dc-reboot-test.service 2>/dev/null; "
            "systemctl disable dc-reboot-test.service 2>/dev/null; "
            f"rm -f {self.REMOTE_SERVICE_FILE}; "
            "systemctl daemon-reload 2>/dev/null",
            "sed -i '/DCreboot_test.sh/d' /etc/rc.local 2>/dev/null",
            "sed -i '/DCreboot_test.sh/d' /etc/init.d/boot.local 2>/dev/null",
        ]
        for cmd in cleanup_cmds:
            try:
                self.ssh_run(cmd, silent=True, timeout=5)
            except:
                pass
        # ==========================================================================
        exe_path = f"{self.remote_dc_dir}/DCreboot_test.sh"

        if self._check_remote_dir(self.remote_dc_dir):
            if self._check_remote_file(self.REMOTE_SCRIPT):
                self.log("INFO", "DC 脚本已存在，跳过准备")
                self.ssh_run(f"chmod +x {exe_path}", silent=True)
                return True
            else:
                self.log("INFO", "DC 目录存在但缺少脚本，重新上传...")

        target_dir = self._upload_and_extract_tar("DC.tar.gz")
        if target_dir is None:
            self.log("ERROR", "DC.tar.gz 上传/解压失败")
            return False

        if not self._check_remote_file(self.REMOTE_SCRIPT):
            self.log("ERROR", f"解压后未找到脚本: {self.REMOTE_SCRIPT}")
            try:
                ls_result = self.ssh_run(f"ls -la {self.remote_dc_dir}/", silent=True, timeout=10)
                self.log("ERROR", f"目录内容:\n{ls_result}")
            except Exception:
                pass
            return False

        self.ssh_run(f"chmod +x {self.remote_dc_dir}/*.sh", silent=True)
        self.log("INFO", "DC 测试环境准备完成")
        return True

    def _update_overview_info(self, current_cycle, is_final=False):
        """
        更新总览界面
        current_cycle: 已完成的轮次（0 表示还没完成任何轮）
        is_final: 是否全部完成
        显示规则：
        - 未完成：显示"当前正在进行的轮次"= current_cycle + 1（不超过 total）
        - 已完成：显示 total/total 已完成
        """
        if self.test_info_callback is None:
            return
        try:
            if is_final:
                info_text = f"Start:{self.start_time} 当前:{self.total_cycles}/{self.total_cycles} 已完成"
            else:
                display_cycle = min(current_cycle + 1, self.total_cycles)
                info_text = f"Start:{self.start_time} 当前:{display_cycle}/{self.total_cycles}"
            self.test_info_callback(self.bmc_ip, info_text)
        except Exception as e:
            self.log("WARN", f"更新总览界面回调异常：{str(e)}")

    # ===================== 读取远程状态 =====================
    def _read_remote_state(self):
        """读取远程状态文件，返回 (current_cycle, total_cycles)"""
        try:
            state_raw = self.ssh_run(f"cat {self.REMOTE_STATE_FILE} 2>/dev/null", silent=True, timeout=10).strip()
            if not state_raw:
                return 0, 0
            current = 0
            total = 0
            for line in state_raw.splitlines():
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    if k == "CURRENT_CYCLE":
                        current = int(v) if v.isdigit() else 0
                    elif k == "TOTAL_CYCLES":
                        total = int(v) if v.isdigit() else 0
            return current, total
        except Exception:
            return 0, 0

    def _check_script_entered_cycle(self, cycle_num):
        """
        检查脚本日志中是否已出现以下任一标志：
        1. "Test Cycle (cycle_num)/"  —— 脚本已进入第 cycle_num 轮
        2. "ALL N TEST CYCLES COMPLETED" —— 脚本已完成（用于最后一轮）

        用于确认服务器已下电+上电，进入下一轮，或已完成收尾。
        """
        try:
            # 检查1：是否已进入第 cycle_num 轮
            cmd1 = f"grep -c 'Test Cycle {cycle_num}/' {self.REMOTE_LOG_FILE} 2>/dev/null"
            grep_cycle = self.ssh_run(cmd1, silent=True, timeout=10).strip()
            if grep_cycle.isdigit() and int(grep_cycle) > 0:
                return True

            # 检查2：脚本是否已完成（最后一轮）
            cmd2 = f"grep -c 'ALL.*TEST CYCLES COMPLETED' {self.REMOTE_LOG_FILE} 2>/dev/null"
            grep_completed = self.ssh_run(cmd2, silent=True, timeout=10).strip()
            if grep_completed.isdigit() and int(grep_completed) > 0:
                return True

            return False
        except Exception as e:
            # SSH 超时/不可达（服务器下电/上电中）→ 返回 False，不打日志
            return False

    def _check_script_all_completed(self):
        """
        检查脚本日志是否已出现 "ALL N TEST CYCLES COMPLETED"。
        用于"状态文件被删"时判断是否正常完成。
        """
        try:
            cmd = f"grep -c 'ALL.*TEST CYCLES COMPLETED' {self.REMOTE_LOG_FILE} 2>/dev/null"
            result = self.ssh_run(cmd, silent=True, timeout=10).strip()
            return result.isdigit() and int(result) > 0
        except Exception as e:
            # SSH 超时/不可达（服务器下电/上电中）→ 返回 False，不打日志
            return False

    # ===================== 检查错误文件 =====================
    def _check_error_file(self):
        try:
            err_exist = self.ssh_run(f"test -f {self.REMOTE_ERR_FILE} && echo 1 || echo 0",
                                     silent=True, timeout=10).strip()
            if err_exist == "1":
                err_content = self.ssh_run(f"cat {self.REMOTE_ERR_FILE}", silent=True, timeout=10).strip()
                return err_content
            return None
        except Exception:
            return None

    # ===================== 下载远程 DC 日志 =====================
    def _download_remote_dc_logs(self):
        """下载远程 DC 目录下的测试日志"""
        ssh_ok = False
        for attempt in range(3):
            try:
                self.ssh_run("echo ok", silent=True, timeout=5)
                ssh_ok = True
                break
            except:
                time.sleep(2)
        if not ssh_ok:
            self.log("WARN", "SSH 不可通，跳过远程DC测试日志收集")
            return False
        self.log("INFO", "开始下载远程 DC 测试日志...")
        local_dc_dir = os.path.join(self.local_log_dir, "dc_remote_logs")
        os.makedirs(local_dc_dir, exist_ok=True)

        files_to_download = [
            ("dc_reboot_test.log", "dc_reboot_test.log"),
        ]

        for remote_name, local_name in files_to_download:
            remote_path = f"{self.remote_dc_dir}/{remote_name}"
            local_path = os.path.join(local_dc_dir, local_name)
            try:
                content = self.ssh_run(f"cat {remote_path} 2>/dev/null", silent=True, timeout=15)
                if content and content.strip():
                    with open(local_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                    self.log("INFO", f"已下载: {local_name}")
                else:
                    self.log("INFO", f"远程文件为空或不存在: {remote_name}")
            except Exception as e:
                self.log("WARN", f"下载 {remote_name} 失败: {e}")

    # ===================== 核心轮询逻辑 =====================
    # ===================== 核心轮询逻辑 =====================
    def _poll_remote_status(self):
        """轮询远程测试进度，检测错误"""
        self.log("INFO", f"远端状态轮询线程启动，间隔：{self.check_interval}s")

        last_reported_cycle = 0
        last_heartbeat = time.time()
        heartbeat_interval = 180  # 心跳间隔：3 分钟
        wait_cycle_logged = None   # 记录"等待服务器重启完成"已对哪一轮打过日志

        while self.running and not self.stop_event.is_set():
            try:
                # 1. 读取远程状态
                current_cycle, total_cycles = self._read_remote_state()

                # ========== 兼容状态文件被脚本删除的情况 ==========
                if current_cycle == 0:
                    # 状态文件被删 → 可能测试完成或异常中断
                    script_completed = self._check_script_all_completed()

                    if script_completed:
                        # 脚本已完成（日志里有 ALL COMPLETED）
                        # 最后一轮检查还没做的话，现在做
                        if last_reported_cycle < self.total_cycles:
                            cycle_num = last_reported_cycle + 1
                            self.log("INFO", f"第 {cycle_num} 轮：服务器已重启完成，开始检查...")
                            try:
                                dmesg_error, current_mem = self.check_dmesg_and_memory(
                                    self.local_log_dir, 0, cycle_num - 1,
                                    label=f"第 {cycle_num} 次 rtcwake 唤醒内存检查"
                                )

                                sol_error = False
                                if self.sol_scanner:
                                    sol_error, _ = self.sol_scanner.scan_incremental(0, cycle_num, self.local_log_dir)

                                if dmesg_error or sol_error:
                                    error_msg = f"第{cycle_num}轮检测到异常"
                                    if dmesg_error:
                                        error_msg += " (dmesg/内存错误)"
                                    if sol_error:
                                        error_msg += " (SOL错误)"
                                    self.log("ERROR", error_msg)
                                    self.log_cycle(f"第 {cycle_num} 轮检测失败")
                                    self._test_failed = True
                                else:
                                    self.log("INFO", f"第 {cycle_num} 轮检查通过，当前内存: {current_mem} GB")
                                    self.log_cycle(f"第 {cycle_num} 轮上电后检测通过，当前内存: {current_mem} GB")
                                    last_reported_cycle = cycle_num
                            except Exception as e:
                                self.log("WARN", f"第 {cycle_num} 轮检查失败: {type(e).__name__}: {e}")
                                last_reported_cycle = cycle_num

                        # 确认全部完成
                        self.log("INFO", f"全部 {self.total_cycles} 轮DC测试完成")
                        self.log_cycle(f"全部 {self.total_cycles} 轮DC测试完成")
                        self._update_overview_info(self.total_cycles, is_final=True)
                        self._wait_for_script_complete()
                        self.running = False
                        self.stop_event.set()
                        break
                    elif last_reported_cycle >= self.total_cycles:
                        # 兜底
                        self.log("INFO", f"全部 {self.total_cycles} 轮DC测试完成")
                        self.log_cycle(f"全部 {self.total_cycles} 轮DC测试完成")
                        self._update_overview_info(self.total_cycles, is_final=True)
                        self._wait_for_script_complete()
                        self.running = False
                        self.stop_event.set()
                        break
                    else:
                        # 状态文件读不到（SSH 不通 / 服务器重启中），打心跳日志
                        if time.time() - last_heartbeat > heartbeat_interval:
                            self.log("INFO",
                                     f"[DC 等待中] 服务器重启中（SSH 暂不可达），"
                                     f"已完成 {last_reported_cycle}/{self.total_cycles} 轮，继续等待...")
                            last_heartbeat = time.time()
                        self.stop_event.wait(self.check_interval)
                        continue

                # ========== 处理 CURRENT_CYCLE 自增问题 ==========
                actual_completed = current_cycle - 1 if current_cycle > 0 else 0

                # 2. 每轮检查（包括最后一轮）
                if actual_completed > last_reported_cycle and actual_completed > 0:
                    cycle_num = actual_completed

                                        # 确认服务器已下电+上电，脚本进入第 cycle_num+1 轮（或已完成）
                    if not self._check_script_entered_cycle(cycle_num + 1):
                        # 只在第一次进入等待时打日志，避免刷屏
                        if wait_cycle_logged != cycle_num:
                            if cycle_num >= self.total_cycles:
                                # 最后一轮：脚本不会再进入下一轮，会直接打印 ALL COMPLETED
                                self.log("INFO", f"第 {cycle_num} 轮：等待脚本完成本轮收尾...")
                            else:
                                self.log("INFO", f"第 {cycle_num} 轮：等待服务器重启完成（脚本进入第 {cycle_num + 1} 轮）...")
                            wait_cycle_logged = cycle_num
                        self.stop_event.wait(self.check_interval)
                        continue
                    else:
                        # 脚本已进入下一轮，清除等待标记
                        wait_cycle_logged = None

                    self.log("INFO", f"第 {cycle_num} 轮：服务器已重启完成，开始检查...")

                    # 获取 dmesg 并检查错误
                    dmesg_error, current_mem = self.check_dmesg_and_memory(
                        self.local_log_dir, 0, cycle_num - 1,
                        label=f"第 {cycle_num} 次 rtcwake 唤醒内存检查"
                    )

                    # SOL 增量扫描
                    sol_error = False
                    if self.sol_scanner:
                        sol_error, _ = self.sol_scanner.scan_incremental(0, cycle_num, self.local_log_dir)

                    if dmesg_error or sol_error:
                        error_msg = f"第{cycle_num}轮检测到异常"
                        if dmesg_error:
                            error_msg += " (dmesg/内存错误)"
                        if sol_error:
                            error_msg += " (SOL错误)"
                        self.log("ERROR", error_msg)
                        self.log_cycle(f"第 {cycle_num} 轮检测失败")
                        self._handle_test_failure(error_msg)
                        break

                    # 打印"第 N 轮通过"
                    self.log("INFO", f"第 {cycle_num} 轮检查通过，当前内存: {current_mem} GB")
                    self.log_cycle(f"第 {cycle_num} 轮上电后检测通过，当前内存: {current_mem} GB")
                    self.last_processed_cycle = cycle_num
                    self._update_overview_info(cycle_num)
                    last_reported_cycle = actual_completed
                    last_heartbeat = time.time()
                    wait_cycle_logged = None

                    # 判"全部完成"
                    if actual_completed >= self.total_cycles:
                        self.log("INFO", f"全部 {self.total_cycles} 轮DC测试完成")
                        self.log_cycle(f"全部 {self.total_cycles} 轮DC测试完成")
                        self._update_overview_info(self.total_cycles, is_final=True)
                        self._wait_for_script_complete()
                        self.running = False
                        self.stop_event.set()
                        break

                # 3. 检查是否有远程错误文件
                err_content = self._check_error_file()
                if err_content:
                    self.log("ERROR", f"远端检测到硬件故障：{err_content}")
                    self._handle_test_failure(f"远端故障: {err_content}")
                    break

            except Exception as e:
                self.log("WARN", f"轮询远端状态异常：{type(e).__name__}: {e}")

            self.stop_event.wait(self.check_interval)

        self.log("INFO", "远端状态轮询线程退出")

    # ===================== 等待脚本完成收尾 =====================
    def _wait_for_script_complete(self, max_wait=600):
        """
        等待脚本完成收尾工作（检测脚本日志中是否出现 "COMPLETED"）
        max_wait: 最多等待秒数，默认 600 秒（覆盖下电+上电时间）
        """
        self.log("INFO", "等待脚本完成收尾工作...")
        wait_seconds = 0
        check_interval = 10

        while wait_seconds < max_wait:
            if not self.running or self.stop_event.is_set():
                self.log("INFO", "检测到停止信号，提前退出等待")
                return

            try:
                # 读取远程脚本日志最后几行，检查是否包含 "COMPLETED"
                log_tail = self.ssh_run(
                    f"tail -10 {self.REMOTE_LOG_FILE} 2>/dev/null",
                    silent=True, timeout=5
                )
                if log_tail and "COMPLETED" in log_tail:
                    self.log("INFO", "脚本已完成收尾工作")
                    return
            except Exception:
                # 网络异常也继续等
                pass

            time.sleep(check_interval)
            wait_seconds += check_interval

        self.log("WARN", f"等待脚本收尾超时（{max_wait} 秒），继续收尾流程")

    def _handle_test_failure(self, reason):
        """处理测试失败"""
        if self._status_reported:
            return
        self._test_failed = True
        self.log("ERROR", f"测试失败: {reason}")
        self.log_cycle(f"测试失败: {reason}")
        self.running = False
        self.stop_event.set()

    def _finalize_test(self, status):
        """完成测试"""
        if self._status_reported:
            return
        self._status_reported = True
        self.running = False
        self.stop_event.set()
        self.status_callback(status)
        if self.sol_proc or self.sol_bat:
            self.stop_sol(self.sol_proc, self.sol_bat)
            self.sol_proc = None
            self.sol_bat = None

    # ===================== 错误记录回调 =====================
    def _record_reboot_error(self, round_dir, round_idx, reboot_idx, error_type, error_info):
        cycle_num = reboot_idx if reboot_idx > 0 else round_idx + 1
        self.log("ERROR", f"第 {cycle_num} 轮 {error_type} 检测到硬件异常")
        error_file = os.path.join(self.local_log_dir, "dc_errors.txt")
        try:
            with open(error_file, "a", encoding="utf-8") as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{timestamp}] Cycle {cycle_num} - {error_type}: {error_info}\n")
        except:
            pass

    # ===================== 兼容方法 =====================
    def _check_remote_dir(self, remote_path):
        try:
            result = self.ssh_run(f"test -d {remote_path} && echo 'exists' || echo 'not_exists'",
                                  silent=True, timeout=5).strip()
            return result == "exists"
        except:
            return False

    def _check_remote_file(self, remote_path):
        try:
            result = self.ssh_run(f"test -f {remote_path} && echo 'exists' || echo 'not_exists'",
                                  silent=True, timeout=5).strip()
            return result == "exists"
        except:
            return False

    # ===================== 主线程 =====================
    def _run(self):
        failed = False

        try:
            self.log("INFO", "========== DC断电循环测试开始 ==========")
            self.log("INFO", f"测试参数：总循环{self.total_cycles}次，OS等待{self.os_wait_sec}秒，DC休眠{self.dc_sleep_sec}秒")

            # 1. 连通性检查
            self.log("INFO", "检查服务器连通性...")
            try:
                self.ssh_run("echo ok", silent=False, timeout=10)
            except Exception as e:
                self.log("ERROR", f"服务器连通性检查失败: {e}")
                failed = True
                return

            # 2. 创建本地日志目录
            server_id = self.bmc_ip if self.bmc_ip else self.os_ip
            base_local = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.local_log_dir = os.path.join(base_local, f"{ts}_DCreboot")
            os.makedirs(self.local_log_dir, exist_ok=True)
            self.log("INFO", f"结果保存目录: {self.local_log_dir}")

            # 3. 备份SEL
            self.backup_sel_elist_before_test(self.local_log_dir)

            # 4. 保存测试前系统信息
            self.save_system_info_before_test(self.local_log_dir)

            # 5. 清理远程日志
            clear_remote_logs(
                self.bmc_ip, self.bmc_user, self.bmc_pwd,
                self.os_ip, self.os_user, self.ssh_key, self.log
            )

            # 6. 初始化循环日志
            self.init_cycle_log("dc_cycle.log")
            self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 7. 启动SOL串口监听
            self.sol_log_path = os.path.join(self.local_log_dir, "sol_full.log")
            self.sol_proc, self.sol_bat = self.start_sol(self.sol_log_path)
            self.sol_scanner = LogScanner(
                sol_log_file=self.sol_log_path,
                error_patterns=MEMORY_ERROR_PATTERNS,
                log_callback=self.log,
                error_record_callback=self._record_reboot_error
            )
            self.log("INFO", f"SOL日志将保存到 {self.sol_log_path}")

            # 8. 部署远端测试环境
            if not self._prepare_environment():
                self.log("ERROR", "测试环境准备失败")
                failed = True
                return

            # 9. 清理历史状态/错误文件
            self.log("INFO", "清理远程历史状态文件...")
            self.ssh_run(f"rm -f {self.REMOTE_STATE_FILE} {self.REMOTE_ERR_FILE} {self.REMOTE_DIMM_FILE} {self.remote_dc_dir}/dc_start.log", silent=True)

            # 10. 获取基准内存容量
                     
            if not self._check_dmidecode():
                failed = True
                return

            baseline_dimms, snap_path = self.save_baseline_dimm_snapshot(self.local_log_dir)
            if not baseline_dimms:
                self.log("ERROR", "无法获取物理内存条基准，测试无法继续")
                failed = True
                return

            # 11. 启动远端DC循环脚本（后台运行，输出丢弃）
            start_cmd = (
                f"nohup bash {self.REMOTE_SCRIPT} "
                f"{self.total_cycles} {self.os_wait_sec} {self.dc_sleep_sec} "
                f"> /dev/null 2>&1 &"
            )
            self.log("INFO", f"执行远端启动命令：{start_cmd}")
            try:
                self.ssh_run(start_cmd, timeout=10)
                self.log("INFO", "远端DC测试已启动（后台运行）")
            except Exception as e:
                self.log("ERROR", f"远端启动失败：{str(e)}")
                failed = True
                return

            # 12. 初始推送总览
            self._update_overview_info(0)

            # 13. 启动轮询线程
            self.poll_thread = threading.Thread(
                target=self._poll_remote_status,
                daemon=True
            )
            self.poll_thread.start()

            # 主线程等待停止信号
            while self.running and not self.stop_event.is_set():
                self.stop_event.wait(1)

        except Exception as e:
            self.log("ERROR", f"DC测试主线程异常: {e}")
            failed = True
        finally:
            self.log("INFO", "主线程退出，执行收尾工作")

            # ========== 1. 先收集收尾日志，判断是否有 ECC/内存错误 ==========
            try:
                if self.collect_messages_syslog_dmesg_sel(self.local_log_dir):
                    self.log("ERROR", "收尾日志中发现 ECC/内存错误，测试判定为 failed")
                    self._test_failed = True
            except Exception as e:
                self.log("WARN", f"收尾日志收集失败: {e}")

            # ========== 2. 下载远程 DC 日志 ==========
            try:
                self._download_remote_dc_logs()
            except Exception as e:
                self.log("WARN", f"下载远程 DC 日志失败: {e}")

            # 关闭SOL
            if self.sol_proc or self.sol_bat:
                self.stop_sol(self.sol_proc, self.sol_bat)
                self.sol_proc = None
                self.sol_bat = None

            self.running = False

            # ========== 3. 确定最终状态 ==========
            if self.user_stopped:
                final_status = "stopped"
            elif failed or self._test_failed:
                final_status = "failed"
            else:
                final_status = "finished"

            self.close_cycle_log(test_status=final_status)

            # ========== 4. 最后触发状态回调 ==========
            if not self._status_reported:
                self._status_reported = True
                self.status_callback(final_status)

            # ========== 5. 打印测试结束 ==========
            self.log("INFO", "========== DC断电循环测试结束 ==========")

            # ========== 6. 启动远端清理（异步） ==========
            if not self._cleanup_started:
                self._cleanup_started = True
                if failed or self._test_failed:
                    self.log("INFO", "⚠️ 测试失败，远端清理将在后台继续执行，请勿立即关闭程序")
                elif self.user_stopped:
                    self.log("INFO", "⚠️ 用户停止测试，远端清理将在后台继续执行，请勿立即关闭程序")
                else:
                    self.log("INFO", "远端清理将在后台继续执行，请勿立即关闭程序")
                threading.Thread(target=self._remote_cleanup, args=(True, None), daemon=True).start()