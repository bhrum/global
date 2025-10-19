# -*- coding: utf-8 -*-
import socket
import os
import random
import time
import hashlib
import struct
import subprocess
import ipaddress
import csv
import threading
import json
import signal
import re
import queue
import sys
from datetime import datetime, timedelta
from collections import deque

# --- 【优化】检测运行环境 ---
# 程序现在仅以非交互模式运行，无图形界面
IS_INTERACTIVE = False


# 尝试导入 psutil 库，这是脚本核心功能所必需的
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[!] 严重警告: 未找到 'psutil' 库。")
    print("[!] 驱动器自动扫描功能将完全失效。")
    print("[!] 请运行 'pip install psutil' 来安装此依赖库。")

# ==============================================================================
# --- 配置区域 ---
# ==============================================================================

# --- 文件与全局控制 ---
MIN_FILE_DELAY_S = 3
MAX_FILE_DELAY_S = 7
MONTHLY_LIMIT_GB = 180000000000000000000000

# --- 数据库与状态文件路径 ---
ASN_BLOCKS_DB_PATH = 'GeoLite2-ASN-Blocks-IPv4.csv'
COUNTRY_BLOCKS_DB_PATH = 'GeoLite2-Country-Blocks-IPv4.csv'
COUNTRY_LOCATIONS_DB_PATH = 'GeoLite2-Country-Locations-en.csv'
STATE_FILE_PATH = 'dandelion_state.json'
ASN_EXCLUSION_LIST = []

# --- 【优化】线程与数据包配置 ---
CPU_COUNT = os.cpu_count() or 1
MAX_WORKERS = CPU_COUNT * 50
CHUNK_SIZE = 1024
# 【修改】使用双队列系统
WAN_TASK_QUEUE = queue.Queue(maxsize=MAX_WORKERS * 4)
LAN_TASK_QUEUE = queue.Queue(maxsize=MAX_WORKERS * 4)
RANDOM_IP_POOL_SIZE = 1000

# --- 驱动器与网络扫描 ---
ENABLE_DRIVE_AUTO_SCAN = True
ENABLE_THROTTLED_SURFACE_SCAN = True
ENABLE_METADATA_SCAN = True # 新增：控制元数据扫描的开关
ENABLE_FORCED_LAN_SCAN = True
FALLBACK_SUBNETS = [
    "192.168.0.0/24", "192.168.1.0/24", "192.168.43.0/24",
    "172.20.10.0/24", "10.0.0.0/24", "10.42.0.0/24",
]

# --- 其他功能 ---
ENABLE_LOOPBACK_SEND = True
ENABLE_RENAMING_EFFECT = True
RENAMING_INTERVAL = 0.01

# --- 基础配置 ---
# 【优化】使用脚本所在目录作为基准目录，确保在任何工作目录下都能正确找到文件
BASE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOURCE_DIRECTORY = BASE_DIRECTORY

# --- 全局控制变量 ---
EXIT_FLAG = threading.Event()
GLOBAL_COUNTRY_POOL = []
state_lock = threading.Lock()
active_threads = []
source_dirs_lock = threading.Lock()
DYNAMIC_SOURCE_DIRS = []

# --- 速度控制全局变量 ---
params_lock = threading.Lock()
CURRENT_IP_BATCH_SIZE = 10
CURRENT_DELAY_BETWEEN_BATCHES_S = 0.5
CURRENT_TARGET_WORKERS = 2

# --- 流量追踪 (线程安全) ---
session_total_bytes_sent = 0
session_bytes_lock = threading.Lock()

# --- 状态显示与日志 ---
WORKER_STATUS = {}
LOG_QUEUE = deque(maxlen=100) # 增加日志队列容量以备非交互模式使用
STATUS_LOCK = threading.Lock()

# --- 性能优化缓存 ---
IP_CACHE = {}
NETWORK_CACHE = {}
CACHE_LOCK = threading.Lock()

# --- 速度等级定义 ---
SPEED_LEVELS = [
    (8, 1000, 1),       # Level 0 (最低速度)
    (11, 200, 2),       # Level 1 (白天限速) - 约 110 KB/s
    (25, 80, 4),        # Level 2
    (35, 40, 6),        # Level 3
    (50, 25, 8),        # Level 4
    (70, 15, MAX_WORKERS) # Level 5 (夜间全速)
]


# ==============================================================================
# --- 辅助函数与线程类定义 ---
# ==============================================================================
def log_message(message):
    timestamp = datetime.now().strftime('%H:%M:%S')
    full_message = f"[{timestamp}] {message}"
    LOG_QUEUE.append(full_message)
    # 【优化】在非交互模式下，直接打印日志
    if not IS_INTERACTIVE:
        print(full_message)


# 图形界面功能已移除 - create_progress_bar函数不再需要

def find_specific_interfaces():
    wired_iface, wireless_iface = None, None
    if not PSUTIL_AVAILABLE:
        log_message("[!] [网卡识别] psutil 不可用，无法绑定特定网卡。")
        return None, None
    try:
        addrs, stats = psutil.net_if_addrs(), psutil.net_if_stats()
        for iface, iface_addrs in addrs.items():
            if iface not in stats or not stats[iface].isup: continue
            # 修复：检查isloop属性是否存在，如果不存在则使用其他方法判断
            try:
                if hasattr(stats[iface], 'isloop') and stats[iface].isloop: continue
                # 备选方法：通过接口名称判断是否为回环接口
                if iface.lower() in ['lo', 'lo0', 'loopback']: continue
            except: continue
            ipv4_addr = next((addr.address for addr in iface_addrs if addr.family == socket.AF_INET), None)
            if not ipv4_addr: continue
            iface_lower = iface.lower()
            if not wired_iface and any(k in iface_lower for k in ['ethernet', 'eth', 'enp', 'eno', 'ens']):
                wired_iface = {'name': iface, 'ip': ipv4_addr}
                log_message(f"[*] [网卡识别] 发现有线网卡: {iface} ({ipv4_addr})")
            if not wireless_iface and any(k in iface_lower for k in ['wi-fi', 'wlan', 'wlp', 'wlx']):
                wireless_iface = {'name': iface, 'ip': ipv4_addr}
                log_message(f"[*] [网卡识别] 发现无线网卡: {iface} ({ipv4_addr})")
            if wired_iface and wireless_iface: break
    except Exception as e:
        log_message(f"[!] [网卡识别] 查找特定网卡时出错: {e}")
    return wired_iface, wireless_iface

# ==============================================================================
# --- 【核心引擎】 定时调速 ---
# ==============================================================================
def _apply_speed_level(level, reason):
    if not (0 <= level < len(SPEED_LEVELS)):
        log_message(f"[!] [定时调速] 无效的速度等级: {level}")
        return
    batch_size, delay_ms, num_workers = SPEED_LEVELS[level]
    with params_lock:
        global CURRENT_IP_BATCH_SIZE, CURRENT_DELAY_BETWEEN_BATCHES_S, CURRENT_TARGET_WORKERS
        CURRENT_IP_BATCH_SIZE = batch_size
        CURRENT_DELAY_BETWEEN_BATCHES_S = delay_ms / 1000.0
        CURRENT_TARGET_WORKERS = num_workers
    progress_str = f"等级 {level}/{len(SPEED_LEVELS)-1}"
    details_str = f"并发:{num_workers}, 批次:{batch_size}, 延迟:{delay_ms}ms"
    with STATUS_LOCK:
        WORKER_STATUS["定时调速引擎"] = {"file": "N/A", "mode": reason, "progress": progress_str, "details": details_str}

class TimeBasedPacerThread(threading.Thread):
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event, self.daemon, self.name = stop_event, True, "定时调速引擎"
        self.current_mode = None
    def run(self):
        log_message(f"[*] [{self.name}] 引擎已启动。")
        while not self.stop_event.is_set():
            now = datetime.now()
            # 【修改】将全速时间调整为凌晨1点到早上7点 (1:00 - 6:59)
            if now.hour >= 1 and now.hour < 7:
                if self.current_mode != "NIGHT":
                    log_message(f"[*] [{self.name}] 进入夜间全速模式。")
                    _apply_speed_level(5, "夜间全速")
                    self.current_mode = "NIGHT"
            else:
                if self.current_mode != "DAY":
                    log_message(f"[*] [{self.name}] 进入白天限速模式 (约 110 KB/s)。")
                    _apply_speed_level(1, "白天限速")
                    self.current_mode = "DAY"
            self.stop_event.wait(60)
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 线程已停止。")

# ==============================================================================
# --- 【修改】单线程节流表面扫描 ---
# ==============================================================================
class ThrottledSurfaceScanThread(threading.Thread):
    """
    一个独立的后台线程，用于【单线程】循环对所有大于100GB的驱动器执行低优先级的表面读取扫描。
    """
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event
        self.daemon = True
        self.name = "节流表面扫描器"
        self.read_chunk_size = 4096
        self.delay_between_reads = 0.001
        self.min_disk_size_gb = 100

    def run(self):
        log_message(f"[*] [{self.name}] 引擎已启动，将【单线程】循环扫描所有大于 {self.min_disk_size_gb}GB 的硬盘。")
        if not PSUTIL_AVAILABLE:
            log_message(f"[!] [{self.name}] 已禁用 ('psutil' 不可用)。")
            return

        min_size_bytes = self.min_disk_size_gb * (1024 ** 3)

        while not self.stop_event.is_set():
            scannable_partitions = []
            try:
                all_partitions = psutil.disk_partitions(all=True)
                for p in all_partitions:
                    try:
                        usage = psutil.disk_usage(p.mountpoint)
                        if usage.total < min_size_bytes:
                            continue
                    except Exception:
                        continue
                    
                    opts_list = p.opts.split(',')
                    skip_reason = None
                    if not p.device.startswith('/dev/') and os.name != 'nt': skip_reason = "非物理设备"
                    elif 'ro' in opts_list: skip_reason = "只读"
                    elif 'cdrom' in opts_list: skip_reason = "光驱"
                    elif 'loop' in p.device: skip_reason = "虚拟设备"
                    elif not p.fstype or p.fstype in ['sysfs', 'proc', 'devtmpfs', 'devpts', 'tmpfs', 'securityfs', 'cgroup2', 'pstore', 'efivarfs', 'bpf', 'autofs', 'hugetlbfs', 'mqueue', 'debugfs', 'tracefs', 'fusectl', 'configfs', 'nfsd', 'ramfs', 'rpc_pipefs', 'binfmt_misc']:
                        skip_reason = f"系统/虚拟文件系统({p.fstype})"
                    
                    if not skip_reason:
                        scannable_partitions.append(p)
            except Exception as e:
                log_message(f"[!] [{self.name}] 获取驱动器列表时出错: {e}")
                self.stop_event.wait(60)
                continue

            if not scannable_partitions:
                with STATUS_LOCK:
                    WORKER_STATUS[self.name] = { "file": "N/A", "mode": "等待驱动器", "progress": "---", "details": f"未找到 >{self.min_disk_size_gb}GB 的驱动器" }
                self.stop_event.wait(60)
                continue

            for p in scannable_partitions:
                if self.stop_event.is_set(): break
                
                device_path = p.device
                if os.name == 'nt':
                    clean_device_name = p.device.strip('\\')
                    device_path = f"\\\\.\\{clean_device_name}"

                total_size = 0
                try:
                    total_size = psutil.disk_usage(p.mountpoint).total
                except Exception as e:
                    log_message(f"[*] [{self.name}] 无法获取 {p.device} 大小，跳过: {e}")
                    continue
                
                if total_size == 0: continue

                handle = None
                try:
                    handle = open(device_path, 'rb')
                    log_message(f"[*] [{self.name}] 开始对 {p.device} ({total_size >> 30} GB) 进行表面扫描。")

                    while not self.stop_event.is_set():
                        chunk = handle.read(self.read_chunk_size)
                        if not chunk: break

                        current_pos = handle.tell()
                        progress = (current_pos / total_size) * 100
                        details = f"{current_pos >> 20}MB / {total_size >> 20}MB"
                        with STATUS_LOCK:
                            WORKER_STATUS[self.name] = {"file": p.device, "mode": "节流表面扫描", "progress": f"{progress:.1f}%", "details": details}
                        time.sleep(self.delay_between_reads)

                    if not self.stop_event.is_set():
                        log_message(f"[*] [{self.name}] 对 {p.device} 的扫描已完成。")
                        with STATUS_LOCK:
                            WORKER_STATUS[self.name] = {"file": p.device, "mode": "完成", "progress": "100%", "details": "即将扫描下一个"}

                except PermissionError:
                    log_message(f"[!] [{self.name}] 权限不足，无法打开 {device_path}。")
                    with STATUS_LOCK:
                        WORKER_STATUS[self.name] = {"file": p.device, "mode": "权限错误", "progress": "---", "details": "请以管理员/root身份运行"}
                    self.stop_event.wait(300)
                except IOError as e:
                    log_message(f"[!] [{self.name}] 读取 {device_path} 时发生I/O错误: {e}")
                except Exception as e:
                    log_message(f"[!] [{self.name}] 扫描 {device_path} 时发生未知错误: {e}")
                finally:
                    if handle: handle.close()
        
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 引擎已停止。")

# ==============================================================================
# --- 【修改】单线程元数据扫描 ---
# ==============================================================================
class MetadataScanThread(threading.Thread):
    """
    一个独立的后台线程，用于【单线程】循环对所有大于100GB的驱动器执行高速的元数据扫描。
    """
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event
        self.daemon = True
        self.name = "元数据扫描器"
        self.min_disk_size_gb = 100

    def run(self):
        log_message(f"[*] [{self.name}] 引擎已启动，将【单线程】循环扫描所有大于 {self.min_disk_size_gb}GB 硬盘的元数据。")
        if not PSUTIL_AVAILABLE:
            log_message(f"[!] [{self.name}] 已禁用 ('psutil' 不可用)。")
            return
        
        min_size_bytes = self.min_disk_size_gb * (1024 ** 3)

        while not self.stop_event.is_set():
            scannable_mounts = []
            try:
                for p in psutil.disk_partitions(all=False):
                    try:
                        usage = psutil.disk_usage(p.mountpoint)
                        if usage.total < min_size_bytes:
                            continue
                    except Exception:
                        continue
                    
                    opts_list = p.opts.split(',')
                    if 'ro' in opts_list: continue
                    if p.fstype.lower() in ['sysfs', 'proc', 'devtmpfs', 'devpts', 'tmpfs', 'securityfs', 'cgroup', 'cgroup2', 'pstore', 'efivarfs', 'bpf', 'autofs', 'hugetlbfs', 'mqueue', 'debugfs', 'tracefs', 'fusectl', 'configfs', 'nfs', 'nfsd', 'ramfs', 'rpc_pipefs', 'binfmt_misc', 'smb', 'cifs']: continue
                    if 'cdrom' in opts_list or 'loop' in p.device: continue
                    if os.name != 'nt' and not p.device.startswith('/dev/'): continue
                    scannable_mounts.append(p)
            except Exception as e:
                log_message(f"[!] [{self.name}] 获取挂载点列表时出错: {e}")
                self.stop_event.wait(60)
                continue

            if not scannable_mounts:
                with STATUS_LOCK:
                    WORKER_STATUS[self.name] = { "file": "N/A", "mode": "等待驱动器", "progress": "---", "details": f"未找到 >{self.min_disk_size_gb}GB 的驱动器" }
                self.stop_event.wait(60)
                continue

            for p in scannable_mounts:
                if self.stop_event.is_set(): break
                mountpoint = p.mountpoint
                try:
                    log_message(f"[*] [{self.name}] 开始对 {mountpoint} 进行元数据扫描。")
                    files_scanned = 0
                    dirs_scanned = 0
                    for _root, dirs, files in os.walk(mountpoint, topdown=True):
                        if self.stop_event.is_set(): break
                        dirs[:] = [d for d in dirs if not d.startswith('.')]
                        files_scanned += len(files)
                        dirs_scanned += len(dirs)
                        with STATUS_LOCK:
                            WORKER_STATUS[self.name] = {
                                "file": mountpoint, "mode": "元数据扫描", "progress": "进行中", "details": f"扫描 {dirs_scanned} 目录, {files_scanned} 文件"
                            }
                    
                    if not self.stop_event.is_set():
                        log_message(f"[*] [{self.name}] 对 {mountpoint} 的元数据扫描已完成。")
                        with STATUS_LOCK:
                            WORKER_STATUS[self.name] = {
                                "file": mountpoint, "mode": "完成", "progress": "100%", "details": f"共扫描 {files_scanned} 文件"
                            }
                except Exception as e:
                    log_message(f"[!] [{self.name}] 元数据扫描 {mountpoint} 时出错: {e}")
                    self.stop_event.wait(10)

        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 引擎已停止。")


# ==============================================================================
# --- 其他线程与工具函数 ---
# ==============================================================================
class RenamerThread(threading.Thread):
    def __init__(self, file_path, stop_event):
        super().__init__()
        self.original_path, self.current_path = file_path, file_path
        self.stop_event, self.daemon = stop_event, True
        self.directory = os.path.dirname(file_path)
        self.original_basename = os.path.basename(file_path)
        self.original_name_part, self.original_ext_part = os.path.splitext(self.original_basename)
    def run(self):
        try:
            while not self.stop_event.is_set() and not EXIT_FLAG.is_set():
                new_name = f"{self.original_name_part}.{time.time_ns()}{self.original_ext_part}"
                new_path = os.path.join(self.directory, new_name)
                try:
                    os.rename(self.current_path, new_path)
                    self.current_path = new_path
                except FileNotFoundError: break
                except Exception as e: log_message(f"[!] 重命名 {self.original_basename} 时出错: {e}"); break
                time.sleep(RENAMING_INTERVAL)
        finally: self.restore_filename()
    def restore_filename(self):
        if self.current_path != self.original_path:
            try:
                if not os.path.exists(self.original_path): os.rename(self.current_path, self.original_path)
                else: log_message(f"[!] 警告: 无法恢复 '{self.original_basename}'，目标已存在。")
            except Exception as e: log_message(f"[!] 恢复 '{self.original_basename}' 失败: {e}")

class ForcedNetworkScanThread(threading.Thread):
    def __init__(self, stop_event, lan_socket, lan_queue):
        super().__init__()
        self.stop_event, self.daemon, self.name = stop_event, True, "局域网广播器"
        self.lan_socket = lan_socket
        self.lan_queue = lan_queue
    def run(self):
        if self.lan_socket is None:
            log_message("[!] [广播线程] 无可用无线网卡，线程已禁用。")
            with STATUS_LOCK:
                WORKER_STATUS[self.name] = {"file": "N/A", "mode": "禁用", "progress": "---", "details": "无可用无线网卡"}
            while not self.stop_event.is_set(): self.stop_event.wait(60)
            return

        permission_warning_shown = False
        while not self.stop_event.is_set():
            try:
                packet_to_send, _, _, _ = self.lan_queue.get(timeout=1)
                for subnet_str in FALLBACK_SUBNETS:
                    if self.stop_event.is_set(): break
                    try:
                        self.lan_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        broadcast_ip = str(ipaddress.ip_network(subnet_str).broadcast_address)
                        self.lan_socket.sendto(packet_to_send, (broadcast_ip, random.randint(10240, 65535)))
                    except ValueError: continue
                    except PermissionError:
                        if not permission_warning_shown:
                            log_message("[!] [广播线程] 权限错误: 请使用 root 或管理员权限。")
                            permission_warning_shown = True
                        break
                    except Exception as e:
                        log_message(f"[!] [广播线程] 广播至 {subnet_str} 时出错: {e}")
                self.lan_queue.task_done()
                with STATUS_LOCK:
                    WORKER_STATUS[self.name] = {"file": "N/A", "mode": "本地广播", "progress": f"{self.lan_queue.qsize()} 排队", "details": f"广播至 {len(FALLBACK_SUBNETS)} 个子网"}
            except queue.Empty:
                with STATUS_LOCK:
                    WORKER_STATUS[self.name] = {"file": "N/A", "mode": "本地广播", "progress": f"{self.lan_queue.qsize()} 排队", "details": "等待数据包..."}
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 线程已停止。")

# 【优化】为非交互模式添加的日志打印线程
class HeadlessLogPrinter(threading.Thread):
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event
        self.daemon = True
        self.name = "HeadlessLogger"

    def run(self):
        """
        在非交互模式下，此线程会定期从 LOG_QUEUE 中取出日志并打印到标准输出。
        注意：log_message 函数现在会直接打印，这个线程作为备用和确保队列消息被处理。
        """
        log_message(f"[*] [{self.name}] 无头日志打印机已启动。")
        while not self.stop_event.wait(5): # 每5秒检查一次队列
            while True:
                try:
                    # LOG_QUEUE 是一个 deque，popleft 是高效的
                    # log_message 已经打印了，这里清空队列即可
                    LOG_QUEUE.popleft()
                except IndexError:
                    break # 队列为空
        log_message(f"[*] [{self.name}] 线程已停止。")

# 图形界面功能已移除 - 程序现在仅以非交互模式运行

class StartupDriveScanner(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon, self.name = True, "驱动器扫描器"
    def run(self):
        global DYNAMIC_SOURCE_DIRS
        if not PSUTIL_AVAILABLE:
            log_message(f"[!] [{self.name}] 已禁用 ('psutil' 不可用)，仅使用默认目录。")
            with source_dirs_lock:
                if os.path.isdir(DEFAULT_SOURCE_DIRECTORY): DYNAMIC_SOURCE_DIRS = [DEFAULT_SOURCE_DIRECTORY]
            return
        log_message(f"[*] {self.name} 已启动，开始进行一次性驱动器扫描...")
        with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "检测驱动器", "progress": "50.0%", "details": "正在扫描分区..."}
        found_dirs = {os.path.realpath(DEFAULT_SOURCE_DIRECTORY)}
        # 添加当前工作目录
        current_work_dir = os.getcwd()
        if os.path.isdir(current_work_dir) and current_work_dir != DEFAULT_SOURCE_DIRECTORY:
            found_dirs.add(os.path.realpath(current_work_dir))
            log_message(f"[*] [{self.name}] 添加当前工作目录: {current_work_dir}")
        try:
            for p in psutil.disk_partitions(all=False):
                if 'cdrom' in p.opts or p.fstype == '' or 'loop' in p.device or 'ro' in p.opts: continue
                if os.name == 'nt' and 'rw' not in p.opts: continue
                if 'removable' in p.opts or 'external' in p.opts:
                    log_message(f"[*] [{self.name}] 跳过可移动驱动器: {p.mountpoint}")
                    continue
                found_dirs.add(os.path.realpath(p.mountpoint))
        except Exception as e: log_message(f"[!] [{self.name}] 扫描驱动器时出错: {e}")
        with source_dirs_lock:
            new_dirs = sorted(list(found_dirs))
            if len(new_dirs) > 1 and '/' in new_dirs:
                new_dirs.remove('/')
                log_message(f"[*] [{self.name}] 发现其他分区，将从扫描列表中移除根目录 '/'。")
            log_message(f"[*] [{self.name}] 驱动器扫描完成。发现目录: {', '.join(new_dirs)}")
            DYNAMIC_SOURCE_DIRS = new_dirs
        details_str = f"扫描完成，发现 {len(DYNAMIC_SOURCE_DIRS)} 个位置。"
        with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "完成", "progress": "100.0%", "details": details_str}
        time.sleep(10)
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 线程已结束。")

class FileScannerThread(threading.Thread):
    def __init__(self, wan_queue, lan_queue, files_to_exclude, lan_socket):
        super().__init__()
        self.wan_queue, self.lan_queue = wan_queue, lan_queue
        self.files_to_exclude = files_to_exclude
        self.lan_socket = lan_socket
        self.name, self.daemon = "文件扫描器", True
    def run(self):
        log_message(f"[*] {self.name} 已启动。")
        while not EXIT_FLAG.is_set():
            with source_dirs_lock:
                current_dirs_to_scan = list(DYNAMIC_SOURCE_DIRS) if DYNAMIC_SOURCE_DIRS else []
            if not current_dirs_to_scan:
                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "等待目录", "progress": "---", "details": "等待驱动器扫描结果..."}
                time.sleep(10)
                continue
            
            dirs_scanned, files_found = 0, 0
            for directory in current_dirs_to_scan:
                if EXIT_FLAG.is_set(): break
                if not os.path.isdir(directory): continue
                restore_interrupted_files(directory, self.name)
                try:
                    for root, dirs, files in os.walk(directory, topdown=True):
                        if EXIT_FLAG.is_set(): break
                        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
                        dirs_scanned += 1
                        for file in files:
                            if EXIT_FLAG.is_set(): break
                            if not file.startswith('.') and file not in self.files_to_exclude:
                                files_found += 1
                                self.process_file(os.path.join(root, file))
                        with STATUS_LOCK:
                            WORKER_STATUS[self.name] = {"file": os.path.basename(directory), "mode": "扫描中", "progress": "...", "details": f"已扫 {dirs_scanned} 目录, 发现 {files_found} 文件"}
                except Exception as e:
                    log_message(f"[!] [{self.name}] 扫描目录 '{directory}' 时出错: {e}")

            if not EXIT_FLAG.is_set():
                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "空闲", "progress": "100%", "details": f"一轮扫描完成，发现 {files_found} 文件。"}

        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 线程已停止。")
    def process_file(self, file_path):
        if load_state()['total_sent_gb'] >= MONTHLY_LIMIT_GB:
            log_message(f"\n[!] 已达到月度流量上限。{self.name} 暂停..."); time.sleep(3600); return
        file_handle, renamer_thread, renamer_stop_event = None, None, None
        try:
            if not os.path.exists(file_path): return
            file_size = os.path.getsize(file_path)
            if file_size == 0: return
            file_handle = open(file_path, 'rb')
            if ENABLE_RENAMING_EFFECT:
                renamer_stop_event = threading.Event()
                renamer_thread = RenamerThread(file_path, renamer_stop_event); renamer_thread.start()
            chunk_index = 0
            while not EXIT_FLAG.is_set():
                if load_state()['total_sent_gb'] >= MONTHLY_LIMIT_GB: break
                chunk_data = file_handle.read(CHUNK_SIZE)
                if not chunk_data: break
                packet = struct.pack('!I', chunk_index) + hashlib.md5(chunk_data).digest() + chunk_data
                chunk_index += 1
                task = (packet, os.path.basename(file_path), file_size, file_handle.tell())
                
                self.wan_queue.put(task)
                if self.lan_socket is not None:
                    self.lan_queue.put(task)
                
                progress = (file_handle.tell() / file_size) * 100
                with STATUS_LOCK:
                    WORKER_STATUS[self.name] = {"file": os.path.basename(file_path), "mode": "读取并入队", "progress": f"{progress:.1f}%", "details": f"处理数据块 {chunk_index}"}
        except (IOError, OSError) as e: log_message(f"[!] [{self.name}] I/O 错误: {e}")
        except Exception as e:
            if not EXIT_FLAG.is_set(): log_message(f"[!] [{self.name}] 处理时发生意外错误: {e}")
        finally:
            if file_handle: file_handle.close()
            if renamer_thread: renamer_stop_event.set(); renamer_thread.join(timeout=2.0)
            if not EXIT_FLAG.is_set(): time.sleep(random.uniform(MIN_FILE_DELAY_S, MAX_FILE_DELAY_S))

class ChunkSenderWorker(threading.Thread):
    def __init__(self, task_queue, name, udp_socket, target_ip_pool, stop_event):
        super().__init__()
        self.task_queue, self.name, self.udp_socket = task_queue, name, udp_socket
        self.target_ip_pool, self.stop_event, self.daemon = target_ip_pool, stop_event, True
    def run(self):
        while not EXIT_FLAG.is_set() and not self.stop_event.is_set():
            try:
                task = self.task_queue.get(timeout=1)
                packet, filename, total_size, current_pos = task
                progress = (current_pos / total_size) * 100 if total_size > 0 else 0
                is_internet_ok = check_internet_connectivity()
                mode_str = "广域网发送" if is_internet_ok else "离线"
                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": filename, "mode": mode_str, "progress": f"{progress:.1f}%", "details": "发送中..."}
                if is_internet_ok:
                    bytes_sent = send_chunk_in_batches(self.udp_socket, packet, self.target_ip_pool)
                    if bytes_sent > 0:
                        with session_bytes_lock: global session_total_bytes_sent; session_total_bytes_sent += bytes_sent
                self.task_queue.task_done()
            except queue.Empty:
                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "空闲", "progress": "---", "details": "等待任务"}
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]

class WorkerManagerThread(threading.Thread):
    def __init__(self, task_queue, udp_socket, target_ip_pool):
        super().__init__()
        self.name, self.daemon = "工人管理器", True
        self.task_queue, self.udp_socket, self.target_ip_pool = task_queue, udp_socket, target_ip_pool
        self.active_workers = {}
    def run(self):
        log_message(f"[*] {self.name} 已启动。")
        worker_id_counter = 0
        while not EXIT_FLAG.is_set():
            with params_lock: target_count = CURRENT_TARGET_WORKERS
            current_count = len(self.active_workers)
            with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "管理并发", "progress": "---", "details": f"当前: {current_count} / 目标: {target_count}"}
            if current_count < target_count:
                for _ in range(target_count - current_count):
                    worker_id_counter += 1
                    worker_name = f"发送器-{worker_id_counter}"
                    stop_event = threading.Event()
                    worker = ChunkSenderWorker(self.task_queue, worker_name, self.udp_socket, self.target_ip_pool, stop_event)
                    worker.start()
                    self.active_workers[worker_name] = (worker, stop_event)
            elif current_count > target_count:
                for worker_name in list(self.active_workers.keys())[:current_count - target_count]:
                    _worker_thread, stop_event = self.active_workers.pop(worker_name)
                    stop_event.set()
            for name in [name for name, (thread, _event) in self.active_workers.items() if not thread.is_alive()]:
                del self.active_workers[name]
            time.sleep(1)
        for _name, (_thread, stop_event) in self.active_workers.items(): stop_event.set()
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 线程已停止。")

class StateSaveThread(threading.Thread):
    def __init__(self, stop_event, interval_s=60):
        super().__init__()
        self.stop_event, self.interval_s = stop_event, interval_s
        self.daemon, self.name = True, "状态保存器"
    def run(self):
        log_message(f"[*] {self.name} 已启动，每 {self.interval_s} 秒保存一次。")
        while not self.stop_event.wait(self.interval_s): self.save_current_state()
        log_message(f"[*] {self.name} 正在执行最后一次状态保存..."); self.save_current_state()
        log_message(f"[*] {self.name} 线程已停止。")
    def save_current_state(self):
        global session_total_bytes_sent
        bytes_to_save = 0
        with session_bytes_lock:
            if session_total_bytes_sent > 0:
                bytes_to_save = session_total_bytes_sent
                session_total_bytes_sent = 0
        
        if bytes_to_save > 0:
            try:
                state = load_state()
                bytes_gb = bytes_to_save / (1024 ** 3)
                state['total_sent_gb'] += bytes_gb
                save_state(state)
                log_message(f"[*] [状态保存] 已将 {bytes_gb:.4f} GB 添加到月度总计。")
            except Exception as e:
                log_message(f"[!] [状态保存] 保存状态时出错: {e}")

# ==============================================================================
# --- 状态管理与工具函数 ---
# ==============================================================================
def graceful_shutdown(signum, frame):
    if EXIT_FLAG.is_set(): return
    log_message(f"\n[*] 收到终止信号 {signum}。准备优雅退出...")
    EXIT_FLAG.set()
    time.sleep(0.2)
    alive = [t for t in active_threads if t.is_alive()]
    if alive:
        log_message(f"[*] 等待 {len(alive)} 个工作线程完成...")
        for thread in alive: thread.join(timeout=5.0)
    log_message("[*] 清理完成，程序即将退出。")

def load_state():
    state_path = os.path.join(BASE_DIRECTORY, STATE_FILE_PATH)
    current_month_str = datetime.now().strftime("%Y-%m")
    try:
        with state_lock, open(state_path, 'r') as f:
            state = json.load(f)
        if state.get('current_month') != current_month_str:
            log_message("[*] 检测到新的月份。重置流量计数器。")
            state = {'current_month': current_month_str, 'total_sent_gb': 0.0}
            save_state(state)
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        state = {'current_month': current_month_str, 'total_sent_gb': 0.0}
        save_state(state)
        return state

def save_state(state):
    state_path = os.path.join(BASE_DIRECTORY, STATE_FILE_PATH)
    try:
        with state_lock, open(state_path, 'w') as f:
            json.dump(state, f, indent=4)
    except Exception as e: log_message(f"[!] 严重错误: 无法保存状态文件 '{state_path}': {e}")

def load_country_database(blocks_path, locations_path):
    blocks_path = os.path.join(BASE_DIRECTORY, blocks_path)
    locations_path = os.path.join(BASE_DIRECTORY, locations_path)
    if not os.path.exists(locations_path) or not os.path.exists(blocks_path): return None
    id_to_country, country_ip_ranges = {}, {}
    try:
        with open(locations_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if row and len(row) > 5 and row[5]: id_to_country[row[0]] = row[5]
    except Exception as e: log_message(f"[!] 读取国家位置文件时出错: {e}"); return None
    try:
        with open(blocks_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if row and len(row) > 1:
                    country_name = id_to_country.get(row[1])
                    if country_name: country_ip_ranges.setdefault(country_name, []).append(row[0])
    except Exception as e: log_message(f"[!] 读取国家 IP 范围文件时出错: {e}"); return None
    log_message(f"[+] 国家数据库加载完毕！找到 {len(country_ip_ranges)} 个国家/地区。")
    return country_ip_ranges

def generate_ip_from_cidr(cidr_str):
    with CACHE_LOCK:
        if cidr_str in IP_CACHE:
            net_start, net_end = IP_CACHE[cidr_str]
        else:
            try:
                net = ipaddress.ip_network(cidr_str, strict=False)
                if net.num_addresses <= 2: return None
                net_start, net_end = int(net.network_address) + 1, int(net.broadcast_address) - 1
                IP_CACHE[cidr_str] = (net_start, net_end)
            except ValueError: return None
    return str(ipaddress.ip_address(random.randint(net_start, net_end)))

def check_internet_connectivity():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(("8.8.8.8", 53))
        return True
    except socket.error: return False
    
def scan_network_devices():
    devices = []
    try:
        cmd = ['arp', '-a']
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if any(net in line for net in ['192.168.', '10.', '172.']):
                    parts = line.split()
                    if len(parts) >= 1:
                        try:
                            devices.append(str(ipaddress.ip_address(parts[0].strip('()'))))
                        except ValueError:
                            continue
    except Exception:
        pass
    return sorted(list(set(devices)))

# 图形界面功能已移除 - analyze_network_connections函数不再需要

def prepare_target_ip_pool(country_db):
    target_ips = []; log_message("[*] [IP池] 准备目标IP地址池...")
    if country_db and GLOBAL_COUNTRY_POOL:
        for country in GLOBAL_COUNTRY_POOL:
            if EXIT_FLAG.is_set(): break
            ranges = country_db.get(country, [])
            if ranges:
                ip_str = generate_ip_from_cidr(random.choice(ranges))
                if ip_str: target_ips.append(ip_str)
    else: target_ips = [str(ipaddress.IPv4Address(random.randint(0, 2**32 - 1))) for _ in range(RANDOM_IP_POOL_SIZE)]
    if ENABLE_LOOPBACK_SEND: target_ips.append('127.0.0.1')
    random.shuffle(target_ips)
    return target_ips

def send_chunk_in_batches(sock, packet, target_ip_pool):
    bytes_sent, packet_len = 0, len(packet)
    with params_lock:
        batch_size = CURRENT_IP_BATCH_SIZE
        delay = CURRENT_DELAY_BETWEEN_BATCHES_S
    if not target_ip_pool or batch_size <= 0: return 0
    pool_len = len(target_ip_pool)
    for i in range(pool_len):
        if EXIT_FLAG.is_set(): break
        try:
            sock.sendto(packet, (target_ip_pool[i], random.randint(10240, 65535)))
            bytes_sent += packet_len
        except OSError: pass
        if (i + 1) % batch_size == 0 and i < pool_len -1:
            time.sleep(delay)
    return bytes_sent

def restore_interrupted_files(directory, thread_name):
    log_message(f"[*] [{thread_name}] 检查 '{directory}' 中是否有未恢复文件...")
    try:
        timestamp_pattern = re.compile(r'(.*)\.(\d{19,})(.*)')
        for root, _, files in os.walk(directory):
            if EXIT_FLAG.is_set(): break
            for filename in files:
                match = timestamp_pattern.match(filename)
                if match:
                    original_name = f"{match.group(1)}{match.group(3)}"
                    current_path = os.path.join(root, filename)
                    restored_path = os.path.join(root, original_name)
                    try:
                        if os.path.exists(restored_path): os.remove(current_path)
                        else: os.rename(current_path, restored_path)
                    except Exception as e: log_message(f"[!] [{thread_name}] 恢复文件 '{filename}' 失败: {e}")
    except Exception as e: log_message(f"[!] [{thread_name}] 检查未恢复文件时发生严重错误: {e}")

# ==============================================================================
# --- 主函数 ---
# ==============================================================================
def main():
    global active_threads
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    # 图形界面功能已移除 - 程序现在仅以非交互模式运行
    log_message("[*] 程序现在仅以非交互模式运行，无图形界面。日志将直接打印到标准输出。")

    time.sleep(0.1) # 稍作等待，确保第一条日志能显示
    log_message("[*] 欢迎使用全球法布施。正在初始化...")

    script_name = os.path.basename(__file__)
    files_to_exclude = {script_name, ASN_BLOCKS_DB_PATH, COUNTRY_BLOCKS_DB_PATH, COUNTRY_LOCATIONS_DB_PATH, STATE_FILE_PATH, 'nohup.out'}
    
    wired_interface, wireless_interface = find_specific_interfaces()
    global_socket, lan_socket = None, None

    try:
        global_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if wired_interface:
            try:
                global_socket.bind((wired_interface['ip'], 0))
                log_message(f"[*] [广域网发送] 成功绑定到有线网口: {wired_interface['name']} ({wired_interface['ip']})")
            except Exception as e:
                log_message(f"[!] [广域网发送] 绑定有线网口失败: {e}。将使用默认接口。")
                global_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) # Recreate if bind fails
        else:
            log_message("[!] [广域网发送] 未找到有线网口。将使用默认接口。")
    except Exception as e: log_message(f"[!] 严重错误: 无法创建广域网套接字: {e}"); return

    if ENABLE_FORCED_LAN_SCAN:
        if wireless_interface:
            try:
                lan_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                lan_socket.bind((wireless_interface['ip'], 0))
                log_message(f"[*] [局域网发送] 成功绑定到无线网卡: {wireless_interface['name']} ({wireless_interface['ip']})")
            except Exception as e:
                log_message(f"[!] [局域网发送] 绑定无线网卡失败: {e}。局域网广播将禁用。")
                if lan_socket: lan_socket.close()
                lan_socket = None
        else:
            log_message("[!] [局域网发送] 未找到无线网卡。局域网广播已禁用。")
            lan_socket = None

    country_db = load_country_database(COUNTRY_BLOCKS_DB_PATH, COUNTRY_LOCATIONS_DB_PATH)
    if country_db: global GLOBAL_COUNTRY_POOL; GLOBAL_COUNTRY_POOL = list(country_db.keys())
    
    target_ip_pool = prepare_target_ip_pool(country_db)
    if not target_ip_pool: log_message("[!] 严重警告: 无法生成任何目标IP。")
    
    pacer_thread = TimeBasedPacerThread(EXIT_FLAG); pacer_thread.start(); active_threads.append(pacer_thread)
    
    lan_scanner_thread = ForcedNetworkScanThread(EXIT_FLAG, lan_socket, LAN_TASK_QUEUE); lan_scanner_thread.start(); active_threads.append(lan_scanner_thread)
    
    state_saver = StateSaveThread(EXIT_FLAG); state_saver.start(); active_threads.append(state_saver)
    
    worker_manager = WorkerManagerThread(WAN_TASK_QUEUE, global_socket, target_ip_pool); worker_manager.start(); active_threads.append(worker_manager)
    
    if ENABLE_DRIVE_AUTO_SCAN:
        drive_monitor = StartupDriveScanner(); drive_monitor.start(); active_threads.append(drive_monitor)
    else:
        with source_dirs_lock:
            if os.path.isdir(DEFAULT_SOURCE_DIRECTORY): DYNAMIC_SOURCE_DIRS.append(DEFAULT_SOURCE_DIRECTORY)
        log_message(f"[*] 驱动器自动扫描已禁用。仅扫描默认目录: {DEFAULT_SOURCE_DIRECTORY}")
    
    if ENABLE_THROTTLED_SURFACE_SCAN:
        surface_scanner = ThrottledSurfaceScanThread(EXIT_FLAG)
        surface_scanner.start()
        active_threads.append(surface_scanner)
    
    if ENABLE_METADATA_SCAN:
        metadata_scanner = MetadataScanThread(EXIT_FLAG)
        metadata_scanner.start()
        active_threads.append(metadata_scanner)

    scanner = FileScannerThread(wan_queue=WAN_TASK_QUEUE, lan_queue=LAN_TASK_QUEUE, files_to_exclude=files_to_exclude, lan_socket=lan_socket)
    scanner.start(); active_threads.append(scanner)
    
    log_message("[*] 初始化完成。系统正在运行。")
    EXIT_FLAG.wait()
    log_message("\n[*] 主线程循环已结束。")
    if global_socket: global_socket.close()
    if lan_socket: lan_socket.close()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if not EXIT_FLAG.is_set():
            print(f"\n[!] 脚本发生未处理的致命错误: {e}")
            import traceback
            traceback.print_exc()
    finally:
        print("\n[*] 程序执行完毕。")
