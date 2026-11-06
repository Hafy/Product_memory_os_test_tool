import os
import time
import threading
from datetime import datetime
from .base import PrepareMixin, RebootCheckMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE
from utils.helpers import clear_remote_logs, clean_remote_log
from utils.background_process import BackgroundProcess

class MemoryBurnTestExecutor(PrepareMixin, RebootCheckMixin):
    def __init__(self, server_info, test_name, params, log_callback, status_callback,
                 remote_dir, script_name, remote_subdir, check_interval=30, test_info_callback=None):
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
        self.script_name = script_name
        self.stop_event = threading.Event()
        self.test_info_callback = test_info_callback
        self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        self.bmc_ip = server_info.get('bmc_ip')
        self.bmc_user = server_info.get('bmc_user')
        self.bmc_pwd = server_info.get('bmc_pwd')
        self.os_ip = server_info.get('os_ip')
        self.os_user = server_info.get('os_user')
        self.ssh_key = SSH_KEY

        self.remote_script_dir = f"{remote_dir}/{remote_subdir}"
        self.remote_script_path = f"{self.remote_script_dir}/{self.script_name}"
        self.remote_log_file = f"{self.remote_script_dir}/memory_burn.log"
        self._cleaned = False
        self.test_duration_hours = self.params.get("测试时间(小时)", 24)  # 0 表示无限运行
        self.start_timestamp = None
        self.time_expired = False

    def log(self, level, msg):
        self.log_callback(level, msg)

    def ssh_run(self, cmd, silent=False, timeout=30):
        from utils.ssh_utils import ssh_run
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=self.ssh_key, silent=silent, timeout=timeout)

    def start(self):
        self.running = True
        self.status_callback("running")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.user_stopped = True
        self.stop_event.set()
        self.running = False
        #threading.Thread(target=self._kill_all_processes, daemon=True).start()
        self._kill_all_processes()
        self.log("INFO", "正在停止 Memory Burn In 测试...")

    def _kill_all_processes(self):
        if self._cleaned:
            return
        self._cleaned = True
        self.log("INFO", "开始清理测试进程（通过 HWMEM 关键字）...")
        try:
            self.ssh_run("pkill -f HWMEM", silent=True)
        except:
            pass
        time.sleep(1)
        try:
            self.ssh_run("pkill -9 -f HWMEM", silent=True)
        except:
            pass
        try:
            pid_list = self.ssh_run("pgrep -f HWMEM", silent=True).strip()
            if pid_list:
                for pid in pid_list.split():
                    self.ssh_run(f"kill -9 {pid}", silent=True)
        except:
            pass
        try:
            self.ssh_run("pkill -f '{}'".format(self.script_name), silent=True)
        except:
            pass
        time.sleep(1)
        self.log("INFO", "进程清理完成")

    def _start_remote_test(self):
        self.log("INFO", "启动 Memory Burn In 测试...")
    
        # 清理旧日志和 PID 文件
        self.ssh_run(f"rm -f {self.remote_log_file}", silent=True)
        pid_file = f"{self.remote_script_dir}/memory_burn.pid"
        self.ssh_run(f"rm -f {pid_file}", silent=True)
        
        # 构建命令（不带重定向，BackgroundProcess 会自动处理）
        cmd = f"./{self.script_name}"
        
        proc = BackgroundProcess(
            host=self.os_ip,
            user=self.os_user,
            key_path=self.ssh_key,
            work_dir=self.remote_script_dir,
            cmd=cmd,
            log_file=self.remote_log_file,
            pid_file=pid_file,
            check_interval=10
        )
        
        if not proc.start():
            self.log("ERROR", "启动 Memory Burn In 失败")
            return False
        
        #self.current_proc = proc
        self.log("INFO", "Memory Burn In 启动命令已发送")
        for _ in range(10):  # 最多等待 10 秒
            time.sleep(1)
            if self._is_test_running():
                self.log("INFO", "检测到测试进程已运行")
                return True
        
        self.log("ERROR", "启动后未检测到测试进程")
        return False
            

    def _is_test_running(self):
        try:
            if self.ssh_run("pgrep -f HWMEM", silent=True).strip():
                self.log("INFO", "pgrep -f HWMEM")
                return True
            if self.ssh_run("pgrep -f memtester", silent=True).strip():
                self.log("INFO", "pgrep -f memtester")
                return True
        except:
            self.log("ERROR", "进程启动失败")
            return False

    def _collect_log(self, log_dir):
        if not os.path.exists(log_dir):
            return False
        local_log_path = os.path.join(log_dir, "memory_burn.log")
        try:
            content = self.ssh_run(f"cat {self.remote_log_file}", silent=True, timeout=30)
            cleaned = clean_remote_log(content)
            with open(local_log_path, 'w', encoding='utf-8') as f:
                f.write(cleaned)
            self.log("INFO", f"日志已保存到 {local_log_path}")
        except Exception as e:
            self.log("WARN", f"下载日志失败: {e}")

    
    def _prepare_environment(self):
        """准备 Memory Burn In 测试环境"""
        self.log("INFO", "准备 Memory Burn In 测试环境...")
        
        script_name = self.params.get("script", "memory_burn_test%gen3_107.sh")
        remote_script = f"{self.remote_script_dir}/{script_name}"
        
        if self._check_remote_file(remote_script):
            self.log("INFO", f"{script_name} 已存在，跳过准备")
            return True
        
        # 上传压缩包
        if not self._upload_and_extract_tar("memory_burn_test%gen3_107-2.tar.gz"):
            return False
        
        self.ssh_run(f"chmod +x {remote_script}")
        self.log("INFO", "Memory Burn In 环境准备完成")
        return True

    def _run(self):
        failed = False
        try:
            self.log("INFO", "========== Memory Burn In 测试开始 ==========")

            self.log("INFO", f"script_name:{self.script_name}")
            server_id = self.bmc_ip if self.bmc_ip else self.os_ip
            base_local = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.local_log_dir = os.path.join(base_local, f"{timestamp}_{self.test_name}")
            os.makedirs(self.local_log_dir, exist_ok=True)
            self.log("INFO", f"结果保存目录: {self.local_log_dir}")

            self.log("INFO", "检查服务器连通性...")
            self.ssh_run("echo ok", silent=False, timeout=10)

            if not self._prepare_environment():
                failed = True
                return

            
            self.save_system_info_before_test(self.local_log_dir)
            # 备份SEL ELIST
            self.backup_sel_elist_before_test(self.local_log_dir)
            clear_remote_logs(self.bmc_ip, self.bmc_user, self.bmc_pwd,
                              self.os_ip, self.os_user, self.ssh_key, self.log)

            if not self._start_remote_test():
                failed = True
                return
            
            self.start_timestamp = time.time()
            self.log("INFO", "Memory Burn In 进程已启动，开始计时")

            if self.test_info_callback:
                test_info = f"Start: {self.start_time}"
                self.test_info_callback(self.server_info.get('bmc_ip', ''), test_info)

            while not self.user_stopped:
                # 检查时间限制
                if self.test_duration_hours > 0:
                    elapsed = (time.time() - self.start_timestamp) / 3600.0
                    if elapsed >= self.test_duration_hours:
                        self.time_expired = True
                        self.log("INFO", f"测试已运行 {self.test_duration_hours} 小时，自动停止")
                        self._kill_all_processes()
                        break
                
                if not self._is_test_running():
                    self.log("INFO", "测试进程已退出，请检查测试结果...")
                    break
                time.sleep(self.check_interval)
            
            if self.user_stopped:
                self._kill_all_processes()
                self.log("INFO", "用户手动停止测试，测试正常结束（仅对Memory burn in 测试而言）")

        except InterruptedError:
            self.log("WARN", "用户停止测试")
            self._kill_all_processes()
            self.user_stopped = True
        except Exception as e:
            self.log("ERROR", f"测试异常: {e}")
            failed = True
        finally:
            ssh_ok = False
            self.log("INFO", "尝试连接服务器做日志收尾工作")
            for attempt in range(3):
                try:
                    self.ssh_run("echo ok", silent=True, timeout=5)
                    ssh_ok = True
                    break
                except:
                    time.sleep(1)
            if ssh_ok:
                self._collect_log(self.local_log_dir)
              
                if self.collect_dmesg_single(self.local_log_dir):
                    self.log("ERROR", "dmesg中发现内存错误，请检查日志")
                    failed = True
                if self.collect_messages_syslog_only(self.local_log_dir):
                    failed = True
            if self.check_sel_full(self.local_log_dir, 0, 0):
                self.log("ERROR", "SEL 中发现内存错误, 请检查日志...")
                failed = True
            self.log("INFO", "日志收尾工作完成")
            self.running = False

            if self.time_expired:
                # 时间到自动停止 → 正常完成
                final_status = "finished"
                self.log("INFO", "测试因时间到达而结束")
            elif self.user_stopped:
                # 用户手动停止
                if failed:
                    final_status = "failed"
                    self.log("WARN", "用户手动停止，但检测到错误，请检查日志")
                else:
                    final_status = "stopped"
                    self.log("INFO", "用户手动停止测试")
            elif failed:
                final_status = "failed"
            else:
                final_status = "finished"

            self.status_callback(final_status)
