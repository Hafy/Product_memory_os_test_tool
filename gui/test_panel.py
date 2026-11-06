import os
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import logging
import time
from datetime import datetime

# 从 utils 导入
from utils.constants import SSH_KEY, LOCAL_LOG_BASE, MEMORY_ERROR_PATTERNS, SCRIPT_DIR
from utils.helpers import clear_remote_logs
from utils.ssh_utils import ssh_run

# 从 executors 导入所有测试执行器
from executors.rmt import RMTTestExecutor
from executors.reboot import RebootTestExecutor
from executors.ac import ACTestExecutor
from executors.sleep import SleepTestExecutor
from executors.stressapptest import StressapptestExecutor
from executors.stream import StreamTestExecutor
from executors.mlc import MlcTestExecutor
from executors.lmbench import LmbenchTestExecutor
from executors.memory_burn import MemoryBurnTestExecutor
from executors.reboot_sat import RebootSATTestExecutor
from executors.memtester import MemtesterExecutor
from executors.dcreboot import DCRebootTestExecutor

# ===================== 统一测试面板 =====================
class TestPanel:
    # 内置测试说明（后备，当配置中无 info 时使用）
    DEFAULT_INFO_MAP = {
        "Sleep测试": "无特定目录（使用系统 rtcwake 命令）\n命令示例：rtcwake -m mem -s <睡眠秒数>",
        "MLC测试（INTEL专用）": "备注：Intel 专用\n目录：mlc_v3.11/Linux/\n命令示例：\n  - 默认测试：./mlc\n  - 空闲延迟测试：./mlc --idle_latency",
        "Stressapptest测试": "目录：SAT_V1.3/stressapptest-1.0.7_autoconf/src/\n命令示例：./stressapptest -M <内存容量MB> -s <测试秒数>",
        "Memtester测试": "目录：memtester-4.7.1\n命令示例：./memtester <测试容量M> <圈数>",
        "Reboot测试": "目录：无特定目录（使用系统 reboot 命令）\n命令：reboot",
        "Reboot+SAT测试": "目录：SAT_V1.3/stressapptest-1.0.7_autoconf/src/\n命令示例：./stressapptest -M <内存容量MB> -s <测试秒数>",
        "RMT内存测试": "备注：该测试适用于通过BIOS开启RMT开关，ipmitool捕获服务器输出的场景\n说明：该测试会在多轮重启中动态偏移内存插槽，请按界面提示操作。",
        "Memory Burn In测试": "目录：memory_burn_test%gen3_107-2\n脚本：memory_burn_test%gen3_107.sh\n说明：设置测试时间（小时）。",
        "Lmbench测试": "目录：lmbench-master\n命令：numactl -C 0 -m 0 ./lat_mem_rd -P 1 -W 5 -N 5 -t size stride\nnumactl -C 0 -m 1 ./lat_mem_rd -P 1 -W 测试次数-N 测试次数 -t size stride\nL3缓存 命令: lscpu | grep 'L3 cache' | awk '{print $3}\nsize(测试内存)请输入至少4倍L3缓存",
        "Stream测试": """目录：STREAM-master
                        编译说明：
                        1. 单线程：gcc -O3 -DSTREAM_ARRAY_SIZE={array_size} -mcmodel=large -DNTIME=20 stream.c -o streamSingleThread
                        2. 多线程：gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE={array_size} -mcmodel=large -DNTIME=20 stream.c -o streamMutipleThread

                        参数计算公式：
                        • DSTREAM_ARRAY_SIZE = int(L3_cache_MB * 1024 * 1024 * 5 * cpu_sockets / 8)
                        
                        • L3_cache_MB 获取方式：lscpu | grep 'L3 cache' | awk '{print $3}'
                            
                        • cpu_sockets 获取方式：
                            - 方法1：lscpu | grep 'Socket(s)' | awk '{print $2}'
                            - 方法2：cat /sys/devices/system/cpu/socket* 2>/dev/null | sort -u | wc -l

                        说明：array_size 由程序自动计算，点击"获取L3缓存"按钮可自动获取 L3 缓存大小""",
        "DCreboot测试": """目录：DC
                    脚本：DCreboot_test.sh
                    说明：通过 RTC 唤醒实现整机断电重启循环测试

                    测试流程：
                    1. 设置 RTC 唤醒时间（DC断电休眠后自动上电）
                    2. 等待 OS运行等待时间
                    3. 执行 shutdown -h now 断电
                    4. RTC 唤醒后自动上电
                    5. 校验内存和 dmesg
                    6. 循环直到完成

                    参数说明：
                    • OS运行等待时间（秒）：关机前系统运行时间（建议 60 秒）
                    • DC断电休眠时间（秒）：断电到下次上电的间隔时间（建议 120 秒）

                    注意：
                    ⚠️ 测试（完成/失败/强制停止）后会自动清理自启动服务（最长等待网络恢复10分钟），如果失败，根据提示自行清理！""",

        "AC电源测试": """目录：AC
                    命令：./start.sh <测试次数>

                    测试流程：
                    1. 上传 AC 测试脚本到远程服务器
                    2. 启动远程 AC 测试
                    3. 服务器关机断电
                    4. 智能插座下电（下电时间 >= 60 秒）
                    5. 智能插座上电（通电时间）
                    6. 等待系统启动
                    7. 检查内存和 dmesg
                    8. 循环直到完成

                    ⚠️ 智能插座配置要求：
                    • 下电时间：建议 120 秒以上
                    • 通电时间：测试前先进行一次AC上电，确认完整AC上电用时后，在此基础上+3分钟设置为通电时间

                    注意事项：
                    • 测试前服务器处于已开机状态
                    • 测试结束后请拔掉智能插座，恢复服务器正常上电
                    • 建议将该测试项放在测试队列最后执行
                    • 测试过程中请勿手动操作智能插座
                    """

    }

    def __init__(self, parent, server_info, platform_config, on_finish_callback,
                 status_update_callback=None, test_name_update_callback=None,
                 stop_callback=None, close_callback=None, show_continue_btn=None,
                 queue_update_callback=None, test_info_update_callback=None,
                   global_log_callback=None, memory_refresh_callback=None,
                   memory_info_update_callback=None):
        self.parent = parent
        self.server_info = server_info
        self.platform_config = platform_config
        self.on_finish_callback = on_finish_callback
        self.status_update_callback = status_update_callback
        self.test_name_update_callback = test_name_update_callback
        self.stop_callback = stop_callback
        self.close_callback = close_callback
        self.show_continue_btn = show_continue_btn
        self.queue_update_callback =queue_update_callback
        self.test_info_update_callback = test_info_update_callback
        self.global_log_callback = global_log_callback
        self.memory_refresh_callback = memory_refresh_callback
        self.memory_info_update_callback = memory_info_update_callback

        # ========== 新增：为每个服务器创建独立的文件日志 ==========
        self.server_ip = server_info.get('bmc_ip', 'unknown')
        self.logger = logging.getLogger(f"server_{self.server_ip}")
        self.logger.setLevel(logging.DEBUG)
        
        # 避免重复添加 handler（防止多次实例化同一个服务器）
        if not self.logger.handlers:
            log_dir = os.path.join(SCRIPT_DIR, "logs", "servers")
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"{self.server_ip}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)
            self.logger.propagate = False  # 不传播到 root logger，避免重复写入
            self.logger.info(f"=== 服务器 {self.server_ip} 日志文件开始 ===")

        # 任务队列相关
        self.task_queue = []          # 存储任务对象 (dict)
        self.current_queue_task = None
        self.current_executor = None
        self.queue_processing = False

        self.setup_ui()
        self.refresh_queue_display()

    def setup_ui(self):
        # 使用 PanedWindow 左右分割
        self.paned = ttk.PanedWindow(self.parent, orient=tk.HORIZONTAL)
        self.paned.pack(fill=tk.BOTH, expand=True)

        # 左侧框架（原有内容）
        self.left_frame = ttk.Frame(self.paned)
        self.paned.add(self.left_frame, weight=2)
        # 右侧框架（任务队列）
        self.right_frame = ttk.Frame(self.paned)
        self.paned.add(self.right_frame, weight=1)

        # =========== Grid权重配置 ===========
        self.left_frame.grid_columnconfigure(0, weight=1)
        # row3=日志区域，权重1，抢占剩余高度
        self.left_frame.grid_rowconfigure(3, weight=1)

        # ===== 左侧UI =====
        header_frame = ttk.Frame(self.left_frame)
        header_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        display_ip = self.server_info.get('ip', self.server_info.get('bmc_ip', 'unknown'))
        ttk.Label(header_frame, text=f"服务器: {display_ip}",
                font=("微软雅黑", 10, "bold")).pack(side="left")
        self.status_label = ttk.Label(header_frame, text="● 未开始", foreground="gray")
        self.status_label.pack(side="right", padx=10)

        test_frame = ttk.LabelFrame(self.left_frame, text="选择测试项", padding=10)
        test_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        test_frame.columnconfigure(1, weight=1)

        ttk.Label(test_frame, text="测试类型:").grid(row=0, column=0, padx=5, pady=5, sticky='w')
        self.test_var = tk.StringVar()

        # 自定义样式，消除透明看不清问题
        style = ttk.Style()
        style.configure("ClearMB.TMenubutton",
                        background="white",
                        foreground="#000000",
                        relief="solid",
                        borderwidth=1)

        self.test_combo = ttk.Menubutton(
            test_frame,
            textvariable=self.test_var,
            width=25,
            direction="below",
            style="ClearMB.TMenubutton"
        )
        self.test_menu = tk.Menu(self.test_combo, tearoff=0)

        test_items = list(self.platform_config.get('test_items', {}).keys())
        for item in test_items:
            self.test_menu.add_command(
                label=item,
                command=lambda val=item: self._handle_test_select(val)
            )
        self.test_combo.config(menu=self.test_menu)
        self.test_combo.grid(row=0, column=1, padx=5, pady=5, sticky='w')

        # 默认显示提示文字，一眼知道这里是下拉框
        self.test_var.set("请选择测试项")

        # 重点：删掉原有的 bind("<<ComboboxSelected>>")，Menubutton不支持该事件

        self.params_frame = ttk.Frame(test_frame)
        self.params_frame.grid(row=1, column=0, columnspan=2, pady=10, sticky='w')
        self.param_vars = {}

        # params_frame 已创建完成，此时再执行选中，不会报错

        # 【重点改造：测试说明改为只读滚动文本，固定高度】
        self.info_frame = ttk.LabelFrame(test_frame, text="📋 测试说明", padding=5)
        self.info_frame.grid(row=2, column=0, columnspan=2, pady=5, sticky="ew")
        test_frame.columnconfigure(1, weight=1)

        self.info_text = scrolledtext.ScrolledText(self.info_frame, wrap=tk.WORD, font=("微软雅黑",9), height=6)
        self.info_text.pack(fill="x", padx=2, pady=2)
        self.info_text.config(state="disabled") # 默认只读

        # 按钮区
        btn_frame = ttk.Frame(self.left_frame)
        btn_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=5)
        self.add_btn = ttk.Button(btn_frame, text="新增测试", command=self.add_to_queue)
        self.add_btn.pack(side="left", padx=5)
        self.close_btn = ttk.Button(btn_frame, text="关闭该选项卡", command=self.close_panel)
        self.close_btn.pack(side="left", padx=5)

        if self.show_continue_btn:
            self.continue_btn = ttk.Button(btn_frame, text="内存偏移继续测试", command=self.continue_test)
            self.continue_btn.pack(side="left", padx=5)
            self.continue_btn.config(state="disabled")

        # 测试日志
        log_frame = ttk.LabelFrame(self.left_frame, text="测试日志", padding=5)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("error", foreground="red")
        self.log_text.tag_config("alert", foreground="#b85c00", font=("微软雅黑", 10, "bold"))

        # ===== 右侧队列UI 完全不变 =====
        queue_label = ttk.Label(self.right_frame, text="测试任务队列", font=("微软雅黑", 10, "bold"))
        queue_label.pack(pady=5)
        columns = ("#", "测试项", "参数", "状态")
        self.queue_tree = ttk.Treeview(self.right_frame, columns=columns, show="headings", height=5)
        self.queue_tree.heading("#", text="序号")
        self.queue_tree.heading("测试项", text="测试项")
        self.queue_tree.heading("参数", text="参数")
        self.queue_tree.heading("状态", text="状态")
        self.queue_tree.column("#", width=50)
        self.queue_tree.column("测试项", width=120)
        self.queue_tree.column("参数", width=150)
        self.queue_tree.column("状态", width=80)
        self.queue_tree.pack(fill=tk.BOTH, expand=True, pady=5)

        queue_btn_frame = ttk.Frame(self.right_frame)
        queue_btn_frame.pack(fill=tk.X, pady=5)
        self.remove_btn = ttk.Button(queue_btn_frame, text="删除选中", command=self.remove_selected_task)
        self.remove_btn.pack(side="left", padx=5)
        self.stop_btn = ttk.Button(queue_btn_frame, text="停止测试", command=self.stop_selected_task)
        self.stop_btn.pack(side="left", padx=5)

    def _handle_test_select(self, test_name):
        self.test_var.set(test_name)
        self.on_test_selected(None)
        
    def stop_selected_task(self):
        """停止选中的正在运行的任务，并清空后续所有等待任务"""
        selected = self.queue_tree.selection()
        if not selected:
            messagebox.showwarning("警告", "请先选中一个任务")
            return
        item = selected[0]
        values = self.queue_tree.item(item, "values")
        idx = int(values[0]) - 1
        if idx >= len(self.task_queue):
            return
        task = self.task_queue[idx]
        # 只能停止正在运行的任务
        if task["status"] != "running":
            messagebox.showwarning("警告", "只能停止正在运行的任务")
            return

        # 停止当前执行器
        if self.current_executor and self.current_executor.running:
            # 1. 立即将任务状态设为“停止中”，并刷新界面
            task["status"] = "stopping"
            self.refresh_queue_display()
            if self.status_update_callback:
                self.status_update_callback("stopping")
            self.log("WARN", f"用户停止任务: {task['test_name']}")

            # 2. 立即记录停止事件到全局日志（不等收尾）
            if self.global_log_callback:
                self.global_log_callback(self.server_info.get('bmc_ip', ''), task['test_name'], 'stopped')

            # 3. 将 executor.stop() 放到后台线程，避免阻塞 GUI
            def do_stop():
                try:
                    if self.current_executor:
                        self.current_executor.stop()
                except Exception as e:
                    err_msg = str(e)
                    # 异常时回到主线程记录日志
                    self.parent.after(0, lambda msg=err_msg: self.log("ERROR", f"停止测试异常: {msg}"))
            threading.Thread(target=do_stop, daemon=True).start()

            # 4. 不清空后续 pending 任务（等收尾完成后在 on_queued_task_status 中清空）
            # 但需要清空执行器引用，避免二次操作
            # 注意：不能立即设置 current_executor = None，因为 stop() 完成后会回调 on_queued_task_status
            # 所以保留引用，等回调后再清空
        else:
            # 如果没有执行器，直接标记为 stopped
            task["status"] = "stopped"
            self.refresh_queue_display()
            if self.status_update_callback:
                self.status_update_callback("stopped")

        self.log("INFO", f"测试 {task['test_name']} 正在停止，请等待收尾完成")
        # 不立即清空后续任务，交给 on_queued_task_status 处理

    

    # ---------------------- 原有方法保留（部分需调整）---------------------
    def on_test_selected(self, event):
        # 清空参数区
        for widget in self.params_frame.winfo_children():
            widget.destroy()
        self.param_vars.clear()
        test_name = self.test_var.get()
        if not test_name:
            return

        # ================= 新增：拦截 SendCommand =================
        if test_name == "SendCommand":
            # 在参数区生成一个输入框
            ttk.Label(self.params_frame, text="输入命令:").grid(
                row=0, column=0, padx=5, pady=5, sticky="e")
            
            var = tk.StringVar(value="")
            entry = ttk.Entry(self.params_frame, textvariable=var, width=50)
            entry.grid(row=0, column=1, padx=5, pady=5, sticky="w")
            self.param_vars["命令内容"] = var
            
            # 更新说明区
            info_text = "说明：输入任意 SSH 命令（如 dmesg -T, cat /var/log/messages），执行结果将保存到当前目录的 SendCommand 文件夹下。"
            self.info_text.config(state="normal")
            self.info_text.delete(1.0, tk.END)
            self.info_text.insert(tk.END, info_text)
            self.info_text.config(state="disabled")
            return  # 拦截，不走后面的通用参数解析
        # =========================================================

        # 下面是原有的普通测试项逻辑
        test_config = self.platform_config['test_items'][test_name]
        params = test_config.get('params', [])
        for i, param in enumerate(params):
            param_name = param['name']
            ttk.Label(self.params_frame, text=param['name'] + ":").grid(
                row=i, column=0, padx=5, pady=5, sticky="e")
            if param.get('type') == 'bool':
                var = tk.BooleanVar(value=param.get('default', False))
                cb = ttk.Checkbutton(self.params_frame, variable=var)
                cb.grid(row=i, column=1, padx=5, pady=5, sticky="w")
            else:
                var = tk.StringVar(value=str(param.get('default', '')))
                entry = ttk.Entry(self.params_frame, textvariable=var, width=20)
                entry.grid(row=i, column=1, padx=5, pady=5, sticky="w")
            self.param_vars[param['name']] = var
            print(f"创建参数输入框:{param_name}, var={var}")
        
        # 更新说明信息
        info_text = test_config.get('info')
        if not info_text:
            info_text = self.DEFAULT_INFO_MAP.get(test_name, "暂无说明信息")
        
        self.info_text.config(state="normal")
        self.info_text.delete(1.0, tk.END)
        self.info_text.insert(tk.END, info_text)
        self.info_text.config(state="disabled")

        if test_name in ("Memtester测试", "Stressapptest测试", "Reboot+SAT测试"):
            btn_row = len(params)
            self.get_mem_btn = ttk.Button(self.params_frame, text="获取内存容量 (free -m)", command=self.fetch_memory_capacity)
            self.get_mem_btn.grid(row=btn_row, column=0, columnspan=2, pady=5)
        
        if test_name == "Lmbench测试":
            btn_row = len(params)
            self.get_l3_btn = ttk.Button(self.params_frame, text="获取L3缓存(M)", command=self.fetch_l3_cache)
            self.get_l3_btn.grid(row=btn_row, column=0, columnspan=2, pady=5)


    def fetch_memory_capacity(self):
        """异步获取远程服务器内存容量，避免阻塞UI"""
        # 立即禁用按钮，防止重复点击
        if hasattr(self, 'get_mem_btn'):
            self.get_mem_btn.config(state="disabled", text="获取中...")
        self.log("INFO", "正在获取远程服务器内存容量，请稍候...")
        # 启动后台线程执行实际获取操作
        threading.Thread(target=self._background_fetch_memory, daemon=True).start()

    def _background_fetch_memory(self):
        try:
            server_ip = self.server_info.get('os_ip', self.server_info.get('ip'))
            ssh_user = self.server_info.get('ssh_user', self.server_info.get('os_user'))
            os_passwd = self.server_info.get('os_passwd', None)
            cmd = "free -m | awk 'NR==2{print $7}'"
            result = ssh_run(cmd, server_ip, ssh_user, os_passwd, key_path=SSH_KEY, silent=False, timeout=15)
            if result is None or result.strip() == "":
                raise Exception("SSH 命令执行失败，无输出")
            mem_available = int(result.strip())
            # recommended = int(mem_available * 0.9)
            self.parent.after(0, lambda: self._update_memory_capacity_success(mem_available))
        except Exception as e:
            # 获取有意义的错误信息
            error_msg = str(e)
            if not error_msg or error_msg == "None":
                error_msg = "网络不通或 SSH 连接失败"
            self.parent.after(0, lambda: self._update_memory_capacity_failed(error_msg))

    def _update_memory_capacity_success(self, recommended):
        if "测试容量(MB)" in self.param_vars:
            self.param_vars["测试容量(MB)"].set(str(recommended))
        self.log("ALERT", f"当前可用内存: {recommended} MB，请按需计算后填入测试容量")
        if hasattr(self, 'get_mem_btn') and self.get_mem_btn.winfo_exists():
            self.get_mem_btn.config(state="normal", text="获取内存容量 (free -m)")

    def _update_memory_capacity_failed(self, error_msg):
        self.log("ERROR", f"获取内存容量失败: {error_msg}")
        if hasattr(self, 'get_mem_btn') and self.get_mem_btn.winfo_exists():
            self.get_mem_btn.config(state="normal", text="获取内存容量 (free -m)")

    def fetch_l3_cache(self):
        """异步获取远程服务器的 L3 缓存大小"""
        if hasattr(self, 'get_l3_btn'):
            self.get_l3_btn.config(state="disabled", text="获取中...")
        self.log("INFO", "正在获取远程服务器 L3 缓存大小...")
        threading.Thread(target=self._background_fetch_l3, daemon=True).start()

    def _parse_l3_cache_output(self, raw_output):
        """
        解析 L3 缓存输出，返回 MB 值
        
        支持格式：
        - "61440K"                → 60 MB
        - "48 MiB"                → 48 MB
        - "512M"                  → 512 MB
        - "16384K"                → 16 MB
        - "32 MiB (2 instances)"  → 32 MB
        - "61440K (4 instances)"  → 60 MB
        - "L3 缓存：32 MiB"        → 32 MB
        - "256"                   → 256 MB
        """
        import re
        
        raw_output = raw_output.strip()
        if not raw_output:
            return 0
        
        # 匹配数字和单位（K/M/G），忽略后面任何字符
        match = re.search(r'(\d+)\s*([KMG]?)(?:i?B)?', raw_output, re.IGNORECASE)
        if not match:
            return 0
        
        num = int(match.group(1))
        unit = match.group(2).upper() if match.group(2) else ""
        
        if unit == "K":
            return num // 1024
        elif unit == "G":
            return num * 1024
        else:
            # M 或没有单位
            return num
        
    def _background_fetch_l3(self):
        """异步获取 L3 缓存大小"""
        try:
            server_ip = self.server_info.get('os_ip', self.server_info.get('ip'))
            ssh_user = self.server_info.get('ssh_user', self.server_info.get('os_user'))
            ssh_password = self.server_info.get('ssh_password', None)
            
            cmd = "lscpu | grep 'L3 cache' | awk '{print $3, $4}'"
            result = ssh_run(cmd, server_ip, ssh_user, ssh_password, key_path=SSH_KEY, silent=False, timeout=30)
            
            if not result:
                # 备用：中文环境
                cmd = "lscpu | grep 'L3 缓存' | awk '{print $3, $4}'"
                result = ssh_run(cmd, server_ip, ssh_user, ssh_password, key_path=SSH_KEY, silent=False, timeout=30)
                
            if not result:
                raise Exception("无法获取 L3 缓存大小")
            
            # ========== 直接调用解析函数 ==========
            l3_mb = self._parse_l3_cache_output(result)
            
            if l3_mb > 0:
                self.parent.after(0, lambda: self._update_l3_cache(l3_mb))
            else:
                raise Exception(f"解析 L3 缓存失败: {result}")
                
        except Exception as e:
            error_msg = str(e) if str(e) else "未知错误"
            if error_msg == "None":
                error_msg = "网络不通或SSH连接失败"
            self.parent.after(0, lambda: self._update_l3_cache_failed(error_msg))

    def _update_l3_cache(self, l3_mb):
        if "测试内存(MB)" in self.param_vars:
            self.param_vars["测试内存(MB)"].set(str(l3_mb))
            self.log("ALERT", f"L3 缓存大小: {l3_mb} MB，请手动乘以4后填入")
        else:
            self.log("WARN", "未找到 '测试内存(MB)' 参数输入框")
        if hasattr(self, 'get_l3_btn') and self.get_l3_btn.winfo_exists():
            self.get_l3_btn.config(state="normal", text="获取 L3 缓存大小")

    def _update_l3_cache_failed(self, error_msg):
        self.log("ERROR", f"获取 L3 缓存失败: {error_msg}")
        if hasattr(self, 'get_l3_btn') and self.get_l3_btn.winfo_exists():
            self.get_l3_btn.config(state="normal", text="获取 L3 缓存大小")

    # ---------------------- 任务队列管理 ----------------------
    def add_to_queue(self):
        test_name = self.test_var.get()
        if not test_name:
            messagebox.showerror("错误", "请选择测试项")
            return

        # 所有测试项通用逻辑（包括 SendCommand），直接从 param_vars 取
        if test_name == "SendCommand":
            cmd_val = self.param_vars["命令内容"].get().strip()
            if not cmd_val:
                messagebox.showerror("错误", "请输入要执行的命令")
                return
            params = {"命令内容": cmd_val}
            test_config = {"type": "send_command"}
        else:
            test_config = self.platform_config['test_items'][test_name]
            params = {}
            for param in test_config.get('params', []):
                var = self.param_vars.get(param['name'])
                if var:
                    val = var.get()
                    if param.get('type') == 'int':
                        params[param['name']] = int(val)
                    elif param.get('type') == 'bool':
                        params[param['name']] = val
                    else:
                        params[param['name']] = val

        task = {
            "test_name": test_name,
            "test_config": test_config,
            "params": params,
            "status": "pending"
        }
        self.task_queue.append(task)
        self.refresh_queue_display()
        if not self.current_queue_task and not self.queue_processing:
            self.start_next_task()

    def remove_selected_task(self):
        selected = self.queue_tree.selection()
        if not selected:
            return
        item = selected[0]
        values = self.queue_tree.item(item, "values")
        idx = int(values[0]) - 1
        if idx >= len(self.task_queue):
            return
        task = self.task_queue[idx]
        if task["status"] != "pending":
            messagebox.showwarning("警告", "只能删除等待中的测试任务")
            return
        
        #if self.global_log_callback:
        #    self.global_log_callback(self.server_info.get('bmc_ip', ''), task['test_name'], 'deleted')
        del self.task_queue[idx]
        self.refresh_queue_display()

    def refresh_queue_display(self):
        # 清空右侧 Treeview 现有条目
        for item in self.queue_tree.get_children():
            self.queue_tree.delete(item)

        # 设置行高，让卡片更明显
        style = ttk.Style()
        style.configure("Treeview", rowheight=75, fieldbackground="white")
        
        # 如果当前主题不支持行背景色，可以尝试切换为 'clam' 主题（会改变整体风格）
        # 如果不想改变整体风格，可以先尝试下面的配置，在 Windows 的 'vista' 主题下通常有效
        # style.theme_use('clam')  # 启用此可确保背景色生效，但会影响整体外观

        # 配置标签样式（背景色 + 文字颜色）
        self.queue_tree.tag_configure("pending", background="#f5f5f5", foreground="#888888")
        self.queue_tree.tag_configure("running", background="#bbdefb", foreground="#0d47a1")   # 浅蓝色
        self.queue_tree.tag_configure("stopping", background="#fff3cd", foreground="#856404")  # 浅黄色
        self.queue_tree.tag_configure("passed",  background="#c8e6c9", foreground="#1b5e20")    # 深绿色，白色字体
        self.queue_tree.tag_configure("failed", background="#ff1744", foreground="#ffffff")    # 深红色
        self.queue_tree.tag_configure("stopped", background="#ffe5b4", foreground="#856404")   # 浅橙色

        # 调整参数列宽度
        self.queue_tree.column("参数", width=200)

        # 状态图标映射
        status_icon = {
            "pending": "⏳",
            "running": "🔄",
            "stopping": "⏸",
            "passed": "✅",
            "failed": "❌",
            "stopped": "⏹"
        }

        current_test = None
        queue_count = 0
        for i, task in enumerate(self.task_queue, start=1):
            # 参数换行显示
            if task["params"]:
                param_str = "\n".join([f"{k}={v}" for k, v in task["params"].items()])
            else:
                param_str = ""

            status_text = f"{status_icon.get(task['status'], '')} {task['status']}"
            values = (i, task["test_name"], param_str, status_text)
            item_id = self.queue_tree.insert("", tk.END, values=values)

            # 根据状态设置标签
            status_tag = task["status"]
            self.queue_tree.item(item_id, tags=(status_tag,))

            if status_tag == "pending":
                queue_count += 1
            elif status_tag == "running":
                current_test = task["test_name"]

        # 通知总览更新队列信息
        if self.queue_update_callback:
            display_ip = self.server_info.get('bmc_ip', 'unknown')
            self.queue_update_callback(display_ip, current_test, queue_count)

    def start_next_task(self):
        if self.queue_processing:
            return
        # 找到第一个 pending 的任务
        next_task = None
        for task in self.task_queue:
            if task["status"] == "pending":
                next_task = task
                break
        if not next_task:
            self.current_queue_task = None
            self.queue_processing = False
            return
        self.current_queue_task = next_task
        self.queue_processing = True
        next_task["status"] = "running"
        self.refresh_queue_display()
        if self.global_log_callback:
            self.global_log_callback(self.server_info.get('bmc_ip', ''), next_task['test_name'], 'start')
        self._run_queued_test(next_task)

    def _run_queued_test(self, task):
        # 通知总览更新测试项名称
        if self.test_name_update_callback:
            self.test_name_update_callback(task["test_name"])
        # 通知总览状态为running(确保总览立即显示测试中)
        if self.status_update_callback:
            self.status_update_callback("running")

        # ========== 新增：测试开始前刷新总览内存信息 ==========
        if getattr(self, "memory_refresh_callback", None):
            try:
                self.memory_refresh_callback(self.server_info.get('bmc_ip', ''))
            except Exception as e:
                self.log("WARN", f"刷新总览内存信息失败: {e}")
        # ====================================================
        
        task['status'] = "running"
        self.refresh_queue_display()

        test_type = task["test_config"].get('type', 'ssh')
        params = task["params"]
        remote_dir = self.platform_config.get('remote_dir', '/root/product_test_dir')
        remote_subdir = task["test_config"].get('remote_subdir', None)

        def status_callback(status):
            self.on_queued_task_status(task, status)

                # ... 在 _run_queued_test 里面 ...

        if test_type == 'send_command':
            from executors.send_command import SendCommandExecutor
            executor = SendCommandExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                status_callback=status_callback,
                test_info_callback=self.test_info_update_callback
            )
        elif test_type == 'rmt':
            def on_waiting_changed(waiting):
                if self.continue_btn:
                    if waiting:
                        self.continue_btn.config(state="normal")
                        self.log("ALERT", "按钮已启用，请点击【内存偏移继续测试】")
                    else:
                        self.continue_btn.config(state="disabled")
            executor = RMTTestExecutor(
                server_info=self.server_info,
                params=params,
                log_callback=self.log,
                status_callback=status_callback,
                test_info_callback=self.test_info_update_callback,
                on_waiting_changed=on_waiting_changed,
                memory_info_update_callback=self.memory_info_update_callback
            )
            #executor.task.on_waiting_status_changed = on_waiting_changed
        elif test_type == 'stream':
            executor = StreamTestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                script_name=task["test_config"].get('script', 'stream.c'),
                params=params,
                log_callback=self.log,
                status_callback=status_callback,
                test_info_callback=self.test_info_update_callback,
                check_interval=task["test_config"].get('check_interval', 300),
                remote_dir=remote_dir,
                remote_subdir=remote_subdir
            )
        elif test_type == 'mlc':
            executor = MlcTestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                status_callback=status_callback,
                test_info_callback=self.test_info_update_callback,
                remote_dir=remote_dir,
                remote_subdir=remote_subdir,
                check_interval=task["test_config"].get('check_interval', 60)
            )
        elif test_type == 'reboot':
            executor = RebootTestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                status_callback=lambda status: self.on_queued_task_status(task, status),
                test_info_callback=self.test_info_update_callback,
                remote_dir=self.platform_config.get('remote_dir', '/root/product_test_dir'),
                remote_subdir=task["test_config"].get('remote_subdir', 'Reboot'),
                check_interval=task["test_config"].get('check_interval', 60)
            )
        elif test_type == 'sleep':
            executor = SleepTestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                test_info_callback=self.test_info_update_callback,
                status_callback=lambda status: self.on_queued_task_status(task, status)
            )
        elif test_type == 'stressapptest':
            executor = StressapptestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                status_callback=lambda status: self.on_queued_task_status(task, status),
                test_info_callback=self.test_info_update_callback,
                remote_dir=self.platform_config.get('remote_dir', '/root/product_test_dir'),
                remote_subdir=task["test_config"].get('remote_subdir', 'SAT_V1.3/stressapptest-1.0.7_autoconf'),
                check_interval=task["test_config"].get('check_interval', 120)
            )
        elif test_type == 'ac':
            executor = ACTestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                status_callback=lambda status: self.on_queued_task_status(task, status),
                remote_dir=self.platform_config.get('remote_dir', '/root/product_test_dir'),
                remote_subdir=task["test_config"].get('remote_subdir', 'AC'),
                check_interval=task["test_config"].get('check_interval', 60),
                test_info_callback=self.test_info_update_callback
            )
        elif test_type == 'memory_burn':
                executor = MemoryBurnTestExecutor(
                    server_info=self.server_info,
                    test_name=task["test_name"],
                    params=params,
                    log_callback=self.log,
                    status_callback=lambda status: self.on_queued_task_status(task, status),
                    remote_dir=self.platform_config.get('remote_dir', '/root/product_test_dir'),
                    script_name=task["test_config"].get('script', 'memory_burn_test%gen3_107.sh'),
                    remote_subdir=task["test_config"].get('remote_subdir', 'memory_burn_test%gen3_107-2'),
                    check_interval=task["test_config"].get('check_interval', 30),
                    test_info_callback=self.test_info_update_callback
                )
        elif test_type == 'reboot_sat':
            executor = RebootSATTestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                status_callback=lambda status: self.on_queued_task_status(task, status),
                remote_dir=self.platform_config.get('remote_dir', '/root/product_test_dir'),
                remote_subdir=task["test_config"].get('remote_subdir', 'SAT_V1.3/stressapptest-1.0.7_autoconf'),
                check_interval=task["test_config"].get('check_interval', 30),
                test_info_callback=self.test_info_update_callback
            )
        elif test_type == 'lmbench':
            executor = LmbenchTestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                status_callback=lambda status: self.on_queued_task_status(task, status),
                remote_dir=self.platform_config.get('remote_dir', '/root/product_test_dir'),
                remote_subdir=task["test_config"].get('remote_subdir', 'lmbench-master'),
                check_interval=task["test_config"].get('check_interval', 10),
                test_info_callback=self.test_info_update_callback
            )
        elif test_type == 'memtester':
            script_name = task["test_config"].get('script')
           
            executor = MemtesterExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                script_name=script_name,
                params=params,
                log_callback=self.log,
                status_callback=status_callback,
                check_interval=task["test_config"].get('check_interval', 300),
                remote_dir=remote_dir,
                remote_subdir=remote_subdir
            )
        elif test_type == "dcreboot":
            executor = DCRebootTestExecutor(
                server_info=self.server_info,
                test_name=task["test_name"],
                params=params,
                log_callback=self.log,
                status_callback=lambda status: self.on_queued_task_status(task, status),
                remote_dir=self.platform_config.get('remote_dir', '/root/product_test_dir'),
                remote_subdir=task["test_config"].get('remote_subdir', 'DC'),
                check_interval=task["test_config"].get('check_interval', 10),
                test_info_callback=self.test_info_update_callback
            )
        self.current_executor = executor
        self.current_task = executor  # 保持兼容性

        # 设置测试信息到总览界面
        if self.test_info_update_callback and hasattr(executor, 'get_test_info_string'):
            test_info = executor.get_test_info_string()
            self.test_info_update_callback(self.server_info.get('bmc_ip', ''), test_info)
        executor.start()

    def on_queued_task_status(self, task, status):
        """任务状态回调（由执行器调用）"""
        log_msg = None
        
        if status == "finished":
            task["status"] = "passed"
            overall_status = "finished"
            #info_text = "测试完成"
            if self.global_log_callback:
                self.global_log_callback(self.server_info.get('bmc_ip', ''), task['test_name'], 'finish')
        elif status == "failed":
            task["status"] = "failed"
            overall_status = "failed"
            log_msg = f"测试 {task['test_name']} 失败，后续任务已取消"
            #info_text = "测试失败"
            if self.global_log_callback:
                self.global_log_callback(self.server_info.get('bmc_ip', ''), task['test_name'], 'failed')
        elif status == "stopped":
            # 将状态从 stopping 更新为 stopped
            if task["status"] == "stopping":
                task["status"] = "stopped"
            else:
                task["status"] = "stopped"
            overall_status = "stopped"
            #info_text = "已停止"
            # 不调用 global_log_callback（已在 stop_selected_task 中调用）
            # ========== 清空后续 pending 任务 ==========
            for i in range(len(self.task_queue) - 1, -1, -1):
                if self.task_queue[i]["status"] == "pending":
                    self.task_queue[i]["status"] = "failed"
            # =========================================
        else:
            return

        # 刷新队列显示
        self.refresh_queue_display()
        if self.status_update_callback:
            self.status_update_callback(overall_status)

        #if info_text is not None and self.test_info_update_callback:
        #    self.test_info_update_callback(self.server_info.get('bmc_ip', ''), info_text)

        # 清空执行器引用
        self.current_executor = None
        self.current_task = None
        self.queue_processing = False
        self.current_queue_task = None

        # 根据状态决定后续操作
        if status == "finished":
            self.start_next_task()
        elif status == "failed":
            # 失败时也清空后续任务（如果之前没清空）
            for t in self.task_queue:
                if t["status"] == "pending":
                    t["status"] = "failed"
            self.refresh_queue_display()
            if log_msg:
                self.log("ERROR", log_msg)
        # stopped 分支已经清空 pending，不需要额外处理
       

    # ---------------------- 其他方法（log, update_status, force_stop, continue_test等）---------------------
    def log(self, level, msg):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {msg}\n"
        self.log_text.insert(tk.END, line)
        if level == "ERROR":
            self.log_text.tag_add("error", "end-2c linestart", "end-1c")
            self.logger.error(msg)
        elif level == "ALERT":
            self.log_text.tag_add("alert", "end-2c linestart", "end-1c")
            self.logger.warning(msg)
        else:
            self.logger.info(msg)
        self.log_text.see(tk.END)

    def update_status(self, status):
        print(f"updtaing _status invoked:status={status}")
        if status == "running":
            self.status_label.config(text="● 测试中", foreground="blue")
            self.stop_btn.config(state="normal")
            self.close_btn.config(state="disabled")
            if self.status_update_callback:
                self.status_update_callback("running")
        elif status == "finished":
            self.status_label.config(text="● 测试结束", foreground="green")
            self.stop_btn.config(state="disabled")
            self.close_btn.config(state="normal")
            if self.on_finish_callback:
                self.on_finish_callback()
            if self.status_update_callback:
                self.status_update_callback("finished")
        elif status == "failed":
            self.status_label.config(text="● 测试失败", foreground="red")
            self.stop_btn.config(state="disabled")
            self.close_btn.config(state="normal")
            if self.on_finish_callback:
                self.on_finish_callback()
            if self.status_update_callback:
                self.status_update_callback("failed")
        elif status == "stopped":
            self.status_label.config(text="● 已停止", foreground="red")
            self.stop_btn.config(state="disabled")
            self.close_btn.config(state="normal")
            if self.status_update_callback:
                self.status_update_callback("stopped")

    def force_stop(self):
        if self.current_executor and self.current_executor.running:
            self.current_executor.stop()
            self.log("WARN", "强制停止测试")
            # 将当前任务状态设为 failed
            if self.current_queue_task:
                self.current_queue_task["status"] = "failed"
            # 清空后续 pending 任务
            for t in self.task_queue:
                if t["status"] == "pending":
                    t["status"] = "failed"
            self.refresh_queue_display()
            self.current_queue_task = None
            self.queue_processing = False
            self.current_executor = None
            self.current_task = None
            if self.stop_callback:
                self.stop_callback()

    def continue_test(self):
        if self.current_executor and hasattr(self.current_executor, 'task') and self.current_executor.task:
            if self.current_executor.task.waiting_for_continue:
                self.current_executor.task.continue_event.set()
                self.log("INFO", "已发送继续信号")
            else:
                self.log("WARN", "当前未处于等待状态，继续信号无效")
        else:
            self.log("WARN", "当前测试无需继续（或未在阻塞状态）")

    def close_panel(self):
        if self.current_executor and getattr(self.current_executor, 'running', False):
            if not messagebox.askyesno("确认关闭", "测试正在运行，关闭选项卡将停止测试，确定继续吗？"):
                return
            self.force_stop()
        else:
            if not messagebox.askyesno("确认关闭", "确定要关闭此选项卡吗？"):
                return
        if self.close_callback:
            self.close_callback()

    def _poll_continue_status(self):
        # 原样保留
        print(f"[DEBUG] _poll_continue_status被调用, running={self.current_executor.running if self.current_executor else None}")
        if not self.current_executor or not getattr(self.current_executor, 'running', False):
            return
        if hasattr(self, 'continue_btn'):
            waiting = False
            if hasattr(self.current_executor, 'task') and self.current_executor.task:
                waiting = getattr(self.current_executor.task, 'waiting_for_continue', False)
            new_state = "normal" if waiting else "disabled"
            if self.continue_btn['state'] != new_state:
                self.continue_btn.config(state=new_state)
        if self.current_executor and getattr(self.current_executor, 'running', False):
            self.continue_btn.after(1000, self._poll_continue_status)