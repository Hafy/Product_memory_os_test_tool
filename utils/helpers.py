import sys
import os
import time
import json
import subprocess
import shutil
import tarfile
import logging
import re
from datetime import datetime
from .constants import SSH_KEY, SCRIPT_DIR, get_ipmitool_path
from .ssh_utils import ssh_run, scp_down



def setup_logging():
    log_dir = os.path.join(SCRIPT_DIR, "logs", "app")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"Tigo_Debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return log_file

def clean_remote_log(raw: str) -> str:
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    cleaned = ansi_escape.sub('', raw)
    cleaned = cleaned.replace('\r\n', '\n').replace('\r', '\n')
    output = []
    for ch in cleaned:
        if ch == '\b':
            if output:
                output.pop()
        else:
            output.append(ch)
    cleaned = ''.join(output)
    cleaned = ''.join(ch for ch in cleaned if ch >= ' ' or ch in '\n\t')
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned

def wait_for_finish_flag(remote_flag_file, os_ip, os_user, ssh_key, stop_event=None, log_callback=None, interval=60, max_wait=4*3600):
    from .ssh_utils import ssh_run
    start = time.time()
    def log(msg):
        if log_callback:
            log_callback("INFO", msg)
    log(f"remote_flag_file: {remote_flag_file}")
    while True:
        if stop_event and stop_event.is_set():
            raise InterruptedError("测试被用户停止")
        elapsed = time.time() - start
        if elapsed > max_wait:
            raise TimeoutError("等待完成标记超时")
        try:
            content = ssh_run(f"cat {remote_flag_file} 2>/dev/null", os_ip, os_user, key_path=ssh_key, silent=True).strip()
            if content == "DONE":
                ssh_run(f"rm -f {remote_flag_file}", os_ip, os_user, key_path=ssh_key, silent=True)
                log("检测到完成标记")
                return
        except Exception as e:
            log(f"检查标记文件异常: {e}")
        log(f"未检测结束标记，已等待 {int(elapsed//60)} 分钟")
        time.sleep(interval)

def download_and_extract_logs(remote_work_dir, local_log_dir, round_idx, os_ip, os_user, ssh_key, log_callback=None, exclude_patterns=None):
    from .ssh_utils import ssh_run, scp_down
    if exclude_patterns is None:
        exclude_patterns = []
    tar_file = f"rmt_tmp_{round_idx}.tar.gz"
    remote_tar = f"/tmp/{tar_file}"
    local_tar = os.path.join(local_log_dir, tar_file)
    ssh_run(f"rm -f {remote_tar}", os_ip, os_user, key_path=ssh_key, silent=True)
    exclude_args = " ".join([f"--exclude='{p}'" for p in exclude_patterns])
    tar_cmd = f"tar -zcf {remote_tar} {exclude_args} -C {remote_work_dir} ."
    ssh_run(tar_cmd, os_ip, os_user, key_path=ssh_key)
    scp_down(remote_tar, local_tar, os_ip, os_user, ssh_key)
    with tarfile.open(local_tar, "r:gz") as tar:
        tar.extractall(path=local_log_dir, filter="data")
    os.remove(local_tar)
    if log_callback:
        log_callback("INFO", f"日志已保存到 {local_log_dir}")

def fetch_system_logs(server_ip, ssh_user, os_passwd, local_base_dir, log_callback=None):
    from .ssh_utils import ssh_run
    from .constants import SSH_KEY
    # 完整实现（同原文件）
    def log(msg):
        if log_callback:
            log_callback("INFO", msg)
        else:
            print(msg)
    try:
        os.makedirs(local_base_dir, exist_ok=True)
        dmesg_file = os.path.join(local_base_dir, "dmesg.txt")
        log(f"正在拉取 {server_ip} 的 dmesg...")
        result = ssh_run("dmesg -T 2>/dev/null || dmesg", server_ip, ssh_user, os_passwd, key_path=SSH_KEY, silent=True, timeout=30)
        with open(dmesg_file, "w", encoding="utf-8") as f:
            f.write(result)
        log(f"已保存 dmesg 到: {dmesg_file}")
        messages_file = os.path.join(local_base_dir, "messages.txt")
        log(f"正在拉取 {server_ip} 的 /var/log/messages...")
        try:
            result = ssh_run("cat /var/log/messages 2>/dev/null", server_ip, ssh_user, os_passwd, key_path=SSH_KEY, silent=True, timeout=30)
            with open(messages_file, "w", encoding="utf-8") as f:
                f.write(result)
            log(f"已保存 messages 到: {messages_file}")
        except Exception as e:
            log(f"拉取 messages 失败: {e}")
        syslog_file = os.path.join(local_base_dir, "syslog.txt")
        log(f"正在拉取 {server_ip} 的 /var/log/syslog...")
        try:
            result = ssh_run("cat /var/log/syslog 2>/dev/null", server_ip, ssh_user, os_passwd, key_path=SSH_KEY, silent=True, timeout=30)
            with open(syslog_file, "w", encoding="utf-8") as f:
                f.write(result)
            log(f"已保存 syslog 到: {syslog_file}")
        except Exception as e:
            log(f"拉取 syslog 失败: {e}")
    except Exception as e:
        log(f"拉取系统日志异常: {e}")

def collect_and_analyze_logs(server_ip, ssh_user, os_passwd, local_log_dir, bmc_ip=None, bmc_user=None, bmc_pwd=None, log_callback=None):
    from .ssh_utils import ssh_run
    
    # 完整实现同原文件
    def log(level, msg):
        if log_callback:
            log_callback(level, msg)
        else:
            print(f"[{level}], {msg}")
    try:
        ssh_run("echo ok", server_ip, ssh_user, os_passwd, key_path=SSH_KEY, silent=True, timeout=5)
    except Exception as e:
        log("WARN", f"网络不通，跳过系统日志拉取: {e}")
        return
    fetch_system_logs(server_ip, ssh_user, os_passwd, local_log_dir, log_callback)
    has_error = False
    if bmc_ip and bmc_user and bmc_pwd:
        sel_file = os.path.join(local_log_dir, "sel.txt")
        try:
            ipmitool_exe = get_ipmitool_path()
            cmd = [get_ipmitool_path, "-I", "lanplus", "-C", "17", "-H", bmc_ip, "-U", bmc_user, "-P", bmc_pwd, "sel", "elist"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            with open(sel_file, "w", encoding="utf-8") as f:
                f.write(result.stdout)
            if result.stdout.strip():
                log("INFO", f"SEL 日志已保存到 {sel_file}, 共 {len(result.stdout)} 字节")
            else:
                log("WARN", "SEL日志为空")
            if re.search(r'failed|error', result.stdout, re.IGNORECASE):
                log("ERROR", "SEL 中发现 failed 或 error 关键字")
                has_error = True
        except Exception as e:
            log("WARN", f"获取 SEL 日志失败: {e}")
    ecc_error = scan_logs_for_ecc(local_log_dir, log_callback)
    return has_error or ecc_error

def scan_logs_for_ecc(local_dir, log_callback=None):
    """
    扫描日志中的 ECC 内存错误，使用更精确的模式：
    - 同时包含 'ecc' 和 'error'（不区分大小写）
    - 或包含 'uncorrectable', 'corrected error', 'CE', 'UE', 'memory error' 等
    输出包含匹配行的摘要，并保存到 ecc_scan_results.txt
    """
    from .constants import MEMORY_ERROR_PATTERNS
    def log(level, msg):
        if log_callback:
            log_callback(level, msg)
        else:
            print(f"[{level}] {msg}")


    combined_re = re.compile('|'.join(MEMORY_ERROR_PATTERNS), re.IGNORECASE)

    found_any = False
    summary_file = os.path.join(local_dir, "ecc_scan_results.txt")
    for filename in ["dmesg.txt", "messages.txt", "syslog.txt"]:
        file_path = os.path.join(local_dir, filename)
        if not os.path.exists(file_path):
            continue
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            matches = []
            for line in lines:
                if combined_re.search(line):
                    matches.append(line.strip())
            if matches:
                log("ERROR", f"{filename} 中发现 ECC 相关错误: {len(matches)} 行")
                found_any = True
                with open(summary_file, "a", encoding="utf-8") as sf:
                    sf.write(f"=== {filename} ===\n")
                    for match in matches[:20]:  # 最多记录20行
                        sf.write(match + "\n")
                    if len(matches) > 20:
                        sf.write(f"... 还有 {len(matches)-20} 行\n")
            else:
                log("INFO", f"{filename} 中未发现 ECC 错误")
        except Exception as e:
            log("WARN", f"扫描 {filename} 失败: {e}")

    if not found_any:
        log("INFO", "所有日志均未发现 ECC 错误")
    return found_any

def clear_remote_logs(bmc_ip, bmc_user, bmc_pwd, os_ip, os_user, ssh_key, log_callback=None):
    from .ssh_utils import ssh_run
    
    def log(msg):
        if log_callback:
            log_callback("INFO", msg)
    try:
        ssh_run("dmesg -c", os_ip, os_user, key_path=ssh_key, silent=True)
        log("已清理 dmesg")
        ssh_run("echo '' > /var/log/messages", os_ip, os_user, key_path=ssh_key, silent=True)
        log("已清理 /var/log/messages")
        ssh_run("echo '' > /var/log/syslog", os_ip, os_user, key_path=ssh_key, silent=True)
        log("已清理 /var/log/syslog")
        
        ipmitool_exe = get_ipmitool_path()
        cmd = [ipmitool_exe, "-I", "lanplus", "-C", "17", "-H", bmc_ip, "-U", bmc_user, "-P", bmc_pwd, "sel", "clear"]
        subprocess.run(cmd, creationflags=subprocess.CREATE_NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        log("已清理 BMC SEL")
    except Exception as e:
        log(f"清理日志失败: {e}")

def scan_single_log_for_ecc(file_path, log_callback=None):
    """扫描单个日志文件，返回是否存在ECC/内存错误"""
    def log(level, msg):
        if log_callback:
            log_callback(level, msg)
    patterns = [
        r'\becc\b.*?(error|fail)\b',
        r'DDR training fail',
        r'memory error',
        r'\[Hardware Error\](?=.*?\b(rank|bank|column)\b)(?=.*?\b(?!.*\1\b)(rank|bank|column)\b).*',
        r'\bDQ\[.*?\]\s+Error\b',
        r'\b([NCE]\d+(?:\.[NCE]\d+)+):\s*ERROR\b'
    ]

    combined_re = re.compile('|'.join(patterns), re.IGNORECASE)
    if not os.path.exists(file_path):
        return False
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if combined_re.search(line):
                    log("ERROR", f"发现错误行: {line.strip()}")
                    return True
    except Exception as e:
        log("WARN", f"扫描文件 {file_path} 失败: {e}")
    return False
