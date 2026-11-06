import os
import threading
import re
from datetime import datetime
from .base import PrepareMixin, RebootCheckMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE
from utils.helpers import clear_remote_logs

class StreamTestExecutor(PrepareMixin, RebootCheckMixin):
    def __init__(self, server_info, test_name, script_name, params, log_callback, status_callback,
                 remote_dir, remote_subdir, check_interval=60, test_info_callback=None):
        self.server_info = server_info
        self.test_name = test_name
        self.script_name = script_name
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

        self.bmc_ip = server_info.get('bmc_ip')
        self.bmc_user = server_info.get('bmc_user')
        self.bmc_pwd = server_info.get('bmc_pwd')
        self.os_ip = server_info.get('os_ip')
        self.os_user = server_info.get('os_user')
        self.ssh_key = SSH_KEY
        self.os_passwd = server_info.get('os_passwd')

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
        self.running = False
        self.ssh_run("pkill -f streamSingleThread; pkill -f streamMutipleThread", silent=True, timeout=5)
        self.log("INFO", "已发送停止信号")

    def _get_cpu_info(self):
        self.log("INFO", "正在获取远程服务器 CPU 信息...")
        l3_mb = self.get_l3_cache_mb()
        sockets = self.get_cpu_sockets()
        
        if l3_mb == 0:
            self.log("ERROR", "无法获取 L3 缓存大小，Stream 测试无法继续")
            self.log("ERROR", "请检查服务器是否支持 lscpu 命令")
            raise RuntimeError("无法获取 L3 缓存大小")
        
        self.log("INFO", f"L3 缓存: {l3_mb} MB, CPU路数: {sockets}")
        return l3_mb, sockets

    def _file_exists(self, remote_path):
        try:
            return self.ssh_run(f"test -f {remote_path} && echo 'exists'", silent=True, timeout=5).strip() == "exists"
        except:
            return False

    def _parse_stream_output(self, output):
        lines = output.split('\n')
        result = {}
        for line in lines:
            match = re.match(r'\s*(Copy|Scale|Add|Triad):\s+([\d.]+)', line)
            if match:
                result[match.group(1)] = float(match.group(2))
        return result if len(result) == 4 else None

    def _save_to_excel(self, single_results, multi_results, excel_path):
        try:
            import pandas as pd
        except ImportError:
            self.log("WARN", "未安装 pandas，无法生成 Excel 文件，将保存为 CSV")
            csv_path = excel_path.replace('.xlsx', '.csv')
            # 简化处理
            return
        if single_results:
            single_df = pd.DataFrame(single_results)
            single_df.index = [f"{i}st" for i in range(1, len(single_results)+1)]
            single_mean = single_df.mean()
            single_df.loc['均值'] = single_mean
        else:
            single_df = pd.DataFrame(columns=['Copy', 'Scale', 'Add', 'Triad'])
        if multi_results:
            multi_df = pd.DataFrame(multi_results)
            multi_df.index = [f"{i}st" for i in range(1, len(multi_results)+1)]
            multi_mean = multi_df.mean()
            multi_df.loc['均值'] = multi_mean
        else:
            multi_df = pd.DataFrame(columns=['Copy', 'Scale', 'Add', 'Triad'])
        concat_df = pd.concat([single_df, multi_df], axis=1, keys=['Single Threads(MB/s)', 'Multi Threads(MB/s)'])
        with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
            concat_df.to_excel(writer, sheet_name='Stream结果')
        self.log("INFO", f"结果已保存至 Excel: {excel_path}")

    def _prepare_environment(self):
        """准备 Stream 测试环境"""
        self.log("INFO", "准备 Stream 测试环境...")
        
        work_dir = f"{self.remote_dir}/{self.remote_subdir}"
        
        if self._check_remote_dir(work_dir):
            self.log("INFO", f"Stream 目录已存在: {work_dir}")
            return True
        
        # 上传并解压
        if not self._upload_and_extract_tar("STREAM-master.tar.gz"):
            return False
        
        # ========== 清理旧文件（如果有） ==========
        self.ssh_run(f"cd {work_dir} && make clean 2>/dev/null || true", silent=True)
        
        self.log("INFO", "Stream 环境准备完成")
        return True

    def _run(self):
        failed = False
        final_status = None
        try:
            self.log("INFO", "========== Stream 测试开始 ==========")
            base_dir = os.path.join(LOCAL_LOG_BASE, self.server_info.get('bmc_ip'), self.test_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            result_dir = os.path.join(base_dir, f"{timestamp}_{self.test_name}")
            self.local_log_dir = result_dir

            os.makedirs(result_dir, exist_ok=True)
            self.log("ALERT", f"测试结果保存到: {result_dir}")

            self.log("INFO", "检查服务器连通性...")
            self.ssh_run("echo ok", silent=False, timeout=10)

            # ========== 统一环境准备 ==========
            if not self._prepare_environment():
                failed = True
                return

            self.save_system_info_before_test(result_dir)
            self.backup_sel_elist_before_test(self.local_log_dir)
            clear_remote_logs(self.bmc_ip, self.bmc_user, self.bmc_pwd,
                              self.os_ip, self.os_user, SSH_KEY, self.log)

            self.log("INFO", " 获取 CPU 信息...")
            l3_mb, sockets = self._get_cpu_info()
            array_size = int(l3_mb * 1024 * 1024 * 5 * sockets / 8)
            self.log("INFO", f"计算得到 DSTREAM_ARRAY_SIZE = {array_size}")
      
            base_dir = os.path.join(self.remote_dir, self.remote_subdir)

            self.log("INFO", " 检查并编译单线程 Stream...")
            single_bin = f"{base_dir}/streamSingleThread"
            try:
                self.ssh_run(f"rm -f {single_bin}")
                compile_cmd = f"cd {base_dir} && gcc -O3 -DSTREAM_ARRAY_SIZE={array_size} -mcmodel=large -DNTIME=20 stream.c -o streamSingleThread"
                self.ssh_run(compile_cmd, silent=False, timeout=300)
                self.log("INFO", "单线程编译完成")
            except Exception as e:
                self.log("ERROR", f"单线程编译失败: {e}")
                failed = True
                return

            self.log("INFO", "步骤5: 检查并编译多线程 Stream...")
            multi_bin = f"{base_dir}/streamMutipleThread"
            try:
                self.ssh_run(f"rm -f {multi_bin}")
                compile_cmd = f"cd {base_dir} && gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE={array_size} -mcmodel=large -DNTIME=20 stream.c -o streamMutipleThread"
                self.ssh_run(compile_cmd, silent=False, timeout=300)
                self.log("INFO", "多线程编译完成")
            except Exception as e:
                self.log("ERROR", f"多线程编译失败: {e}")
                failed = True
                return

            single_result_file = os.path.join(result_dir, f"stream_single_{timestamp}.txt")
            multi_result_file = os.path.join(result_dir, f"stream_multi_{timestamp}.txt")
            excel_file = os.path.join(result_dir, f"stream_results_{timestamp}.xlsx")

            single_count = self.params.get("单线程测试次数", 3)
            multi_count = self.params.get("多线程测试次数", 3)
            self.log("INFO", f"步骤6: 开始单线程测试，共 {single_count} 次")
            single_results = []
            for i in range(1, single_count+1):
                if self.user_stopped:
                    self.log("WARN", "用户停止测试，终止执行")
                    break
                self.log("INFO", f"执行第 {i} 次单线程测试...")
                try:
                    output = self.ssh_run(single_bin, silent=False, timeout=3600)
                    values = self._parse_stream_output(output)
                    if values:
                        single_results.append(values)
                        with open(single_result_file, "a", encoding="utf-8") as f:
                            f.write(f"第{i}次: Copy={values['Copy']}, Scale={values['Scale']}, Add={values['Add']}, Triad={values['Triad']}\n")
                        self.log("INFO", f"第{i}次结果: {values}")
                    else:
                        self.log("WARN", f"第{i}次结果解析失败")
                        return
                except Exception as e:
                    self.log("ERROR", f"第{i}次测试失败: {e}")
                    failed = True
                    return

            self.log("INFO", f"步骤7: 开始多线程测试，共 {multi_count} 次")
            multi_results = []
            for i in range(1, multi_count+1):
                if self.user_stopped:
                    self.log("WARN", "用户停止测试，终止执行")
                    break
                self.log("INFO", f"执行第 {i} 次多线程测试...")
                try:
                    output = self.ssh_run(multi_bin, silent=False, timeout=3600)
                    values = self._parse_stream_output(output)
                    if values:
                        multi_results.append(values)
                        with open(multi_result_file, "a", encoding="utf-8") as f:
                            f.write(f"第{i}次: Copy={values['Copy']}, Scale={values['Scale']}, Add={values['Add']}, Triad={values['Triad']}\n")
                        self.log("INFO", f"第{i}次结果: {values}")
                    else:
                        self.log("WARN", f"第{i}次结果解析失败")
                        failed = True
                        return
                except Exception as e:
                    self.log("ERROR", f"第{i}次测试失败: {e}")
                    failed = True
                    return

            self.log("INFO", "步骤8: 保存测试结果到 Excel...")
            self._save_to_excel(single_results, multi_results, excel_file)

        except Exception as e:
            self.log("ERROR", f"Stream 测试异常: {str(e)}")
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
