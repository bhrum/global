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
# 如果 sys.stdout.isatty() 为 True，说明在一个交互式终端中，可以启动UI
# 否则，为非交互式环境（如开机自启服务），应禁用UI
IS_INTERACTIVE = sys.stdout.isatty()

# 尝试导入 curses 库，用于创建高级终端UI
try:
    import curses
    CURSES_AVAILABLE = True
except ImportError:
    CURSES_AVAILABLE = False


# ==============================================================================
# --- Linux 原生系统接口（替代 psutil）---
# ==============================================================================

class DiskPartition:
    """模拟 psutil 的分区对象"""
    def __init__(self, device, mountpoint, fstype, opts):
        self.device = device
        self.mountpoint = mountpoint
        self.fstype = fstype
        self.opts = opts

class DiskUsage:
    """模拟 psutil 的磁盘使用对象"""
    def __init__(self, total, used, free):
        self.total = total
        self.used = used
        self.free = free

def linux_get_disk_partitions(all_partitions=False):
    """
    解析 /proc/mounts 获取磁盘分区信息
    返回类似 psutil.disk_partitions() 的分区列表
    """
    partitions = []
    # 仅过滤纯虚拟的系统文件系统类型
    virtual_fs = {'sysfs', 'proc', 'devtmpfs', 'devpts', 'securityfs',
                  'cgroup', 'cgroup2', 'pstore', 'efivarfs', 'bpf', 'autofs',
                  'hugetlbfs', 'mqueue', 'debugfs', 'tracefs', 'fusectl',
                  'configfs', 'nfsd', 'rpc_pipefs', 'binfmt_misc', 'nsfs'}
    
    try:
        with open('/proc/mounts', 'r') as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 4:
                    continue
                device, mountpoint, fstype, opts = parts[0], parts[1], parts[2], parts[3]
                
                # 过滤虚拟文件系统（除非 all_partitions=True）
                if not all_partitions:
                    if fstype in virtual_fs:
                        continue
                    # 【修复】更宽松的设备过滤，允许 fuse 和绑定挂载
                    # 只跳过明显的非存储设备，但保留 overlay、tmpfs 等可能含数据的分区
                    if device in ('none', 'udev', 'devpts'):
                        continue
                
                # 处理挂载点中的转义字符（如 \040 表示空格）
                mountpoint = mountpoint.replace('\\040', ' ').replace('\\011', '\t')
                
                partitions.append(DiskPartition(device, mountpoint, fstype, opts))
    except Exception as e:
        print(f"[!] 读取 /proc/mounts 失败: {e}")
    
    return partitions

def linux_get_disk_usage(mountpoint):
    """
    使用 os.statvfs 获取磁盘使用情况
    返回类似 psutil.disk_usage() 的使用信息对象
    """
    try:
        st = os.statvfs(mountpoint)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize  # 非 root 用户可用空间
        used = total - (st.f_bfree * st.f_frsize)
        return DiskUsage(total, used, free)
    except Exception as e:
        raise OSError(f"无法获取 {mountpoint} 的磁盘使用情况: {e}")

def linux_get_net_interfaces():
    """
    通过 /sys/class/net 和 socket 获取网卡信息
    返回格式: {
        'addrs': {iface_name: [{'address': ip, 'family': AF_INET}]},
        'stats': {iface_name: {'isup': bool, 'isloop': bool}}
    }
    """
    result = {'addrs': {}, 'stats': {}}
    net_path = '/sys/class/net'
    
    try:
        for iface in os.listdir(net_path):
            iface_path = os.path.join(net_path, iface)
            
            # 获取网卡状态
            is_up = False
            is_loop = iface == 'lo'
            operstate_path = os.path.join(iface_path, 'operstate')
            if os.path.exists(operstate_path):
                try:
                    with open(operstate_path, 'r') as f:
                        state = f.read().strip()
                        is_up = state in ('up', 'unknown')
                except:
                    pass
            
            result['stats'][iface] = {'isup': is_up, 'isloop': is_loop}
            result['addrs'][iface] = []
            
            # 获取 IPv4 地址
            try:
                # 使用 socket 获取接口 IP
                import fcntl
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    ip_addr = socket.inet_ntoa(fcntl.ioctl(
                        sock.fileno(),
                        0x8915,  # SIOCGIFADDR
                        struct.pack('256s', iface.encode('utf-8')[:15])
                    )[20:24])
                    result['addrs'][iface].append({
                        'address': ip_addr,
                        'family': socket.AF_INET
                    })
                except:
                    pass
                finally:
                    sock.close()
            except:
                pass
    except Exception as e:
        print(f"[!] 读取网络接口失败: {e}")
    
    return result

def linux_get_net_connections():
    """
    解析 /proc/net/tcp 和 /proc/net/tcp6 获取已建立的连接数
    返回 ESTABLISHED 状态的连接数量
    """
    established_count = 0
    # TCP 状态码 01 = ESTABLISHED
    
    for proto_file in ['/proc/net/tcp', '/proc/net/tcp6']:
        try:
            if not os.path.exists(proto_file):
                continue
            with open(proto_file, 'r') as f:
                lines = f.readlines()[1:]  # 跳过标题行
                for line in lines:
                    parts = line.split()
                    if len(parts) >= 4:
                        # 第4列是状态码
                        state = parts[3]
                        if state == '01':  # ESTABLISHED
                            established_count += 1
        except Exception:
            pass
    
    return established_count

# ==============================================================================
# --- 配置区域 ---
# ==============================================================================

# --- 文件与全局控制 ---
MIN_FILE_DELAY_S = 3
MAX_FILE_DELAY_S = 7

# --- 【极速优化】本地回环配置 ---
LOOPBACK_FILE_DELAY_S = 0  # 本地回环文件间延迟（0=极速）
LOOPBACK_CHUNK_SIZE = 65536  # 本地回环数据块大小（64KB，最大化吞吐）
LOOPBACK_PORT_START = 10000  # 本地回环起始端口
LOOPBACK_PORT_END = 10500    # 本地回环结束端口（500个端口，5倍吞吐）
LOOPBACK_THREAD_COUNT = 8    # 回环发送器线程数（并行发送）
LOOPBACK_BATCH_PORTS = 50    # 每批次发送的端口数（减少循环开销）

# --- 数据库与状态文件路径 ---
ASN_BLOCKS_DB_PATH = 'GeoLite2-ASN-Blocks-IPv4.csv'
COUNTRY_BLOCKS_DB_PATH = 'GeoLite2-Country-Blocks-IPv4.csv'
COUNTRY_LOCATIONS_DB_PATH = 'GeoLite2-Country-Locations-en.csv'
ASN_EXCLUSION_LIST = []

# --- 【优化】线程与数据包配置 ---
CPU_COUNT = os.cpu_count() or 1
MAX_WORKERS = CPU_COUNT * 50
CHUNK_SIZE = 1024
# 【修改】使用三队列系统 - 回环队列无限制,其他队列有限制
WAN_TASK_QUEUE = queue.Queue(maxsize=MAX_WORKERS * 4)
LAN_TASK_QUEUE = queue.Queue(maxsize=MAX_WORKERS * 4)
LOOPBACK_TASK_QUEUE = queue.Queue()  # 无限制以实现最大回环发送速度
RANDOM_IP_POOL_SIZE = 1000

# --- 驱动器与网络扫描 ---
ENABLE_DRIVE_AUTO_SCAN = True
ENABLE_THROTTLED_SURFACE_SCAN = True
ENABLE_METADATA_SCAN = True # 新增：控制元数据扫描的开关
ENABLE_FORCED_LAN_SCAN = True
ENABLE_EXTERNAL_DRIVE_MONITOR = True  # 【新增】外置硬盘自动识别挂载监控
EXTERNAL_DRIVE_SCAN_INTERVAL = 10  # 【新增】外置硬盘扫描间隔（秒）
FALLBACK_SUBNETS = [
    "192.168.0.0/24", "192.168.1.0/24", "192.168.43.0/24",
    "172.20.10.0/24", "10.0.0.0/24", "10.42.0.0/24",
]

# --- 其他功能 ---
ENABLE_LOOPBACK_SEND = True
ENABLE_RENAMING_EFFECT = True
RENAMING_INTERVAL = 0.01

# --- 5G热点配置区域 ---
ENABLE_5G_HOTSPOT = True
HOTSPOT_SSID = "GlobalDandelion_5G"
HOTSPOT_CHANNEL = 36
HOTSPOT_BROADCAST_INTERVAL = 0  # 无延迟，最大速度广播
WIFI_INTERFACE = "wlan0"

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

# --- 速度追踪 ---
WORKER_SPEED_STATS = {}  # 存储每个工作线程的速度统计
SPEED_STATS_LOCK = threading.RLock() # 使用可重入锁以支持嵌套调用

# --- 状态显示与日志 ---
WORKER_STATUS = {}
LOG_QUEUE = deque(maxlen=100) # 增加日志队列容量以备非交互模式使用
STATUS_LOCK = threading.Lock()

# --- 性能优化缓存 ---
IP_CACHE = {}
NETWORK_CACHE = {}

# --- 5G热点状态跟踪 ---
HOTSPOT_STATUS = {'active': False, 'files_broadcast': 0, 'bytes_transmitted': 0}
HOTSPOT_LOCK = threading.Lock()
hotspot_broadcaster = None  # 用于安全关闭
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


def create_progress_bar(percentage, width=10):
    if not isinstance(percentage, (int, float)) or not 0 <= percentage <= 100:
        return f"{str(percentage):<18}"
    filled_len = int(width * percentage // 100)
    bar = '█' * filled_len + '░' * (width - filled_len)
    return f"[{bar}] {percentage:5.1f}%"

def update_speed_stats(worker_name, bytes_sent):
    """
    更新工作线程的速度统计
    【修复】记录已发送字节，速度计算逻辑移至 get_worker_speed 以支持实时预估
    """
    current_time = time.time()
    with SPEED_STATS_LOCK:
        if worker_name not in WORKER_SPEED_STATS:
            WORKER_SPEED_STATS[worker_name] = {
                'bytes_in_window': 0,      # 当前窗口累积的字节数
                'window_start': current_time,  # 窗口开始时间
                'last_update': current_time,
                'speed_history': deque(maxlen=10),
                'current_speed': 0
            }
        
        stats = WORKER_SPEED_STATS[worker_name]
        stats['bytes_in_window'] += bytes_sent
        stats['last_update'] = current_time
        
        # 每隔 0.5 秒更新一次历史速度，降低锁竞争并提高响应速度
        window_duration = current_time - stats['window_start']
        if window_duration >= 0.5:
            speed = stats['bytes_in_window'] / window_duration
            stats['speed_history'].append(speed)
            if stats['speed_history']:
                stats['current_speed'] = sum(stats['speed_history']) / len(stats['speed_history'])
            
            # 重置窗口
            stats['bytes_in_window'] = 0
            stats['window_start'] = current_time

def format_speed(bytes_per_sec):
    """格式化速度显示"""
    if bytes_per_sec < 1024:
        return f"{bytes_per_sec:.1f} B/s"
    elif bytes_per_sec < 1024 * 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    else:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"

def _get_instantaneous_speed(stats, current_time):
    """
    内部辅助函数：根据统计对象计算实时预估速度（需持有 SPEED_STATS_LOCK）
    """
    window_duration = current_time - stats['window_start']
    if window_duration > 0.1 and stats['bytes_in_window'] > 0:
        instant_speed = stats['bytes_in_window'] / window_duration
        if stats.get('current_speed', 0) > 0:
            return (stats['current_speed'] * 0.7) + (instant_speed * 0.3)
        return instant_speed
    return stats.get('current_speed', 0)

def get_worker_speed(worker_name):
    """
    获取工作线程的当前速度（带实时预估）
    """
    current_time = time.time()
    with SPEED_STATS_LOCK:
        if worker_name in WORKER_SPEED_STATS:
            return _get_instantaneous_speed(WORKER_SPEED_STATS[worker_name], current_time)
    return 0

def get_category_speeds():
    """获取各类别的总速度"""
    speeds = {
        '全球发送': 0,
        '本地回环': 0,
        '局域网广播': 0,
        '表面扫描': 0,
        '元数据扫描': 0
    }
    
    current_time = time.time()
    with SPEED_STATS_LOCK:
        for worker_name, stats in WORKER_SPEED_STATS.items():
            speed = _get_instantaneous_speed(stats, current_time)
            # 【修复】更宽松的归类逻辑，涵盖数字命名的端口发送器
            if '回环发送器' in worker_name:
                speeds['本地回环'] += speed
            elif '发送器' in worker_name or '端口' in worker_name:
                speeds['全球发送'] += speed
            elif '广播器' in worker_name:
                speeds['局域网广播'] += speed
            elif '表面扫描' in worker_name:
                speeds['表面扫描'] += speed
            elif '元数据扫描' in worker_name:
                speeds['元数据扫描'] += speed
    
    return speeds

def cleanup_inactive_speed_stats():
    """清理不活跃的速度统计数据"""
    current_time = time.time()
    with SPEED_STATS_LOCK:
        inactive_workers = []
        for worker_name, stats in WORKER_SPEED_STATS.items():
            # 如果超过30秒没有更新，认为是不活跃的
            if current_time - stats['last_update'] > 30:
                inactive_workers.append(worker_name)
        
        for worker_name in inactive_workers:
            del WORKER_SPEED_STATS[worker_name]

def find_specific_interfaces():
    wired_iface, wireless_iface = None, None
    try:
        net_info = linux_get_net_interfaces()
        addrs, stats = net_info['addrs'], net_info['stats']
        for iface, iface_addrs in addrs.items():
            if not (iface in stats and stats[iface]['isup'] and not stats[iface]['isloop']): continue
            ipv4_addr = next((addr['address'] for addr in iface_addrs if addr['family'] == socket.AF_INET), None)
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
        self.name = "全速表面扫描器"
        self.read_chunk_size = 4096
        self.min_disk_size_gb = 100

    def run(self):
        log_message(f"[*] [{self.name}] 引擎已启动，将【全速】循环扫描所有大于 {self.min_disk_size_gb}GB 的硬盘。")

        min_size_bytes = self.min_disk_size_gb * (1024 ** 3)

        while not self.stop_event.is_set():
            scannable_partitions = []
            try:
                all_partitions = linux_get_disk_partitions(all_partitions=True)
                for p in all_partitions:
                    try:
                        usage = linux_get_disk_usage(p.mountpoint)
                        if usage.total < min_size_bytes:
                            continue
                    except Exception:
                        continue
                    
                    opts_list = p.opts.split(',')
                    # 【修改】包含所有硬盘(内置+外置+U盘) - 只跳过系统/虚拟设备
                    if 'ro' in opts_list: continue  # 跳过只读
                    if 'cdrom' in opts_list: continue  # 跳过光驱
                    if 'loop' in p.device: continue  # 跳过虚拟设备
                    if os.name != 'nt' and not p.device.startswith('/dev/'): continue  # Unix下只处理物理设备
                    if not p.fstype or p.fstype in ['sysfs', 'proc', 'devtmpfs', 'devpts', 'tmpfs', 'securityfs', 'cgroup2', 'pstore', 'efivarfs', 'bpf', 'autofs', 'hugetlbfs', 'mqueue', 'debugfs', 'tracefs', 'fusectl', 'configfs', 'nfsd', 'ramfs', 'rpc_pipefs', 'binfmt_misc']:
                        continue  # 跳过系统文件系统
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
                    total_size = linux_get_disk_usage(p.mountpoint).total
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
                        
                        # 更新速度统计
                        update_speed_stats(self.name, len(chunk))
                        current_speed = get_worker_speed(self.name)
                        
                        details = f"{current_pos >> 20}MB / {total_size >> 20}MB | {format_speed(current_speed)}"
                        with STATUS_LOCK:
                            WORKER_STATUS[self.name] = {"file": p.device, "mode": "全速表面扫描", "progress": f"{progress:.1f}%", "details": details}

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
        
        # 清理速度统计
        with SPEED_STATS_LOCK:
            if self.name in WORKER_SPEED_STATS:
                del WORKER_SPEED_STATS[self.name]
        
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
        
        min_size_bytes = self.min_disk_size_gb * (1024 ** 3)

        while not self.stop_event.is_set():
            scannable_mounts = []
            try:
                for p in linux_get_disk_partitions(all_partitions=False):
                    try:
                        usage = linux_get_disk_usage(p.mountpoint)
                        if usage.total < min_size_bytes:
                            continue
                    except Exception:
                        continue
                    
                    opts_list = p.opts.split(',')
                    # 【修改】包含所有硬盘(内置+外置+U盘) - 只跳过系统/虚拟/网络设备
                    if 'ro' in opts_list: continue  # 跳过只读
                    if 'cdrom' in opts_list or 'loop' in p.device: continue  # 跳过光驱和虚拟设备
                    if os.name != 'nt' and not p.device.startswith('/dev/'): continue  # Unix下只处理物理设备
                    if p.fstype.lower() in ['sysfs', 'proc', 'devtmpfs', 'devpts', 'tmpfs', 'securityfs', 'cgroup', 'cgroup2', 'pstore', 'efivarfs', 'bpf', 'autofs', 'hugetlbfs', 'mqueue', 'debugfs', 'tracefs', 'fusectl', 'configfs', 'nfs', 'nfsd', 'ramfs', 'rpc_pipefs', 'binfmt_misc', 'smb', 'cifs']: continue
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
                        # 模拟元数据扫描速度（每个文件平均512字节）
                        update_speed_stats(self.name, len(files) * 512)
                        current_speed = get_worker_speed(self.name)
                        
                        with STATUS_LOCK:
                            WORKER_STATUS[self.name] = {
                                "file": mountpoint, "mode": "元数据扫描", "progress": "进行中", "details": f"扫描 {dirs_scanned} 目录, {files_scanned} 文件 | {format_speed(current_speed)}"
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

        # 清理速度统计
        with SPEED_STATS_LOCK:
            if self.name in WORKER_SPEED_STATS:
                del WORKER_SPEED_STATS[self.name]
        
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 引擎已停止。")


# ==============================================================================
# --- 5G热点数据发射引擎 (集成自动配置) ---
# ==============================================================================
class HotspotDataBroadcaster(threading.Thread):
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event
        self.daemon = True
        self.name = "5G热点广播器"
        self.broadcast_socket = None
        self.hostapd_process = None
        self.conf_path = "/tmp/dandelion_hostapd.conf"
        # 从全局配置读取参数
        self.wifi_interface = WIFI_INTERFACE
        self.hotspot_ssid = HOTSPOT_SSID
        self.hotspot_channel = HOTSPOT_CHANNEL
        self.hotspot_ip = "192.168.100.1"  # 为热点分配固定的子网
        self.broadcast_interval = HOTSPOT_BROADCAST_INTERVAL
        self.chunk_size = 1400  # 【优化】提升至接近 MTU 的大小 (1400 < 1500)
        self.broadcast_addr = ".".join(self.hotspot_ip.split('.')[:-1]) + ".255" # 【优化】预计算广播地址

    def _run_command(self, command, suppress_errors=False):
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            stdout, stderr = process.communicate()
            if process.returncode != 0 and not suppress_errors:
                log_message(f'[!] 命令失败: {" ".join(command)}')
                log_message(f'[!] 错误: {stderr.strip()}')
                return False
            return True
        except FileNotFoundError:
            log_message(f'[!] 命令未找到: {command[0]}。是否已安装并加入PATH?')
            return False
        except Exception as e:
            log_message(f'[!] 运行命令时发生错误: {e}')
            return False

    def setup_hotspot(self):
        log_message('>>> [热点] 开始热点设置...')
        if os.geteuid() != 0:
            log_message('[!] [热点] 关键错误: 创建热点需要root权限。')
            return False
        if not self._run_command(['which', 'hostapd']):
            log_message('[!] [热点] 关键错误: `hostapd` 未安装或不在PATH中。')
            return False

        hostapd_config = (
            f"interface={self.wifi_interface}\n"
            f"driver=nl80211\n"
            f"ssid={self.hotspot_ssid}\n"
            f"hw_mode=a\n"
            f"channel={self.hotspot_channel}\n"
            f"ignore_broadcast_ssid=0\n"
        )

        try:
            with open(self.conf_path, 'w') as f:
                f.write(hostapd_config)
        except Exception as e:
            log_message(f'[!] [热点] 创建配置文件失败: {e}')
            return False

        log_message(f'[*] [热点] 正在配置网络接口: {self.wifi_interface}')
        if not self._run_command(['ip', 'link', 'set', self.wifi_interface, 'down']): return False
        self._run_command(['ip', 'addr', 'flush', 'dev', self.wifi_interface], suppress_errors=True)
        if not self._run_command(['ip', 'addr', 'add', f'{self.hotspot_ip}/24', 'dev', self.wifi_interface], suppress_errors=True):
            log_message('[*] [热点] 添加IP失败(可能已存在), 继续...')
        if not self._run_command(['ip', 'link', 'set', self.wifi_interface, 'up']): return False

        log_message('[*] [热点] 正在后台启动hostapd服务...')
        try:
            self.hostapd_process = subprocess.Popen(['hostapd', self.conf_path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            time.sleep(3)
            if self.hostapd_process.poll() is not None:
                _, stderr = self.hostapd_process.communicate()
                log_message(f'[!] [热点] hostapd启动失败. 错误: {stderr.decode("utf-8", errors="ignore").strip()}')
                return False
            log_message(f'[+] [热点] 热点 "{self.hotspot_ssid}" 应该已开始广播!')
        except Exception as e:
            log_message(f'[!] [热点] 执行hostapd失败: {e}')
            return False

        try:
            self.broadcast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.broadcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # 【优化】设置极大的发送缓冲区以支持爆发性流量
            self.broadcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024 * 2) 
            self.broadcast_socket.bind((self.hotspot_ip, 0))
            log_message('[*] [热点] UDP广播套接字已就绪 (SO_SNDBUF=2MB)。')
        except Exception as e:
            log_message(f'[!] [热点] 创建广播套接字失败: {e}')
            return False
        return True

    def cleanup_hotspot(self):
        log_message('>>> [热点] 开始清理热点资源...')
        if self.broadcast_socket: self.broadcast_socket.close()
        if self.hostapd_process:
            self.hostapd_process.terminate()
            try:
                self.hostapd_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.hostapd_process.kill()
        if os.path.exists(self.conf_path):
            os.remove(self.conf_path)
        self._run_command(['ip', 'addr', 'flush', 'dev', self.wifi_interface], suppress_errors=True)
        log_message('>>> [热点] 清理完毕 <<<')

    def get_broadcast_files(self):
        files_to_broadcast = []
        script_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            for root, dirs, files in os.walk(script_dir):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for file in files:
                    if not file.startswith('.') and file != os.path.basename(__file__):
                        files_to_broadcast.append(os.path.join(root, file))
        except Exception as e:
            log_message(f'[!] [热点] 扫描脚本目录失败: {e}')
        return files_to_broadcast

    def create_broadcast_packet(self, file_path, chunk_data, chunk_index):
        try:
            filename_str = os.path.basename(file_path)
            filename = filename_str.encode('utf-8', errors='replace')[:64].ljust(64, b'\x00')
            chunk_idx = struct.pack('!I', chunk_index)
            packet = filename + chunk_idx + chunk_data
            return packet
        except Exception as e:
            log_message(f'[!] [热点] 创建数据包失败: {e}')
            return None

    def run(self):
        if not ENABLE_5G_HOTSPOT:
            log_message('[*] [热点] 5G热点功能已在配置中禁用。')
            return
        log_message(f'[*] [{self.name}] 引擎启动...')
        if not self.setup_hotspot():
            log_message(f'[!] [{self.name}] 因设置失败而中止。')
            self.cleanup_hotspot()
            return
        with HOTSPOT_LOCK: HOTSPOT_STATUS['active'] = True
        try:
            while not self.stop_event.is_set():
                files = self.get_broadcast_files()
                if not files:
                    with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "等待文件", "progress": "---", "details": "未找到可广播的文件"}
                    self.stop_event.wait(10)
                    continue
                for file_path in files:
                    if self.stop_event.is_set(): break
                    try:
                        file_size = os.path.getsize(file_path)
                        if file_size == 0: continue
                        filename = os.path.basename(file_path)
                        
                        # 局部变量缓存以加速访问
                        b_addr = self.broadcast_addr
                        b_sock = self.broadcast_socket
                        b_interval = self.broadcast_interval
                        c_size = self.chunk_size
                        
                        with open(file_path, 'rb') as f:
                            chunk_index = 0
                            bytes_since_last_stat = 0
                            last_stat_update = time.time()
                            
                            while not self.stop_event.is_set():
                                chunk = f.read(c_size)
                                if not chunk: break
                                packet = self.create_broadcast_packet(file_path, chunk, chunk_index)
                                if packet:
                                    b_sock.sendto(packet, (b_addr, 33333))
                                    packet_len = len(packet)
                                    bytes_since_last_stat += packet_len
                                    
                                    # 【优化】批量更新统计和状态，减少颗粒度过细导致的锁竞争
                                    # 每 1MB 或 0.5 秒更新一次
                                    current_now = time.time()
                                    if bytes_since_last_stat > 1024 * 1024 or current_now - last_stat_update > 0.5:
                                        with HOTSPOT_LOCK: 
                                            HOTSPOT_STATUS['bytes_transmitted'] += bytes_since_last_stat
                                        
                                        progress = (f.tell() / file_size) * 100 if file_size > 0 else 100
                                        details = f'频道:{self.hotspot_channel} IP:{self.hotspot_ip}'
                                        
                                        # 更新速度统计
                                        update_speed_stats(self.name, bytes_since_last_stat)
                                        
                                        with STATUS_LOCK: 
                                            WORKER_STATUS[self.name] = {
                                                "file": filename, 
                                                "mode": "5G广播", 
                                                "progress": f'{progress:.1f}%', 
                                                "details": details
                                            }
                                        
                                        bytes_since_last_stat = 0
                                        last_stat_update = current_now
                                        
                                chunk_index += 1
                                if b_interval > 0:
                                    time.sleep(b_interval)
                        
                        # 完成一个文件，确保最后一点数据被计入
                        if bytes_since_last_stat > 0:
                            with HOTSPOT_LOCK: HOTSPOT_STATUS['bytes_transmitted'] += bytes_since_last_stat
                            update_speed_stats(self.name, bytes_since_last_stat)
                            
                        with HOTSPOT_LOCK: HOTSPOT_STATUS['files_broadcast'] += 1
                    except Exception as e:
                        log_message(f'[!] [{self.name}] 广播文件 {os.path.basename(file_path)} 时出错: {e}')
        finally:
            self.cleanup_hotspot()
            with HOTSPOT_LOCK: HOTSPOT_STATUS['active'] = False
            with STATUS_LOCK:
                if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
            log_message(f'[*] [{self.name}] 引擎已停止。')


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

class LoopbackSenderThread(threading.Thread):
    """
    【极速优化】本地回环发送器 - 文件内分块并行版本
    - 多线程同时处理同一个文件的不同块
    - 每个线程从不同的起始块开始，交错读取
    - 即使只有一个文件也能充分利用所有线程
    """
    def __init__(self, stop_event, files_to_exclude, thread_id=0):
        super().__init__()
        self.stop_event = stop_event
        self.daemon = True
        self.thread_id = thread_id
        self.name = f"回环发送器-{thread_id}"
        self.files_to_exclude = files_to_exclude
        self.sockets = []
        # 预分配目标地址列表
        self.target_addrs = [('127.0.0.1', port) for port in range(LOOPBACK_PORT_START, LOOPBACK_PORT_END)]
        self.port_count = len(self.target_addrs)
        
    def run(self):
        # 创建多个socket用于并行发送
        socket_count = min(4, self.port_count // LOOPBACK_BATCH_PORTS + 1)
        try:
            for i in range(socket_count):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024 * 1024)
                self.sockets.append(sock)
            log_message(f"[*] [{self.name}] 极速引擎已启动 (块并行模式, x{socket_count}sockets, {self.port_count}端口)")
        except Exception as e:
            log_message(f"[!] [{self.name}] 创建套接字失败: {e}")
            return
        
        while not self.stop_event.is_set():
            with source_dirs_lock:
                current_dirs = list(DYNAMIC_SOURCE_DIRS) if DYNAMIC_SOURCE_DIRS else []
            if not current_dirs:
                with STATUS_LOCK:
                    WORKER_STATUS[self.name] = {"file": "N/A", "mode": "等待目录", "progress": "---", "details": "等待驱动器扫描..."}
                time.sleep(5)
                continue
            
            for directory in current_dirs:
                if self.stop_event.is_set(): break
                if not os.path.isdir(directory): continue
                try:
                    for root, dirs, files in os.walk(directory, topdown=True):
                        if self.stop_event.is_set(): break
                        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
                        for file in files:
                            if self.stop_event.is_set(): break
                            if not file.startswith('.') and file not in self.files_to_exclude:
                                # 所有线程都处理同一个文件，但从不同块开始
                                self.process_file_parallel(os.path.join(root, file))
                except Exception as e:
                    log_message(f"[!] [{self.name}] 扫描目录 '{directory}' 时出错: {e}")
        
        # 清理
        for sock in self.sockets:
            try:
                sock.close()
            except Exception:
                pass
        
        with SPEED_STATS_LOCK:
            if self.name in WORKER_SPEED_STATS:
                del WORKER_SPEED_STATS[self.name]
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 线程已停止。")
    
    def process_file_parallel(self, file_path):
        """
        文件内分块并行处理
        - 每个线程从 thread_id 对应的块开始
        - 步进为 LOOPBACK_THREAD_COUNT（跳过其他线程的块）
        """
        try:
            if not os.path.exists(file_path): return
            file_size = os.path.getsize(file_path)
            if file_size == 0: return
            
            chunk_size = LOOPBACK_CHUNK_SIZE
            total_chunks = (file_size + chunk_size - 1) // chunk_size  # 向上取整
            thread_count = LOOPBACK_THREAD_COUNT
            
            # 计算本线程负责的起始块和步进
            start_chunk = self.thread_id
            step = thread_count
            
            # 计算本线程需要处理的块数
            my_chunks = (total_chunks - start_chunk + step - 1) // step
            if my_chunks <= 0:
                return  # 文件太小，本线程无需处理
            
            with open(file_path, 'rb') as f:
                socket_idx = 0
                batch_size = LOOPBACK_BATCH_PORTS
                processed_chunks = 0
                
                for chunk_index in range(start_chunk, total_chunks, step):
                    if self.stop_event.is_set(): break
                    
                    # 定位到对应块的位置
                    offset = chunk_index * chunk_size
                    f.seek(offset)
                    chunk_data = f.read(chunk_size)
                    if not chunk_data: continue
                    
                    # 构建数据包
                    packet = struct.pack('!I', chunk_index) + hashlib.md5(chunk_data).digest() + chunk_data
                    packet_len = len(packet)
                    processed_chunks += 1
                    
                    bytes_sent = 0
                    # 批量发送到所有端口
                    for i in range(0, self.port_count, batch_size):
                        if self.stop_event.is_set(): break
                        sock = self.sockets[socket_idx % len(self.sockets)]
                        socket_idx += 1
                        
                        batch_end = min(i + batch_size, self.port_count)
                        batch_bytes = 0
                        for j in range(i, batch_end):
                            try:
                                sock.sendto(packet, self.target_addrs[j])
                                batch_bytes += packet_len
                            except Exception:
                                pass
                        
                        # 【优化】累积本地流量，减少对全局锁的频繁竞争
                        if batch_bytes > 0:
                            bytes_sent += batch_bytes
                            # 仅在处理完一个端口批次后更新一次统计
                            update_speed_stats(self.name, batch_bytes)
                    
                    # 计算本线程的进度
                    progress = (processed_chunks / my_chunks) * 100 if my_chunks > 0 else 100
                    current_speed = get_worker_speed(self.name)
                    
                    with STATUS_LOCK:
                        WORKER_STATUS[self.name] = {
                            "file": os.path.basename(file_path),
                            "mode": "极速回环",
                            "progress": f"{progress:.1f}%",
                            "details": f"块{chunk_index}/{total_chunks} x{self.port_count}端口 | {format_speed(current_speed)}"
                        }
        except Exception as e:
            if not self.stop_event.is_set():
                log_message(f"[!] [{self.name}] 处理文件 {file_path} 时出错: {e}")

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
                bytes_sent = 0
                for subnet_str in FALLBACK_SUBNETS:
                    if self.stop_event.is_set(): break
                    try:
                        self.lan_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        broadcast_ip = str(ipaddress.ip_network(subnet_str).broadcast_address)
                        self.lan_socket.sendto(packet_to_send, (broadcast_ip, random.randint(10240, 65535)))
                        bytes_sent += len(packet_to_send)
                    except ValueError: continue
                    except PermissionError:
                        if not permission_warning_shown:
                            log_message("[!] [广播线程] 权限错误: 请使用 root 或管理员权限。")
                            permission_warning_shown = True
                        break
                    except Exception as e:
                        log_message(f"[!] [广播线程] 广播至 {subnet_str} 时出错: {e}")
                
                # 更新速度统计
                if bytes_sent > 0:
                    update_speed_stats(self.name, bytes_sent)
                
                current_speed = get_worker_speed(self.name)
                speed_str = format_speed(current_speed)
                
                self.lan_queue.task_done()
                with STATUS_LOCK:
                    WORKER_STATUS[self.name] = {"file": "N/A", "mode": "本地广播", "progress": f"{self.lan_queue.qsize()} 排队", "details": f"广播至 {len(FALLBACK_SUBNETS)} 个子网 | {speed_str}"}
            except queue.Empty:
                current_speed = get_worker_speed(self.name)
                speed_str = format_speed(current_speed) if current_speed > 0 else "0 B/s"
                with STATUS_LOCK:
                    WORKER_STATUS[self.name] = {"file": "N/A", "mode": "本地广播", "progress": f"{self.lan_queue.qsize()} 排队", "details": f"等待数据包... | {speed_str}"}
        
        # 清理速度统计
        with SPEED_STATS_LOCK:
            if self.name in WORKER_SPEED_STATS:
                del WORKER_SPEED_STATS[self.name]
        
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

class StatusDisplayThread(threading.Thread):
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event, self.daemon = stop_event, True
        self._conn_analysis = ""
    def run(self):
        # 【优化】只有在交互模式且curses可用时才启动UI
        if not IS_INTERACTIVE or not CURSES_AVAILABLE:
            if not IS_INTERACTIVE:
                log_message("[*] 非交互模式，UI线程已禁用。")
            else:
                log_message("[!] 'curses' 库不可用，回退到简单的清屏模式。")
                self.fallback_loop()
            return

        try:
            curses.wrapper(self.curses_main_loop)
        except Exception as e:
            EXIT_FLAG.set()
            # 确保在退出前能看到错误信息
            curses.endwin()
            print(f"\n[!] Curses UI 启动失败: {e}")
            print("[!] 请检查您的终端是否支持 curses (例如，在Windows上使用 Windows Terminal)。\n")
            log_message(f"[!] Curses UI 启动失败: {e}")

    def fallback_loop(self):
        clear_cmd = 'cls' if os.name == 'nt' else 'clear'
        while not self.stop_event.is_set():
            try:
                os.system(clear_cmd)
                print("全球法布施 (回退模式)\n" + "="*30)
                # 打印最新的15条日志
                log_copy = list(LOG_QUEUE)
                for msg in log_copy[-15:]:
                    print(msg)
                time.sleep(1)
            except Exception:
                # 在某些环境下 os.system 可能失败
                time.sleep(5)

    def curses_main_loop(self, stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLUE)
        last_net_analysis_time = 0
        while not self.stop_event.is_set():
            if curses.is_term_resized(*stdscr.getmaxyx()): curses.update_lines_cols()
            if time.time() - last_net_analysis_time > 10:
                self._conn_analysis = analyze_network_connections()
                last_net_analysis_time = time.time()
            self.draw_ui(stdscr)
            # 【优化】提高 UI 刷新频率以更实时地显示极速回环速度
            time.sleep(0.2)
            try:
                if stdscr.getch() == ord('q'): EXIT_FLAG.set()
            except curses.error: pass
    def draw_ui(self, stdscr):
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        self.draw_overview(stdscr, 0, 0, 4, w)
        
        bottom_reserved_h = 12
        status_table_h = h - 4 - bottom_reserved_h
        if status_table_h < 3: status_table_h = 3
        
        self.draw_status_table(stdscr, 4, 0, status_table_h, w)
        
        remaining_h = h - 4 - status_table_h
        log_h = max(3, remaining_h - 4)
        net_analysis_h = max(0, remaining_h - log_h)
        
        log_start_y = 4 + status_table_h
        net_analysis_start_y = log_start_y + log_h

        self.draw_logs(stdscr, log_start_y, 0, log_h, w)
        self.draw_net_analysis(stdscr, net_analysis_start_y, 0, net_analysis_h, w)
        
        stdscr.refresh()
    def draw_overview(self, win, y, x, h, w):
        win.addstr(y, x, " " * (w - 1), curses.color_pair(2))
        title = " 全球法布施 (引擎: 定时调速) | 按 'q' 退出 "
        win.addstr(y, x + (w - len(title)) // 2, title, curses.color_pair(2) | curses.A_BOLD)
        try: traffic_str = "" # 移除本月已发送统计
        except Exception: traffic_str = "本月已发送: 状态加载失败..."
        queue_str = f"任务队列 (WAN/LAN/回环): {WAN_TASK_QUEUE.qsize()}/{LAN_TASK_QUEUE.qsize()}/{LOOPBACK_TASK_QUEUE.qsize()}"
        with params_lock: workers_str = f"并发线程: {CURRENT_TARGET_WORKERS}"
        
        # 5G热点状态
        with HOTSPOT_LOCK:
            hotspot_str = f"5G热点: {'活跃' if HOTSPOT_STATUS['active'] else '停止'} | 已广播: {HOTSPOT_STATUS['files_broadcast']} 文件 {HOTSPOT_STATUS['bytes_transmitted'] / (1024*1024):.2f}MB"
        
        # 获取各类别速度
        speeds = get_category_speeds()
        speed_line1 = f"全球: {format_speed(speeds['全球发送']):<12} | 回环: {format_speed(speeds['本地回环']):<12} | 广播: {format_speed(speeds['局域网广播']):<12}"
        speed_line2 = f"表面扫描: {format_speed(speeds['表面扫描']):<12} | 元数据: {format_speed(speeds['元数据扫描']):<12}"
        
        win.addstr(y + 2, x + 2, f"{traffic_str:<35} {queue_str:<40} {workers_str:<15}")
        win.addstr(y + 3, x + 2, f"{hotspot_str:<45} {speed_line1}")
        if h > 4:
            win.addstr(y + 4, x + 2, f"{' ':<45} {speed_line2}")
    def draw_status_table(self, win, y, x, h, w):
        win.addstr(y, x + 1, " 任务状态 (包含发送速度) ", curses.A_REVERSE)
        header = f"{'线程名称':<25} | {'文件/模式':<35} | {'进度':<22} | {'详情/速度'}"
        win.addstr(y + 2, x, "─" * (w - 1))
        win.addstr(y + 1, x, header[:w - 1])
        win.addstr(y + 2, x, "─" * (w - 1))
        with STATUS_LOCK:
            priority_order = ['引擎', '扫描器', '管理器', '发送器']
            sort_key = lambda item: next((i for i, k in enumerate(priority_order) if k in item[0]), len(priority_order))
            sorted_status = sorted(WORKER_STATUS.items(), key=sort_key)
            for i, (name, status) in enumerate(sorted_status):
                if i >= h - 4: break
                file_str = f"{status.get('file', 'N/A')} ({status.get('mode', 'N/A')})"
                if len(file_str) > 33: file_str = file_str[:30] + "..."
                progress_raw = status.get('progress', 'N/A')
                if isinstance(progress_raw, str) and progress_raw.endswith('%'):
                    try: progress_display = create_progress_bar(float(progress_raw.strip('%')), 10)
                    except ValueError: progress_display = f"{progress_raw:<18}"
                else: progress_display = f"{str(progress_raw):<18}"
                details_str = status.get('details', '')
                # 【优化】如果 details 包含速度，确保速度部分可见
                if '|' in details_str:
                    parts = details_str.split('|')
                    if len(parts) > 1:
                        speed_part = parts[-1].strip()
                        info_part = parts[0].strip()
                        # 根据剩余宽度分配
                        remaining_w = w - (25 + 3 + 35 + 3 + 22 + 3) - 2
                        if remaining_w > 0:
                            if len(details_str) > remaining_w:
                                # 优先保证速度显示
                                details_str = f"{info_part[:max(0, remaining_w-len(speed_part)-5)]}... | {speed_part}"
                
                line_str = f"{name:<25} | {file_str:<35} | {progress_display:<22} | {details_str}"
                try:
                    win.addstr(y + 3 + i, x, line_str[:w - 1])
                except curses.error:
                    pass
    def draw_logs(self, win, y, x, h, w):
        if h <= 2: return
        log_color = curses.color_pair(4)
        for i in range(h):
            try: win.addstr(y + i, x, " " * (w - 1), log_color)
            except curses.error: pass
        try:
            win.addstr(y, x + 1, " 实时日志 ", log_color | curses.A_REVERSE)
            log_copy = list(LOG_QUEUE)
            for i, msg in enumerate(reversed(log_copy)):
                if i >= h - 2: break
                win.addstr(y + h - 2 - i, x + 1, msg[:w - 2], log_color)
        except curses.error: pass
    def draw_net_analysis(self, win, y, x, h, w):
        if h <= 2: return
        try:
            win.addstr(y, x + 1, " 网络分析 ", curses.A_REVERSE)
            if self._conn_analysis:
                lines = self._conn_analysis.split('\n')
                for i, line in enumerate(lines):
                    if i >= h - 1: break
                    win.addstr(y + 1 + i, x + 1, line[:w - 2])
        except curses.error: pass

class StartupDriveScanner(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon, self.name = True, "驱动器扫描器"
    def run(self):
        global DYNAMIC_SOURCE_DIRS
        log_message(f"[*] {self.name} 已启动，开始进行一次性驱动器扫描...")
        with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "检测驱动器", "progress": "50.0%", "details": "正在扫描分区..."}
        found_dirs = {os.path.realpath(DEFAULT_SOURCE_DIRECTORY)}
        try:
            for p in linux_get_disk_partitions(all_partitions=False):
                if 'cdrom' in p.opts or p.fstype == '' or 'loop' in p.device or 'ro' in p.opts: continue
                if os.name == 'nt' and 'rw' not in p.opts: continue
                # 【修改】包含外置硬盘 - 不再跳过removable/external设备
                found_dirs.add(os.path.realpath(p.mountpoint))
                if 'removable' in p.opts or 'external' in p.opts:
                    log_message(f"[*] [{self.name}] 包含外置驱动器: {p.mountpoint}")
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


# ==============================================================================
# --- 【新增】外置硬盘自动识别挂载监控 ---
# ==============================================================================
class ExternalDriveMonitorThread(threading.Thread):
    """
    持续监控系统驱动器变化的线程。
    当检测到新的外置硬盘/U盘插入时，自动将其添加到扫描目录列表。
    当检测到设备拔出时，自动从列表中移除。
    """
    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event
        self.daemon = True
        self.name = "外置硬盘监控器"
        self.known_drives = set()  # 记录已知的驱动器挂载点
        self.scan_interval = EXTERNAL_DRIVE_SCAN_INTERVAL

    def _is_valid_drive(self, partition):
        """检查分区是否是有效的可扫描驱动器"""
        try:
            opts_list = partition.opts.split(',')
            
            # 跳过只读、光驱、虚拟设备
            if 'ro' in opts_list or 'cdrom' in opts_list:
                return False
            if 'loop' in partition.device:
                return False
            
            # Unix下只处理物理设备
            if os.name != 'nt' and not partition.device.startswith('/dev/'):
                return False
            
            # 跳过空文件系统类型
            if not partition.fstype:
                return False
            
            # 跳过系统/虚拟文件系统
            system_fs = [
                'sysfs', 'proc', 'devtmpfs', 'devpts', 'tmpfs', 'securityfs',
                'cgroup', 'cgroup2', 'pstore', 'efivarfs', 'bpf', 'autofs',
                'hugetlbfs', 'mqueue', 'debugfs', 'tracefs', 'fusectl',
                'configfs', 'nfsd', 'ramfs', 'rpc_pipefs', 'binfmt_misc'
            ]
            if partition.fstype.lower() in system_fs:
                return False
            
            # Windows下额外检查
            if os.name == 'nt' and 'rw' not in opts_list:
                return False
            
            return True
        except Exception:
            return False

    def _detect_drive_type(self, partition):
        """检测驱动器类型（外置/内置/U盘）"""
        opts_lower = partition.opts.lower()
        device_lower = partition.device.lower()
        
        # 检查是否为可移动设备
        if 'removable' in opts_lower:
            return "U盘/移动存储"
        
        # macOS 外置硬盘检测
        if '/volumes/' in partition.mountpoint.lower():
            # 排除系统卷
            if '/volumes/macintosh' not in partition.mountpoint.lower():
                return "外置驱动器"
        
        # Linux 外置硬盘检测
        if '/media/' in partition.mountpoint.lower() or '/mnt/' in partition.mountpoint.lower():
            return "外置驱动器"
        
        # Windows 检测 (通常外置硬盘会有特定的驱动器号模式)
        if os.name == 'nt':
            # USB设备通常有特定的设备路径模式
            if 'usb' in device_lower or 'usbstor' in device_lower:
                return "USB设备"
        
        return "内置驱动器"

    def _scan_all_drives(self):
        """扫描所有有效驱动器并返回挂载点集合"""
        drives = set()
        try:
            for p in linux_get_disk_partitions(all_partitions=False):
                if self._is_valid_drive(p):
                    drives.add(os.path.realpath(p.mountpoint))
        except Exception as e:
            log_message(f"[!] [{self.name}] 扫描驱动器时出错: {e}")
        return drives

    def run(self):
        log_message(f"[*] [{self.name}] 引擎已启动，每 {self.scan_interval} 秒扫描一次驱动器变化。")
        
        # 初始化已知驱动器列表
        self.known_drives = self._scan_all_drives()
        log_message(f"[*] [{self.name}] 初始驱动器: {len(self.known_drives)} 个")
        
        with STATUS_LOCK:
            WORKER_STATUS[self.name] = {
                "file": "N/A",
                "mode": "监控中",
                "progress": "---",
                "details": f"已知驱动器: {len(self.known_drives)} 个"
            }
        
        while not self.stop_event.is_set():
            self.stop_event.wait(self.scan_interval)
            if self.stop_event.is_set():
                break
            
            # 扫描当前驱动器
            current_drives = self._scan_all_drives()
            
            # 检测新插入的驱动器
            new_drives = current_drives - self.known_drives
            if new_drives:
                for drive in new_drives:
                    # 获取分区信息用于日志
                    drive_type = "未知类型"
                    try:
                        for p in linux_get_disk_partitions(all_partitions=False):
                            if os.path.realpath(p.mountpoint) == drive:
                                drive_type = self._detect_drive_type(p)
                                break
                    except Exception:
                        pass
                    
                    log_message(f"[+] [{self.name}] 检测到新驱动器: {drive} ({drive_type})")
                    
                    # 添加到动态扫描目录
                    with source_dirs_lock:
                        if drive not in DYNAMIC_SOURCE_DIRS:
                            DYNAMIC_SOURCE_DIRS.append(drive)
                            log_message(f"[+] [{self.name}] 已将 {drive} 添加到扫描列表。")
            
            # 检测已拔出的驱动器
            removed_drives = self.known_drives - current_drives
            if removed_drives:
                for drive in removed_drives:
                    log_message(f"[-] [{self.name}] 驱动器已移除: {drive}")
                    
                    # 从动态扫描目录移除
                    with source_dirs_lock:
                        if drive in DYNAMIC_SOURCE_DIRS:
                            DYNAMIC_SOURCE_DIRS.remove(drive)
                            log_message(f"[-] [{self.name}] 已将 {drive} 从扫描列表移除。")
            
            # 更新已知驱动器列表
            self.known_drives = current_drives
            
            # 更新状态显示
            with STATUS_LOCK:
                status_details = f"已知驱动器: {len(self.known_drives)} 个"
                if new_drives:
                    status_details += f" | 新增: +{len(new_drives)}"
                if removed_drives:
                    status_details += f" | 移除: -{len(removed_drives)}"
                
                WORKER_STATUS[self.name] = {
                    "file": "N/A",
                    "mode": "监控中",
                    "progress": "---",
                    "details": status_details
                }
        
        # 清理
        with STATUS_LOCK:
            if self.name in WORKER_STATUS:
                del WORKER_STATUS[self.name]
        log_message(f"[*] {self.name} 线程已停止。")

class FileScannerThread(threading.Thread):
    def __init__(self, wan_queue, lan_queue, loopback_queue, files_to_exclude, lan_socket):
        super().__init__()
        self.wan_queue, self.lan_queue, self.loopback_queue = wan_queue, lan_queue, loopback_queue
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
                chunk_data = file_handle.read(CHUNK_SIZE)
                if not chunk_data: break
                packet = struct.pack('!I', chunk_index) + hashlib.md5(chunk_data).digest() + chunk_data
                chunk_index += 1
                task = (packet, os.path.basename(file_path), file_size, file_handle.tell())
                
                self.wan_queue.put(task)
                # 【修改】始终向局域网队列添加任务（即使 lan_socket 为 None，广播线程会处理）
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
        self.last_send_time = time.time()
    def run(self):
        while not EXIT_FLAG.is_set() and not self.stop_event.is_set():
            try:
                task = self.task_queue.get(timeout=1)
                packet, filename, total_size, current_pos = task
                progress = (current_pos / total_size) * 100 if total_size > 0 else 0
                is_internet_ok = check_internet_connectivity()
                mode_str = "广域网发送" if is_internet_ok else "离线"
                
                current_speed = get_worker_speed(self.name)
                speed_str = format_speed(current_speed)
                
                with STATUS_LOCK: 
                    WORKER_STATUS[self.name] = {
                        "file": filename, 
                        "mode": mode_str, 
                        "progress": f"{progress:.1f}%", 
                        "details": f"发送中... {speed_str}"
                    }
                
                if is_internet_ok:
                    send_start_time = time.time()
                    bytes_sent = send_chunk_in_batches(self.udp_socket, packet, self.target_ip_pool)
                    if bytes_sent > 0:
                        # 更新速度统计
                        update_speed_stats(self.name, bytes_sent)
                
                self.task_queue.task_done()
            except queue.Empty:
                current_speed = get_worker_speed(self.name)
                speed_str = format_speed(current_speed) if current_speed > 0 else "0 B/s"
                with STATUS_LOCK: 
                    WORKER_STATUS[self.name] = {
                        "file": "N/A", 
                        "mode": "空闲", 
                        "progress": "---", 
                        "details": f"等待任务 ({speed_str})"
                    }
        
        # 清理速度统计
        with SPEED_STATS_LOCK:
            if self.name in WORKER_SPEED_STATS:
                del WORKER_SPEED_STATS[self.name]
        
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
    return {'current_month': datetime.now().strftime("%Y-%m"), 'total_sent_gb': 0.0}

def save_state(state):
    pass

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

def analyze_network_connections():
    try:
        total_count = linux_get_net_connections()
        device_status = "禁用"
        if ENABLE_FORCED_LAN_SCAN:
            device_status = f"{len(scan_network_devices())}个设备"
        return f"--- 网络状态 ---\n总连接数: {total_count}\n局域网设备: {device_status}"
    except Exception: return "分析网络时出错"

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
            # 【关键修复】每发送一个包都上报统计，解决因 time.sleep 导致的统计滞后
            update_speed_stats(threading.current_thread().name, packet_len)
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
    
    # 【优化】根据环境决定启动哪个显示/日志线程
    if IS_INTERACTIVE:
        log_message("[*] 检测到交互式终端，将启动图形化UI。")
        display_thread = StatusDisplayThread(EXIT_FLAG)
        display_thread.start()
        active_threads.append(display_thread)
    else:
        # 在非交互模式下，log_message 会直接打印，所以不需要额外的打印线程
        log_message("[*] 非交互模式 (例如: 开机自启)，UI被禁用。日志将直接打印到标准输出。")

    time.sleep(0.1) # 稍作等待，确保第一条日志能显示
    log_message("[*] 欢迎使用全球法布施。正在初始化...")

    script_name = os.path.basename(__file__)
    files_to_exclude = {script_name, ASN_BLOCKS_DB_PATH, COUNTRY_BLOCKS_DB_PATH, COUNTRY_LOCATIONS_DB_PATH, 'nohup.out'}
    
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
        # 【修改】优先使用无线网卡，无线不可用时回退到有线网卡，都不可用时使用默认套接字
        lan_interface = wireless_interface or wired_interface
        try:
            lan_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if lan_interface:
                try:
                    lan_socket.bind((lan_interface['ip'], 0))
                    interface_type = "无线网卡" if wireless_interface else "有线网卡"
                    log_message(f"[*] [局域网发送] 成功绑定到{interface_type}: {lan_interface['name']} ({lan_interface['ip']})")
                except Exception as e:
                    log_message(f"[!] [局域网发送] 绑定网卡失败: {e}。将使用默认接口。")
                    # 重新创建套接字使用默认接口
                    lan_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            else:
                log_message("[*] [局域网发送] 未找到特定网卡，使用默认接口进行广播。")
        except Exception as e:
            log_message(f"[!] [局域网发送] 创建套接字失败: {e}。局域网广播将禁用。")
            lan_socket = None

    country_db = load_country_database(COUNTRY_BLOCKS_DB_PATH, COUNTRY_LOCATIONS_DB_PATH)
    if country_db: global GLOBAL_COUNTRY_POOL; GLOBAL_COUNTRY_POOL = list(country_db.keys())
    
    target_ip_pool = prepare_target_ip_pool(country_db)
    if not target_ip_pool: log_message("[!] 严重警告: 无法生成任何目标IP。")
    
    pacer_thread = TimeBasedPacerThread(EXIT_FLAG); pacer_thread.start(); active_threads.append(pacer_thread)
    
    lan_scanner_thread = ForcedNetworkScanThread(EXIT_FLAG, lan_socket, LAN_TASK_QUEUE); lan_scanner_thread.start(); active_threads.append(lan_scanner_thread)
    
    if ENABLE_LOOPBACK_SEND:
        # 【极速优化】启动多个回环发送器线程并行发送
        log_message(f"[*] 启动 {LOOPBACK_THREAD_COUNT} 个极速回环发送器...")
        for i in range(LOOPBACK_THREAD_COUNT):
            loopback_thread = LoopbackSenderThread(EXIT_FLAG, files_to_exclude, thread_id=i)
            loopback_thread.start()
            active_threads.append(loopback_thread)
    
    # state_saver 线程已移除
    
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
    
    # 【新增】启动外置硬盘自动识别监控
    if ENABLE_EXTERNAL_DRIVE_MONITOR:
        external_drive_monitor = ExternalDriveMonitorThread(EXIT_FLAG)
        external_drive_monitor.start()
        active_threads.append(external_drive_monitor)
    
    # 启动5G热点广播器
    if ENABLE_5G_HOTSPOT:
        global hotspot_broadcaster
        hotspot_broadcaster = HotspotDataBroadcaster(EXIT_FLAG)
        hotspot_broadcaster.start()
        active_threads.append(hotspot_broadcaster)

    scanner = FileScannerThread(wan_queue=WAN_TASK_QUEUE, lan_queue=LAN_TASK_QUEUE, loopback_queue=LOOPBACK_TASK_QUEUE, files_to_exclude=files_to_exclude, lan_socket=lan_socket)
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
            if CURSES_AVAILABLE and IS_INTERACTIVE:
                try: curses.endwin()
                except: pass
            print(f"\n[!] 脚本发生未处理的致命错误: {e}")
            import traceback
            traceback.print_exc()
    finally:
        print("\n[*] 程序执行完毕。")
