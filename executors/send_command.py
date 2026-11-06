import os
import threading
from datetime import datetime
from .base import PrepareMixin, RebootCheckMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE
from utils.ssh_utils import ssh_run

class SendCommandExecutor(PrepareMixin, RebootCheckMixin):
    def __init__(self, server_info, test_name, params, log_callback, status_callback,
                 remote_dir=None, remote_subdir=None, check_interval=10, test_info_callback=None):
        self.server_info = server_info
        self.test_name = test_name
        self.params = params
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.test_info_callback = test_info_callback
        
        self.running = False
        self.thread = None
        
        self.bmc_ip = server_info.get('bmc_ip')
        self.bmc_user = server_info.get('bmc_user')
        self.bmc_pwd = server_info.get('bmc_pwd')
        self.os_ip = server_info.get('os_ip')
        self.os_user = server_info.get('os_user')
        self.ssh_key = SSH_KEY
        
        # 从 params 获取用户输入的命令
        self.custom_cmd = self.params.get("命令内容", "").strip()
        
        # 保存目录: APPLog/BMC_IP/SendCommand/时间戳
        server_id = self.bmc_ip if self.bmc_ip else self.os_ip
        self.local_log_dir = os.path.join(LOCAL_LOG_BASE, server_id, "SendCommand", 
                                          f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_SendCommand")
        os.makedirs(self.local_log_dir, exist_ok=True)

    def log(self, level, msg):
        self.log_callback(level, msg)

    def ssh_run(self, cmd, silent=False, timeout=30):
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=self.ssh_key, silent=silent, timeout=timeout)

    def start(self):
        self.running = True
        self.status_callback("running")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.log("WARN", "用户尝试停止，但发送命令通常瞬间完成")

    def _run(self):
        try:
            self.log("INFO", f"========== SendCommand 开始 ==========")
            self.log("INFO", f"待执行命令: {self.custom_cmd}")
            
            if not self.custom_cmd:
                self.log("ERROR", "未提供要执行的命令！")
                self.status_callback("failed")
                return

            if self.test_info_callback:
                self.test_info_callback(self.bmc_ip, f"正在执行: {self.custom_cmd}")

            self.log("INFO", "检查服务器连通性...")
            try:
                self.ssh_run("echo ok", silent=False, timeout=10)
                self.log("INFO", "SSH 连通性正常，开始发送命令...")
            except Exception as e:
                self.log("ERROR", f"SSH 连接失败，无法执行命令: {e}")
                self.status_callback("failed")
                return

            self.log("INFO", f"正在执行命令: {self.custom_cmd} (最长等待 300 秒)")
            try:
                output = self.ssh_run(self.custom_cmd, silent=True, timeout=300)
                self.log("INFO", "命令执行完成，正在保存输出...")
            except Exception as e:
                self.log("ERROR", f"命令执行过程中发生异常: {e}")
                self.status_callback("failed")
                return

            # 保存输出到文件
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_cmd_name = "".join([c if c.isalnum() else "_" for c in self.custom_cmd])[:30]
            file_name = f"output_{timestamp}_{safe_cmd_name}.txt"
            file_path = os.path.join(self.local_log_dir, file_name)
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(f"# 执行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"# 目标主机: {self.os_ip}\n")
                f.write(f"# 命令内容: {self.custom_cmd}\n")
                f.write("=" * 50 + "\n\n")
                f.write(output)

            self.log("INFO", f"✅ 命令结果已保存到: {file_path}")
            
            if self.test_info_callback:
                self.test_info_callback(self.bmc_ip, f"✅ 命令执行完成")

            self.status_callback("finished")

        except Exception as e:
            self.log("ERROR", f"SendCommand 异常: {e}")
            self.status_callback("failed")
        finally:
            self.running = False