import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import json
import os
import paramiko
import traceback
from datetime import datetime
from utils.constants import get_ipmitool_path, SCRIPT_DIR, SSH_KEY, get_resource_path


class AddServerDialog:
    def __init__(self, parent, platform_name, app, callback):
        self.parent = parent
        self.platform_name = platform_name
        self.app = app
        self.callback = callback
        self.dialog = tk.Toplevel(parent)
        self.dialog.title(f"新增服务器 - {platform_name}")
        self.dialog.geometry("500x580")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        labels = ["bmc_ip", "bmc_user", "bmc_pwd", "os_ip", "os_user", "ssh_passwd"]
        self.entries = {}
        default_values = {
            "bmc_user": "Administrator",
            "bmc_pwd": "Admin@9000",
            "os_user": "root",
            "ssh_passwd": "tigo@123",
            "bmc_ip": "192.168.10.108",
            "os_ip": "192.168.2.205"
        }

        for i, label in enumerate(labels):
            ttk.Label(self.dialog, text=label + ":").grid(row=i, column=0, padx=10, pady=5, sticky="e")
            entry = ttk.Entry(self.dialog, width=30)
            entry.grid(row=i, column=1, padx=10, pady=5)
            if label in default_values:
                entry.insert(0, default_values[label])
            self.entries[label] = entry

        # ========== 备注名输入框 ==========
        alias_row = len(labels)
        ttk.Label(self.dialog, text="备注名:").grid(row=alias_row, column=0, padx=10, pady=5, sticky="e")
        self.alias_entry = ttk.Entry(self.dialog, width=30)
        self.alias_entry.grid(row=alias_row, column=1, padx=10, pady=5)
        self.alias_entry.insert(0, "")

        # 日志框下移一行，防止遮挡备注名
        log_row = alias_row + 1
        log_frame = ttk.LabelFrame(self.dialog, text="操作日志", padding=5)
        log_frame.grid(row=log_row, column=0, columnspan=2, padx=10, pady=10, sticky="nsew")
        self.log_text = tk.Text(log_frame, height=10, width=60, state="disabled", wrap="word")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 按钮行继续下移
        btn_frame = ttk.Frame(self.dialog)
        btn_frame.grid(row=log_row + 1, column=0, columnspan=2, pady=20)

        self.ok_btn = ttk.Button(btn_frame, text="确定", command=self.on_ok)
        self.ok_btn.pack(side="left", padx=10)
        ttk.Button(btn_frame, text="取消", command=self.dialog.destroy).pack(side="left")

        self.dialog.grid_rowconfigure(log_row, weight=1)
        self.dialog.grid_columnconfigure(1, weight=1)

    def log(self, level, msg):
        self.log_text.config(state="normal")

        # 根据级别设置颜色
        if level == "ERROR":
            tag = "error"
            self.log_text.tag_config("error", foreground="red")
        elif level == "WARN":
            tag = "warn"
            self.log_text.tag_config("warn", foreground="orange")
        elif level == "INFO":
            tag = "info"
            self.log_text.tag_config("info", foreground="green")
        elif level == "ALERT":
            tag = "alert"
            self.log_text.tag_config("alert", foreground="#b85c00", font=("微软雅黑", 10, "bold"))
        else:
            tag = None

        self.log_text.insert("end", f"[{level}] {msg}\n", tag)
        self.log_text.see("end")
        self.log_text.config(state="disabled")
        self.dialog.update_idletasks()

        # ===================== on_ok：外层兜底 =====================
    def on_ok(self):
        """外层兜底：捕获所有未处理异常，写日志 + 写文件"""
        try:
            self._on_ok_impl()
        except Exception as e:
            tb = traceback.format_exc()
            # 1. 写到用户一定能写的位置（桌面），打包后也能看到
            try:
                crash_log = os.path.join(
                    os.path.expanduser("~"), "Desktop", "add_server_crash.log"
                )
                with open(crash_log, "a", encoding="utf-8") as f:
                    f.write("\n" + "=" * 70 + "\n")
                    f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] on_ok 未捕获异常\n")
                    try:
                        bmc_ip_now = self.entries["bmc_ip"].get().strip()
                    except Exception:
                        bmc_ip_now = "N/A"
                    f.write(f"BMC IP: {bmc_ip_now}\n")
                    f.write(tb)
                    f.write("\n")
            except Exception:
                pass
            # 2. 打印到对话框
            try:
                self.log("ERROR", f"on_ok 未捕获异常: {e}\n{tb}")
            except Exception:
                pass
            # 3. 弹框提示
            try:
                messagebox.showerror(
                    "内部错误",
                    f"发生未捕获异常:\n{e}\n\n详情见桌面 add_server_crash.log",
                    parent=self.dialog
                )
            except Exception:
                pass

    # ===================== on_ok 真正的实现 =====================
    def _on_ok_impl(self):
        # ---------- 收集输入 ----------
        data = {}
        for k, entry in self.entries.items():
            val = entry.get().strip()
            if not val:
                messagebox.showerror("错误", f"{k} 不能为空", parent=self.dialog)
                return
            data[k] = val

        config_path = os.path.join(SCRIPT_DIR, "servers.json")
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}

        if self.platform_name not in config:
            config[self.platform_name] = {"servers": []}

        alias_input = self.alias_entry.get().strip()

        # ---------- 校验 BMC IP 重复 ----------
        for s in config[self.platform_name]["servers"]:
            if s.get("bmc_ip") == data["bmc_ip"]:
                self.log("ERROR", f"BMC IP {data['bmc_ip']} 已存在")
                messagebox.showerror("错误", f"BMC IP {data['bmc_ip']} 已存在", parent=self.dialog)
                return

        # ---------- 校验备注名重复 ----------
        if alias_input:
            for s in config[self.platform_name]["servers"]:
                exist_alias = s.get("alias", "").strip()
                if exist_alias == alias_input:
                    self.log("ERROR", f"备注名「{alias_input}」已存在")
                    messagebox.showerror("错误", f"备注名「{alias_input}」已存在，请更换备注名", parent=self.dialog)
                    return

        # ---------- 1. 检查 BMC ----------
        self.log("INFO", f"正在通过检查 BMC {data['bmc_ip']} ...")
        try:
            ipmitool_exe = get_ipmitool_path()
            cmd = [ipmitool_exe, "-I", "lanplus", "-C", "17", "-H", data["bmc_ip"],
                   "-U", data["bmc_user"], "-P", data["bmc_pwd"], "lan", "print"]
            self.log("INFO", f"Running {cmd}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode != 0:
                self.log("ERROR", "BMC 认证失败")
                messagebox.showerror("错误", "BMC 认证失败，请检查：\n• BMC IP\n• 用户名\n• 密码", parent=self.dialog)
                return
            self.log("INFO", "BMC 认证成功")
        except Exception:
            self.log("ERROR", "BMC 连接失败（网络或超时）")
            messagebox.showerror("错误", "无法连接到 BMC，请检查网络或 IP 地址是否正确。", parent=self.dialog)
            return

        # ---------- 2. 上传 SSH 公钥 ----------
        ssh_key_pub = get_resource_path("id_rsa.pub")
        if not os.path.exists(ssh_key_pub):
            self.log("ERROR", "本地公钥文件不存在")
            messagebox.showerror("错误", "本地公钥文件 id_rsa.pub 不存在", parent=self.dialog)
            return

        self.log("INFO", f"正在测试 SSH 连接 {data['os_ip']} ...")
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh_client.connect(hostname=data['os_ip'],
                               username=data['os_user'],
                               password=data['ssh_passwd'],
                               timeout=10)
            self.log("INFO", "SSH 密码认证成功，正在上传公钥...")
            with ssh_client.open_sftp() as sftp:
                try:
                    sftp.stat('.ssh')
                except FileNotFoundError:
                    sftp.mkdir('.ssh')
                with open(ssh_key_pub, 'r') as f:
                    pub_key_content = f.read().strip()
                remote_key_path = '.ssh/authorized_keys'
                try:
                    with sftp.open(remote_key_path, 'r') as rf:
                        existing = rf.read().decode()
                    if pub_key_content not in existing:
                        with sftp.open(remote_key_path, 'a') as af:
                            af.write(pub_key_content + '\n')
                        self.log("INFO", "公钥已添加到远程 authorized_keys")
                    else:
                        self.log("INFO", "公钥已存在，跳过添加")
                except FileNotFoundError:
                    with sftp.open(remote_key_path, 'w') as wf:
                        wf.write(pub_key_content + '\n')
                    self.log("INFO", "公钥已添加到远程 authorized_keys")
            ssh_client.exec_command('chmod 600 .ssh/authorized_keys')
            self.log("INFO", "公钥上传及权限设置完成")
        except Exception as e:
            self.log("ERROR", f"SSH 操作失败: {e}")
            messagebox.showerror("错误", f"SSH 连接或公钥上传失败: {e}", parent=self.dialog)
            return
        finally:
            # ========== 修复：close 异常不影响主流程 ==========
            try:
                ssh_client.close()
            except Exception as e:
                self.log("WARN", f"SSH 关闭异常（忽略）: {e}")

        # ---------- 3. 上传 product_test_dir ----------
        local_product_dir = os.path.join(SCRIPT_DIR, "product_test_dir")
        if not os.path.isdir(local_product_dir):
            self.log("ERROR", f"本地目录不存在: {local_product_dir}")
            messagebox.showerror("错误", f"本地目录不存在: {local_product_dir}", parent=self.dialog)
            return

        self.log("INFO", "正在检查远程服务器 /root/product_test_dir ...")
        ssh_key = get_resource_path("id_rsa")

        need_extract = False

        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            ssh_client.connect(hostname=data['os_ip'],
                               username=data['os_user'],
                               key_filename=ssh_key,
                               timeout=30)
            sftp = ssh_client.open_sftp()
            remote_dir = "/root/product_test_dir"

            try:
                sftp.stat(remote_dir)
                self.log("INFO", "远程目录已存在，跳过上传")
            except FileNotFoundError:
                need_extract = True
                self.log("INFO", "远程目录不存在，开始上传...")

                def upload_dir(local_path, remote_path):
                    try:
                        sftp.stat(remote_path)
                    except FileNotFoundError:
                        sftp.mkdir(remote_path)
                    for item in os.listdir(local_path):
                        local_item = os.path.join(local_path, item)
                        remote_item = os.path.join(remote_path, item).replace("\\", "/")
                        if os.path.isdir(local_item):
                            upload_dir(local_item, remote_item)
                        else:
                            if item.endswith('.tar.gz'):
                                self.log("INFO", f"上传: {item}")
                                sftp.put(local_item, remote_item)

                upload_dir(local_product_dir, remote_dir)
                self.log("INFO", "所有 tar.gz 压缩包上传完成")

            # ========== 修复：sftp.close() 单独保护 ==========
            try:
                sftp.close()
            except Exception as e:
                self.log("WARN", f"sftp.close() 异常（忽略）: {e}")

        except Exception as e:
            self.log("ERROR", f"上传失败: {e}")
            messagebox.showerror("错误", f"上传测试目录失败: {e}", parent=self.dialog)
            return
        finally:
            # ========== 修复：ssh_client.close() 单独保护 ==========
            try:
                ssh_client.close()
            except Exception as e:
                self.log("WARN", f"上传阶段 ssh_client.close() 异常（忽略）: {e}")

        # ---------- 4. 解压（仅在需要时） ----------
        if need_extract:
            self.log("INFO", "开始解压远程服务器上的所有 tar.gz 包...")
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                ssh_client.connect(hostname=data['os_ip'],
                                   username=data['os_user'],
                                   key_filename=ssh_key,
                                   timeout=30)

                remote_base = "/root/product_test_dir"

                # 获取远程架构
                stdin, stdout, stderr = ssh_client.exec_command("uname -m")
                remote_arch = stdout.read().decode().strip()
                self.log("INFO", f"远程服务器架构: {remote_arch}")

                if remote_arch == "x86_64":
                    lmbench_package = "lmbench_lat_mem_rd_x86_64.tar.gz"
                elif remote_arch in ("aarch64", "arm64", "armv8l"):
                    lmbench_package = "lmbench_lat_mem_rd_arm64.tar.gz"
                else:
                    lmbench_package = None
                    self.log("WARN", f"未知架构 {remote_arch}，跳过 lmbench 解压")
                
                extract_cmd = f"""
cd {remote_base}
for tar_file in $(find . -maxdepth 1 -name '*.tar.gz' -type f | grep -v 'lmbench'); do
    echo "解压通用包: $tar_file"
    tar -xzf "$tar_file" --no-same-owner 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "  解压完成"
    else
        echo "  解压失败"
    fi
done
if [ -n "{lmbench_package}" ] && [ -f "{lmbench_package}" ]; then
    echo "解压 lmbench: {lmbench_package}"
    mkdir -p lmbench-master
    tar -xzf "{lmbench_package}" -C lmbench-master --no-same-owner 2>/dev/null
    if [ $? -eq 0 ]; then
        rm -f "{lmbench_package}"
        echo "  lmbench 解压完成"
    else
        echo "  lmbench 解压失败"
    fi
else
    echo "未找到对应的 lmbench 包或架构不匹配"
fi
echo "ALL_DONE"
"""
                stdin, stdout, stderr = ssh_client.exec_command(extract_cmd, timeout=300)
                exit_status = stdout.channel.recv_exit_status()
                output = stdout.read().decode()
                error = stderr.read().decode()

                if exit_status == 0:
                    self.log("INFO", f"解压完成:\n{output}")
                else:
                    self.log("WARN", f"解压可能有错误: {error}")
                    self.log("INFO", f"输出: {output}")

            except Exception as e:
                self.log("WARN", f"解压压缩包失败: {e}")
            finally:
                # ========== 修复：解压阶段的 close 单独保护（根因就在这里） ==========
                try:
                    ssh_client.close()
                except Exception as e:
                    self.log("WARN", f"解压阶段 ssh_client.close() 异常（忽略）: {e}")

        # ========== 改动 3：流程埋点 ==========
        self.log("INFO", "[流程] 解压块结束，准备写入 servers.json")

        # ---------- 5. 保存配置 ----------
        new_server = {
            "bmc_ip": data["bmc_ip"],
            "bmc_user": data["bmc_user"],
            "bmc_pwd": data["bmc_pwd"],
            "os_ip": data["os_ip"],
            "os_user": data["os_user"],
            "os_passwd": data["ssh_passwd"],
            "alias": alias_input
        }

        # 重新加载，防止并发覆盖
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}
        if self.platform_name not in config:
            config[self.platform_name] = {"servers": []}

        # 再次检查重复
        for s in config[self.platform_name]["servers"]:
            if s.get("bmc_ip") == data["bmc_ip"]:
                self.log("ERROR", f"BMC IP {data['bmc_ip']} 已存在（保存前检查）")
                return

        config[self.platform_name]["servers"].append(new_server)

        # ========== 改动 3：写入本身也保护 ==========
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            self.log("INFO", f"服务器信息已写入 {config_path}")
        except Exception as e:
            self.log("ERROR", f"写入 servers.json 失败: {e}\n{traceback.format_exc()}")
            messagebox.showerror("错误", f"写入配置文件失败:\n{e}", parent=self.dialog)
            return

        # ---------- 6. 更新 UI ----------
        self.ok_btn.config(state="disabled", text="✅ 已完成")
        self.log("INFO", "✅ 服务器已添加，所有压缩包已解压完成")

        if self.callback:
            self.callback()
        self.app.refresh_platform_servers()