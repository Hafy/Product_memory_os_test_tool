import os
import threading
import time
from datetime import datetime
from .base import PrepareMixin, RebootCheckMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE
from utils.background_process import BackgroundProcess
from utils.helpers import clear_remote_logs

class MlcTestExecutor(PrepareMixin, RebootCheckMixin):
    def __init__(self, server_info, test_name, params, log_callback, status_callback,
                 remote_dir, remote_subdir, check_interval=60, test_info_callback=None):
        self.server_info = server_info
        self.test_name = test_name
        self.params = params
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.test_info_callback = test_info_callback
        self.remote_dir = remote_dir
        self.remote_subdir = remote_subdir
        self.check_interval = check_interval
        self.running = False
        self.user_stopped = False
        self.thread = None
        self.local_log_dir = None
        self.current_proc = None

        self.bmc_ip = server_info.get('bmc_ip')
        self.bmc_user = server_info.get('bmc_user')
        self.bmc_pwd = server_info.get('bmc_pwd')
        self.os_ip = server_info.get('os_ip')
        self.os_user = server_info.get('os_user')
        self.ssh_key = SSH_KEY

    def log(self, level, msg):
        self.log_callback(level, msg)

    def start(self):
        self.running = True
        self.status_callback("running")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.user_stopped = True
        self.running = False
        if self.current_proc:
            self.current_proc.kill()
            self.log("INFO", "已停止当前测试进程")

    def ssh_run(self, cmd, silent=False, timeout=30):
        from utils.ssh_utils import ssh_run
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=SSH_KEY, silent=silent, timeout=timeout)

    def _run_single_test(self, test_type, test_index, cmd, remote_log_name, local_result_dir):
        if self.user_stopped:
            return False
        work_dir = os.path.join(self.remote_dir, self.remote_subdir)
        remote_log = os.path.join(work_dir, remote_log_name)
        pid_file = os.path.join(work_dir, "mlc.pid")
        interval = 60 if test_type == "idle" else self.check_interval
        proc = BackgroundProcess(
            host=self.os_ip,
            user=self.os_user,
            key_path=SSH_KEY,
            work_dir=work_dir,
            cmd=cmd,
            log_file=remote_log,
            pid_file=pid_file,
            check_interval=interval
        )
        if not proc.start():
            self.log("ERROR", f"启动后台进程失败: {cmd}")
            return False
        self.current_proc = proc
        self.log("INFO", f"开始第{test_index}次 {test_type}测试")
        def stop_check():
            return self.user_stopped
        success = proc.wait(stop_check=stop_check)
        self.current_proc = None
        if not success:
            self.log("WARN", f"{test_type} 第 {test_index} 次测试被停止或异常")
            return False
        local_log = os.path.join(local_result_dir, remote_log_name)
        try:
            content = self.ssh_run(f"cat {remote_log}", silent=True, timeout=30)
            with open(local_log, "w", encoding="utf-8") as f:
                f.write(content)
            self.log("INFO", f"已保存日志到 {local_log}")
        except Exception as e:
            self.log("WARN", f"下载日志失败: {e}")
        summary_file = os.path.join(local_result_dir, f"mlc_{test_type}_summary.txt")
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(f"========== 第{test_index}次 MLC测试结果==========\n")
            f.write(content)
            f.write("\n")
        return True

    def _prepare_environment(self):
        """准备 MLC 测试环境"""
        self.log("INFO", "准备 MLC 测试环境...")
        
        work_dir = os.path.join(self.remote_dir, self.remote_subdir)
        mlc_bin = f"{work_dir}/mlc"
        
        if self._check_remote_file(mlc_bin):
            self.log("INFO", "MLC 已存在，跳过准备")
            self.ssh_run(f"chmod +x {mlc_bin}")
            return True
        
        # 上传并解压
        if not self._upload_and_extract_tar("mlc_v3.11.tar.gz"):
            return False
        
        self.ssh_run(f"chmod +x {mlc_bin}")
        self.log("INFO", "MLC 环境准备完成")
        return True

    def _run(self):
        failed = False
        final_status = None
        try:
            self.log("INFO", "========== MLC 内存时延测试开始 ==========")

          
            server_id = self.server_info.get('bmc_ip', self.os_ip)
            base_local = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            local_result_dir = os.path.join(base_local, f"{timestamp}_{self.test_name}")
            os.makedirs(local_result_dir, exist_ok=True)
            self.local_log_dir = local_result_dir
            self.log("INFO", f"结果保存目录: {local_result_dir}")

            self.log("INFO", "检查服务器连通性...")
            self.ssh_run("echo ok", silent=False, timeout=10)

            if not self._prepare_environment():
                failed = True
                return
                    

            self.save_system_info_before_test(self.local_log_dir)
             # 备份SEL ELIST
            self.backup_sel_elist_before_test(self.local_log_dir)
            
            clear_remote_logs(self.bmc_ip, self.bmc_user, self.bmc_pwd,
                              self.os_ip, self.os_user, SSH_KEY, self.log)

    

            default_count = self.params.get("测试次数", 5)
            idle_count = self.params.get("测试次数", 5)

            self.log("INFO", f"开始默认 mlc 测试，共 {default_count} 次")
            for i in range(1, default_count+1):
                if self.user_stopped:
                    break
                remote_log = f"mlc_default_{i}.log"
                cmd = "./mlc"
                if not self._run_single_test("default", i, cmd, remote_log, local_result_dir):
                    break
                if i < default_count and not self.user_stopped:
                    time.sleep(1)

            self.log("INFO", f"开始 idle_latency 测试，共 {idle_count} 次")
            for i in range(1, idle_count+1):
                if self.user_stopped:
                    break
                remote_log = f"mlc_idle_{i}.log"
                cmd = "./mlc --idle_latency"
                if not self._run_single_test("idle", i, cmd, remote_log, local_result_dir):
                    break
                if i < idle_count and not self.user_stopped:
                    time.sleep(1)

            if self.user_stopped:
                self.log("WARN", "测试被用户停止")
                final_status = "stopped"
            else:
                self.log("INFO", "MLC 测试全部完成")

        except Exception as e:
            self.log("ERROR", f"MLC 测试异常: {str(e)}")
            failed = True
        finally:
            self.log("INFO", "尝试连接服务器做日志收尾工作")
            if self.collect_messages_syslog_dmesg_sel(self.local_log_dir):
                failed = True
            self.log("INFO", "日志收尾工作完成")
            self.running = False
            if self.user_stopped:
                final_status = "stopped"
            elif failed:
                final_status = "failed"
            else:
                
                final_status = "finished"
            if final_status:
                self.status_callback(final_status)
