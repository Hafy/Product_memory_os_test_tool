
import os
import threading
from datetime import datetime
from .base import PrepareMixin, RebootCheckMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE
from utils.background_process import BackgroundProcess
from utils.helpers import clear_remote_logs

class MemtesterExecutor(PrepareMixin, RebootCheckMixin):
    def __init__(self, server_info, test_name, script_name, params, log_callback, status_callback,
                 check_interval=300, remote_dir=None, remote_subdir=None, test_info_callback=None):
        self.server_info = server_info
        self.test_name = test_name
        self.script_name = script_name
        self.params = params
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.test_info_callback = test_info_callback
        self.pid = None
        self.running = False
        self.thread = None
        self.remote_dir = remote_dir or "/root/product_test_dir"
        self.remote_subdir = remote_subdir

        self.bmc_ip = server_info.get('bmc_ip')
        self.bmc_user = server_info.get('bmc_user')
        self.bmc_pwd = server_info.get('bmc_pwd')
        self.ssh_key = SSH_KEY
        self.os_ip = server_info.get('os_ip')
        self.os_user = server_info.get('os_user')
        self.os_passwd = server_info.get('os_passwd')
        self.user_stopped = False
        self.check_interval = check_interval

        self.current_proc = None

        if not self.os_ip or not self.os_user:
            raise ValueError(f"服务器配置缺少 SSH 连接信息: {server_info}")

    def log(self, level, msg):
        self.log_callback(level, msg)

    def start(self):
        self.running = True
        self.status_callback("running")
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def ssh_run(self, cmd, silent=False, timeout=20):
        from utils.ssh_utils import ssh_run
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=SSH_KEY, silent=silent, timeout=timeout)

    def _prepare_environment(self):
        """准备 Memtester 测试环境"""
        self.log("INFO", "准备 Memtester 测试环境...")
        
        work_dir = f"{self.remote_dir}/{self.remote_subdir}"
        exe_path = f"{work_dir}/memtester"
        
        # 场景C：可执行文件已存在，直接返回
        if self._check_remote_file(exe_path):
            self.log("INFO", "Memtester 已存在，跳过准备")
            self.ssh_run(f"chmod +x {exe_path}", silent=True)
            return True
        
        # 场景B：目录存在但可执行文件不存在 → 只chmod + 编译
        if self._check_remote_dir(work_dir):
            self.log("INFO", "Memtester 目录已存在但未编译，开始赋权并编译...")
        else:
            # 场景A：目录不存在 → 上传解压
            self.log("INFO", "Memtester 目录不存在，开始上传解压...")
            if not self._upload_and_extract_tar("memtester-4.7.1.tar.gz"):
                return False
        
        # ========== 通用步骤：给目录下所有sh文件赋权 ==========
        self.ssh_run(
            f"find {work_dir} -name '*.sh' -exec chmod +x {{}} \\; 2>/dev/null",
            silent=True
        )
        
        # ========== 编译安装 ==========
        try:
            self.ssh_run(f"cd {work_dir} && make clean", silent=False, timeout=30)
            self.ssh_run(f"cd {work_dir} && make", silent=False, timeout=60)
            try:
                self.ssh_run(f"cd {work_dir} && make install", silent=False, timeout=30)
            except Exception as e:
                self.log("WARN", f"make install 失败，使用源码目录中的可执行文件: {e}")
            
            self.ssh_run(f"chmod +x {exe_path}")
            self.log("INFO", "Memtester 编译安装完成")
            return True
        except Exception as e:
            self.log("ERROR", f"Memtester 编译安装失败: {e}")
            return False

    def _run(self):
        failed = False
        final_status = None
        server_id = self.server_info.get('bmc_ip', self.os_ip)
        base_dir = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.local_log_dir = os.path.join(base_dir, f"{timestamp}_{self.test_name}")
        os.makedirs(self.local_log_dir, exist_ok=True)
        self.log("INFO", f"日志目录: {self.local_log_dir}")
        

        try:
            self.log("INFO", "检查服务器连通性....")
            self.ssh_run("echo ok", silent=False)

            # 统一调用环境准备
            if not self._prepare_environment():
                failed = True
                return

            self.save_system_info_before_test(self.local_log_dir)

            self.backup_sel_elist_before_test(self.local_log_dir)
            
            clear_remote_logs(self.bmc_ip, self.bmc_user, self.bmc_pwd,
                              self.os_ip, self.os_user, SSH_KEY, self.log)

           

            # 后续直接使用 script_path，不再重复检查
            #script_path = f"{self.remote_dir}/{self.remote_subdir}/{self.script_name}"

            if self.test_name == "Memtester测试":
                mem_size = self.params.get("测试容量(MB)", "1024")
                loops = self.params.get("测试圈数", "1")
                args_str = f"{mem_size}M {loops}"
            else:
                args_str = " ".join([str(v) for v in self.params.values()])

            remote_work_dir = f"{self.remote_dir}/{self.remote_subdir}"
            log_file = f"{remote_work_dir}/{self.script_name}.log"
            pid_file = f"{remote_work_dir}/{self.script_name}.pid"
            cmd = f"./{self.script_name} {args_str}"

            self.current_proc = BackgroundProcess(
                host=self.os_ip,
                user=self.os_user,
                key_path=SSH_KEY,
                work_dir=remote_work_dir,
                cmd=cmd,
                log_file=log_file,
                pid_file=pid_file,
                check_interval=self.check_interval
            )
            if not self.current_proc.start():
                self.log("ERROR", "启动后台进程失败")
                failed = True
                return

            self.log("INFO", "后台进程已启动")

            def stop_check():
                return self.user_stopped

            success = self.current_proc.wait(stop_check=stop_check)
            if not success:
                self.log("WARN", "进程被用户停止或异常")
                if self.user_stopped:
                    final_status = "stopped"
                else:
                    failed = True
                    final_status = "failed"
                return

            local_log_path = os.path.join(self.local_log_dir, f"{self.script_name}.log")
            if not self.download_remote_log(log_file, local_log_path, clean=True):
                failed = True

        except Exception as e:
            self.log("ERROR", f"测试异常: {str(e)}")
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

    def stop(self):
        self.user_stopped = True
        self.running = False
        if self.current_proc:
            self.current_proc.kill()


























