# -*- coding: utf-8 -*-
# Python 3.5 Compatibility Version
# 此版本整合了全功能引擎与自动化的5G热点创建功能。
# 运行前请确保已安装 'hostapd' 和 'psutil'。
# 需要以root权限运行: sudo python3 dandelion_full_version.py

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
from datetime import datetime
from collections import deque

# --- [优化] 检测运行环境 ---
IS_INTERACTIVE = sys.stdout.isatty()

try:
    import curses
    CURSES_AVAILABLE = True
except ImportError:
    CURSES_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[!] 关键警告: 未找到 'psutil' 库。")
    print("[!] 硬盘自动扫描功能将被完全禁用。")
    print("[!] 请运行 'pip install psutil' 来安装此依赖。")

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

# --- 线程与数据包配置 ---
CPU_COUNT = os.cpu_count() or 1
MAX_WORKERS = CPU_COUNT * 50
CHUNK_SIZE = 1024
WAN_TASK_QUEUE = queue.Queue(maxsize=MAX_WORKERS * 4)
LAN_TASK_QUEUE = queue.Queue(maxsize=MAX_WORKERS * 4)
RANDOM_IP_POOL_SIZE = 1000

# --- 硬盘与网络扫描 ---
ENABLE_DRIVE_AUTO_SCAN = True
ENABLE_THROTTLED_SURFACE_SCAN = True
ENABLE_METADATA_SCAN = True
ENABLE_FORCED_LAN_SCAN = True
FALLBACK_SUBNETS = [
    "192.168.0.0/24", "192.168.1.0/24", "192.168.43.0/24",
    "172.20.10.0/24", "10.0.0.0/24", "10.42.0.0/24",
]

# --- 5G热点配置区域 ---
ENABLE_5G_HOTSPOT = True
HOTSPOT_SSID = "GlobalDandelion_5G"
HOTSPOT_CHANNEL = 36
HOTSPOT_BROADCAST_INTERVAL = 0.05
WIFI_INTERFACE = "wlan0"

# --- 其他功能 ---
ENABLE_LOOPBACK_SEND = True
ENABLE_RENAMING_EFFECT = True
RENAMING_INTERVAL = 0.01

# --- 基础配置 ---
BASE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOURCE_DIRECTORY = BASE_DIRECTORY

# --- 全局控制变量 ---
EXIT_FLAG = threading.Event()
GLOBAL_COUNTRY_POOL = []
state_lock = threading.Lock()
active_threads = []
hotspot_broadcaster = None # 用于安全关闭
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

# --- 5G热点状态跟踪 ---
HOTSPOT_STATUS = {'active': False, 'files_broadcast': 0, 'bytes_transmitted': 0}
HOTSPOT_LOCK = threading.Lock()

# --- 状态显示与日志 ---
WORKER_STATUS = {}
LOG_QUEUE = deque(maxlen=100)
STATUS_LOCK = threading.Lock()

# --- 性能优化缓存 ---
IP_CACHE = {}
NETWORK_CACHE = {}
CACHE_LOCK = threading.Lock()

# --- 速度等级定义 ---
SPEED_LEVELS = [
    (8, 1000, 1),
    (11, 200, 2),
    (25, 80, 4),
    (35, 40, 6),
    (50, 25, 8),
    (70, 15, MAX_WORKERS)
]

# ==============================================================================
# --- 辅助函数与线程类定义 ---
# ==============================================================================
def log_message(message):
    timestamp = datetime.now().strftime('%H:%M:%S')
    full_message = '[{}] {}'.format(timestamp, message)
    LOG_QUEUE.append(full_message)
    if not IS_INTERACTIVE:
        print(full_message)

def create_progress_bar(percentage, width=10):
    if not isinstance(percentage, (int, float)) or not 0 <= percentage <= 100:
        return '{:<18}'.format(str(percentage))
    filled_len = int(width * percentage // 100)
    bar = '█' * filled_len + '░' * (width - filled_len)
    return '[{}] {:5.1f}%'.format(bar, percentage)

def find_specific_interfaces():
    wired_iface, wireless_iface = None, None
    if not PSUTIL_AVAILABLE:
        log_message("[!] [接口识别] psutil不可用, 无法绑定特定接口。")
        return None, None
    try:
        addrs, stats = psutil.net_if_addrs(), psutil.net_if_stats()
        for iface, iface_addrs in addrs.items():
            if iface not in stats or not stats[iface].isup: continue
            ipv4_addr = next((addr.address for addr in iface_addrs if addr.family == socket.AF_INET), None)
            if not ipv4_addr: continue
            iface_lower = iface.lower()
            if not wired_iface and any(k in iface_lower for k in ['eth', 'enp']):
                wired_iface = {'name': iface, 'ip': ipv4_addr}
            if not wireless_iface and any(k in iface_lower for k in ['wlan', 'wlp']):
                wireless_iface = {'name': iface, 'ip': ipv4_addr}
            if wired_iface and wireless_iface: break
    except Exception as e:
        log_message('[!] [接口识别] 寻找接口时出错: {}'.format(e))
    return wired_iface, wireless_iface

# ==============================================================================
# --- [新增] 5G热点数据发射引擎 (集成自动配置) ---
# ==============================================================================
class HotspotDataBroadcaster(threading.Thread):
    def __init__(self, stop_event):
        super(HotspotDataBroadcaster, self).__init__()
        self.stop_event = stop_event
        self.daemon = True
        self.name = "5G Hotspot Broadcaster"
        self.broadcast_socket = None
        self.hostapd_process = None
        self.conf_path = "/tmp/dandelion_hostapd.conf"
        # 从全局配置读取参数
        self.wifi_interface = WIFI_INTERFACE
        self.hotspot_ssid = HOTSPOT_SSID
        self.hotspot_channel = HOTSPOT_CHANNEL
        self.hotspot_ip = "192.168.100.1"  # 为热点分配固定的子网
        self.broadcast_interval = HOTSPOT_BROADCAST_INTERVAL
        self.chunk_size = CHUNK_SIZE

    def _run_command(self, command, suppress_errors=False):
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            stdout, stderr = process.communicate()
            if process.returncode != 0 and not suppress_errors:
                log_message('[!] 命令失败: {}'.format(' '.join(command)))
                log_message('[!] 错误: {}'.format(stderr.strip()))
                return False
            return True
        except FileNotFoundError:
            log_message('[!] 命令未找到: {}。是否已安装并加入PATH?'.format(command[0]))
            return False
        except Exception as e:
            log_message('[!] 运行命令时发生错误: {}'.format(e))
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
            "interface={}\n"
            "driver=nl80211\n"
            "ssid={}\n"
            "hw_mode=a\n"
            "channel={}\n"
            "ignore_broadcast_ssid=0\n"
        ).format(self.wifi_interface, self.hotspot_ssid, self.hotspot_channel)

        try:
            with open(self.conf_path, 'w') as f:
                f.write(hostapd_config)
        except Exception as e:
            log_message('[!] [热点] 创建配置文件失败: {}'.format(e))
            return False

        log_message('[*] [热点] 正在配置网络接口: {}'.format(self.wifi_interface))
        if not self._run_command(['ip', 'link', 'set', self.wifi_interface, 'down']): return False
        self._run_command(['ip', 'addr', 'flush', 'dev', self.wifi_interface], suppress_errors=True)
        if not self._run_command(['ip', 'addr', 'add', '{}/24'.format(self.hotspot_ip), 'dev', self.wifi_interface], suppress_errors=True):
            log_message('[*] [热点] 添加IP失败(可能已存在), 继续...')
        if not self._run_command(['ip', 'link', 'set', self.wifi_interface, 'up']): return False

        log_message('[*] [热点] 正在后台启动hostapd服务...')
        try:
            self.hostapd_process = subprocess.Popen(['hostapd', self.conf_path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            time.sleep(3)
            if self.hostapd_process.poll() is not None:
                _, stderr = self.hostapd_process.communicate()
                log_message('[!] [热点] hostapd启动失败. 错误: {}'.format(stderr.decode('utf-8', errors='ignore').strip()))
                return False
            log_message('[+] [热点] 热点 "{}" 应该已开始广播!'.format(self.hotspot_ssid))
        except Exception as e:
            log_message('[!] [热点] 执行hostapd失败: {}'.format(e))
            return False

        try:
            self.broadcast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.broadcast_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.broadcast_socket.bind((self.hotspot_ip, 0))
            log_message('[*] [热点] UDP广播套接字已就绪。')
        except Exception as e:
            log_message('[!] [热点] 创建广播套接字失败: {}'.format(e))
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
            log_message('[!] [热点] 扫描脚本目录失败: {}'.format(e))
        return files_to_broadcast

    def create_broadcast_packet(self, file_path, chunk_data, chunk_index):
        try:
            filename_str = os.path.basename(file_path)
            filename = filename_str.encode('utf-8', errors='replace')[:64].ljust(64, b'\x00')
            chunk_idx = struct.pack('!I', chunk_index)
            packet = filename + chunk_idx + chunk_data
            return packet
        except Exception as e:
            log_message('[!] [热点] 创建数据包失败: {}'.format(e))
            return None

    def run(self):
        if not ENABLE_5G_HOTSPOT:
            log_message('[*] [热点] 5G热点功能已在配置中禁用。')
            return
        log_message('[*] [{}] 引擎启动...'.format(self.name))
        if not self.setup_hotspot():
            log_message('[!] [{}] 因设置失败而中止。'.format(self.name))
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
                        with open(file_path, 'rb') as f:
                            chunk_index = 0
                            while not self.stop_event.is_set():
                                chunk = f.read(self.chunk_size)
                                if not chunk: break
                                packet = self.create_broadcast_packet(file_path, chunk, chunk_index)
                                if packet:
                                    broadcast_addr = ".".join(self.hotspot_ip.split('.')[:-1]) + ".255"
                                    self.broadcast_socket.sendto(packet, (broadcast_addr, 33333))
                                    with HOTSPOT_LOCK: HOTSPOT_STATUS['bytes_transmitted'] += len(packet)
                                progress = (f.tell() / file_size) * 100 if file_size > 0 else 100
                                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": filename, "mode": "5G广播", "progress": '{:.1f}%'.format(progress), "details": '频道:{} IP:{}'.format(self.hotspot_channel, self.hotspot_ip)}
                                chunk_index += 1
                                time.sleep(self.broadcast_interval)
                        with HOTSPOT_LOCK: HOTSPOT_STATUS['files_broadcast'] += 1
                    except Exception as e:
                        log_message('[!] [{}] 广播文件 {} 时出错: {}'.format(self.name, os.path.basename(file_path), e))
        finally:
            self.cleanup_hotspot()
            with HOTSPOT_LOCK: HOTSPOT_STATUS['active'] = False
            with STATUS_LOCK:
                if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
            log_message('[*] [{}] 引擎已停止。'.format(self.name))

# ... (The rest of the original full script's classes and functions go here) ...
# --- [核心引擎] 定时速度调控 ---
def _apply_speed_level(level, reason):
    if not (0 <= level < len(SPEED_LEVELS)):
        log_message('[!] [定时调速] 无效的速度等级: {}'.format(level))
        return
    batch_size, delay_ms, num_workers = SPEED_LEVELS[level]
    with params_lock:
        global CURRENT_IP_BATCH_SIZE, CURRENT_DELAY_BETWEEN_BATCHES_S, CURRENT_TARGET_WORKERS
        CURRENT_IP_BATCH_SIZE = batch_size
        CURRENT_DELAY_BETWEEN_BATCHES_S = delay_ms / 1000.0
        CURRENT_TARGET_WORKERS = num_workers
    progress_str = '等级 {}/{}'.format(level, len(SPEED_LEVELS)-1)
    details_str = '并发:{}, 批次:{}, 延迟:{}ms'.format(num_workers, batch_size, delay_ms)
    with STATUS_LOCK:
        WORKER_STATUS["定时调速引擎"] = {"file": "N/A", "mode": reason, "progress": progress_str, "details": details_str}

class TimeBasedPacerThread(threading.Thread):
    def __init__(self, stop_event):
        super(TimeBasedPacerThread, self).__init__()
        self.stop_event, self.daemon, self.name = stop_event, True, "定时调速引擎"
        self.current_mode = None
    def run(self):
        log_message('[*] [{}] 引擎已启动。'.format(self.name))
        while not self.stop_event.is_set():
            now = datetime.now()
            if now.hour >= 1 and now.hour < 7:
                if self.current_mode != "NIGHT":
                    log_message('[*] [{}] 进入夜间全速模式。'.format(self.name))
                    _apply_speed_level(5, "夜间全速")
                    self.current_mode = "NIGHT"
            else:
                if self.current_mode != "DAY":
                    log_message('[*] [{}] 进入日间限速模式 (~ 110 KB/s)。'.format(self.name))
                    _apply_speed_level(1, "日间限速")
                    self.current_mode = "DAY"
            self.stop_event.wait(60)
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]
        log_message('[*] {} 线程已停止。'.format(self.name))

# --- 状态显示线程 ---
class StatusDisplayThread(threading.Thread):
    def __init__(self, stop_event):
        super(StatusDisplayThread, self).__init__()
        self.stop_event, self.daemon = stop_event, True
    def run(self):
        if not IS_INTERACTIVE or not CURSES_AVAILABLE:
            if not IS_INTERACTIVE:
                log_message("[*] 非交互模式, UI线程已禁用。")
            else:
                log_message("[!] 'curses' 库不可用, 回退到简单清屏模式。")
                self.fallback_loop()
            return
        try:
            curses.wrapper(self.curses_main_loop)
        except Exception as e:
            EXIT_FLAG.set()
            try: curses.endwin()
            except: pass
            print('\n[!] Curses UI 启动失败: {}'.format(e))

    def fallback_loop(self):
        clear_cmd = 'cls' if os.name == 'nt' else 'clear'
        while not self.stop_event.is_set():
            try:
                os.system(clear_cmd)
                print("全球蒲公英 (回退模式)\n" + "="*30)
                log_copy = list(LOG_QUEUE)
                for msg in log_copy[-15:]:
                    print(msg)
                time.sleep(1)
            except Exception:
                time.sleep(5)

    def curses_main_loop(self, stdscr):
        curses.curs_set(0); stdscr.nodelay(True); curses.start_color()
        curses.init_pair(1, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
        curses.init_pair(3, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLUE)
        while not self.stop_event.is_set():
            self.draw_ui(stdscr)
            time.sleep(0.5)
            try:
                if stdscr.getch() == ord('q'): EXIT_FLAG.set()
            except curses.error: pass
    def draw_ui(self, stdscr):
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        self.draw_overview(stdscr, 0, 0, w)
        self.draw_status_table(stdscr, 4, 0, h - 9, w)
        self.draw_logs(stdscr, h - 5, 0, 5, w)
        stdscr.refresh()
    def draw_overview(self, win, y, x, w):
        win.addstr(y, x, " " * (w - 1), curses.color_pair(2))
        title = " 全球蒲公英 (引擎: 定时调速) | 按 'q' 退出 "
        win.addstr(y, x + (w - len(title)) // 2, title, curses.color_pair(2) | curses.A_BOLD)
        traffic_str = '本月已发送: {:.4f} GB'.format(load_state().get('total_sent_gb', 0))
        queue_str = '任务队列 (WAN/LAN): {}/{}'.format(WAN_TASK_QUEUE.qsize(), LAN_TASK_QUEUE.qsize())
        with HOTSPOT_LOCK:
            hotspot_str = '5G热点: {} | 已广播: {} 文件 {:.2f}MB'.format(
                '活跃' if HOTSPOT_STATUS['active'] else '停止',
                HOTSPOT_STATUS['files_broadcast'],
                HOTSPOT_STATUS['bytes_transmitted'] / (1024*1024)
            )
        win.addstr(y + 2, x + 2, '{:<40} {:<40}'.format(traffic_str, queue_str))
        win.addstr(y + 3, x + 2, hotspot_str)
    def draw_status_table(self, win, y, x, h, w):
        header = '{:<25} | {:<35} | {:<22} | {}'.format('线程名称', '当前文件', '进度', '详情')
        win.addstr(y + 1, x, header[:w - 1])
        win.addstr(y + 2, x, "─" * (w - 1))
        with STATUS_LOCK:
            sorted_status = sorted(WORKER_STATUS.items())
            for i, (name, status) in enumerate(sorted_status):
                if i >= h - 4: break
                filename = status.get('file', 'N/A')
                if len(filename) > 33: filename = filename[:30] + '...'
                progress_raw = status.get('progress', 'N/A')
                if isinstance(progress_raw, str) and '%' in progress_raw:
                    try: progress_display = create_progress_bar(float(progress_raw.strip('%')))
                    except: progress_display = '{:<18}'.format(progress_raw)
                else: progress_display = '{:<18}'.format(str(progress_raw))
                details = '{} | {}'.format(status.get('mode', 'N/A'), status.get('details', ''))
                line = '{:<25} | {:<35} | {:<22} | {}'.format(name, filename, progress_display, details)
                try: win.addstr(y + 3 + i, x, line[:w - 1])
                except: pass
    def draw_logs(self, win, y, x, h, w):
        if h > 2:
            log_copy = list(LOG_QUEUE)
            for i, msg in enumerate(reversed(log_copy)):
                if i >= h - 1: break
                try: win.addstr(y + h - 1 - i, x + 1, msg[:w-2], curses.color_pair(3))
                except: pass

# --- 其他工作线程和工具函数 ---
class StartupDriveScanner(threading.Thread):
    def __init__(self):
        super(StartupDriveScanner, self).__init__()
        self.daemon, self.name = True, "硬盘扫描器"
    def run(self):
        global DYNAMIC_SOURCE_DIRS
        if not PSUTIL_AVAILABLE:
            log_message('[!] [{}] 已禁用 (psutil不可用), 仅使用默认目录。'.format(self.name))
            with source_dirs_lock:
                if os.path.isdir(DEFAULT_SOURCE_DIRECTORY): DYNAMIC_SOURCE_DIRS = [DEFAULT_SOURCE_DIRECTORY]
            return
        log_message('[*] {} 已启动, 开始一次性硬盘扫描...'.format(self.name))
        with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "检测硬盘", "progress": "50%", "details": "扫描分区中..."}
        found_dirs = {os.path.realpath(DEFAULT_SOURCE_DIRECTORY)}
        try:
            for p in psutil.disk_partitions(all=False):
                if 'cdrom' in p.opts or not p.fstype or 'ro' in p.opts: continue
                found_dirs.add(os.path.realpath(p.mountpoint))
        except Exception as e: log_message('[!] [{}] 扫描硬盘出错: {}'.format(self.name, e))
        with source_dirs_lock:
            DYNAMIC_SOURCE_DIRS = sorted(list(found_dirs))
            log_message('[*] [{}] 硬盘扫描完成。发现目录: {}'.format(self.name, ', '.join(DYNAMIC_SOURCE_DIRS)))
        with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "完成", "progress": "100%", "details": '扫描完成'}
        time.sleep(10);
        with STATUS_LOCK:
            if self.name in WORKER_STATUS: del WORKER_STATUS[self.name]

class FileScannerThread(threading.Thread):
    def __init__(self, wan_queue, lan_queue, files_to_exclude):
        super(FileScannerThread, self).__init__()
        self.wan_queue, self.lan_queue, self.files_to_exclude = wan_queue, lan_queue, files_to_exclude
        self.name, self.daemon = "文件扫描器", True
    def run(self):
        log_message('[*] {} 已启动。'.format(self.name))
        while not EXIT_FLAG.is_set():
            with source_dirs_lock:
                current_dirs_to_scan = list(DYNAMIC_SOURCE_DIRS)
            if not current_dirs_to_scan:
                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "等待目录", "progress": "---", "details": "等待硬盘扫描结果..."}
                time.sleep(5)
                continue
            for directory in current_dirs_to_scan:
                if EXIT_FLAG.is_set(): break
                try:
                    for root, dirs, files in os.walk(directory):
                        if EXIT_FLAG.is_set(): break
                        dirs[:] = [d for d in dirs if not d.startswith('.')]
                        for file in files:
                            if EXIT_FLAG.is_set(): break
                            if not file.startswith('.') and file not in self.files_to_exclude:
                                self.process_file(os.path.join(root, file))
                except Exception as e:
                    log_message('[!] [{}] 扫描目录 \'{}\' 出错: {}'.format(self.name, directory, e))
        log_message('[*] {} 线程已停止。'.format(self.name))
    def process_file(self, file_path):
        try:
            file_size = os.path.getsize(file_path)
            if file_size == 0: return
            with open(file_path, 'rb') as f:
                chunk_index = 0
                while not EXIT_FLAG.is_set():
                    chunk_data = f.read(CHUNK_SIZE)
                    if not chunk_data: break
                    packet = struct.pack('!I', chunk_index) + hashlib.md5(chunk_data).digest() + chunk_data
                    task = (packet, os.path.basename(file_path), file_size, f.tell())
                    self.wan_queue.put(task)
                    self.lan_queue.put(task)
                    progress = (f.tell() / file_size) * 100
                    with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": os.path.basename(file_path), "mode": "读取并入队", "progress": '{:.1f}%'.format(progress), "details": '处理块 {}'.format(chunk_index)}
        except (IOError, OSError): pass
        finally:
            if not EXIT_FLAG.is_set(): time.sleep(random.uniform(MIN_FILE_DELAY_S, MAX_FILE_DELAY_S))

class ChunkSenderWorker(threading.Thread):
    def __init__(self, task_queue, name, udp_socket, target_ip_pool, stop_event):
        super(ChunkSenderWorker, self).__init__()
        self.task_queue, self.name, self.udp_socket, self.target_ip_pool, self.stop_event, self.daemon = task_queue, name, udp_socket, target_ip_pool, stop_event, True
    def run(self):
        while not EXIT_FLAG.is_set() and not self.stop_event.is_set():
            try:
                packet, filename, total_size, current_pos = self.task_queue.get(timeout=1)
                progress = (current_pos / total_size) * 100 if total_size > 0 else 0
                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": filename, "mode": "WAN发送", "progress": '{:.1f}%'.format(progress), "details": "发送中..."}
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
        super(WorkerManagerThread, self).__init__()
        self.name, self.daemon = "Worker管理器", True
        self.task_queue, self.udp_socket, self.target_ip_pool = task_queue, udp_socket, target_ip_pool
        self.active_workers = {}
    def run(self):
        log_message('[*] {} 已启动。'.format(self.name))
        worker_id_counter = 0
        while not EXIT_FLAG.is_set():
            with params_lock: target_count = CURRENT_TARGET_WORKERS
            current_count = len(self.active_workers)
            with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "管理并发", "progress": "---", "details": '当前: {} / 目标: {}'.format(current_count, target_count)}
            if current_count < target_count:
                worker_id_counter += 1; worker_name = 'Sender-{}'.format(worker_id_counter)
                stop_event = threading.Event()
                worker = ChunkSenderWorker(self.task_queue, worker_name, self.udp_socket, self.target_ip_pool, stop_event)
                worker.start(); self.active_workers[worker_name] = (worker, stop_event)
            elif current_count > target_count:
                worker_name_to_stop = next(iter(self.active_workers))
                _worker_thread, stop_event = self.active_workers.pop(worker_name_to_stop)
                stop_event.set()
            time.sleep(1)
        for _name, (_thread, stop_event) in self.active_workers.items(): stop_event.set()
        log_message('[*] {} 线程已停止。'.format(self.name))

# --- 状态管理与工具函数 ---
def graceful_shutdown(signum, frame):
    if EXIT_FLAG.is_set(): return
    log_message('\n[*] 收到终止信号 {}. 准备安全退出...'.format(signum))
    EXIT_FLAG.set()

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
        return {'current_month': current_month_str, 'total_sent_gb': 0.0}

def save_state(state):
    state_path = os.path.join(BASE_DIRECTORY, STATE_FILE_PATH)
    try:
        with state_lock, open(state_path, 'w') as f: json.dump(state, f, indent=4)
    except Exception as e: log_message('[!] 严重错误: 无法保存状态文件: {}'.format(e))

def generate_ip_from_cidr(cidr_str):
    try:
        net = ipaddress.ip_network(cidr_str, strict=False)
        if net.num_addresses <= 2: return None
        return str(ipaddress.ip_address(random.randint(int(net.network_address) + 1, int(net.broadcast_address) - 1)))
    except ValueError: return None

def prepare_target_ip_pool():
    return [str(ipaddress.IPv4Address(random.randint(0, 2**32 - 1))) for _ in range(RANDOM_IP_POOL_SIZE)]

def send_chunk_in_batches(sock, packet, target_ip_pool):
    bytes_sent = 0
    with params_lock: batch_size, delay = CURRENT_IP_BATCH_SIZE, CURRENT_DELAY_BETWEEN_BATCHES_S
    if not target_ip_pool or batch_size <= 0: return 0
    for i, ip in enumerate(target_ip_pool):
        if EXIT_FLAG.is_set(): break
        try:
            sock.sendto(packet, (ip, random.randint(10240, 65535)))
            bytes_sent += len(packet)
        except OSError: pass
        if (i + 1) % batch_size == 0: time.sleep(delay)
    return bytes_sent
# ==============================================================================
# --- 主函数 ---
# ==============================================================================
def main():
    global active_threads, hotspot_broadcaster
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    if IS_INTERACTIVE:
        display_thread = StatusDisplayThread(EXIT_FLAG)
        display_thread.start(); active_threads.append(display_thread)
    else:
        log_message("[*] 非交互模式, UI已禁用。日志将直接打印到标准输出。")

    log_message("[*] 欢迎使用全球蒲公英。正在初始化...")

    files_to_exclude = {os.path.basename(__file__), STATE_FILE_PATH}
    
    try:
        global_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except Exception as e:
        log_message('[!] 严重错误: 无法创建WAN套接字: {}'.format(e)); return

    target_ip_pool = prepare_target_ip_pool()

    pacer_thread = TimeBasedPacerThread(EXIT_FLAG); pacer_thread.start(); active_threads.append(pacer_thread)
    worker_manager = WorkerManagerThread(WAN_TASK_QUEUE, global_socket, target_ip_pool); worker_manager.start(); active_threads.append(worker_manager)
    
    if ENABLE_DRIVE_AUTO_SCAN:
        drive_monitor = StartupDriveScanner(); drive_monitor.start(); active_threads.append(drive_monitor)
    
    if ENABLE_5G_HOTSPOT:
        hotspot_broadcaster = HotspotDataBroadcaster(EXIT_FLAG)
        hotspot_broadcaster.start()
        active_threads.append(hotspot_broadcaster)

    scanner = FileScannerThread(wan_queue=WAN_TASK_QUEUE, lan_queue=LAN_TASK_QUEUE, files_to_exclude=files_to_exclude)
    scanner.start(); active_threads.append(scanner)
    
    log_message("[*] 初始化完成。系统正在运行。")
    EXIT_FLAG.wait()
    log_message("\n[*] 正在等待所有线程结束...")
    for thread in active_threads:
        if thread.is_alive():
            thread.join(timeout=5)
    if global_socket: global_socket.close()
    log_message("[*] 清理完成，程序即将退出。")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if not EXIT_FLAG.is_set():
            if CURSES_AVAILABLE and IS_INTERACTIVE:
                try: curses.endwin()
                except: pass
            print('\n[!] 脚本遇到未处理的致命错误: {}'.format(e))
            import traceback
            traceback.print_exc()
    finally:
        print("\n[*] 程序执行完毕。")
