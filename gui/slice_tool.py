import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog
import threading
import logging
import re
import os
from datetime import datetime
from utils.constants import MEMORY_ERROR_PATTERNS

# ===================== 默认切片关键字 =====================
# 与原 awk 脚本中的切片规则完全一致
DEFAULT_SPLIT_PATTERNS = [
    r"SecStartupPhase2\(\) .*PeiCoreEntryPoint",
    r"BIOS Log @",
    r"DebugMaskValue Value2",
    r"BIOS HOST_LOG",
]


class SliceToolPanel:
    def __init__(self, parent):
        self.parent = parent
        self.extra_patterns = []          # 用户额外添加的关键字（仅当前会话有效）
        self._compiled_patterns = []      # 预编译后的正则（默认 + 额外）
        self.setup_ui()
        self.refresh_compiled_patterns()

    def refresh_compiled_patterns(self):
        """重新编译所有关键字（默认 + 额外）"""
        all_patterns = list(DEFAULT_SPLIT_PATTERNS)
        for p in self.extra_patterns:
            p = p.strip()
            if p and p not in all_patterns:
                all_patterns.append(p)

        self._compiled_patterns = []
        for p in all_patterns:
            try:
                self._compiled_patterns.append((re.compile(p), p))
            except re.error as e:
                logging.warning(f"正则表达式无效，已跳过: {p} - {e}")

    # ===================== UI 构建 =====================
    def setup_ui(self):
        # ===== 顶部按钮 =====
        btn_frame = ttk.Frame(self.parent)
        btn_frame.pack(fill="x", padx=10, pady=5)
        ttk.Button(btn_frame, text="选择文件/目录进行RMT日志切片",
                   command=self.open_slice_tool).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="🔍 日志扫描",
                   command=self.open_log_scanner).pack(side="left", padx=5)

        # ===== 切片关键字配置面板 =====
        self._build_pattern_config_panel()

        # ===== 日志输出区 =====
        self.log_text = scrolledtext.ScrolledText(self.parent, wrap=tk.WORD,
                                                   font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, padx=10, pady=5)
        self.log_text.tag_config("error", foreground="red")
        self.log_text.tag_config("warn", foreground="orange")
        self.log_text.tag_config("success", foreground="green")

    def _build_pattern_config_panel(self):
        """构建切片关键字配置面板"""
        self.config_frame = ttk.LabelFrame(
            self.parent,
            text="▼ 切片关键字设置（可选，不填则使用默认）",
            padding=5
        )
        self.config_frame.pack(fill="x", padx=10, pady=5)

        # ---- 默认关键字展示（只读） ----
        ttk.Label(self.config_frame, text="默认关键字（固定生效）：",
                  font=("微软雅黑", 9, "bold")).pack(anchor="w", padx=5, pady=(5, 0))

        default_text = tk.Text(self.config_frame, height=4, wrap="word",
                               font=("Consolas", 9), bg="#f5f5f5", fg="#555555")
        default_text.pack(fill="x", padx=5, pady=2)
        for p in DEFAULT_SPLIT_PATTERNS:
            default_text.insert(tk.END, f"  • {p}\n")
        default_text.config(state="disabled")

        # ---- 额外关键字输入 ----
        ttk.Label(self.config_frame, text="额外关键字（每行一个，支持正则表达式，#开头为注释）：",
                  font=("微软雅黑", 9, "bold")).pack(anchor="w", padx=5, pady=(8, 0))

        input_frame = ttk.Frame(self.config_frame)
        input_frame.pack(fill="x", padx=5, pady=2)

        self.extra_input = tk.Text(input_frame, height=4, wrap="word",
                                    font=("Consolas", 9))
        self.extra_input.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(input_frame, orient="vertical",
                                   command=self.extra_input.yview)
        self.extra_input.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        # ---- 操作按钮（移除「保存为默认」） ----
        op_frame = ttk.Frame(self.config_frame)
        op_frame.pack(fill="x", padx=5, pady=(5, 5))

        ttk.Button(op_frame, text="🗑 清空输入",
                   command=self._clear_input).pack(side="left", padx=3)

        ttk.Label(op_frame,
                  text="提示：不填即只用默认关键字；填了则与默认叠加；额外关键字仅本次运行有效，关闭程序后不保存",
                  font=("微软雅黑", 8), foreground="#888888").pack(side="left", padx=5)

    def _clear_input(self):
        """清空输入框"""
        self.extra_input.delete("1.0", tk.END)
        self.extra_patterns = []
        self.refresh_compiled_patterns()
        self.slice_log("INFO", "已清空额外关键字，恢复为默认切片规则")

    def _read_extra_patterns_from_ui(self):
        """从输入框读取额外关键字，更新到 self.extra_patterns"""
        raw = self.extra_input.get("1.0", tk.END)
        patterns = []
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):   # 支持 # 注释
                patterns.append(line)

        # 去重，保持顺序
        seen = set()
        unique = []
        for p in patterns:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        self.extra_patterns = unique
        self.refresh_compiled_patterns()

        if unique:
            self.slice_log("INFO", f"已加载 {len(unique)} 条额外切片关键字（仅本次运行有效）")

    # ===================== 日志输出 =====================
    def log(self, level, msg):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {msg}\n"
        self.log_text.insert(tk.END, line)
        if level == "ERROR":
            self.log_text.tag_add("error", "end-2c linestart", "end-1c")
        self.log_text.see(tk.END)

    def slice_log(self, level, msg):
        """线程安全的日志输出"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {msg}\n"
        self.log_text.after(0, lambda: self._insert_log(level, line))
        if level == "ERROR":
            logging.error(msg)
        elif level == "WARN":
            logging.warning(msg)
        else:
            logging.info(msg)

    def _insert_log(self, level, line):
        self.log_text.insert(tk.END, line)
        if level == "ERROR":
            self.log_text.tag_add("error", "end-2c linestart", "end-1c")
        elif level == "WARN":
            self.log_text.tag_add("warn", "end-2c linestart", "end-1c")
        elif level == "SUCCESS":
            self.log_text.tag_add("success", "end-2c linestart", "end-1c")
        self.log_text.see(tk.END)

    # ===================== RMT 切片工具 =====================
    def open_slice_tool(self):
        self.slice_log("INFO", "=" * 50)
        self.slice_log("INFO", "RMT 切片工具已启动（Python 原生引擎）")

        # 每次切片前，重新读取界面上的额外关键字，确保实时生效
        self._read_extra_patterns_from_ui()

        selected_file = filedialog.askopenfilename(
            title="请选择日志文件（.log），或点击取消选择目录",
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        target = None
        if selected_file:
            target = selected_file
            self.slice_log("INFO", f"选择文件: {target}")
        else:
            selected_dir = filedialog.askdirectory(title="请选择包含日志文件的目录")
            if selected_dir:
                target = selected_dir
                self.slice_log("INFO", f"选择目录: {target}")
            else:
                self.slice_log("INFO", "用户取消了选择")
                return

        threading.Thread(
            target=self.run_slice_script,
            args=(target,),
            daemon=True
        ).start()

    def run_slice_script(self, target):
        """RMT 日志切片主入口（纯 Python 实现）"""
        try:
            target = os.path.abspath(target)

            # 1. 路径校验
            if not os.path.exists(target):
                self.slice_log("ERROR", f"路径不存在: {target}")
                return

            # 2. 收集待处理的 .log 文件
            log_files = []
            if os.path.isfile(target):
                if target.lower().endswith('.log'):
                    log_files.append(target)
                else:
                    self.slice_log("ERROR", f"文件不是 .log 格式: {target}")
                    return
                output_dir = os.path.join(os.path.dirname(target), "rmt_logs_split")
            else:
                for f in os.listdir(target):
                    full_path = os.path.join(target, f)
                    if os.path.isfile(full_path) and f.lower().endswith('.log'):
                        log_files.append(full_path)
                output_dir = os.path.join(target, "rmt_logs_split")

            if not log_files:
                self.slice_log("ERROR", f"在 {target} 下未找到任何 .log 文件")
                return

            # 3. 准备输出目录
            if os.path.exists(output_dir):
                self.slice_log("INFO", f"清空已存在的分片目录: {output_dir}")
                for f in os.listdir(output_dir):
                    fp = os.path.join(output_dir, f)
                    if os.path.isfile(fp):
                        try:
                            os.remove(fp)
                        except Exception as e:
                            self.slice_log("WARN", f"删除旧分片失败: {fp} - {e}")
            else:
                os.makedirs(output_dir, exist_ok=True)
                self.slice_log("INFO", f"创建分片目录: {output_dir}")

            # 4. 打印当前生效的关键字
            self.slice_log("INFO", f"当前生效的切片关键字共 {len(self._compiled_patterns)} 条：")
            for _, p in self._compiled_patterns:
                self.slice_log("INFO", f"  • {p}")

            # 5. 逐个文件切片
            self.slice_log("INFO", f"共发现 {len(log_files)} 个 .log 文件，开始切片...")
            total_slices = 0
            success_files = 0

            for file_idx, log_file in enumerate(log_files, 1):
                try:
                    filename_no_ext = os.path.splitext(os.path.basename(log_file))[0]
                    self.slice_log("INFO",
                                   f"[{file_idx}/{len(log_files)}] 处理: {os.path.basename(log_file)}")
                    slices = self._split_single_log(log_file, filename_no_ext, output_dir)
                    total_slices += slices
                    success_files += 1
                    self.slice_log("SUCCESS", f"  ✅ 完成，共生成 {slices} 个分片")
                except Exception as e:
                    self.slice_log("ERROR", f"  ❌ 处理失败: {log_file} - {e}")

            # 6. 汇总
            self.slice_log("SUCCESS", "=" * 50)
            self.slice_log("SUCCESS", f"🎉 全部完成！")
            self.slice_log("SUCCESS", f"   成功处理文件: {success_files}/{len(log_files)}")
            self.slice_log("SUCCESS", f"   生成分片总数: {total_slices}")
            self.slice_log("SUCCESS", f"   输出目录: {output_dir}")
            self.slice_log("SUCCESS", "=" * 50)

        except Exception as e:
            import traceback
            self.slice_log("ERROR", f"切片异常: {e}")
            self.slice_log("ERROR", f"详细:\n{traceback.format_exc()}")

    def _should_split(self, line):
        """判断当前行是否为切片标记行"""
        for compiled, _ in self._compiled_patterns:
            if compiled.search(line):
                return True
        return False

    def _split_single_log(self, log_file, filename_no_ext, output_dir):
        """
        对单个日志文件执行切片
        返回生成的切片文件数量
        """
        slice_idx = 0
        current_lines = []
        in_slice = False
        total_lines = 0
        matched_markers = 0

        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                total_lines += 1
                if self._should_split(line):
                    if in_slice and current_lines:
                        self._write_slice(output_dir, filename_no_ext, slice_idx, current_lines)
                        slice_idx += 1
                        current_lines = []
                    in_slice = True
                    matched_markers += 1
                    current_lines.append(line)
                elif in_slice:
                    current_lines.append(line)

        if in_slice and current_lines:
            self._write_slice(output_dir, filename_no_ext, slice_idx, current_lines)
            slice_idx += 1

        if matched_markers == 0:
            self.slice_log("WARN",
                           f"  ⚠️ 未匹配到任何切片标记（共 {total_lines} 行），"
                           f"请确认该文件是否为 RMT/BIOS 日志，或添加新的切片关键字")
            if total_lines > 0:
                self.slice_log("INFO", "  已按整份文件作为 1 个分片输出")
                try:
                    with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                        self._write_slice(output_dir, filename_no_ext, 0, f.readlines())
                    slice_idx = 1
                except Exception as e:
                    self.slice_log("ERROR", f"  兜底输出失败: {e}")

        return slice_idx

    def _write_slice(self, output_dir, filename_no_ext, slice_idx, lines):
        """将一段日志写入文件"""
        out_file = os.path.join(output_dir, f"{filename_no_ext}_{slice_idx}.log")
        with open(out_file, 'w', encoding='utf-8') as f:
            f.writelines(lines)

    # ===================== 日志扫描工具 =====================
    def open_log_scanner(self):
        file_path = filedialog.askopenfilename(
            title="选择日志文件",
            filetypes=[("Log files", "*.log *.txt"), ("All files", "*.*")]
        )
        if not file_path:
            return

        self.slice_log("INFO", "=" * 80)
        self.slice_log("INFO", f"开始扫描文件: {file_path}")
        threading.Thread(
            target=self._run_log_scanner,
            args=(file_path,),
            daemon=True
        ).start()

    def _run_log_scanner(self, file_path):
        """后台执行日志扫描，避免阻塞 UI"""
        try:
            compiled_patterns = [(re.compile(p, re.IGNORECASE), p) for p in MEMORY_ERROR_PATTERNS]
            errors = []
            total_lines = 0

            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_no, line in enumerate(f, 1):
                    total_lines += 1
                    line_stripped = line.rstrip('\n')
                    for compiled, pattern_str in compiled_patterns:
                        if compiled.search(line_stripped):
                            errors.append((line_no, line_stripped, pattern_str))
                            break

            if errors:
                self.slice_log("ERROR", f"发现 {len(errors)} 处内存错误（共扫描 {total_lines} 行）:")
                for line_no, line, pattern in errors[:100]:
                    display_line = line[:300] + "...(已截断)" if len(line) > 300 else line
                    self.slice_log("INFO", f"行 {line_no} | 内容: {display_line}")
                    self.slice_log("ERROR", f"  pattern: {pattern}")
                if len(errors) > 100:
                    self.slice_log("INFO", f"... 还有 {len(errors) - 100} 行未显示")
            else:
                self.slice_log("SUCCESS", f"未发现内存错误（共扫描 {total_lines} 行）")
        except Exception as e:
            import traceback
            self.slice_log("ERROR", f"扫描失败: {e}")
            self.slice_log("ERROR", f"详细:\n{traceback.format_exc()}")