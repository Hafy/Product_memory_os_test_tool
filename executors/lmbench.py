import os
import time
import threading
import re
from datetime import datetime
from .base import PrepareMixin, RebootCheckMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE, SCRIPT_DIR
from utils.helpers import clear_remote_logs
from utils.background_process import BackgroundProcess

class LmbenchTestExecutor(PrepareMixin, RebootCheckMixin):
    def __init__(self, server_info, test_name, params, log_callback, status_callback,
                 remote_dir, remote_subdir, check_interval=10, test_info_callback=None):
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

        self.remote_base_dir = f"{remote_dir}/{remote_subdir}"
        self.remote_src_dir = f"{self.remote_base_dir}/src"
        self.remote_bin_dir = f"{self.remote_base_dir}/bin"
        self.lat_mem_rd_path = None

        self.test_count = self.params.get("测试次数", 5)
        self.size_mb = self.params.get("测试内存(MB)", 0)
        self.stride = self.params.get("测试步长(Bytes)", 64)
        self.current_proc = None

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
        self.log("INFO", "用户停止 lmbench 测试，正在终止后台进程...")
        if self.current_proc:
            self.current_proc.kill()
        try:
            self.ssh_run("pkill -9 lat_mem_rd", silent=True)
        except:
            pass

    def _prepare_enviroment(self):
        """准备 lmbench 环境（使用预编译包，不编译）"""
        self.log("INFO", "检查 lmbench 环境...")
        
        # 1. 检查是否已存在 lat_mem_rd
        find_cmd = f"find {self.remote_base_dir} -name lat_mem_rd -type f 2>/dev/null | head -1"
        result = self.ssh_run(find_cmd, silent=True).strip()
        if result:
            self.lat_mem_rd_path = result
            self.log("INFO", f"lmbench 已存在: {self.lat_mem_rd_path}")
            return True
        
        self.log("INFO", "lmbench 不存在，开始部署...")
        
        # 2. 获取远程架构
        arch = self.ssh_run("uname -m", silent=True).strip()
        self.log("INFO", f"远程服务器架构: {arch}")
        
        # 3. 根据架构选择包名
        if arch == "x86_64":
            package_name = "lmbench_lat_mem_rd_x86_64.tar.gz"
        elif arch in ("aarch64", "arm64", "armv8l"):
            package_name = "lmbench_lat_mem_rd_arm64.tar.gz"
        else:
            self.log("ERROR", f"不支持的架构: {arch}")
            return False
        
        # 4. 检查本地压缩包
        local_tar = os.path.join(SCRIPT_DIR, "product_test_dir", package_name)
        if not os.path.exists(local_tar):
            self.log("ERROR", f"本地压缩包不存在: {local_tar}")
            return False
        
        # 5. 上传并解压
        try:
            self.ssh_run(f"mkdir -p {self.remote_base_dir}", silent=True)
            remote_tar = f"/tmp/{package_name}"
            self._scp_upload(local_tar, remote_tar)
            
            # ========== 修复：加 --no-same-owner ==========
            self.ssh_run(f"cd {self.remote_base_dir} && tar -xzf {remote_tar} --no-same-owner", timeout=30)
            self.ssh_run(f"rm -f {remote_tar}", silent=True)
            self.ssh_run(f"chmod +x {self.remote_base_dir}/lat_mem_rd", silent=True)
            
            self.lat_mem_rd_path = f"{self.remote_base_dir}/lat_mem_rd"
            self.log("INFO", f"lmbench 部署完成: {self.lat_mem_rd_path}")
            return True
            
        except Exception as e:
            self.log("ERROR", f"部署 lmbench 失败: {e}")
            return False

    def _run_single_test(self, node, iteration, size_mb, stride):
        """运行一次 lat_mem_rd 测试，返回输出字符串"""
        raw_cmd = f"numactl -C 0 -m {node} {self.lat_mem_rd_path} -P 1 -W 5 -N 5 -t {size_mb} {stride}"
        cmd = f"bash -c '{raw_cmd} 2>&1'"
        self.log("INFO", f"节点 {node} 第 {iteration+1} 次测试: {cmd}")
        
        output_file = os.path.join(self.local_log_dir, f"lat_mem_rd_node{node}.txt")
        
        try:
            # silent=True 让 ssh_run 不抛异常，我们自己处理输出
            out = self.ssh_run(cmd, silent=True, timeout=300)
            
            # 检查输出是否包含错误关键字
            error_keywords = [
                "command not found",
                "out of range",
                "invalid",
                "Error",
                "error",
                "failed",
                "Warning"
            ]
            
            has_error = any(kw.lower() in out.lower() for kw in error_keywords)
            
            if has_error:
                # 有错误，保存错误日志
                with open(output_file, 'a', encoding='utf-8') as f:
                    f.write(f"\n=== 错误: 第 {iteration+1} 次测试 ===\n")
                    f.write(f"命令: {cmd}\n")
                    f.write(out)
                    f.write("\n")
                self.log("ERROR", f"节点 {node} 第 {iteration+1} 次测试失败: {out}")
                return None
            
            # 正常输出
            self.log("INFO", f"节点 {node} 第 {iteration+1} 次测试完成")
            return out
            
        except Exception as e:
            # SSH 连接异常等
            error_msg = str(e)
            self.log("ERROR", f"节点 {node} 第 {iteration+1} 次测试异常: {error_msg}")
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(f"\n=== 错误: 第 {iteration+1} 次测试 ===\n")
                f.write(f"命令: {cmd}\n")
                f.write(f"错误: {error_msg}\n")
                f.write("\n")
            return None

    def _extract_last_value_per_iteration(self, file_path):
        """从原始输出文件中提取每个迭代块最后一行最右边的数值"""
        values = []
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        except Exception as e:
            self.log("WARN", f"读取文件失败 {file_path}: {e}")
            return values

        in_block = False
        current_block = []
        for line in lines:
            if line.startswith('=== Iteration'):
                if in_block and current_block:
                    last_non_empty = None
                    for l in reversed(current_block):
                        if l.strip():
                            last_non_empty = l.strip()
                            break
                    if last_non_empty:
                        numbers = re.findall(r'(\d+\.?\d*)', last_non_empty)
                        if numbers:
                            values.append(float(numbers[-1]))
                        else:
                            values.append(None)
                    else:
                        values.append(None)
                    current_block = []
                in_block = True
                current_block.append(line)
            elif in_block:
                current_block.append(line)

        if in_block and current_block:
            last_non_empty = None
            for l in reversed(current_block):
                if l.strip():
                    last_non_empty = l.strip()
                    break
            if last_non_empty:
                numbers = re.findall(r'(\d+\.?\d*)', last_non_empty)
                if numbers:
                    values.append(float(numbers[-1]))
                else:
                    values.append(None)
            else:
                values.append(None)
        return values

    def _generate_excel_report(self):
        """从原始输出文件生成 Excel 报告（包含每次测试数值和平均值）"""
        try:
            import pandas as pd
        except ImportError:
            self.log("WARN", "pandas 未安装，无法生成 Excel，跳过")
            return

        node0_file = os.path.join(self.local_log_dir, "lat_mem_rd_node0.txt")
        node1_file = os.path.join(self.local_log_dir, "lat_mem_rd_node1.txt")

        if not os.path.exists(node0_file) or not os.path.exists(node1_file):
            self.log("WARN", "原始输出文件不存在，跳过 Excel 生成")
            return

        node0_vals = self._extract_last_value_per_iteration(node0_file)
        node1_vals = self._extract_last_value_per_iteration(node1_file)

        if not node0_vals or not node1_vals:
            self.log("WARN", "未提取到有效数据，跳过 Excel 生成")
            return

        min_len = min(len(node0_vals), len(node1_vals))
        if min_len == 0:
            self.log("WARN", "数据长度为零，跳过 Excel 生成")
            return

        node0_vals = node0_vals[:min_len]
        node1_vals = node1_vals[:min_len]

        data = {
            "测试次数": [f"第{i+1}次" for i in range(min_len)],
            "numactl 0-0": node0_vals,
            "numactl 0-1": node1_vals
        }
        df = pd.DataFrame(data)

        avg_node0 = df["numactl 0-0"].mean(skipna=True)
        avg_node1 = df["numactl 0-1"].mean(skipna=True)
        avg_row = {"测试次数": "平均值", "numactl 0-0": avg_node0, "numactl 0-1": avg_node1}
        df = pd.concat([df, pd.DataFrame([avg_row])], ignore_index=True)

        excel_path = os.path.join(self.local_log_dir, "lmbench_results.xlsx")
        try:
            df.to_excel(excel_path, index=False, sheet_name="lat_mem_rd")
            self.log("INFO", f"Excel 报告已生成: {excel_path}")
        except Exception as e:
            self.log("ERROR", f"生成 Excel 失败: {e}")


    def _run(self):
        failed = False
        try:
            self.log("INFO", "========== lmbench lat_mem_rd 测试开始 ==========")
           
            server_id = self.bmc_ip if self.bmc_ip else self.os_ip
            base_local = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.local_log_dir = os.path.join(base_local, f"{timestamp}_{self.test_name}")
            os.makedirs(self.local_log_dir, exist_ok=True)
            self.log("INFO", f"结果保存目录: {self.local_log_dir}")

            self.log("INFO", "检查服务器连通性...")
            self.ssh_run("echo ok", silent=False, timeout=10)

            if not self._prepare_enviroment():
                failed = True
                return

            self.save_system_info_before_test(self.local_log_dir)

            # 备份SEL ELIST
            self.backup_sel_elist_before_test(self.local_log_dir)

            clear_remote_logs(
                self.bmc_ip, self.bmc_user, self.bmc_pwd,
                self.os_ip, self.os_user, self.ssh_key, self.log
            )

    
            # 2. 获取 L3 缓存
            l3 = self.get_l3_cache_mb()
            if l3 == 0:
                self.log("ERROR", "无法获取 L3 缓存大小，请检查服务器 lscpu 命令")
                failed = True
                return
            min_size = 4 * l3
            self.log("INFO", f"size:{self.size_mb}")
            if self.size_mb < min_size:
                self.log("ERROR", f"测试内存 {self.size_mb} MB 小于 L3 缓存的 4 倍 ({min_size} MB)，测试可能不准确，请重新给定测试参数！！")
                failed = True
                return
            else:
                self.log("INFO", f"使用用户指定的 size = {self.size_mb} MB")
            stride = self.stride

            if self.test_info_callback:
                test_info = f"Start: {self.start_time}, size={self.size_mb}MB stride={stride}"
                self.test_info_callback(self.server_info.get('bmc_ip', ''), test_info)

            local_file_node0 = os.path.join(self.local_log_dir, "lat_mem_rd_node0.txt")
            local_file_node1 = os.path.join(self.local_log_dir, "lat_mem_rd_node1.txt")
            with open(local_file_node0, 'w') as f0, open(local_file_node1, 'w') as f1:
                f0.write(f"# lat_mem_rd test on node 0, size={self.size_mb}MB, stride={stride}, iterations={self.test_count}\n")
                f1.write(f"# lat_mem_rd test on node 1, size={self.size_mb}MB, stride={stride}, iterations={self.test_count}\n")

            for i in range(self.test_count):
                if self.user_stopped:
                    self.log("WARN", "用户停止测试")
                    break

                out0 = self._run_single_test(0, i, self.size_mb, stride)
                if out0 is None:
                    failed = True
                    break
                with open(local_file_node0, 'a') as f:
                    f.write(f"=== Iteration {i+1} ===\n{out0}\n")

                if self.user_stopped:
                    break

                out1 = self._run_single_test(1, i, self.size_mb, stride)
                if out1 is None:
                    failed = True
                    break
                with open(local_file_node1, 'a') as f:
                    f.write(f"=== Iteration {i+1} ===\n{out1}\n")

                self.log("INFO", f"第 {i+1} 轮完成")

            if self.user_stopped:
                self.log("WARN", "用户停止测试")
            else:
                self.log("INFO", "将测试结果保存到EXCEL文件中...")
                self._generate_excel_report()

        except InterruptedError:
            self.log("WARN", "用户停止测试")
            self.user_stopped = True
        except Exception as e:
            self.log("ERROR", f"测试异常: {e}")
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
            self.status_callback(final_status)
