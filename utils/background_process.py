import time
from .ssh_utils import ssh_run

class BackgroundProcess:
    def __init__(self, host, user, key_path, work_dir, cmd, log_file, pid_file, check_interval=60):
        self.host = host
        self.user = user
        self.key_path = key_path
        self.work_dir = work_dir
        self.cmd = cmd
        self.log_file = log_file
        self.pid_file = pid_file
        self.check_interval = check_interval
        self.pid = None
        self._stop_flag = False

    def start(self):
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            private_key = paramiko.RSAKey.from_private_key_file(self.key_path)
            ssh.connect(hostname=self.host, username=self.user, pkey=private_key, timeout=10)
            transport = ssh.get_transport()
            channel = transport.open_session()
            remote_cmd = f"cd {self.work_dir} && (nohup {self.cmd} > {self.log_file} 2>&1 & echo $! > {self.pid_file})"
            print(f"backgroundprocess-cmd: {remote_cmd}")           
            channel.exec_command(remote_cmd)
            channel.shutdown_write()
            time.sleep(0.2)
            channel.close()
            ssh.close()
            return True
        except Exception as e:
            print(f"启动失败: {e}")
            return False

    def wait(self, stop_check=None):
        for _ in range(10):
            if stop_check and stop_check():
                return False
            time.sleep(0.5)
            try:
                result = ssh_run(f"cat {self.pid_file}", self.host, self.user,
                                 key_path=self.key_path, silent=True, timeout=5).strip()
                if result:
                    self.pid = result
                    break
            except:
                pass
        if not self.pid:
            return False
        while True:
            if stop_check and stop_check():
                self.kill()
                return False
            try:
                status = ssh_run(f"kill -0 {self.pid} 2>/dev/null && echo 'running' || echo 'stopped'",
                                 self.host, self.user, key_path=self.key_path, silent=True, timeout=5).strip()
            except Exception:
                status = "stopped"
            if status != "running":
                break
            time.sleep(self.check_interval)
        return True

    def kill(self):
        if self.pid:
            ssh_run(f"kill -9 {self.pid}", self.host, self.user, key_path=self.key_path, silent=True, timeout=5)
        ssh_run(f"rm -f {self.pid_file} {self.log_file}", self.host, self.user,
                key_path=self.key_path, silent=True, timeout=5)
