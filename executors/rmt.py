import os
import time
import threading
import tarfile
import re
import subprocess
from datetime import datetime
from utils.constants import (
    REMOTE_REBOOT_DIR, LOCAL_POW_SCRIPT, DRAM_ECC_INJECTOR,
    LOCAL_LOG_BASE, REMOTE_BIN_DIR, SSH_KEY, get_ipmitool_path, MEMORY_ERROR_PATTERNS
)
from utils.ssh_utils import ssh_run, scp_upload, scp_down
from utils.helpers import clear_remote_logs
from .base import PrepareMixin, SOLMixin, RebootCheckMixin, CycleLoggerMixin


class RMTTestRunner(PrepareMixin, SOLMixin, RebootCheckMixin, CycleLoggerMixin):
    def __init__(self, bmc_ip, bmc_user, bmc_pwd, os_ip, os_user, ssh_key,
                 cycles, wait_sec, offset_cnt, need_bios, log_callback, stop_event,
                 on_finish=None, test_name="RMT测试", on_waiting_changed=None,
                 test_info_callback=None, start_time=None, memory_info_update_callback=None):
        self.bmc_ip = bmc_ip
        self.bmc_user = bmc_user
        self.bmc_pwd = bmc_pwd
        self.os_ip = os_ip
        self.os_user = os_user
        self.ssh_key = ssh_key

        self.cycles = cycles
        self.wait_sec = wait_sec
        self.offset_cnt = offset_cnt
        self.need_bios = need_bios

        # 给 CycleLoggerMixin 用的 params（用于 rmt_cycle.log 打印参数）
        self.params = {
            "每组重启次数": cycles,
            "OS等待秒数": wait_sec,
            "内存配置测试轮数": offset_cnt,
            "首轮进BIOS": bool(need_bios),
        }

        self.log_callback = log_callback
        self.stop_event = stop_event
        self.continue_event = threading.Event()
        self.sol_proc = None
        self.sol_bat = None
        self.on_finish = on_finish
        self.thread = None
        self.user_stopped = False
        self.test_name = test_name
        self.waiting_for_continue = False
        self.on_waiting_changed = on_waiting_changed
        self.sel_backup_file = None
        self.sel_last_id = None

        self.test_info_callback = test_info_callback

        self.memory_info_update_callback = memory_info_update_callback

        self.error_records = []
        if start_time:
            self.start_time = start_time
        else:
            self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def set_waiting(self, waiting):
        if self.waiting_for_continue == waiting:
            return
        self.waiting_for_continue = waiting

        if self.on_waiting_changed:
            self.on_waiting_changed(waiting)

        if self.test_info_callback:
            if waiting:
                self.test_info_callback(
                    self.bmc_ip,
                    "🔴【等待换内存】请 点击继续测试 → 断AC电源 → 偏移内存配置 → 重新上电"
                )
            else:
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.test_info_callback(
                    self.bmc_ip,
                    f"Start:{self.start_time}, 继续测试 {current_time}"
                )

    def log(self, level, msg):
        self.log_callback(level, msg)

    def ssh_run(self, cmd, silent=False, timeout=30):
        return ssh_run(cmd, self.os_ip, self.os_user, key_path=self.ssh_key, silent=silent, timeout=timeout)

    def scp_upload(self, local_file, remote_dir):
        return scp_upload(local_file, remote_dir, self.os_ip, self.os_user, key_path=self.ssh_key)

    def set_boot_bios_once(self):
        ipmitool_exe = get_ipmitool_path()
        # 直接带上 -C 17
        cmd = [ipmitool_exe, "-I", "lanplus", "-C", "17", "-H", self.bmc_ip, "-U", self.bmc_user, "-P", self.bmc_pwd, "chassis", "bootdev", "bios"]
        
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0:
            self.log("INFO", "IPMITOOL设置下次开机进入BIOS成功")
        else:
            raise RuntimeError(f"IPMITOOL设置BIOS启动失败: {result.stderr}")

    def ipmi_soft_reset(self):
        ipmitool_exe = get_ipmitool_path()
        # 直接带上 -C 17
        cmd = [ipmitool_exe, "-I", "lanplus", "-C", "17", "-H", self.bmc_ip, "-U", self.bmc_user, "-P", self.bmc_pwd, "power", "reset"]
        
        result = subprocess.run(cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0:
            self.log("INFO", "IPMITOOL重启服务器成功")
        else:
            raise RuntimeError(f"IPMITOOL重启服务器失败: {result.stderr}")

    def scp_down_all_logs(self, local_base_dir, round_idx):
        tar_file = f"rmt_tmp_{round_idx}.tar.gz"
        remote_tar = f"/tmp/{tar_file}"
        local_tar = os.path.join(local_base_dir, tar_file)
        round_dir = os.path.join(local_base_dir, f"round_{round_idx+1}")
        os.makedirs(round_dir, exist_ok=True)
        self.ssh_run(f"rm -f {remote_tar}")
        tar_cmd = (f"tar -zcf {remote_tar} --no-same-owner "
                   f"--exclude={LOCAL_POW_SCRIPT} --exclude={DRAM_ECC_INJECTOR} "
                   f"-C {REMOTE_REBOOT_DIR} .")
        self.ssh_run(tar_cmd)
        scp_down(remote_tar, local_tar, self.os_ip, self.os_user, self.ssh_key)
        with tarfile.open(local_tar, "r:gz") as tar:
            tar.extractall(path=round_dir, filter="data")
        os.remove(local_tar)
        self.log("ALERT", f"第 {round_idx+1} 轮日志已保存到: {round_dir}")

    def stop(self):
        self.user_stopped = True
        self.stop_event.set()
        self.continue_event.set()
        if self.sol_proc or self.sol_bat:
            self.stop_sol(self.sol_proc, self.sol_bat)

    def _record_error(self, round_idx, reboot_idx, error_type, error_info):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.error_records.append({
            "timestamp": timestamp,
            "round": round_idx + 1,
            "reboot": reboot_idx + 1,
            "type": error_type,
            "info": error_info
        })

        # 打印到 GUI 日志
        if isinstance(error_info, list):
            self.log("ERROR", f"第{round_idx+1}轮 第{reboot_idx+1}次重启: {error_type} - 发现 {len(error_info)} 条错误")
        else:
            self.log("ERROR", f"第{round_idx+1}轮 第{reboot_idx+1}次重启: {error_type} - {error_info}")

        if self.local_log_dir:
            error_file = os.path.join(self.local_log_dir, "reboot_errors.txt")
            mode = 'a' if os.path.exists(error_file) else 'w'
            try:
                with open(error_file, mode, encoding='utf-8') as f:
                    if mode == 'w':
                        f.write("========== RMT 测试错误汇总 ==========\n\n")
                    f.write(f"[{timestamp}] 第{round_idx+1}轮 第{reboot_idx+1}次重启 | {error_type}\n")
                    if isinstance(error_info, list):
                        f.write(f"  发现 {len(error_info)} 条错误，详情：\n")
                        for err in error_info[:20]:
                            f.write(f"    {err[:300]}\n")
                        if len(error_info) > 20:
                            f.write(f"    ...（还有 {len(error_info)-20} 条未记录）\n")
                    else:
                        f.write(f"  {error_info}\n")
            except:
                pass

    def _write_errors_to_file(self):
        if not self.error_records:
            return
        error_file = os.path.join(self.local_log_dir, "reboot_errors.txt")
        with open(error_file, 'w', encoding='utf-8') as f:
            f.write("========== RMT 测试错误汇总 ==========\n\n")
            for err in self.error_records:
                f.write(f"[{err['timestamp']}] ")
                f.write(f"第{err['round']}轮 第{err['reboot']}次重启 | ")
                f.write(f"{err['type']}\n")
                if isinstance(err['info'], list):
                    f.write(f"  发现 {len(err['info'])} 条错误，详情：\n")
                    for line in err['info'][:20]:
                        f.write(f"    {line[:300]}\n")
                    if len(err['info']) > 20:
                        f.write(f"    ...（还有 {len(err['info'])-20} 条未记录）\n")
                else:
                    f.write(f"  {err['info']}\n")
        self.log("INFO", f"错误已汇总到 {error_file}")

    def _scan_sol_errors(self, sol_log_path, round_idx, reboot_idx):
        if not os.path.exists(sol_log_path):
            return False
        self.log("INFO", f"扫描SOL文件: {sol_log_path}")
        pattern = re.compile('|'.join(MEMORY_ERROR_PATTERNS), re.IGNORECASE)
        errors = []
        try:
            with open(sol_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if pattern.search(line):
                        errors.append(line.strip())
        except Exception as e:
            self.log("WARN", f"扫描 SOL 文件失败: {e}")
            return False

        if errors:
            self._record_error(round_idx, reboot_idx, "SOL", errors)
            return True
        return False

    def _format_dimm_summary(self, dimms):
        """把 DIMMInfo 列表格式化为总览显示字符串
        示例：8pcs | 64GB | 5600MT/s | DDR5
        """
        count = len(dimms)
        sizes = sorted({d.size_gb for d in dimms})
        speeds = sorted({d.speed_mts for d in dimms if d.speed_mts})
        types = sorted({d.mem_type for d in dimms if d.mem_type})

        if not sizes:
            size_str = "?"
        elif len(sizes) == 1:
            size_str = f"{sizes[0]}GB"
        else:
            size_str = f"{sizes[0]}-{sizes[-1]}GB"

        if not speeds:
            speed_str = "?"
        elif len(speeds) == 1:
            speed_str = f"{speeds[0]}MT/s"
        else:
            speed_str = f"{speeds[0]}-{speeds[-1]}MT/s"

        if not types:
            type_str = "?"
        elif len(types) == 1:
            type_str = types[0]
        else:
            type_str = "/".join(types)

        return f"{count}pcs | {size_str} | {speed_str} | {type_str}"


    def _capture_round_baseline(self, round_idx, phase_label=""):
        """
        抓取本轮内存基准。
        phase_label: 用于日志，比如 "BIOS 配置后" 或 "" 或 "AC 上电后"
        返回 True/False
        """
        if phase_label:
            self.log("INFO", f"第 {round_idx+1} 轮：抓取本轮内存基准（{phase_label}）...")
        else:
            self.log("INFO", f"第 {round_idx+1} 轮：抓取本轮内存基准...")

        if not self._check_dmidecode():
            return False

        baseline_dimms, snap_path = self.save_baseline_dimm_snapshot(
            self.local_log_dir,
            round_label=f"round{round_idx+1}"
        )
        if not baseline_dimms:
            self.log("ERROR", f"第 {round_idx+1} 轮：无法获取物理内存条基准，测试无法继续")
            return False

        # ========== 新增：刷新总览界面的内存信息 ==========
        if self.memory_info_update_callback:
            try:
                summary = self._format_dimm_summary(baseline_dimms)
                self.memory_info_update_callback(self.bmc_ip, summary)
            except Exception as e:
                self.log("WARN", f"刷新总览内存信息失败: {e}")
        # ================================================
        
        return True

    def run(self):
        failed = False
        user_stopped = False

        self.log("INFO", "========== RMT 测试开始 ==========")

        base_dir = os.path.join(LOCAL_LOG_BASE, self.bmc_ip, self.test_name)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.local_log_dir = os.path.join(base_dir, f"{timestamp}_{self.test_name}")
        os.makedirs(self.local_log_dir, exist_ok=True)
        self.log("INFO", f"日志目录: {self.local_log_dir}")

        sol_dir = os.path.join(self.local_log_dir, "sol")
        dmesg_dir = os.path.join(self.local_log_dir, "dmesg")
        os.makedirs(sol_dir, exist_ok=True)
        os.makedirs(dmesg_dir, exist_ok=True)

        try:
            self.log("INFO", "检查BMC连通性...")
            self.wait_bmc_ready(max_wait=300)

            self.log("INFO", "检查系统网络连通性...")
            self.ssh_run("echo ok", silent=False, timeout=10)

            self.log("INFO", "保存服务器OS, CPU, Memory等信息...")
            self.save_system_info_before_test(self.local_log_dir)

            self.init_cycle_log("rmt_cycle.log")

            self.backup_sel_elist_before_test(self.local_log_dir)

            clear_remote_logs(self.bmc_ip, self.bmc_user, self.bmc_pwd,
                              self.os_ip, self.os_user, self.ssh_key, self.log)

            # ==================== 开始轮次循环 ====================
            for round_idx in range(self.offset_cnt):
                if self.stop_event.is_set():
                    self.log("WARN", "测试被手动终止")
                    user_stopped = True
                    break

                self.log("INFO", f"---------- 第 {round_idx+1}/{self.offset_cnt} 轮 ----------")

                # ========== 首轮进 BIOS 的处理 ==========
                if round_idx == 0 and self.need_bios == 1:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    bios_log = os.path.join(sol_dir, f"bios_{timestamp}.txt")
                    self.log("ALERT", "ipmitool 设置下次启动进入BIOS，并重启服务器...")
                    self.set_boot_bios_once()
                    self.log("ALERT", f"SOL BIOS 日志保存在: {bios_log}")
                    self.sol_proc, self.sol_bat = self.start_sol(bios_log)
                    time.sleep(2)
                    self.ipmi_soft_reset()
                    self.log("ALERT", "服务器重启后请去KVM界面修改RMT相关配置后保存退出(超时时间12小时)")
                    self.log_cycle(f"开始进入BIOS")
                    self.wait_system_ssh_ready(silent_wait=300, max_wait=43200)
                    self.log_cycle("BIOS配置完成并重启成功")

                    self.stop_sol(self.sol_proc, self.sol_bat)
                    self.sol_proc = None
                    self.sol_bat = None
                    self.log("INFO", "BIOS配置流程完成")

                    # ★ 第 1 轮进 BIOS：BIOS 改完、服务器重启完成、SSH 通，抓基准
                    if not self._capture_round_baseline(round_idx, phase_label="BIOS 配置后"):
                        failed = True
                        break

                    # 检查 BIOS 配置后的 dmesg / 内存
                    self.log("INFO", "开始扫描dmesg日志，检查是否有内存丢失情况...")
                    has_error, current_mem = self.check_dmesg_and_memory(
                        self.local_log_dir, round_idx, 0,
                        prefix=f"BIOS_round{round_idx+1}",
                        label=f"第 {round_idx+1} 轮 BIOS 配置后内存检查"
                    )
                    if has_error:
                        self._record_error(round_idx, 0, "BIOS配置后检查", "dmesg或内存错误")
                        self.log("ERROR", f"BIOS配置完成后检查发现错误")
                        failed = True
                        break

                # ========== 非首轮：用户手动换内存 ==========
                if round_idx > 0:
                    # 1. 关机
                    self.log("INFO", "正在执行系统关机：shutdown -h now ...")
                    try:
                        self.ssh_run("shutdown -h now", silent=True)
                    except:
                        pass

                    self.log("INFO", "等待 30 秒让系统完成关机...")
                    time.sleep(30)

                    # 2. 提示用户点击继续
                    self.log("ALERT", "===== 系统已关机，请按以下步骤操作 =====")
                    self.log("ALERT", "1. 点击【继续测试】按钮")
                    self.log("ALERT", "2. 去服务器机位拔掉电源线，更换内存")
                    self.log("ALERT", "3. 插上电源线，等待自动上电")
                    self.log("ALERT", "4. 程序将自动检测断电和上电操作")

                    self.log_cycle("开始等待用户点击内存偏移继续测试按钮...")
                    self.set_waiting(True)
                    while True:
                        if self.stop_event.is_set():
                            raise InterruptedError("测试被用户停止")
                        if self.continue_event.is_set():
                            break
                        self.continue_event.wait(timeout=0.5)
                    self.continue_event.clear()
                    self.set_waiting(False)
                    self.log("INFO", "用户已确认继续，开始检测断电...")

                    # 3. 等待 BMC 断开
                    bmc_offline_confirmed = False
                    offline_stable_start = None
                    wait_offline_start = time.time()
                    timeout_offline = 1800

                    # ★ 明确提示测试人员：限时拔电
                    self.log("ALERT", "=" * 60)
                    self.log("ALERT", f"⚠️ 请务必在 {timeout_offline//60} 分钟内拔掉 AC 电源线（偏移内存配置）！")
                    self.log("ALERT", f"⚠️ 否则 {timeout_offline//60} 分钟后测试将因超时失败！")
                    self.log("ALERT", "=" * 60)

                    last_offline_heartbeat = wait_offline_start
                    while not self.stop_event.is_set():
                        elapsed_offline = time.time() - wait_offline_start
                        if elapsed_offline > timeout_offline:
                            self.log("ERROR", f"等待 BMC 断开超时（{timeout_offline//60}分钟），请确认服务器已断电")
                            failed = True
                            break

                        bmc_alive = self._check_bmc_alive()
                        if not bmc_alive:
                            if offline_stable_start is None:
                                offline_stable_start = time.time()
                                self.log("INFO", "检测到 BMC 断开，开始稳定性计时（20秒）...")
                            elif time.time() - offline_stable_start >= 20:
                                bmc_offline_confirmed = True
                                self.log("INFO", "BMC 已稳定断开超过20秒，确认断电")
                                self.log_cycle("检测到AC 断电...")
                                break
                        else:
                            if offline_stable_start is not None:
                                self.log("INFO", "BMC 恢复连接，取消断电确认（可能是网络抖动）")
                                offline_stable_start = None

                            # ★ 每 5 分钟提醒一次
                            if time.time() - last_offline_heartbeat >= 300:
                                remaining = int((timeout_offline - elapsed_offline) // 60)
                                self.log("ALERT",
                                         f"⚠️ 等待拔掉 AC 电源线... 已等待 {int(elapsed_offline//60)} 分钟，"
                                         f"剩余 {remaining} 分钟，请尽快拔电！")
                                last_offline_heartbeat = time.time()

                        time.sleep(5)

                    if not bmc_offline_confirmed:
                        failed = True
                        break

                    # 4. 等待 BMC 上线
                    self.log("INFO", "等待服务器重新上电（BMC 恢复）...")
                    bmc_online_confirmed = False
                    online_stable_start = None
                    wait_online_start = time.time()
                    timeout_online = 1800

                    while not self.stop_event.is_set():
                        if time.time() - wait_online_start > timeout_online:
                            self.log("ERROR", "等待 BMC 上线超时（30分钟），请确认服务器已上电")
                            failed = True
                            break

                        bmc_alive = self._check_bmc_alive()
                        if bmc_alive:
                            if online_stable_start is None:
                                online_stable_start = time.time()
                                self.log("INFO", "检测到 BMC 上线，开始稳定性计时（8秒）...")
                            elif time.time() - online_stable_start >= 8:
                                bmc_online_confirmed = True
                                self.log("INFO", "BMC 已稳定在线，确认上电")
                                self.log_cycle("检测到AC 上电且BMC网络恢复...")
                                break
                        else:
                            if online_stable_start is not None:
                                self.log("INFO", "BMC 再次断开，取消上电确认（可能是供电不稳定）")
                                online_stable_start = None
                        time.sleep(2)

                    if not bmc_online_confirmed:
                        failed = True
                        break

                    # 5. 启动 SOL 捕获上电过程
                    ac_power_on_log = os.path.join(
                        sol_dir,
                        f"AC_round{round_idx+1}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    )
                    self.sol_proc, self.sol_bat = self.start_sol(ac_power_on_log)
                    self.log("ALERT", f"SOL AC 日志: {ac_power_on_log}")
                    time.sleep(2)

                    self.wait_system_ssh_ready(silent_wait=60, max_wait=3600)
                    self.log_cycle("AC 上电后 SSH 稳定可连...")
                    self.stop_sol(self.sol_proc, self.sol_bat)
                    self.sol_bat = None
                    self.sol_proc = None

                    # ★ 第 2 轮及以后：用户换完内存、服务器上电、SSH 通，抓基准
                    if not self._capture_round_baseline(round_idx, phase_label="AC 上电后"):
                        failed = True
                        break

                    # 检查 AC 上电后的 dmesg / 内存
                    self.log("INFO", "开始扫描dmesg日志，检查是否有内存丢失情况...")
                    has_error, current_mem = self.check_dmesg_and_memory(
                        self.local_log_dir, round_idx, 0,
                        prefix=f"AC_round{round_idx+1}",
                        label=f"第 {round_idx+1} 轮 AC 上电后内存检查"
                    )
                    if has_error:
                        self._record_error(round_idx, 0, "AC上电后检查", "dmesg或内存错误")
                        self.log("ERROR", f"第{round_idx+1}轮 AC 上电后检查发现错误")
                        failed = True
                        break

                # ========== 第 1 轮不进 BIOS：round 开头抓基准 ==========
                if round_idx == 0 and self.need_bios == 0:
                    if not self._capture_round_baseline(round_idx):
                        failed = True
                        break

                # ==================== 本轮 reboot 循环 ====================
                for reboot_idx in range(self.cycles):
                    if self.stop_event.is_set():
                        self.log("WARN", "测试被手动终止")
                        break

                    self.log_cycle(f"第{round_idx+1}轮 第{reboot_idx+1}次重启开始")

                    # 推送总览
                    if self.test_info_callback:
                        self.test_info_callback(
                            self.bmc_ip,
                            f"Start:{self.start_time}, 第 {round_idx+1}/{self.offset_cnt} 轮 第 {reboot_idx+1}/{self.cycles} 次重启"
                        )

                    session_log = os.path.join(
                        sol_dir,
                        f"session_round{round_idx+1}_reboot{reboot_idx+1}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
                    )
                    self.sol_proc, self.sol_bat = self.start_sol(session_log)
                    self.log("ALERT", f"本轮SOL日志保存在: {session_log}")
                    time.sleep(2)

                    try:
                        self.log("INFO", "发送重启命令 reboot...")
                        self.ssh_run("reboot", silent=True)
                    except:
                        pass

                    # 等待 SSH
                    ssh_ready = False
                    try:
                        self.wait_system_ssh_ready(silent_wait=60, max_wait=3600)
                        ssh_ready = True
                    except TimeoutError:
                        self.log("ERROR", f"第{reboot_idx+1}次重启后系统启动超时")
                        self._record_error(round_idx, reboot_idx, "SSH超时", "系统启动超时")

                    # 停止 SOL
                    self.stop_sol(self.sol_proc, self.sol_bat)
                    self.sol_proc = None
                    self.sol_bat = None

                    # 扫描 SOL 日志
                    sol_error = self._scan_sol_errors(session_log, round_idx, reboot_idx)
                    if sol_error:
                        self.log("ERROR", f"第{round_idx+1}轮 第{reboot_idx+1}次重启：SOL日志中发现错误，停止后续测试")
                        failed = True
                        break

                    current_mem = 0

                    if ssh_ready:
                        self.log("INFO", "开始扫描dmesg日志，检查是否有内存丢失情况...")
                        dmesg_error, current_mem = self.check_dmesg_and_memory(
                            self.local_log_dir, round_idx, reboot_idx,
                            prefix=f"round{round_idx+1}_reboot{reboot_idx+1}",
                            label=f"第 {round_idx+1} 轮 第 {reboot_idx+1} 次重启内存检查"
                        )
                        if dmesg_error:
                            self._record_error(round_idx, reboot_idx, "dmesg/内存", "发现错误")
                            failed = True
                            break
                    else:
                        self._record_error(round_idx, reboot_idx, "SSH超时", "系统启动超时")
                        failed = True

                    self.log_cycle(f"第{round_idx+1}轮 第{reboot_idx+1}次重启结束, 当前内存: {current_mem} GB")

                    if failed:
                        break

                    if reboot_idx != self.cycles - 1 and self.wait_sec > 0:
                        self.log("INFO", f"{self.wait_sec}秒后重启系统...")
                        time.sleep(self.wait_sec)

                if failed:
                    break

        except InterruptedError:
            self.log("WARN", "测试被用户停止")
            user_stopped = True
        except Exception as e:
            self.log("ERROR", f"测试异常中断: {str(e)}")
            self.log("ERROR", "请检查服务器网络")
            failed = True
        finally:
            if self.sol_proc or self.sol_bat:
                self.stop_sol(self.sol_proc, self.sol_bat)

            self._write_errors_to_file()

            self.log("INFO", "尝试连接服务器做日志收尾工作")
            if self.collect_messages_syslog_only(self.local_log_dir):
                failed = True

            if self.check_sel_full(self.local_log_dir, 0, 0):
                self.log("ERROR", "最终 SEL 扫描发现内存错误")
                failed = True

            self.log("INFO", "日志收尾工作完成")
            if user_stopped:
                final_status = "stopped"
            elif failed:
                final_status = "failed"
            else:
                final_status = "finished"

            self.close_cycle_log(test_status=final_status)
            if self.on_finish:
                self.on_finish(final_status)


class RMTTestExecutor:
    def __init__(self, server_info, params, log_callback, status_callback,
                 on_waiting_changed=None, test_info_callback=None, memory_info_update_callback=None):
        self.server_info = server_info
        self.params = params
        self.log_callback = log_callback
        self.status_callback = status_callback
        self.task = None
        self.thread = None
        self.running = False
        self.on_waiting_changed = on_waiting_changed
        self.test_info_callback = test_info_callback
        self.memory_info_update_callback = memory_info_update_callback

        self.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cycles = params.get("每组重启次数", 5)
        offset = params.get("内存配置测试轮数", 3)
        self._test_info = f"Start:{self.start_time}, 重启:{cycles}次/轮 | {offset}轮"

    def get_test_info_string(self):
        return self._test_info

    def log(self, level, msg):
        self.log_callback(level, msg)

    def start(self):
        self.running = True
        self.status_callback("running")
        stop_event = threading.Event()
        self.task = RMTTestRunner(
            bmc_ip=self.server_info['bmc_ip'],
            bmc_user=self.server_info['bmc_user'],
            bmc_pwd=self.server_info['bmc_pwd'],
            os_ip=self.server_info['os_ip'],
            os_user=self.server_info['os_user'],
            ssh_key=SSH_KEY,
            cycles=self.params.get("每组重启次数", 5),
            wait_sec=self.params.get("OS等待秒数", 60),
            offset_cnt=self.params.get("内存配置测试轮数", 3),
            need_bios=1 if self.params.get("首轮进BIOS", False) else 0,
            log_callback=self.log,
            stop_event=stop_event,
            on_finish=lambda status: (self.status_callback(status), setattr(self, 'running', False)),
            test_name="RMT测试",
            on_waiting_changed=self.on_waiting_changed,
            test_info_callback=self.test_info_callback,
            start_time=self.start_time,
            memory_info_update_callback=self.memory_info_update_callback
        )
        self.thread = threading.Thread(target=self.task.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.task:
            self.task.stop()