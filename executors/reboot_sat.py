import os
import time
import threading
from datetime import datetime
from .base import PrepareMixin, SOLMixin, RebootCheckMixin, CycleLoggerMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE, MEMORY_ERROR_PATTERNS
from utils.helpers import clear_remote_logs
from utils.log_scanner import LogScanner
from utils.background_process import BackgroundProcess

class RebootSATTestExecutor(PrepareMixin, SOLMixin, RebootCheckMixin, CycleLoggerMixin):
    def __init__(self, server_info, test_name, params, log_callback, status_callback,
                 remote_dir, remote_subdir, check_interval=30, test_info_callback=None):
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
        self._cleaned = False

        self.bmc_ip = server_info.get('bmc_ip')
        self.bmc_user = server_info.get('bmc_user')
        self.bmc_pwd = server_info.get('bmc_pwd')
        self.os_ip = server_info.get('os_ip')
        self.os_user = server_info.get('os_user')
        self.ssh_key = SSH_KEY

        self.remote_sat_dir = f"{remote_dir}/{remote_subdir}"
        self.sat_bin = f"{self.remote_sat_dir}/stressapptest"
        self.sat_log_file = None

        self.sol_proc = None
        self.sol_bat = None
        self.sol_log_file = None
        self.sol_scanner = None

    def log(self, level, msg):
        self.log_callback(level, msg)

    def ssh_run(self, cmd, silent=False, timeout=30):
        from utils.ssh_utils import ssh_run
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=SSH_KEY, silent=silent, timeout=timeout)

    def start(self):
        self.running = True
        self.status_callback("running")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.user_stopped = True
        self.stop_event.set()
        self.running = False
        threading.Thread(target=self._kill_all_processes, daemon=True).start()
        self.log("INFO", "正在停止 Reboot+SAT 测试...")

    def _kill_all_processes(self):
        if self._cleaned:
            return
        self._cleaned = True
        self.log("INFO", "开始清理测试进程...")
        try:
            self.stop_sol(self.sol_proc, self.sol_bat)
        except:
            pass
        try:
            self.ssh_run("pkill -9 stressapptest", silent=True)
        except:
            pass
        time.sleep(1)
        self.log("INFO", "进程清理完成")

    def _run_stressapptest(self, mem_mb, test_sec, round_idx):
        cmd = f"./stressapptest -M {mem_mb} -s {test_sec}"
        log_file = f"{self.remote_sat_dir}/stressapptest_round{round_idx+1}.log"
        pid_file = f"{self.remote_sat_dir}/stressapptest_round{round_idx+1}.pid"
        proc = BackgroundProcess(
            host=self.os_ip,
            user=self.os_user,
            key_path=self.ssh_key,
            work_dir=self.remote_sat_dir,
            cmd=cmd,
            log_file=log_file,
            pid_file=pid_file,
            check_interval=10
        )
        if not proc.start():
            self.log("ERROR", "启动 stressapptest 后台进程失败")
            return None
        for _ in range(10):
            try:
                result = self.ssh_run(f"cat {pid_file}", silent=True, timeout=5).strip()
                if result:
                    proc.pid = result
                    self.log("INFO", f"获取到PID: {proc.pid}")
                    break
            except:
                pass
            time.sleep(0.5)
        if not proc.pid:
            return None
        self.log("INFO", f"stressapptest 已启动，PID={proc.pid}, 内存 {mem_mb} MB，持续 {test_sec} 秒")
        self.sat_log_file = log_file
        return proc

    def _wait_for_stressapptest(self, proc, timeout_sec):
        if not proc.pid:
            self.log("ERROR", "无有效PID, 无法监控")
            return False
        time.sleep(3)
        start = time.time()
        while not self.user_stopped:
            if time.time() - start > timeout_sec + 60:
                self.log("ERROR", "stressapptest 执行超时")
                return False
            try:
                status = self.ssh_run(f"kill -0 {proc.pid} 2>/dev/null && echo 'running' || echo 'stopped'", silent=True).strip()
                if status != "running":
                    self.log("INFO", "stressapptest 进程已结束")
                    return True
            except:
                pass
            time.sleep(5)
        return False

    def _collect_sat_log(self, round_idx):
        if not self.sat_log_file:
            self.log("WARN", "无当前日志路径，跳过下载")
            return False
        has_error, _ = self.download_and_scan_stressapptest_log(
            remote_log_path=self.sat_log_file,
            local_log_dir=self.local_log_dir,
            local_filename="stressapptest.log",
            round_idx=round_idx,
            reboot_idx=0,
            append=True
        )
        return has_error

    def _prepare_environment(self):
        """准备 Reboot+SAT 测试环境"""
        self.log("INFO", "准备 Stressapptest 环境...")
        
        work_dir = f"{self.remote_dir}/{self.remote_subdir}"
        exe_path = f"{work_dir}/stressapptest"
        
        # 场景C：可执行文件已存在，直接返回
        if self._check_remote_file(exe_path):
            self.log("INFO", "Stressapptest 已存在，跳过准备")
            self.ssh_run(f"chmod +x {exe_path}", silent=True)
            return True
        
        # 场景B：目录存在但可执行文件不存在 → 只chmod + 编译
        if self._check_remote_dir(work_dir):
            self.log("INFO", "Stressapptest 目录已存在但未编译，开始赋权并编译...")
        else:
            # 场景A：目录不存在 → 上传解压
            self.log("INFO", "Stressapptest 目录不存在，开始上传解压...")
            if not self._upload_and_extract_tar("SAT_V1.3.tar.gz"):
                return False
        
        # ========== 通用步骤：给sh和configure脚本赋权 ==========
        self.ssh_run(
            f"find {work_dir} -name '*.sh' -exec chmod +x {{}} \\; 2>/dev/null; "
            f"find {work_dir} -name 'configure' -exec chmod +x {{}} \\; 2>/dev/null",
            silent=True
        )
        
        # ========== 编译安装 ==========
        try:
            self.ssh_run(f"cd {work_dir} && make clean", silent=False, timeout=30)
            self.ssh_run(f"cd {work_dir} && ./configure", silent=False, timeout=60)
            self.ssh_run(f"cd {work_dir} && make", silent=False, timeout=300)
            try:
                self.ssh_run(f"cd {work_dir} && make install", silent=False, timeout=30)
            except Exception as e:
                self.log("WARN", f"make install 失败，使用源码目录中的可执行文件: {e}")
            
            self.ssh_run(f"chmod +x {exe_path}")
            self.log("INFO", "Stressapptest 编译安装完成")
            return True
        except Exception as e:
            self.log("ERROR", f"Stressapptest 编译安装失败: {e}")
            return False

    def _run(self):
        failed = False
        try:
            self.log("INFO", "========== Reboot+SAT 组合测试开始 ==========")
           
            server_id = self.bmc_ip if self.bmc_ip else self.os_ip
            base_local = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.local_log_dir = os.path.join(base_local, f"{timestamp}_{self.test_name}")
            os.makedirs(self.local_log_dir, exist_ok=True)
            self.log("INFO", f"结果保存目录: {self.local_log_dir}")

            self.log("INFO","检查服务器连通性...")
            self.ssh_run("echo ok", silent=False, timeout=10)

            if not self._prepare_environment():
                failed = True
                return


            self.init_cycle_log("reboot_sat_cycle.log")
            self.backup_sel_elist_before_test(self.local_log_dir)
            
            self.save_system_info_before_test(self.local_log_dir)

            clear_remote_logs(
                self.bmc_ip, self.bmc_user, self.bmc_pwd,
                self.os_ip, self.os_user, self.ssh_key, self.log
            )


            cycles = self.params.get("测试次数", 5)
            wait_sec = self.params.get("OS等待秒数", 30)
            sat_mem = self.params.get("测试容量(MB)", 1024)
            sat_time = self.params.get("测试时间(秒)", 3600)

            if not self._check_dmidecode():
                failed = True
                return

            baseline_dimms, snap_path = self.save_baseline_dimm_snapshot(self.local_log_dir)
            if not baseline_dimms:
                self.log("ERROR", "无法获取物理内存条基准，测试无法继续")
                failed = True
                return
            sol_full_log = os.path.join(self.local_log_dir, "sol_full.log")
            self.sol_proc, self.sol_bat = self.start_sol(sol_full_log)

            self.sol_scanner = LogScanner(
                sol_log_file=sol_full_log,
                error_patterns=MEMORY_ERROR_PATTERNS,
                log_callback=self.log,
                error_record_callback=self._record_reboot_error
            )
            self.log("ALERT", f"SOL 日志将保存到 {sol_full_log}")
            time.sleep(1)

            for i in range(cycles):
                if self.user_stopped:
                    self.log("WARN", "用户停止测试")
                    break

                self.log_cycle(f"第 {i+1}/{cycles} 轮Reboot开始")
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                test_info = f"Start:{self.start_time}, Reboot:{i+1}/{cycles}, Start:{current_time}"
                if self.test_info_callback:
                    self.test_info_callback(self.server_info.get('bmc_ip'), test_info)

                try:
                    self.log("ALERT", f"第 {i+1} 次重启...")
                    self.ssh_run("reboot", silent=True)
                except:
                    pass

                ssh_ready = False
                try:
                    self.wait_system_ssh_ready(silent_wait=60, max_wait=3600)
                    ssh_ready = True
                except TimeoutError:
                    self.log("ERROR", f"第 {i+1} 次重启后系统启动超时")

                if not ssh_ready:
                    self.log("ERROR", "SSH 不通，无法继续测试")
                    failed = True
                    break

                #reboot_dir = os.path.join(self.local_log_dir, f"reboot_{i+1}")

                self.log_cycle(f"第 {i+1}/{cycles} 轮Reboot重启成功")

                 # 创建dmesg目录
                dmesg_dir = os.path.join(self.local_log_dir, "dmesg")
                os.makedirs(dmesg_dir, exist_ok=True)

                self.log("INFO", "开始扫描dmesg日志，检查是否有内存丢失情况...")
                dmesg_error, current_mem = self.check_dmesg_and_memory(self.local_log_dir, 0, i)
                #sel_error, _ = self.fetch_and_backup_sel(i, 0, reboot_dir)
                sol_error, _ = self.sol_scanner.scan_incremental(i, 0, self.local_log_dir)

                if dmesg_error or sol_error:
                    self.log("ERROR", f"第 {i+1} 次重启后检测到错误")
                    failed = True
                    break

                self.log_cycle(f"第 {i+1}/{cycles} 轮 Stressapptest 开始")
                self.log("INFO", f"开始执行 stressapptest (内存 {sat_mem} MB, {sat_time} 秒)")
                test_info = f"Start:{self.start_time}, Stressapptest:{i+1}/{cycles}"
                if self.test_info_callback:
                    self.test_info_callback(self.server_info.get('bmc_ip'), test_info)


                sat_proc = self._run_stressapptest(sat_mem, sat_time, i)
                if not sat_proc:
                    failed = True
                    break

                if not self._wait_for_stressapptest(sat_proc, sat_time):
                    self.log("ERROR", "stressapptest 未正常完成")
                    failed = True
                    self.ssh_run("pkill -9 stressapptest", silent=True)
                    break

                time.sleep(2)
                if self._collect_sat_log(i):
                    failed = True
                    break

                self.log_cycle(f"第 {i+1}/{cycles} 轮 Stressapptest 完成")
                if i < cycles - 1 and wait_sec > 0:
                    self.log("INFO", f"等待 {wait_sec} 秒后进入下一轮...")
                    time.sleep(wait_sec)

        except InterruptedError:
            self.log("WARN", "用户停止测试")
            self.user_stopped = True
            self._kill_all_processes()
        except Exception as e:
            self.log("ERROR", f"测试异常: {e}")
            failed = True
        finally:
            self.log("INFO", "尝试连接服务器做日志收尾工作")
            self.stop_sol(self.sol_proc, self.sol_bat)
            
            
            if self.check_sel_full(self.local_log_dir, 0, 0):
                self.log("ERROR", "最终 SEL检查发现内存错误")
                failed = True
            if self.collect_messages_syslog_only(self.local_log_dir):
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
