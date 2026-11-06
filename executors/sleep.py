import os
import time
import threading
from datetime import datetime
from .base import RebootCheckMixin, CycleLoggerMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE
from utils.helpers import clear_remote_logs

class SleepTestExecutor(RebootCheckMixin, CycleLoggerMixin):
    def __init__(self, server_info, test_name, params, log_callback, status_callback,
                 check_interval=10, test_info_callback=None):
        self.server_info = server_info
        self.test_name = test_name
        self.params = params
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.test_info_callback = test_info_callback
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
        self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

    def _wait_for_network(self, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            try:
                self.ssh_run("echo ok", silent=False, timeout=5)
                return True
            except:
                time.sleep(5)
        return False

    def ssh_run(self, cmd, silent=False, timeout=30):
        from utils.ssh_utils import ssh_run
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=SSH_KEY, silent=silent, timeout=timeout)

    def _run(self):
        failed = False
        try:
            self.log("INFO", "========== Sleep 测试开始 ==========")
            self.log("INFO", "检查服务器连通性...")
            self.ssh_run("echo ok", silent=False, timeout=10)

            clear_remote_logs(
                self.bmc_ip, self.bmc_user, self.bmc_pwd,
                self.os_ip, self.os_user, SSH_KEY, self.log
            )

            server_id = self.bmc_ip if self.bmc_ip else self.os_ip
            base_local = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.local_log_dir = os.path.join(base_local, f"{timestamp}_{self.test_name}")
            os.makedirs(self.local_log_dir, exist_ok=True)
            self.log("INFO", f"结果保存目录: {self.local_log_dir}")

            self.init_cycle_log("sleep_cycle.log")
            self.save_system_info_before_test(self.local_log_dir)
            self.backup_sel_elist_before_test(self.local_log_dir)

            #cycles_log_file = os.path.join(self.local_log_dir, "sleep_cycle.text")
            cycles = self.params.get("测试次数", 1)
            sleep_sec = self.params.get("睡眠秒数", 30)
            self.log("INFO", f"准备执行 {cycles} 次睡眠测试，每次睡眠 {sleep_sec} 秒")

            for i in range(1, cycles+1):
                if self.user_stopped:
                    self.log("WARN", "用户停止测试，终止后续轮次")
                    break

                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                test_info = f"Start:{self.start_time}, Current:{i}/{cycles}, Start:{current_time}"
                if self.test_info_callback:
                    self.test_info_callback(self.server_info.get('bmc_ip'), test_info)

                start_msg = f"第 {i}/{cycles} 次睡眠测试开始"
                self.log("INFO", start_msg)
                self.log_cycle(start_msg)
                #with open(cycles_log_file, "a", encoding="utf-8") as f:
                #    f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {start_msg}\n")

                cmd = f"rtcwake -m mem -s {sleep_sec}"
                self.log("INFO", f"执行命令: {cmd}")

                try:
                    self.ssh_run(cmd, silent=True, timeout=5)
                except Exception as e:
                    self.log("INFO", "rtcwake命令已发送, 系统将进入睡眠")

                self.log("INFO", f"等待系统睡眠{sleep_sec}秒...")
                time.sleep(sleep_sec)

                self.log("INFO", "等待网络恢复...")
                if self._wait_for_network(timeout=30):
                    end_msg = f"第 {i} 次睡眠结束, 系统已唤醒"
                    self.log("INFO", end_msg)
                    self.log_cycle(end_msg)
                    #with open(cycles_log_file, "a", encoding="utf-8") as f:
                    #    f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {end_msg}\n")
                else:
                    self.log("ERROR", f"第 {i} 次睡眠测试失败：网络未恢复")
                    failed = True
                    break

            if not failed and not self.user_stopped:
                self.log("INFO", "所有 Sleep 测试完成")

        except InterruptedError:
            self.log("WARN", "用户停止测试")
            self.user_stopped = True
        except Exception as e:
            self.log("ERROR", f"测试异常: {e}")
            failed = True
        finally:
            self.running = False
            self.log("INFO", "尝试连接服务器做日志收尾工作")
            if self.collect_messages_syslog_dmesg_sel(self.local_log_dir):
                #self.log("ERROR", "系统日志中发现ECC错误，测试标志为失败")
                failed = True
            self.log("INFO", "日志收尾工作完成")

            if self.user_stopped:
                final_status = "stopped"
            elif failed:
                final_status = "failed"
            else:
                final_status = "finished"

            self.close_cycle_log(test_status=final_status)
            self.status_callback(final_status)
