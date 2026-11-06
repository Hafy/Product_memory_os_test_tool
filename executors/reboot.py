import os
import time
import threading
from datetime import datetime
from .base import SOLMixin, RebootCheckMixin, CycleLoggerMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE, MEMORY_ERROR_PATTERNS
from utils.helpers import clear_remote_logs
from utils.log_scanner import LogScanner

class RebootTestExecutor(SOLMixin, RebootCheckMixin, CycleLoggerMixin):
    def __init__(self, server_info, test_name, params, log_callback, status_callback,
                 remote_dir, remote_subdir, check_interval=60, test_info_callback=None):
        self.server_info = server_info
        self.test_name = test_name
        self.params = params
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.remote_dir = remote_dir
        self.remote_subdir = remote_subdir
        self.check_interval = check_interval
        self.running = False
        self.user_stopped = False
        self.thread = None
        self.local_log_dir = None
        self.stop_event = threading.Event()
        self.test_info_callback = test_info_callback
        self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.bmc_ip = server_info.get('bmc_ip')
        self.bmc_user = server_info.get('bmc_user')
        self.bmc_pwd = server_info.get('bmc_pwd')
        self.os_ip = server_info.get('os_ip')
        self.os_user = server_info.get('os_user')
        self.ssh_key = SSH_KEY

        self.sol_proc = None
        self.sol_bat = None
        self.sol_log_file = None
        self.sol_scanner = None
        self.cycle_log_file = None

    def log(self, level, msg):
        self.log_callback(level, msg)

    def start(self):
        self.running = True
        self.status_callback("running")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.user_stopped = True
        self.stop_event.set()
        self.running = False
        if self.sol_proc or self.sol_bat:
            self.stop_sol(self.sol_proc, self.sol_bat)
            self.sol_proc = None
            self.sol_bat = None

    def ssh_run(self, cmd, silent=False, timeout=30):
        from utils.ssh_utils import ssh_run
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=SSH_KEY, silent=silent, timeout=timeout)

    def _run(self):
        failed = False
        try:
            self.log("INFO", "========== Reboot 测试开始 ==========")
            self.log("INFO", "检查服务器连通性...")
            self.ssh_run("echo ok", silent=False, timeout=10)


            server_id = self.bmc_ip
            base_local = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.local_log_dir = os.path.join(base_local, f"{timestamp}_{self.test_name}")
            os.makedirs(self.local_log_dir, exist_ok=True)
            self.log("INFO", f"结果保存目录: {self.local_log_dir}")

            cycles = self.params.get("测试次数", 5)
            wait_sec = self.params.get("OS等待秒数", 60)

            self.init_cycle_log("reboot_cycle.log")
            #self.backup_sel_elist_before_test(self.local_log_dir)

             # 备份SEL ELIST
            self.backup_sel_elist_before_test(self.local_log_dir)

            self.save_system_info_before_test(self.local_log_dir)

            clear_remote_logs(self.bmc_ip, self.bmc_user, self.bmc_pwd,
                                self.os_ip, self.os_user, SSH_KEY, self.log)

            #self.log("INFO", f"启动 Reboot 测试，共 {cycles} 次重启")

            sol_full_log = os.path.join(self.local_log_dir, "sol_full.log")
            self.sol_proc, self.sol_bat = self.start_sol(sol_full_log)
            self.log("ALERT", f"SOL日志将保存到 {sol_full_log}")
            time.sleep(2)

            # 创建dmesg让目录
            dmesg_dir = os.path.join(self.local_log_dir, "dmesg")

            self.sol_scanner = LogScanner(
                sol_log_file=sol_full_log,
                error_patterns=MEMORY_ERROR_PATTERNS,
                log_callback=self.log,
                error_record_callback=self._record_reboot_error
            )

            if not self._check_dmidecode():
                failed = True
                return

            baseline_dimms, snap_path = self.save_baseline_dimm_snapshot(self.local_log_dir)
            if not baseline_dimms:
                self.log("ERROR", "无法获取物理内存条基准，测试无法继续")
                failed = True
                return
            

            cycles = self.params.get("测试次数", 2)
            self.log_cycle(f"即将开始Reboot测试， 测试次数：{cycles}")
            for i in range(cycles):
                if self.user_stopped:
                    self.log("WARN", "用户停止测试")
                    break
                self.log_cycle(f"第{i+1}次重启开始")
                completed = i + 1
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                test_info = f"Start:{self.start_time}, Current:{completed}/{cycles}, Start:{current_time}"
                if self.test_info_callback:
                    self.test_info_callback(self.server_info.get('bmc_ip'), test_info)

                try:
                    self.log("ALERT", f"系统即将开启第{i+1}次reboot...")
                    self.ssh_run("reboot", silent=True)
                except:
                    pass

                '''
                try:
                    self.wait_bmc_ready()
                except Exception as e:
                    self.log("ERROR", f"等待 BMC 失败: {e}")
                    self._record_reboot_error(self.local_log_dir, 0, i, "BMC等待", str(e))
                    failed = True
                    break
                '''
                ssh_ready = False
                try:
                    self.wait_system_ssh_ready(silent_wait=60)
                    ssh_ready = True
                except TimeoutError:
                    self.log("ERROR", f"第{i+1}次重启后系统启动超时")

                # SOL 增量扫描
                sol_error, _ = self.sol_scanner.scan_incremental(0, i, self.local_log_dir)
                # sel 增量备份
                #sel_error, _ = self.fetch_and_backup_sel(0, i, self.local_log_dir)

                has_error = sol_error
                current_mem = 0
                if ssh_ready:
                    dmesg_error, current_mem = self.check_dmesg_and_memory(
                        self.local_log_dir, 0, i
                    )
                    has_error = has_error or dmesg_error

                self.log_cycle(f"第{i+1}次重启结束, 当前内存: {current_mem} GB" + (" (检测到错误)" if has_error else ""))

                if has_error:
                    self.log("ERROR", f"第{i+1}次重启后检测到错误，停止后续测试")
                    failed = True
                    break

                self.log("INFO", f"第{i+1}次重启完成，未发现错误")
                time.sleep(wait_sec)

        except InterruptedError:
            self.log("WARN", "用户停止测试")
            self.user_stopped = True
        except Exception as e:
            self.log("ERROR", f"测试异常: {e}")
            failed = True
        finally:
            self.log("INFO", "尝试连接服务器做日志收尾工作")
            self.stop_sol()
            self.sol_bat = None
            self.sol_proc = None

            
            if self.collect_messages_syslog_only(self.local_log_dir):
                failed = True

            # SEL 全量检查
            if self.check_sel_full(self.local_log_dir, 0, 0):
                self.log("ERROR", "最终 SEL 扫描发现内存错误")
                failed = True
            self.log("INFO", "日志收尾工作完成")
            self.running = False
            if self.user_stopped:
                final_status = "stopped"
            elif failed:
                final_status = "failed"
            else:
                final_status = "finished"
            self.close_cycle_log(test_status=final_status)
            self.status_callback(final_status)
