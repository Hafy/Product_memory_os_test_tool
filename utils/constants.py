import os
import sys


def get_app_root():
    if getattr(sys, 'frozen', False):
        # 打包模式：返回 exe 所在目录
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        # 开发模式：返回项目根目录（utils 的父目录）
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def get_resource_path(filename):
    if hasattr(sys, '_MEIPASS'):
        # 打包模式下，资源在 _MEIPASS 临时目录
        base = sys._MEIPASS
    else:
        # 开发模式下，资源在项目根目录下的 resources/ 子目录
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resources")
        # 如果 resources 目录不存在，回退到项目根目录
        if not os.path.exists(base):
            base = os.path.abspath(".")
    return os.path.join(base, filename)


SCRIPT_DIR = get_app_root()
LOCAL_LOG_BASE = os.path.join(SCRIPT_DIR, "APPLog")
SSH_KEY = get_resource_path("id_rsa")
#IPMITOOL_EXE_PATH = get_resource_path("ipmitool.exe")
SSH_TIMEOUT = 30
ICO_PATH = get_resource_path('tigo.ico')


REMOTE_BIN_DIR = "/root/product_test_dir"
REMOTE_REBOOT_DIR = os.path.join(REMOTE_BIN_DIR, "Reboot")
LOCAL_POW_SCRIPT = "Powercycle_test.sh"
DRAM_ECC_INJECTOR = "DramErrorInjector"
FLAG_FILE = "round_finish.flag"
REMOTE_FLAG_FILE = f"{REMOTE_BIN_DIR}/{FLAG_FILE}"

MEMORY_ERROR_PATTERNS = [
    r'memory initialization error',
    r'uncorrectable ecc',
    r'\b(?:DIMM|DRAM|ECC|memory)\b.*?\buncorrectable error\b',
    r'\buncorrectable error\b.*?\b(?:DIMM|DRAM|ECC|memory)\b',
    r'dimm.*fail',
    r'DDR training fail',
    r'\btraining failure',
    r'\becc\b.*?(error|fail)\b',
    r'\[Hardware Error\]\s+.*memory.*(error|fail)',
    r'\[Hardware Error\].*\b(rank|bank|column)\b',
    r'\bDQ\[.*?\]\s+Error\b',
    r'ReportDimmErrToBMC',
    r'eye width.*too small',
    r'RxDqsBitDelay.*Out of range',
    r'Rec En Logic Delay.*\>.*\*',
    r'N\d+\.C\d+\.D\d+\.R\d+:\s*Failure\s+detected'
]

STRESSAPPTEST_ERROR_PATTERNS = [
    r'Process Error',
    r'freepages < neededpages',
    r'Initialize Pages failed',
    r'Sat::Initialize\(\) failed',
    r'FAIL - test encountered procedural errors',
    r'Fatal issue encountered',
]


def get_ipmitool_path():
    """返回 ipmitool.exe 的完整路径"""
    base = get_resource_path("")
    return os.path.join(base, "ipmitool", "ipmitool.exe")