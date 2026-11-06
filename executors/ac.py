import os
import time
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from .base import PrepareMixin, RebootCheckMixin, CycleLoggerMixin, SOLMixin
from utils.constants import SSH_KEY, LOCAL_LOG_BASE
from utils.ssh_utils import ssh_run
from utils.helpers import clear_remote_logs


class ACTestExecutor(PrepareMixin, RebootCheckMixin, CycleLoggerMixin, SOLMixin):
    def __init__(self, server_info, test_name, params, remote_dir, remote_subdir, log_callback, status_callback, check_interval=30, test_info_callback=None):
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
        self._cleanup_started = False

        self.bmc_ip = server_info.get('bmc_ip')
        self.bmc_user = server_info.get('bmc_user')
        self.bmc_pwd = server_info.get('bmc_pwd')
        self.os_ip = server_info.get('os_ip')
        self.os_user = server_info.get('os_user')
        self.ssh_key = SSH_KEY

        self.remote_ac_dir = f"{remote_dir}/{remote_subdir}"
        self.sol_proc = None
        self.sol_bat = None
        self.current_sol_file = None

        # 提醒状态
        self._reminder_active = False
        self._reminder_ip = None
        self._reminder_count = 0

        # AC 断电/上电计数（首次人工上电也算第 1 次）
        self._ac_poweroff_count = 0
        self._ac_poweron_count = 0

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
        self.user_stopped = True
        self.stop_event.set()
        self.running = False
        if not hasattr(self, '_cleanup_started') or not self._cleanup_started:
            self._cleanup_started = True
            threading.Thread(target=self._cleanup_remote_service_when_online, daemon=True).start()

    # ===================== AC 清理方法 =====================
    def cleanup_remote_ac_service(self):
        """清理远程 AC 自启动服务"""
        commands = [
            "systemctl disable ac-powercycle.service 2>/dev/null; rm -f /etc/systemd/system/ac-powercycle.service; systemctl daemon-reload 2>/dev/null",
            "sed -i '/powercycle_test.sh/d' /etc/rc.local 2>/dev/null",
            "sed -i '/powercycle_test.sh/d' /etc/init.d/boot.local 2>/dev/null",
            f"rm -f {self.remote_ac_dir}/.ac_test_env {self.remote_ac_dir}/logpath.txt; find {self.remote_ac_dir} -name '*:Zone.Identifier' -delete"
        ]
        for cmd in commands:
            try:
                self.ssh_run(cmd, silent=True, timeout=5)
            except:
                pass
        self.log("INFO", "已清理远程 AC 自启动服务与Windows残留文件")

    def _cleanup_remote_service_when_online(self):
        """等待网络恢复后清理 AC 自启动服务（最多10分钟）"""
        max_attempts = 20
        clean_cmd = (
            "systemctl disable ac-powercycle.service 2>/dev/null; rm -f /etc/systemd/system/ac-powercycle.service; systemctl daemon-reload 2>/dev/null; "
            "sed -i '/powercycle_test.sh/d' /etc/rc.local 2>/dev/null; sed -i '/powercycle_test.sh/d' /etc/init.d/boot.local 2>/dev/null; "
            f"rm -f {self.remote_ac_dir}/.ac_test_env {self.remote_ac_dir}/logpath.txt; find {self.remote_ac_dir} -name '*:Zone.Identifier' -delete"
        )
        for attempt in range(max_attempts):
            try:
                self.ssh_run("echo ok", silent=True, timeout=5)
                self.cleanup_remote_ac_service()
                self.log("INFO", "AC 远程自启动服务清理完成")
                return
            except Exception:
                self.log("INFO", f"等待网络恢复，第 {attempt+1}/{max_attempts} 次尝试，30 秒后重试...")
                time.sleep(30)
        self.log("ERROR", "网络超时未恢复（10分钟），未能自动清理远程 AC 自启动！")
        self.log("ERROR", "服务器上电后 root 用户执行清理命令：")
        self.log("ERROR", clean_cmd)

    def _prepare_environment(self):
        """准备 AC 测试环境"""
        self.log("INFO", "准备 AC 测试环境...")

        if self._check_remote_dir(self.remote_ac_dir):
            if self._check_remote_file(f"{self.remote_ac_dir}/start.sh"):
                self.log("INFO", "AC 脚本已存在，跳过准备")
                return True
            else:
                self.log("INFO", "AC 目录存在但缺少 start.sh，重新上传...")

        target_dir = self._upload_and_extract_tar("AC.tar.gz")
        if target_dir is None:
            self.log("ERROR", "AC.tar.gz 上传/解压失败")
            return False

        self.ssh_run(f"chmod +x {self.remote_ac_dir}/*.sh; find {self.remote_ac_dir} -name '*:Zone.Identifier' -delete", silent=True)
        self.log("INFO", "AC 测试环境准备完成")
        return True

    def upload_ac_scripts(self):
        return self._prepare_environment()

    def start_remote_ac_test(self, log_name, cycles):
        cmd = f"cd {self.remote_ac_dir} && bash start.sh {log_name} {cycles}"
        try:
            self.ssh_run(cmd, silent=False, timeout=10)
            self.log("INFO", f"远程 AC 测试已启动，日志名: {log_name}, 次数: {cycles}")
            return True
        except Exception as e:
            self.log("ERROR", f"启动远程 AC 测试失败: {e}")
            return False

    def get_remote_test_status(self, log_name):
        """读取远端当前/总轮次，失败时返回 (0, 0)"""
        testtimes_file = f"{self.remote_ac_dir}/{log_name}/testtimes.txt"
        total_file = f"{self.remote_ac_dir}/{log_name}/totaltimes.txt"
        try:
            current_raw = self.ssh_run(f"cat {testtimes_file}", silent=True, timeout=5).strip()
            total_raw = self.ssh_run(f"cat {total_file}", silent=True, timeout=5).strip()
            current = int(current_raw) if current_raw.isdigit() else 0
            total = int(total_raw) if total_raw.isdigit() else 0
            return current, total
        except Exception:
            return 0, 0

    def download_ac_logs(self, log_name):
        remote_log_dir = f"{self.remote_ac_dir}/{log_name}"
        local_log_dir = os.path.join(self.local_log_dir, "ac_logs")
        ssh_ok = False
        for attempt in range(3):
            try:
                self.ssh_run("echo ok", silent=True, timeout=5)
                ssh_ok = True
                break
            except:
                time.sleep(1)
        if not ssh_ok:
            self.log("WARN", "SSH 不可通，跳过 AC 测试结果日志收集")
            return False
        self.download_remote_dir_exclude_sh(remote_log_dir, local_log_dir)

    def _stop_current_sol(self):
        if self.sol_proc:
            try:
                self.sol_proc.terminate()
            except:
                pass
            self.sol_proc = None
        if self.sol_bat and os.path.exists(self.sol_bat):
            try:
                os.remove(self.sol_bat)
            except:
                pass
        self.sol_bat = None
        try:
            self.kill_sol_by_title()
        except:
            pass
        self.log("INFO", "已停止当前 SOL 会话")

    def _start_sol_for_cycle(self, cycle_num):
        """启动本轮 SOL 会话"""
        if not self._check_bmc_alive():
            self.log("WARN", "BMC 不可达，无法启动 SOL")
            return None
        self._stop_current_sol()

        sol_dir = os.path.join(self.local_log_dir, "sol")
        os.makedirs(sol_dir, exist_ok=True)

        sol_log = os.path.join(sol_dir, f"sol_ac_cycle_{cycle_num}.log")
        self.sol_proc, self.sol_bat = self.start_sol(sol_log)
        self.current_sol_file = sol_log
        self.log("INFO", f"启动第 {cycle_num} 轮 SOL 日志记录: {sol_log}")
        return sol_log

    # ===================== 稳定检测 =====================
    def _wait_bmc_offline_stable(self, timeout=1800, stable_sec=10):
        """等待 BMC 稳定离线（连续 stable_sec 秒不可达），轮询间隔 2 秒"""
        start = time.time()
        stable_start = None
        while time.time() - start < timeout:
            if self.stop_event.is_set():
                raise InterruptedError("测试被用户停止")
            if not self._check_bmc_alive():
                if stable_start is None:
                    stable_start = time.time()
                    self.log("INFO", f"检测到 BMC 断开，稳定性计时中（{stable_sec}秒）...")
                elif time.time() - stable_start >= stable_sec:
                    return True
            else:
                if stable_start is not None:
                    self.log("INFO", "BMC 恢复连接，取消断电确认（可能是网络抖动），重新计时")
                    stable_start = None
            time.sleep(2)
        self.log("ERROR", f"等待 BMC 稳定离线超时（{timeout} 秒）")
        return False

    def _wait_bmc_online_stable_with_heartbeat(self, timeout=1800, stable_sec=5, next_poweron=1):
        """等待 BMC 稳定上线，期间每 60 秒打一次心跳，轮询间隔 2 秒"""
        start = time.time()
        stable_start = None
        last_log_elapsed = 0
        while time.time() - start < timeout:
            if self.stop_event.is_set():
                raise InterruptedError("测试被用户停止")
            if self._check_bmc_alive():
                if stable_start is None:
                    stable_start = time.time()
                    self.log("INFO", f"检测到 BMC 上线，稳定性计时中（{stable_sec}秒）...")
                elif time.time() - stable_start >= stable_sec:
                    return True
            else:
                if stable_start is not None:
                    self.log("INFO", "BMC 再次断开，取消上电确认（可能是供电不稳定），重新计时")
                    stable_start = None
                elapsed = time.time() - start
                if elapsed - last_log_elapsed >= 60:
                    self.log("INFO", f"[心跳] 等待第 {next_poweron} 次 AC 上电...（已等待 {int(elapsed//60)} 分钟 / {timeout//60} 分钟）")
                    last_log_elapsed = elapsed
            time.sleep(2)
        self.log("ERROR", f"等待 BMC 稳定上线超时（{timeout} 秒）")
        return False

    def _shutdown_server(self):
        try:
            self.ssh_run("shutdown -h now", silent=True, timeout=10)
            self.log("INFO", "已发送关机命令")
            return True
        except Exception as e:
            self.log("WARN", f"关机命令执行异常: {e}")
            return False

    # ===================== 提醒相关 =====================
    def _show_status_reminder(self, bmc_ip, final_status):
        """状态栏闪烁提醒"""
        self._reminder_active = True
        self._reminder_ip = bmc_ip
        self._reminder_count = 0

        def blink():
            if not hasattr(self, '_reminder_active') or not self._reminder_active:
                return
            if self._reminder_count > 20:
                self.log("INFO", f"智能插座提醒已结束，请确认 {bmc_ip} 的插座已拔掉")
                return

            if self._reminder_count % 2 == 0:
                self.log("ALERT", f"🔴 警告：{bmc_ip} AC测试完成，请拔掉智能插座！")
            else:
                self.log("INFO", f"⚠️ {bmc_ip} AC测试 {final_status}，请拔掉智能插座！")

            self._reminder_count += 1
            threading.Timer(5.0, blink).start()

        blink()

    def _clear_status_reminder(self):
        self._reminder_active = False
        self.log("INFO", "智能插座提醒已清除")

    def _show_ac_dialog_and_wait(self, final_status, status_text, on_confirm_callback=None):
        """显示 AC 测试完成对话框"""
        bmc_ip = self.server_info.get('bmc_ip', '未知IP')

        status_map = {
            "finished": "✅ 测试完成",
            "failed": "❌ 测试失败",
            "stopped": "⏹ 测试已停止"
        }
        display_status = status_map.get(final_status, final_status)

        root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()

        top = tk.Toplevel(root)
        top.title(f"⚠️ AC 电源测试 - {bmc_ip}")
        top.geometry("550x350")
        top.transient(root)
        top.lift()
        top.attributes('-topmost', True)

        message = f"""
        ═══════════════════════════════════════

            {display_status}

        ═══════════════════════════════════════

        🔌 请拔掉智能插座！

        服务器 {bmc_ip} 的 AC 测试已结束，
        智能插座仍在循环上下电。

        请立即拔掉智能插座，
        恢复服务器正常上电状态。

        ═══════════════════════════════════════
        """

        label = tk.Label(top, text=message, justify="left", font=("微软雅黑", 11), padx=20, pady=15)
        label.grid(row=0, column=0, sticky="nsew")

        def on_confirm():
            if on_confirm_callback:
                on_confirm_callback()
            self._clear_status_reminder()
            top.destroy()

        btn = ttk.Button(top, text="✅ 我知道了，去拔插座", command=on_confirm, width=30)
        btn.grid(row=1, column=0, pady=15)

        top.grid_rowconfigure(0, weight=1)
        top.grid_columnconfigure(0, weight=1)

        self._show_status_reminder(bmc_ip, final_status)
        self.log("ALERT", f"🔴🔴🔴 AC测试完成，请拔掉 {bmc_ip} 的智能插座！🔴🔴🔴")

    # ===================== 主测试 =====================
    def _run(self):
        failed = False
        ac_started = False
        log_name = f"ac_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        try:
            self.log("INFO", "========== AC 电源循环测试开始 ==========")
            self.log("INFO", "检查服务器连通性")

            self.ssh_run("echo ok", silent=False, timeout=10)

            server_id = self.bmc_ip if self.bmc_ip else self.os_ip
            base_local = os.path.join(LOCAL_LOG_BASE, server_id, self.test_name)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.local_log_dir = os.path.join(base_local, f"{timestamp}_{self.test_name}")
            os.makedirs(self.local_log_dir, exist_ok=True)
            self.log("INFO", f"结果保存目录: {self.local_log_dir}")

            self.init_cycle_log("ac_cycle.log")
            self.save_system_info_before_test(self.local_log_dir)

            # 备份 SEL
            sel_before_file = os.path.join(self.local_log_dir, "sel_before_clear.log")
            sel_output = self._run_ipmitool("sel elist")
            if sel_output:
                with open(sel_before_file, 'w', encoding='utf-8') as f:
                    f.write(sel_output)
                self.log("INFO", f"清空前 SEL 已备份到 {sel_before_file}")
            self._run_ipmitool("sel clear")
            self.log("INFO", "已清空 BMC SEL")

            clear_remote_logs(
                self.bmc_ip, self.bmc_user, self.bmc_pwd,
                self.os_ip, self.os_user, self.ssh_key, self.log
            )

            if not self._check_dmidecode():
                failed = True
                return

            baseline_dimms, snap_path = self.save_baseline_dimm_snapshot(self.local_log_dir)
            if not baseline_dimms:
                self.log("ERROR", "无法获取物理内存条基准，测试无法继续")
                failed = True
                return

            if not self._prepare_environment():
                failed = True
                return

            cycles = self.params.get("测试次数", 3)

            if not self.start_remote_ac_test(log_name, cycles):
                failed = True
                return
            ac_started = True

            # ========== 第 1 次 AC 断电（初始关机） ==========
            self.log("ALERT", "准备关闭服务器，请确保智能插座已配置自动上电/下电周期...")
            self.log_cycle("发送 shutdown 命令...")
            self._shutdown_server()
            self.log("INFO", "等待服务器完全断电（BMC 离线），请操作人员将电源接到智能插座上...")

            if not self._wait_bmc_offline_stable(timeout=3600, stable_sec=10):
                self.log("WARN", "等待 BMC 稳定离线超时")
                failed = True
                return

            self._ac_poweroff_count += 1
            self.log("ALERT", f"⚡ 第 {self._ac_poweroff_count} 次 AC 断电（BMC 已稳定离线，服务器下电中）")
            self.log_cycle(f"第 {self._ac_poweroff_count} 次 AC 断电")

            # ========== 第 1 次 AC 上电（人工接智能插座） ==========
            self.log("INFO", "等待第 1 次 AC 上电完成...")

            if not self._wait_bmc_online_stable_with_heartbeat(timeout=3600, stable_sec=5, next_poweron=1):
                self.log("ERROR", "等待第 1 次 AC 上电超时")
                failed = True
                return

            self._ac_poweron_count += 1
            self.log("ALERT", f"⚡ 第 {self._ac_poweron_count} 次 AC 上电（BMC 已稳定在线）")
            self.log_cycle(f"第 {self._ac_poweron_count} 次 AC 上电")

            if self.test_info_callback:
                _info = f"Start:{self.start_time} Current:1/{cycles}"
                print(f"[AC] test_info_callback: {_info}")
                self.test_info_callback(self.bmc_ip, _info)

            # ========== 进入主循环 ==========
            self._start_sol_for_cycle(1)
            last_cycle = 0
            max_wait_seconds = cycles * 1800
            start_wait = time.time()
            self.log("INFO", f"开始等待 AC 测试完成，预计最多 {max_wait_seconds//60} 分钟")
            dmesg_dir = os.path.join(self.local_log_dir, "dmesg")
            os.makedirs(dmesg_dir, exist_ok=True)

            self.log("INFO", f"等待第 1/{cycles} 次 AC 上电完成...")

            while not self.user_stopped:
                if time.time() - start_wait > max_wait_seconds:
                    self.log("ERROR", "AC 测试超时")
                    failed = True
                    break

                if not self._check_bmc_alive():
                    # ========== 等待 BMC 稳定离线 = 第 N 次 AC 断电 ==========
                    if not self._wait_bmc_offline_stable(timeout=1800, stable_sec=10):
                        self.log("ERROR", "等待 BMC 稳定离线超时，停止测试")
                        failed = True
                        break

                    self._ac_poweroff_count += 1
                    self.log("ALERT", f"⚡ 第 {self._ac_poweroff_count} 次 AC 断电（BMC 已稳定离线，服务器下电中）")
                    self.log_cycle(f"第 {self._ac_poweroff_count} 次 AC 断电")

                    # ========== 等待 BMC 稳定上线 = 第 N 次 AC 上电 ==========
                    next_poweron = self._ac_poweron_count + 1
                    self.log("INFO", f"等待第 {next_poweron} 次 AC 上电（智能插座上电）...")

                    if not self._wait_bmc_online_stable_with_heartbeat(
                        timeout=1800, stable_sec=5, next_poweron=next_poweron
                    ):
                        self.log("ERROR", "等待 BMC 稳定上线超时，停止测试")
                        failed = True
                        break

                    self._ac_poweron_count += 1
                    self.log("ALERT", f"⚡ 第 {self._ac_poweron_count} 次 AC 上电（BMC 已稳定在线）")
                    self.log_cycle(f"第 {self._ac_poweron_count} 次 AC 上电")

                    cycle_num = last_cycle + 1 if last_cycle > 0 else 1
                    self._start_sol_for_cycle(cycle_num)
                    if self.test_info_callback:
                        self.test_info_callback(self.bmc_ip, f"Start:{self.start_time} Current:{cycle_num}/{cycles}")
                    self.log("INFO", f"等待第 {cycle_num}/{cycles} 次 AC 上电完成...")
                else:
                    if self.sol_proc is None:
                        cycle_num = last_cycle + 1 if last_cycle > 0 else 1
                        self._start_sol_for_cycle(cycle_num)

                    current, total = self.get_remote_test_status(log_name)

                    # ========== 新增：只有成功读到非 0 值才更新总览 ==========
                    if current > 0 and total > 0:
                        if self.test_info_callback:
                            _info = f"Start:{self.start_time} Current:{current}/{total}"
                            print(f"[AC] test_info_callback: {_info}")
                            self.test_info_callback(self.bmc_ip, _info)
                    else:
                        print(f"[AC] 读状态失败（current={current}, total={total}），跳过总览更新")
                    # =========================================================

                    if current != last_cycle and current > 0:
                        self.log("INFO", f"检测到第 {current}/{total} 次 AC 上电完成，开始检查...")
                        self.log_cycle(f"第 {current}/{total} 次 AC 上电完成")
                        last_cycle = current
                        self.log("INFO", "开始扫描 dmesg 日志，检查是否有内存丢失情况...")
                        dmesg_error, current_mem = self.check_dmesg_and_memory(
                            self.local_log_dir, 0, current - 1,
                            label=f"第 {current} 次 AC 上电内存检查"
                        )

                        sol_error = False
                        if self.current_sol_file and os.path.exists(self.current_sol_file):
                            sol_error = self.scan_log_file_for_errors(
                                self.current_sol_file, "SOL", self.local_log_dir, 0, current
                            )
                            if sol_error:
                                self.log("ERROR", f"第 {current} 次 AC 上电后 SOL 日志发现错误")

                        if dmesg_error or sol_error:
                            self.log("ERROR", f"第 {current} 次 AC 上电后检测到错误，停止测试")
                            failed = True
                            if not hasattr(self, '_cleanup_started') or not self._cleanup_started:
                                self._cleanup_started = True
                                threading.Thread(target=self._cleanup_remote_service_when_online, daemon=True).start()
                            break

                        self.log("INFO", f"第 {current}/{total} 次 AC 上电后检测通过，当前内存: {current_mem} GB")
                        self.log_cycle(f"第 {current}/{total} 次 AC 上电后检测通过，当前内存: {current_mem} GB")

                        if current >= total:
                            self.log("INFO", f"全部 {total} 次 AC 上电测试完成")
                            self.log_cycle(f"全部 {total} 次 AC 上电测试完成")
                            break
                        else:
                            next_poweroff = self._ac_poweroff_count + 1
                            self.log("INFO", f"等待第 {next_poweroff} 次 AC 断电（智能插座下电）...")

                time.sleep(self.check_interval)

        except InterruptedError:
            self.log("WARN", "用户停止测试")
            self.user_stopped = True
        except Exception as e:
            self.log("ERROR", f"测试异常: {e}")
            failed = True
        finally:
            self.log("ALERT", "开始收尾工作...")

            self._stop_current_sol()

            if ac_started:
                self.download_ac_logs(log_name)

             # ========== 提前启动远程清理（趁服务器还在线） ==========
            if not hasattr(self, '_cleanup_started') or not self._cleanup_started:
                self._cleanup_started = True
                threading.Thread(target=self._cleanup_remote_service_when_online, daemon=True).start()
            # =======================================================

            try:
                self.ssh_run("echo ok", silent=True, timeout=3)
                if self.collect_messages_syslog_only(self.local_log_dir):
                    failed = True
            except:
                self.log("WARN", "服务器不可达，跳过日志采集")

            try:
                if self.check_sel_full(self.local_log_dir, 0, 0):
                    self.log("ERROR", "最终 SEL 检查发现错误")
                    failed = True
            except:
                self.log("WARN", "SEL 检查失败")

            self.running = False

            if self.user_stopped:
                final_status = "stopped"
                status_text = "已停止，请拔掉智能插座"
            elif failed:
                final_status = "failed"
                status_text = "失败，请拔掉智能插座"
            else:
                final_status = "finished"
                status_text = "完成，请拔掉智能插座"

            self.close_cycle_log(test_status=final_status)

            if self.test_info_callback:
                self.test_info_callback(self.bmc_ip, f"⚠️ {status_text}")

            def on_dialog_confirm():
                final_map = {
                    "finished": "✅ 测试完成",
                    "failed": "❌ 测试失败",
                    "stopped": "⏹ 测试已停止"
                }
                if self.test_info_callback:
                    self.test_info_callback(self.bmc_ip, final_map.get(final_status, final_status))
                self.status_callback(final_status)

            self._show_ac_dialog_and_wait(final_status, status_text, on_dialog_confirm)