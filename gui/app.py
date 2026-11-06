import os
import sys
import json
import time
import threading
import logging
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
from datetime import datetime

# 从 utils 导入
from utils.constants import (
    SCRIPT_DIR, LOCAL_LOG_BASE, SSH_KEY,
    ICO_PATH, MEMORY_ERROR_PATTERNS, SSH_TIMEOUT, get_app_root, get_resource_path
)
from utils.helpers import (
    setup_logging,
    clean_remote_log, clear_remote_logs
)
from utils.ssh_utils import ssh_run

# 从 gui 导入
from gui.test_panel import TestPanel
from gui.slice_tool import SliceToolPanel
from gui.add_server_dialog import AddServerDialog
from utils.dimm_inspector import DIMMInspector

import tkinter as tk
from tkinter import ttk

import tkinter as tk
from tkinter import ttk

import tkinter as tk
from tkinter import ttk

import tkinter as tk
from tkinter import ttk

class ScrollableNotebook(ttk.Frame):
    def __init__(self, parent, wheelscroll=True, **kwargs):
        super().__init__(parent,** kwargs)
        self.wheelscroll = wheelscroll

        # 整体垂直布局：标签栏 → 滚动条 → 内容区
        self.tab_bar = ttk.Frame(self)
        self.tab_bar.pack(fill="x", anchor="n")

        self.canvas = tk.Canvas(self.tab_bar, height=34, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.tab_frame = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.tab_frame, anchor="nw")

        # 滚动条：标签栏正下方
        self.scroll_x = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=self.scroll_x.set)

        # 内容区域
        self.content_frame = ttk.Frame(self)
        self.content_frame.pack(fill="both", expand=True)

        self._tab_map = {}
        self.current_tab = None

        self.tab_frame.bind("<Configure>", self._update_scroll)
        if self.wheelscroll:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _update_scroll(self, event=None):
        self.canvas.update_idletasks()
        bbox = self.canvas.bbox("all")
        if bbox:
            self.canvas.configure(scrollregion=bbox)
            # 标签超出宽度 → 显示滚动条，放在标签栏下方
            if bbox[2] > self.canvas.winfo_width():
                self.scroll_x.pack(fill="x", before=self.content_frame)
            else:
                self.scroll_x.pack_forget()

    def _on_wheel(self, event):
        delta = int(-event.delta / 120)
        self.canvas.xview_scroll(delta, "units")

    def add(self, frame, text):
        btn = ttk.Button(self.tab_frame, text=text, command=lambda f=frame: self.select_tab(f))
        btn.pack(side="left", padx=3)
        self._tab_map[frame] = btn
        if self.current_tab is None:
            self.select_tab(frame)
        self._update_scroll()

    def select_tab(self, frame):
        if self.current_tab:
            self.current_tab.pack_forget()
        frame.pack(in_=self.content_frame, fill="both", expand=True)
        self.current_tab = frame

    def forget(self, frame):
        if frame not in self._tab_map:
            return
        btn = self._tab_map.pop(frame)
        btn.destroy()
        frame.pack_forget()
        if self.current_tab == frame and self._tab_map:
            first = list(self._tab_map.keys())[0]
            self.select_tab(first)
        self._update_scroll()

    def get_tab_frames(self):
        return list(self._tab_map.keys())

    def get_tab_text(self, frame):
        if frame in self._tab_map:
            return self._tab_map[frame]["text"]
        return ""


class RMTTestApp:
    def __init__(self, root):
        self.root = root
        self.root.title("自动化测试平台")
        self.root.geometry("1300x800")

        # 初始化日志
        self.log_file = setup_logging()
        print(f"日志文件保存位置: {self.log_file}")

        self.load_config()
        self.setup_ui()
        # 存储以获取的内存信息
        self.server_memory_info = {}
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_ui(self):
        # 平台选择栏
        top_frame = ttk.Frame(self.root)
        top_frame.pack(fill="x", padx=10, pady=5)
        ttk.Label(top_frame, text="选择平台:", font=("微软雅黑", 10, "bold")).pack(side="left", padx=5)
        self.platform_var = tk.StringVar()
        platform_combo = ttk.Combobox(top_frame, textvariable=self.platform_var,
                                    state="readonly", width=20)
        platform_combo['values'] = list(self.config.keys())
        platform_combo.bind("<<ComboboxSelected>>", self.on_platform_change)
        platform_combo.pack(side="left", padx=5)

        self.add_server_btn = ttk.Button(top_frame, text="➕ 新增服务器", command=self.open_add_server_dialog)
        self.add_server_btn.pack(side="left", padx=10)

        # 服务器选择区
        self.server_frame = ttk.LabelFrame(self.root, text="选择服务器", padding=10)
        self.server_frame.pack(fill="x", padx=10, pady=5)
        self.server_check_vars = {}

        # 按钮区
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=5)
        ttk.Button(btn_frame, text="添加测试面板", command=self.add_test_panels).pack(side="left", padx=5)

        # ========== 可横向滚动 Notebook 【核心改动】 ==========
        self.notebook = ScrollableNotebook(self.root, wheelscroll=True)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=5)

        # ========== 总览选项卡 ==========
        self.overview_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.overview_tab, text="📊 总览")
    
        self.setup_overview_tab()

        # ========== 日志分析工具选项卡 ==========
        self.slice_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.slice_tab, text="🔪 日志分析工具")
        self.slice_tool = SliceToolPanel(self.slice_tab)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w").pack(fill="x", padx=10, pady=2)

        # 存储测试任务信息
        self.test_tasks = {}

        # 默认加载第一个平台
        if list(self.config.keys()):
            self.platform_var.set(list(self.config.keys())[0])
            self.on_platform_change()

        
    def on_closing(self):
        """窗口关闭时的确认对话框"""
        if messagebox.askyesno("退出确认", "确定要退出程序吗？"):
            # 【必要修复】退出前停止所有测试，防止后台线程残留
            for task_data in self.test_tasks.values():
                panel = task_data.get("panel")
                if panel:
                    try:
                        panel.force_stop()
                    except Exception:
                        pass
            self.root.destroy()

    def load_config(self):
        config_path = os.path.join(get_app_root(), "servers.json")
        print(f"servers.json path: {config_path}")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
            print(self.config['通用平台']['test_items'].keys())
        except Exception as e:
            logging.error(f"加载配置失败 {e}")
            messagebox.showerror("配置文件错误", f"无法读取servers.json:\n{e}")
            self.config = {}


    def on_server_switch(self, event=None):
        """切换服务器时，显示对应的测试面板"""
        selected = self.server_switch_var.get()
        if not selected:
            return
        
        # 提取 IP（下拉菜单显示的是 "IP - 测试项" 格式）
        # 如果直接存的是 IP
        display_ip = selected
        
        # 先隐藏所有服务器面板
        for ip, panel_frame in self.server_panels.items():
            if ip == display_ip:
                panel_frame.pack(fill="both", expand=True)
            else:
                panel_frame.pack_forget()
        
        # 更新状态栏
        self.status_var.set(f"当前服务器: {display_ip}")

    def log_global_event(self, server_ip, test_name, event_type, reason=""):
        """记录全局测试事件
        event_type: 'start', 'finish', 'failed', 'stopped', 'deleted'
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        icon_map = {
            'start': '▶',
            'finish': '✅',
            'failed': '❌',
            'stopped': '⏹',
            'deleted': '🗑'
        }
        text_map = {
            'start': '开始测试',
            'finish': '测试完成',
            'failed': '测试失败',
            'stopped': '用户停止',
            'deleted': '测试删除'
        }
        if event_type == 'failed' and reason:
            msg = f"[{timestamp}] {icon_map[event_type]} {text_map[event_type]} 服务器{server_ip} {test_name} 原因：{reason}"
        else:
            msg = f"[{timestamp}] {icon_map[event_type]} {text_map[event_type]} 服务器{server_ip} {test_name}"
        self.global_log_text.insert(tk.END, msg + "\n", event_type)
        self.global_log_text.see(tk.END)

    def fetch_memory_info(self, server_info):
        """通过 SSH 获取物理内存条信息，返回格式化的字符串
        格式：数量 | 单根容量 | 频率 | 类型
        示例：8pcs | 64GB | 5600MT/s | DDR5
        """
        os_ip = server_info.get('os_ip')
        os_user = server_info.get('os_user')

        if not os_ip or not os_user:
            return ""

        try:
            output = ssh_run("dmidecode -t memory 2>/dev/null",
                             os_ip, os_user, key_path=SSH_KEY, silent=True, timeout=30)
            if not output or "Memory Device" not in output:
                output = ssh_run("sudo dmidecode -t memory 2>/dev/null",
                                 os_ip, os_user, key_path=SSH_KEY, silent=True, timeout=30)
            if not output or "Memory Device" not in output:
                return ""

            dimms = DIMMInspector.parse(output)
            if not dimms:
                return ""

            return self.format_dimm_summary(dimms)
        except Exception as e:
            logging.warning(f"dmidecode 执行失败 {os_ip}: {e}")
            return ""

    
   
    def format_dimm_summary(self, dimms):
        """把 DIMMInfo 列表格式化为总览显示字符串
        示例：8pcs | 64GB | 5600MT/s | DDR5
        多根规格不同时显示范围：8pcs | 32-64GB | 4800-5600MT/s | DDR5
        """
        count = len(dimms)
        sizes = sorted({d.size_gb for d in dimms})
        speeds = sorted({d.speed_mts for d in dimms if d.speed_mts})
        types = sorted({d.mem_type for d in dimms if d.mem_type})

        # 容量
        if not sizes:
            size_str = "?"
        elif len(sizes) == 1:
            size_str = f"{sizes[0]}GB"
        else:
            size_str = f"{sizes[0]}-{sizes[-1]}GB"

        # 频率（用 Speed 字段）
        if not speeds:
            speed_str = "?"
        elif len(speeds) == 1:
            speed_str = f"{speeds[0]}MT/s"
        else:
            speed_str = f"{speeds[0]}-{speeds[-1]}MT/s"

        # 类型
        if not types:
            type_str = "?"
        elif len(types) == 1:
            type_str = types[0]
        else:
            type_str = "/".join(types)

        return f"{count}pcs | {size_str} | {speed_str} | {type_str}"
    
    def start_memory_info_fetch(self, display_ip):
        """启动异步获取内存信息（如果之前获取失败则重新获取）"""
        existing = self.server_memory_info.get(display_ip, {})
        # 如果已经获取过且信息有效（非空且非"获取失败"），则跳过
        if existing.get("fetched", False) and existing.get("info", "") not in ["", "获取失败"]:
            return
        
        # 获取对应的 server_info
        server_info = None
        for data in self.server_check_vars.values():
            info = data['info']
            if info.get('bmc_ip') == display_ip or info.get('os_ip') == display_ip:
                server_info = info
                break
        
        if not server_info:
            logging.warning(f"未找到服务器信息: {display_ip}")
            return
        
        def fetch():
            try:
                info_str = self.fetch_memory_info(server_info)
                if info_str:
                    self.server_memory_info[display_ip] = {"info": info_str, "fetched": True}
                    # 更新总览表
                    self.root.after(0, lambda: self.update_overview_memory_info(display_ip, info_str))
                else:
                    # 获取失败，显示空（什么都不显示）
                    failed_msg = "获取失败"
                    self.server_memory_info[display_ip] = {"info": "", "fetched": True}
                    self.root.after(0, lambda: self.update_overview_memory_info(display_ip, failed_msg))
            except Exception as e:
                logging.warning(f"获取 {display_ip} 内存信息失败: {e}")
                failed_msg = "获取失败"
                self.server_memory_info[display_ip] = {"info": "", "fetched": True}
                self.root.after(0, lambda: self.update_overview_memory_info(display_ip, failed_msg))
        
        threading.Thread(target=fetch, daemon=True).start()

    def refresh_memory_info(self, display_ip):
        """
        重新异步拉取并更新总览内存信息。
        每次测试项启动前调用一次，保证总览显示的是当前物理内存配置。
        """
        # 找到 server_info
        server_info = None
        for data in self.server_check_vars.values():
            info = data['info']
            if info.get('bmc_ip') == display_ip or info.get('os_ip') == display_ip:
                server_info = info
                break

        # 如果 server_check_vars 里找不到（比如面板已关闭过再重开），从 test_tasks 里找
        if not server_info:
            for ip, task in self.test_tasks.items():
                if ip == display_ip:
                    panel = task.get("panel")
                    if panel:
                        server_info = panel.server_info
                    break

        if not server_info:
            logging.warning(f"refresh_memory_info: 未找到服务器 {display_ip} 的配置")
            return

        # 重置 fetched，让 start_memory_info_fetch 不跳过
        if display_ip not in self.server_memory_info:
            self.server_memory_info[display_ip] = {"info": "获取中", "fetched": False}
        else:
            self.server_memory_info[display_ip]["fetched"] = False

        # 先把总览这一列显示成"获取中..."，给用户反馈
        self.update_overview_memory_info(display_ip, "获取中...")

        # 复用已有的 start_memory_info_fetch 异步逻辑
        self.start_memory_info_fetch(display_ip)

    def update_overview_memory_info(self, display_ip, memory_info):
        """更新总览表中的内存信息列"""
        if display_ip not in self.test_tasks:
            return
        row_id = self.test_tasks[display_ip]["row_id"]
        values = list(self.overview_table.item(row_id, "values"))
        if len(values) >= 2:
            values[1] = memory_info  # 第2列显示内存信息
            self.overview_table.item(row_id, values=values)

    def update_overview_test_info(self, display_ip, test_info):
        """更新总览表格中的测试信息列（包含开始时间、总次数、当前轮次）"""
        # ========== 新增：日志记录入参 ==========
        logging.info(f"[总览更新] 收到 display_ip={display_ip}, test_info={test_info}")
        print(f"[总览更新] 收到 display_ip={display_ip}, test_info={test_info}")

        if display_ip not in self.test_tasks:
            logging.warning(f"[总览更新] 未找到服务器 {display_ip} 的总览行，跳过")
            print(f"[总览更新] 未找到服务器 {display_ip} 的总览行")
            return

        row_id = self.test_tasks[display_ip]["row_id"]
        values = list(self.overview_table.item(row_id, "values"))

        if len(values) > 4:
            # 测试信息列索引为4
            values[4] = test_info
            self.overview_table.item(row_id, values=values)

            # ========== 新增：日志记录更新结果 ==========
            logging.info(f"[总览更新] 已更新 row_id={row_id}, values={values}")
            print(f"[总览更新] 已更新 row_id={row_id}")

            # ========== AC测试完成提醒：红色背景 ==========
            if "请拔掉智能插座" in test_info:
                self.overview_table.tag_configure("ac_warning", background="#ff1744", foreground="#ffffff")
                self.overview_table.item(row_id, tags=("ac_warning",))
                logging.info(f"[总览更新] 应用 AC 警告红色背景")
            else:
                status = self.test_tasks[display_ip].get("status", "waiting")
                self.overview_table.item(row_id, tags=(status,))
                logging.info(f"[总览更新] 应用状态标签: {status}")
        else:
            logging.warning(f"[总览更新] 总览行 {row_id} 列数不足：{len(values)}，期望 > 4")
            print(f"[总览更新] 总览行 {row_id} 列数不足：{len(values)}")

    def setup_overview_tab(self):
        # ---------- 统计卡片 ----------
        stats_frame = ttk.Frame(self.overview_tab)
        stats_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=5)

        self.stats_labels = {}
        color_map = {
            "running": "blue",
            "finished": "green",
            "failed": "red"
        }
        for status in ["running", "finished", "failed"]:
            frame = ttk.Frame(stats_frame, relief="ridge", borderwidth=1)
            frame.pack(side="left", padx=5, pady=5, fill="x", expand=True)
            ttk.Label(frame, text=status.upper(), font=("微软雅黑", 9)).pack()
            label = ttk.Label(frame, text="0", font=("微软雅黑", 16, "bold"), foreground=color_map[status])
            label.pack()
            self.stats_labels[status] = label

        # ---------- 表格 ----------
        # 如果需要，可以设置行高和字段背景色
        style = ttk.Style()
        style.configure("Treeview", rowheight=35, fieldbackground="white")

        columns = ("服务器BMC_IP", "内存信息", "测试项", "状态", "测试信息", "队列信息")
        self.overview_table = ttk.Treeview(self.overview_tab, columns=columns, show="headings", height=10)
        self.overview_table.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)

        for col in columns:
            self.overview_table.heading(col, text=col)
            if col == "队列信息":
                self.overview_table.column(col, width=200)
            elif col == "内存信息":
                self.overview_table.column(col, width=180)
            elif col == "测试项":
                self.overview_table.column(col, width=80)
            elif col == "测试信息":
                self.overview_table.column(col, width=400)
            else:
                self.overview_table.column(col, width=100 if col == "测试项" else 50)

        self.overview_table.bind("<Double-1>", self.on_overview_double_click)

        # ========== 关键：添加背景色配置（与任务队列一致） ==========
        self.overview_table.tag_configure("running", background="#bbdefb", foreground="#0d47a1")   # 浅蓝
        self.overview_table.tag_configure("finished", background="#c8e6c9", foreground="#1b5e20")  # 深绿，白字
        self.overview_table.tag_configure("failed", background="#ff1744", foreground="#ffffff")    # 浅红
        self.overview_table.tag_configure("waiting", background="#f5f5f5", foreground="#888888")   # 浅灰
        self.overview_table.tag_configure("stopped", background="#ffe5b4", foreground="#856404")   # 浅橙
        self.overview_table.tag_configure("stopping", background="#fff3cd", foreground="#856404")   # 浅橙
        self.overview_table.tag_configure("server_row", background="#ffffff", foreground="#0066cc", font=("微软雅黑", 10, "bold"))
        # =============================================================

        # ---------- 全局日志框 ----------
        global_log_frame = ttk.LabelFrame(self.overview_tab, text="📋 全局测试事件", padding=5)
        global_log_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=5)

        self.global_log_text = scrolledtext.ScrolledText(global_log_frame, wrap=tk.WORD, font=("Consolas", 9), height=8)
        self.global_log_text.pack(fill=tk.BOTH, expand=True)
        self.global_log_text.tag_config("start", foreground="blue")
        self.global_log_text.tag_config("finish", foreground="green")
        self.global_log_text.tag_config("failed", foreground="red")
        self.global_log_text.tag_config("stopped", foreground="orange")
        self.global_log_text.tag_config("deleted", foreground="gray")

        # ---------- grid 权重 ----------
        self.overview_tab.grid_rowconfigure(0, weight=0)
        self.overview_tab.grid_rowconfigure(1, weight=1)
        self.overview_tab.grid_rowconfigure(2, weight=0)
        self.overview_tab.grid_columnconfigure(0, weight=1)

    def close_test_panel(self, display_ip):
        """关闭指定服务器的测试面板"""
        if display_ip not in self.test_tasks:
            return

        # 停止测试
        panel = self.test_tasks[display_ip].get("panel")
        if panel:
            panel.force_stop()

        # 从总览表格中删除行（捕获异常，防止重复删除报错）
        row_id = self.test_tasks[display_ip]["row_id"]
        try:
            self.overview_table.delete(row_id)
        except tk.TclError:
            pass

        # ========== 遍历 Tab，按前缀匹配（兼容带别名） ==========
        prefix = f"🖥 {display_ip}"
        target_frame = None
        for frame in self.notebook.get_tab_frames():
            tab_text = self.notebook.get_tab_text(frame)
            # 精确匹配 "🖥 IP" 或前缀匹配 "🖥 IP (alias)"
            if tab_text == prefix or tab_text.startswith(prefix + " "):
                target_frame = frame
                break
        if target_frame:
            self.notebook.forget(target_frame)

        # 从 test_tasks 中删除
        del self.test_tasks[display_ip]

        # 恢复复选框
        if display_ip in self.server_check_vars:
            self.server_check_vars[display_ip]["widget"].config(state="normal")
            self.server_check_vars[display_ip]["var"].set(False)

        self.update_stats()

    def add_overview_row(self, display_ip, test_name, panel):
        if display_ip in self.test_tasks:
            row_id = self.test_tasks[display_ip]["row_id"]
            mem_info = self.server_memory_info.get(display_ip, {}).get("info", "获取中")

            # ========== 获取备注名 ==========
            alias = ""
            for data in self.server_check_vars.values():
                if data['info'].get('bmc_ip') == display_ip:
                    alias = data['info'].get('alias', '')
                    break
            if alias:
                display_text = f"{display_ip} ({alias})"
            else:
                display_text = display_ip

            values = (display_text, mem_info, test_name,
                       self.test_tasks[display_ip]["status"], 
                       self.test_tasks[display_ip]["start_time"], "空闲")
            self.overview_table.item(row_id, values=values)
        else:
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            test_info = f"Start:{start_time}"
            if display_ip in self.server_memory_info:
                mem_info = self.server_memory_info[display_ip].get("info", "")
            else:
                # 首次创建， 显示获取中..., 并记录下状态
                mem_info = "获取中"
                self.server_memory_info[display_ip] = {"info": mem_info, "fetched": False}
                # 异步获取内存信息 只触发一次
                self.start_memory_info_fetch(display_ip)

            # ========== 获取备注名 ==========
            alias = ""
            for data in self.server_check_vars.values():
                if data['info'].get('bmc_ip') == display_ip:
                    alias = data['info'].get('alias', '')
                    break
            if alias:
                display_text = f"{display_ip} ({alias})"
            else:
                display_text = display_ip

            row_id = self.overview_table.insert("", "end", 
                                                values=(display_text,  mem_info, test_name, "等待中", test_info, "空闲"))
            self.test_tasks[display_ip] = {
                "row_id": row_id,
                "status": "waiting",
                "test_name": test_name,
                "start_time": start_time,
                "panel": panel
            }
            self.overview_table.item(row_id, tags=("waiting",))
        self.update_stats()

    def on_overview_double_click(self, event):
        """双击切换到对应服务器的详情选项卡"""
        item = self.overview_table.selection()
        if not item:
            return
        values = self.overview_table.item(item[0], "values")
        display_text = values[0]  # 总览第一列：IP (备注名)
        # 提取纯 IP
        display_ip = display_text.split(" (")[0] if " (" in display_text else display_text
        
        # 查找对应的选项卡（选项卡标题是 "🖥 IP (备注名)"）
        target_tab_text = f"🖥 {display_text}"
        for frame in self.notebook.get_tab_frames():
            if self.notebook.get_tab_text(frame) == target_tab_text:
                self.notebook.select_tab(frame)
                break


    def update_overview_status(self, display_ip, status, msg=""):
        if display_ip not in self.test_tasks:
            return
        old_status = self.test_tasks[display_ip]["status"]
        if old_status == status:
            return
        self.test_tasks[display_ip]["status"] = status
        row_id = self.test_tasks[display_ip]["row_id"]
        values = list(self.overview_table.item(row_id, "values"))
        # 状态图标
        status_icon = {
            "waiting": "⏳",
            "running": "🔄",
            "finished": "✅",
            "failed": "❌",
            "stopped": "⏹",
            "stopping": "⏸"  # 暂停图标
        }
        display_text = status_icon.get(status, '') + " " + status
        values[3] = display_text
        self.overview_table.item(row_id, values=values, tags=(status,))
        self.update_stats()

    def update_overview_queue(self, display_ip, current_test, queue_count):
        if display_ip not in self.test_tasks:
            return
        row_id = self.test_tasks[display_ip]["row_id"]
        values = list(self.overview_table.item(row_id, "values"))
        # 队列信息列索引为4（第5列）
        if current_test:
            queue_info = f"正在测试: {current_test}"
            if queue_count > 0:
                queue_info += f" (排队{queue_count})"
        else:
            queue_info = "空闲"
        values[5] = queue_info
        self.overview_table.item(row_id, values=values)

    def update_stats(self):
        """更新状态汇总卡片中的计数"""
        counts = {"running": 0, "finished": 0, "failed": 0}
        for data in self.test_tasks.values():
            status = data["status"]
            if status in counts:
                counts[status] += 1
        for status, label in self.stats_labels.items():
            label.config(text=str(counts.get(status, 0)))

    def open_add_server_dialog(self):
        platform = self.platform_var.get()
        if not platform:
            messagebox.showerror("错误", "请先选择一个平台")
            return
        AddServerDialog(self.root, platform, self, self.refresh_platform_servers)

    def refresh_platform_servers(self):
        # 重新加载当前平台的服务器列表
        self.load_config()
        self.on_platform_change()

    def on_platform_change(self, event=None):
        for widget in self.server_frame.winfo_children():
            widget.destroy()
        self.server_check_vars.clear()
        platform = self.platform_var.get()
        if not platform:
            return
        servers = self.config[platform].get('servers', [])
        
        if not servers:
            ttk.Label(self.server_frame, text="该平台暂无服务器").pack()
            return
        
        # ========== 固定高度的选择服务器区域 ==========
        # 创建 Canvas 容器，固定高度 100px
        canvas_frame = ttk.Frame(self.server_frame, height=50)
        canvas_frame.pack(fill="x", pady=2)
        canvas_frame.pack_propagate(False)  # 禁止自动调整大小
        
        canvas = tk.Canvas(canvas_frame, highlightthickness=0, height=50)
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # 内部容器（放复选框）
        container = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=container, anchor="nw")
        
        # 布局
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # 添加复选框（8列布局）
        num_servers = len(servers)
        cols = 10
        for i, server in enumerate(servers):
            var = tk.BooleanVar(value=False)
            display_ip = server.get('bmc_ip', 'unknown')

            alias = server.get('alias', '')
            print(f"IP: {display_ip}, alias: {alias}")
            # ========== 组合显示：IP + 备注名 ==========
            if alias:
                display_text = f"{display_ip} ({alias})"
            else:
                display_text = display_ip
            cb = tk.Checkbutton(container, text=display_text, variable=var, anchor="w")
            row = i // cols
            col = i % cols
            cb.grid(row=row, column=col, sticky="w", padx=5, pady=1)
            self.server_check_vars[display_ip] = {
                "var": var,
                "info": server,
                "widget": cb
            }
        
        # 让列均匀分布
        for col in range(cols):
            container.columnconfigure(col, weight=1)
        
        # 更新滚动区域
        def update_scrollregion(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        container.bind("<Configure>", update_scrollregion)
        
        # 鼠标滚轮滚动
        def on_canvas_scroll(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        canvas.bind("<MouseWheel>", on_canvas_scroll)

    def add_test_panels(self):
        platform = self.platform_var.get()
        if not platform:
            messagebox.showerror("错误", "请选择平台")
            return
        selected = [data['info'] for data in self.server_check_vars.values() if data['var'].get()]
        if not selected:
            messagebox.showerror("错误", "请至少选择一台服务器")
            return

        show_continue = platform in ('通用平台', '平台')

        platform_config = self.config[platform]
        existing_ips = set(self.test_tasks.keys())
        new_selected = []
        for server in selected:
            display_ip = server.get('ip', server.get('bmc_ip', 'unknown'))
            if display_ip in existing_ips:
                continue
            new_selected.append(server)

        if not new_selected:
            messagebox.showinfo("提示", "所选服务器均已添加测试面板")
            return

       # 【必要修复】工厂函数，修复回调参数数量不匹配bug
        def build_callbacks(ip_addr):
            def status_cb(status):
                self.update_overview_status(ip_addr, status)
            def test_name_cb(name):
                if ip_addr in self.test_tasks:
                    self.test_tasks[ip_addr]["test_name"] = name
                    row_id = self.test_tasks[ip_addr]["row_id"]
                    values = list(self.overview_table.item(row_id, "values"))
                    values[2] = name
                    self.overview_table.item(row_id, values=values)
            def stop_cb():
                self.update_overview_status(ip_addr, "stopped")
            def close_cb():
                self.close_test_panel(ip_addr)
            # ==========修复点：补齐参数，兼容TestPanel传入的三个参数==========
            def queue_update_cb(display_ip, current_test, queue_count):
                self.update_overview_queue(ip_addr, current_test, queue_count)
            return status_cb, test_name_cb, stop_cb, close_cb, queue_update_cb

        for server in new_selected:
            display_ip = server.get('bmc_ip', 'unknown')

            # ========== 每个服务器独立选项卡 ==========
            alias = server.get('alias', '')

            # ========== 选项卡显示 IP + 备注名 ==========
            if alias:
                tab_text = f"🖥 {display_ip} ({alias})"
            else:
                tab_text = f"🖥 {display_ip}"

            tab = ttk.Frame(self.notebook)
            self.notebook.add(tab, text=tab_text)

            # 生成独立回调，避免闭包变量污染
            status_cb, test_name_cb, stop_cb, close_cb, queue_update_cb = build_callbacks(display_ip)

            '''
            # 定义回调函数
            def status_cb(status, ip=display_ip):
                self.update_overview_status(ip, status)
            def test_name_cb(name, ip=display_ip):
                if ip in self.test_tasks:
                    self.test_tasks[ip]["test_name"] = name
                    row_id = self.test_tasks[ip]["row_id"]
                    values = list(self.overview_table.item(row_id, "values"))
                    values[2] = name
                    self.overview_table.item(row_id, values=values)
            def stop_cb(ip=display_ip):
                self.update_overview_status(ip, "stopped")
            def close_cb(ip=display_ip):
                self.close_test_panel(ip)
            def queue_update_cb(ip, current_test, queue_count):
                self.update_overview_queue(ip, current_test, queue_count)
            '''
            panel = TestPanel(tab, server, platform_config, self.on_task_finished,
                            status_update_callback=status_cb,
                            test_name_update_callback=test_name_cb,
                            stop_callback=stop_cb,
                            close_callback=close_cb,
                            show_continue_btn=show_continue,
                            queue_update_callback=queue_update_cb,
                            test_info_update_callback=self.update_overview_test_info,
                            global_log_callback=self.log_global_event,
                            memory_refresh_callback=self.refresh_memory_info,
                            memory_info_update_callback=self.update_overview_memory_info)
            
            self.add_overview_row(display_ip, "未选择", panel)

            # 【必要修复3】初始化主动推送一次测试信息，总览表格立刻显示内容，不再空白
            self.update_overview_test_info(display_ip, self.test_tasks[display_ip]["start_time"])

            # 【必要修复1】新建面板初始化推送默认队列状态：当前无测试=空闲；测试启动后panel内部自动刷新running
            # 如果刚创建还没启动测试，显示空闲；后续启动测试panel会主动调用queue_update_cb覆盖
            self.update_overview_queue(display_ip, "", 0)

            if display_ip in self.server_check_vars:
                self.server_check_vars[display_ip]['widget'].config(state="disabled")
                self.server_check_vars[display_ip]['var'].set(False)
            
            # 【必要修复2】强制刷新界面后再选中Tab，解决切换失效问题
            self.root.update_idletasks()
            self.notebook.select_tab(tab)
        # ========== 添加完所有选项卡后，检查是否需要显示箭头 ==========
        
        self.status_var.set(f"已添加 {len(new_selected)} 个测试面板")

    def _update_tabs_style(self):
        """更新所有选项卡的样式，防止被压缩"""
        style = ttk.Style()
        for i in range(self.notebook.index("end")):
            tab_text = self.notebook.tab(i, "text")
            text_len = len(tab_text)
            width = max(120, text_len * 9 + 30)
            style.configure(f"TNotebook.Tab{i}.TNotebook.Tab", width=width)


    def update_server_switch_menu(self):
        """更新服务器切换下拉菜单的选项"""
        if not hasattr(self, 'server_switch_combo'):
            return
        ips = list(self.server_panels.keys())
        self.server_switch_combo['values'] = ips
        if ips:
            self.server_switch_var.set(ips[0])

    def on_task_finished(self):
        # 可选的完成回调
        pass
