# -*- coding: utf-8 -*-
import socket
import os
import random
import time
import hashlib
import struct
import ipaddress
import threading
import json
import signal
import queue
import re
import subprocess
from datetime import datetime
from collections import deque

# 尝试导入 psutil 库，用于网络接口和系统信息获取
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("[!] 警告: 未找到 'psutil' 库。")
    print("[!] 'psutil' 对于获取网络接口、驱动器和连接信息是推荐的，但不是强制的。")
    print("[!] 请运行 'pip install psutil' 来安装此依赖库以获得完整功能。")

# ==============================================================================
# --- 配置区域 ---
# ==============================================================================

# --- 热点配置 ---
ENABLE_HOTSPOT_MODE = True  # 设置为 True 来启动时创建热点
HOTSPOT_SSID = "amitabha"
HOTSPOT_PASSWORD = "bhrum108" # 注意: WiFi密码长度必须至少为8个字符

# --- 文件与全局控制 ---
MIN_FILE_DELAY_S = 0.5
MAX_FILE_DELAY_S = 1.0
MONTHLY_LIMIT_GB = 0  # 设置为0表示无限制 (此版本中已移除持久化流量统计)

# --- 线程与数据包配置 ---
CPU_COUNT = os.cpu_count() or 1
MAX_WORKERS = CPU_COUNT * 7
TARGET_WORKERS = MAX_WORKERS
CHUNK_SIZE = 1080
TASK_QUEUE = queue.Queue(maxsize=MAX_WORKERS * 3)

# --- 网络与广播配置 ---
ENABLE_DRIVE_AUTO_SCAN = True

# --- 其他功能 ---
ENABLE_RENAMING_EFFECT = True
RENAMING_INTERVAL = 0.001

# --- 基础配置 ---
BASE_DIRECTORY = os.getcwd()
DEFAULT_SOURCE_DIRECTORY = BASE_DIRECTORY

# --- 全局控制变量 ---
EXIT_FLAG = threading.Event()
active_threads = []
hotspot_active = False

# --- 流量与速度追踪 (线程安全) ---
bytes_sent_in_period = 0
bytes_sent_lock = threading.Lock()
session_total_bytes = 0
session_bytes_lock = threading.Lock()


# --- 状态显示与日志 ---
WORKER_STATUS = {}
LOG_QUEUE = deque(maxlen=10)
STATUS_LOCK = threading.Lock()

# --- 文件发送统计 ---
FILE_SEND_COUNT = 0
file_count_lock = threading.Lock()


# ==============================================================================
# --- 辅助函数与线程类定义 ---
# ==============================================================================
def log_message(message):
    """优化的日志记录函数，过滤发送器相关的日志"""
    if "[发送器-" in message or "创建广播套接字" in message:
        return
    timestamp = datetime.now().strftime('%H:%M:%S')
    LOG_QUEUE.append(f"[{timestamp}] {message}")

def manage_hotspot(action='start'):
    """使用 nmcli 创建或关闭WiFi热点 (需要sudo权限)"""
    global hotspot_active
    if os.geteuid() != 0:
        log_message("[!] 错误: 创建热点需要root权限。请使用 'sudo' 运行脚本。")
        return False

    if action == 'start':
        log_message(f"[*] 正在尝试创建热点: SSID={HOTSPOT_SSID}")
        
        if len(HOTSPOT_PASSWORD) < 8:
            log_message("[!] 错误: WiFi热点密码必须至少为8个字符。")
            log_message(f"[!] 当前密码 '{HOTSPOT_PASSWORD}' 长度为 {len(HOTSPOT_PASSWORD)}。请在脚本中修改 HOTSPOT_PASSWORD。")
            return False

        try:
            result = subprocess.run(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'device'], capture_output=True, text=True, check=True)
            wifi_device = None
            for line in result.stdout.strip().split('\n'):
                if 'wifi' in line:
                    wifi_device = line.split(':')[0]
                    break
            if not wifi_device:
                log_message("[!] 错误: 未找到可用的WiFi设备来创建热点。")
                return False
            log_message(f"[*] 发现WiFi设备: {wifi_device}")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log_message(f"[!] 错误: 查找WiFi设备失败: {e}")
            log_message("[!] 请确保 NetworkManager (nmcli) 已安装并正在运行。")
            return False

        command = [
            'nmcli', 'dev', 'wifi', 'hotspot', 'ifname', wifi_device,
            'ssid', HOTSPOT_SSID, 'password', HOTSPOT_PASSWORD
        ]
        try:
            subprocess.run(['nmcli', 'con', 'down', HOTSPOT_SSID], capture_output=True, text=True)
            subprocess.run(['nmcli', 'con', 'delete', HOTSPOT_SSID], capture_output=True, text=True)
            subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
            log_message(f"[*] 热点 '{HOTSPOT_SSID}' 创建成功。")
            hotspot_active = True
            log_message("[*] 等待5秒让网络稳定...")
            time.sleep(5)
            return True
        except FileNotFoundError:
            log_message("[!] 错误: 'nmcli' 命令未找到。请确保已安装NetworkManager。")
            return False
        except subprocess.TimeoutExpired:
            log_message("[!] 错误: 创建热点超时。")
            return False
        except subprocess.CalledProcessError as e:
            log_message(f"[!] 错误: 创建热点时出错: {e.stderr.strip()}")
            return False

    elif action == 'stop':
        if not hotspot_active:
            return True
        log_message(f"[*] 正在关闭并删除热点 '{HOTSPOT_SSID}'...")
        try:
            subprocess.run(['nmcli', 'con', 'down', HOTSPOT_SSID], capture_output=True, text=True, timeout=10)
            subprocess.run(['nmcli', 'con', 'delete', HOTSPOT_SSID], capture_output=True, text=True, timeout=10)
            log_message(f"[*] 热点 '{HOTSPOT_SSID}' 已关闭。")
            hotspot_active = False
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
            log_message(f"[!] 关闭热点时出错: {e}")
            return False

def get_broadcast_addresses():
    """获取WiFi接口的广播地址，并增加一个全局广播地址。"""
    interface_details = []
    if PSUTIL_AVAILABLE:
        interfaces = psutil.net_if_addrs()
        wifi_interfaces = []
        for interface_name, addresses in interfaces.items():
            if any(keyword in interface_name.lower() for keyword in ['wlan', 'wlp', 'wifi', 'wl', 'ap']):
                for addr in addresses:
                    if addr.family == socket.AF_INET and addr.address != '127.0.0.1':
                        wifi_interfaces.append({'name': interface_name, 'ip': addr.address, 'netmask': addr.netmask})
                        break
        if wifi_interfaces:
            log_message(f"[*] 通过 psutil 发现WiFi接口: {[iface['name'] for iface in wifi_interfaces]}")
            for iface in wifi_interfaces:
                try:
                    network = ipaddress.IPv4Network(f"{iface['ip']}/{iface['netmask']}", strict=False)
                    broadcast_addr = str(network.broadcast_address)
                    interface_details.append({"name": iface['name'], "local_ip": iface['ip'], "broadcast": broadcast_addr})
                    log_message(f"[*] 准备使用WiFi接口 {iface['name']}: {iface['ip']} -> 广播至 {broadcast_addr}")
                except Exception as e:
                    log_message(f"[!] 处理WiFi接口 {iface['name']} 时出错: {e}")
            
            # 如果找到了有效的WiFi接口，则增加全局广播地址
            if interface_details:
                log_message("[*] 增加全局广播地址 255.255.255.255")
                interface_details.append({"name": "Global Broadcast", "local_ip": "0.0.0.0", "broadcast": "255.255.255.255"})
                return interface_details, f"WiFi模块 ({len(interface_details)-1}个接口 + 全局)"

    log_message("[!] 警告: 未找到任何可用的WiFi接口。")
    log_message("[!] 脚本将进入【安全本地模式】，数据不会发送到局域网。")
    interface_details.append({"name": "Safe-Local-Mode", "local_ip": "127.0.0.1", "broadcast": "127.0.0.1"})
    return interface_details, "安全本地模式"

class RenamerThread(threading.Thread):
    """文件重命名特效线程。"""
    def __init__(self, file_path, stop_event):
        super().__init__()
        self.original_path, self.current_path = file_path, file_path
        self.stop_event = stop_event
        self.daemon = True
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
                except (FileNotFoundError, Exception): break
                time.sleep(RENAMING_INTERVAL)
        finally: self.restore_filename()
    def restore_filename(self):
        if self.current_path != self.original_path:
            try:
                if not os.path.exists(self.original_path): os.rename(self.current_path, self.original_path)
            except Exception as e: log_message(f"[!] 恢复文件名 '{self.original_basename}' 失败: {e}")

class StatusDisplayThread(threading.Thread):
    """状态显示线程，定期刷新屏幕上的状态信息。"""
    def __init__(self, stop_event, connection_type):
        super().__init__()
        self.stop_event = stop_event; self.daemon = True
        self.connection_type = connection_type
        self.last_check_time = time.monotonic()
    def run(self):
        global bytes_sent_in_period, FILE_SEND_COUNT, session_total_bytes
        clear_cmd = 'cls' if os.name == 'nt' else 'clear'
        while not self.stop_event.is_set():
            current_time = time.monotonic(); time_delta = current_time - self.last_check_time
            with bytes_sent_lock:
                bytes_now = bytes_sent_in_period
                bytes_sent_in_period = 0
            
            if bytes_now > 0:
                with session_bytes_lock:
                    session_total_bytes += bytes_now

            speed_mbps = (bytes_now / (1024 * 1024)) / time_delta if time_delta > 0 else 0
            self.last_check_time = current_time
            os.system(clear_cmd)
            print("=" * 60); print(" 数据蒲公英脚本 (模式: 局域网全速) ".center(60)); print("=" * 60)
            
            with session_bytes_lock:
                session_gb = session_total_bytes / (1024**3)

            hotspot_status = f"热点模式 ({HOTSPOT_SSID})" if ENABLE_HOTSPOT_MODE else self.connection_type
            print(f"网络模式: {hotspot_status:<20} | 当前速度: {speed_mbps: >7.2f} MB/s")
            print(f"任务队列: {TASK_QUEUE.qsize():<5} / {TASK_QUEUE.maxsize:<5} | 已完成文件: {FILE_SEND_COUNT} 个")
            print(f"会话流量: {session_gb:.4f} GB (本次运行)")
            
            print("-" * 60)
            scanner_status = WORKER_STATUS.get("文件扫描器", {}); file_name = scanner_status.get('file', '等待任务...')
            if len(file_name) > 40: file_name = "..." + file_name[-37:]
            progress_str = scanner_status.get('progress', '0.00%'); details = scanner_status.get('details', '')
            print(f"当前文件: {file_name}")
            try:
                progress_val = float(progress_str.strip('%')); bar_length = 40
                filled_length = int(bar_length * progress_val / 100)
                bar = '█' * filled_length + '-' * (bar_length - filled_length)
                print(f"进度: |{bar}| {progress_val:>6.2f}% ({details})")
            except (ValueError, TypeError): print(f"进度: {progress_str} ({details})")
            print("\n" + "--- 最近日志 ".ljust(60, "-"))
            log_copy = list(LOG_QUEUE)
            for msg in log_copy: print(msg)
            print("=" * 60); time.sleep(0.5)

class FileScannerThread(threading.Thread):
    """文件扫描线程，递归扫描源目录，并将文件块放入任务队列。"""
    def __init__(self, task_queue, source_dirs, files_to_exclude):
        super().__init__(); self.task_queue = task_queue; self.source_dirs = source_dirs
        self.files_to_exclude = files_to_exclude; self.name = "文件扫描器"; self.daemon = True
    def run(self):
        log_message(f"[*] {self.name} 已启动。")
        while not EXIT_FLAG.is_set():
            with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "N/A", "mode": "扫描目录", "progress": "---", "details": f"扫描 {len(self.source_dirs)} 个源..."}
            for directory in self.source_dirs:
                if os.path.isdir(directory): restore_interrupted_files(directory, self.name)
            all_files = []
            for directory in self.source_dirs:
                if not os.path.isdir(directory): continue
                all_files.extend(get_files_from_directory(directory, self.files_to_exclude, self.name))
            if not all_files:
                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": "无", "mode": "空闲", "progress": "0.00%", "details": "未找到文件，等待30秒"}
                time.sleep(30); continue
            log_message(f"[*] {self.name} 发现 {len(all_files)} 个文件，开始处理。")
            for file_path in all_files:
                if EXIT_FLAG.is_set(): break
                self.process_file(file_path)
            if not EXIT_FLAG.is_set(): log_message(f"[*] {self.name} 完成一轮扫描。"); time.sleep(random.uniform(MIN_FILE_DELAY_S, MAX_FILE_DELAY_S))
        log_message(f"[*] {self.name} 线程已停止。")
    def process_file(self, file_path):
        global FILE_SEND_COUNT
        log_message(f"[*] 开始处理: {os.path.basename(file_path)}")
        file_handle, renamer_thread, renamer_stop_event = None, None, None
        try:
            if not os.path.exists(file_path): return
            file_size = os.path.getsize(file_path);
            if file_size == 0: return
            file_handle = open(file_path, 'rb')
            if ENABLE_RENAMING_EFFECT:
                renamer_stop_event = threading.Event(); renamer_thread = RenamerThread(file_path, renamer_stop_event); renamer_thread.start()
            chunk_index = 0
            while not EXIT_FLAG.is_set():
                chunk_data = file_handle.read(CHUNK_SIZE)
                if not chunk_data: break
                packet = struct.pack('!I', chunk_index) + hashlib.md5(chunk_data).digest() + chunk_data
                chunk_index += 1; self.task_queue.put(packet)
                progress = (file_handle.tell() / file_size) * 100; sent_mb = file_handle.tell() / (1024*1024); total_mb = file_size / (1024*1024)
                with STATUS_LOCK: WORKER_STATUS[self.name] = {"file": os.path.basename(file_path), "mode": "读取并入队", "progress": f"{progress:.2f}%", "details": f"{sent_mb:.2f} / {total_mb:.2f} MB"}
            if not EXIT_FLAG.is_set() and file_handle.tell() == file_size:
                with file_count_lock: FILE_SEND_COUNT += 1
                log_message(f"[*] 文件完成: {os.path.basename(file_path)} | 总完成文件数: {FILE_SEND_COUNT}")
        except (IOError, OSError) as e: log_message(f"[!] I/O 错误 '{file_path}': {e}")
        except Exception as e:
            if not EXIT_FLAG.is_set(): log_message(f"[!] 意外错误 '{file_path}': {e}")
        finally:
            if file_handle: file_handle.close()
            if renamer_thread: renamer_stop_event.set(); renamer_thread.join(timeout=2.0)

class ChunkSenderWorker(threading.Thread):
    """工作线程，从任务队列获取数据块，并通过指定的WiFi接口广播。"""
    def __init__(self, task_queue, name, stop_event, interface_infos):
        super().__init__(); self.task_queue = task_queue; self.name = name
        self.stop_event = stop_event; self.interface_infos = interface_infos
        self.sockets = []; self.daemon = True
    def setup_sockets(self):
        for info in self.interface_infos:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if info['local_ip'] != '0.0.0.0':
                    try:
                        sock.bind((info['local_ip'], 0))
                        log_message(f"[*] [{self.name}] 成功绑定到WiFi接口 {info['name']} ({info['local_ip']})")
                    except Exception as e:
                        log_message(f"[!] [{self.name}] 绑定到接口 {info['name']} ({info['local_ip']}) 失败: {e}，跳过此接口")
                        sock.close()
                        continue
                else:
                    log_message(f"[*] [{self.name}] 创建全局广播套接字...")
                self.sockets.append((sock, info['broadcast']))
            except Exception as e: log_message(f"[!] [{self.name}] 创建套接字时出错: {e}")
    def run(self):
        global bytes_sent_in_period
        self.setup_sockets()
        if not self.sockets: log_message(f"[!] [{self.name}] 无可用套接字，线程退出。"); return
        try:
            while not EXIT_FLAG.is_set() and not self.stop_event.is_set():
                try: packet = self.task_queue.get(timeout=1)
                except queue.Empty: continue
                if EXIT_FLAG.is_set() or self.stop_event.is_set(): break
                bytes_this_packet = 0
                for sock, broadcast_addr in self.sockets:
                    try:
                        if broadcast_addr == '127.0.0.1':
                            port = random.randint(10000, 65535); sock.sendto(packet, (broadcast_addr, port)); bytes_this_packet += len(packet)
                        else:
                            # 对非本地地址（包括子网和全局广播）发送3次以增加到达率
                            for _ in range(3):
                                port = random.randint(10000, 65535)
                                sock.sendto(packet, (broadcast_addr, port))
                            bytes_this_packet += len(packet) * 3
                    except Exception as e:
                        if not EXIT_FLAG.is_set(): log_message(f"[!] [{self.name}] 广播到 {broadcast_addr} 失败: {e}")
                with bytes_sent_lock: bytes_sent_in_period += bytes_this_packet
                self.task_queue.task_done()
        finally:
            for sock, _ in self.sockets: sock.close()

class WorkerManagerThread(threading.Thread):
    """管理 ChunkSenderWorker 线程的生命周期。"""
    def __init__(self, task_queue, interface_infos):
        super().__init__(); self.name = "工人管理器"; self.daemon = True
        self.task_queue = task_queue; self.interface_infos = interface_infos; self.active_workers = {}
    def run(self):
        worker_id_counter = 0
        while not EXIT_FLAG.is_set():
            target_count = TARGET_WORKERS
            dead_workers = [name for name, (thread, _event) in self.active_workers.items() if not thread.is_alive()]
            for name in dead_workers: del self.active_workers[name]
            current_count = len(self.active_workers)
            if current_count < target_count:
                for _ in range(target_count - current_count):
                    worker_id_counter += 1; worker_name = f"发送器-{worker_id_counter}"; stop_event = threading.Event()
                    worker = ChunkSenderWorker(self.task_queue, worker_name, stop_event, self.interface_infos)
                    worker.start(); self.active_workers[worker_name] = (worker, stop_event)
            elif current_count > target_count:
                num_to_stop = current_count - target_count
                for worker_name in list(self.active_workers.keys())[:num_to_stop]:
                    _worker_thread, stop_event = self.active_workers.pop(worker_name); stop_event.set()
            time.sleep(1)
        for _name, (_thread, stop_event) in self.active_workers.items(): stop_event.set()

# ==============================================================================
# --- 状态管理与工具函数 ---
# ==============================================================================
def graceful_shutdown(signum, frame):
    if EXIT_FLAG.is_set(): return
    print(f"\n[*] 收到终止信号 {signum}。准备优雅退出...")
    EXIT_FLAG.set()
    if ENABLE_HOTSPOT_MODE: manage_hotspot(action='stop')
    alive = [t for t in active_threads if t.is_alive() and t != threading.current_thread()]
    if alive: print(f"[*] 等待 {len(alive)} 个工作线程完成...");
    for thread in alive: thread.join(timeout=5.0)
    print("[*] 清理完成，程序即将退出。")

def get_files_from_directory(directory, files_to_exclude, thread_name):
    """递归地查找目录中所有非隐藏的文件。"""
    filepaths = []
    try:
        for root, dirs, files in os.walk(directory, topdown=True):
            # 过滤隐藏目录和特殊目录
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            
            for filename in files:
                # 过滤隐藏文件和指定排除的文件
                if filename.startswith('.') or filename in files_to_exclude:
                    continue
                filepaths.append(os.path.join(root, filename))
    except FileNotFoundError:
        log_message(f"[*] 扫描时目录未找到: {directory}")
        return []
    return filepaths

def restore_interrupted_files(directory, thread_name):
    restored_count = 0
    try:
        timestamp_pattern = re.compile(r'(.*)\.(\d{19,})(.*)')
        for root, _, files in os.walk(directory):
            if EXIT_FLAG.is_set(): break
            for filename in files:
                if EXIT_FLAG.is_set(): break
                match = timestamp_pattern.match(filename)
                if match:
                    original_name = f"{match.group(1)}{match.group(3)}"; current_path, restored_path = os.path.join(root, filename), os.path.join(root, original_name)
                    try:
                        if os.path.exists(restored_path): os.remove(current_path)
                        else: os.rename(current_path, restored_path)
                        restored_count += 1
                    except Exception as e: log_message(f"[!] [{thread_name}] 恢复 '{filename}' 失败: {e}")
        if restored_count > 0: log_message(f"[*] [{thread_name}] 在 '{directory}' 中恢复了 {restored_count} 个中断的文件。")
    except Exception as e: log_message(f"[!] [{thread_name}] 检查中断文件时出错: {e}")

# ==============================================================================
# --- 主函数 ---
# ==============================================================================
def main():
    global active_threads
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    try:
        if ENABLE_HOTSPOT_MODE:
            if not manage_hotspot(action='start'):
                print("[!] 致命错误: 无法启动热点模式。请检查权限和NetworkManager状态。程序退出。")
                return

        interface_infos, connection_type = get_broadcast_addresses()
        if not interface_infos:
            print("[!] 致命错误: 无法找到任何可用的网络接口进行广播。程序退出。")
            return

        display_thread = StatusDisplayThread(EXIT_FLAG, connection_type)
        display_thread.start(); active_threads.append(display_thread)

        log_message("[*] 脚本以【局域网全速】模式启动。")
        log_message(f"[*] 当前网络模式: {connection_type}")
        
        is_safe_mode = any(info['name'] == 'Safe-Local-Mode' for info in interface_infos)
        if is_safe_mode:
            log_message("[!] 警告: 未检测到WiFi接口，已进入安全本地模式。")
        else:
            log_message("[*] 已锁定使用WiFi模块发送数据，不会占用有线网口。")

        script_name = os.path.basename(__file__)
        files_to_exclude = {script_name, 'nohup.out'}
        
        worker_manager = WorkerManagerThread(TASK_QUEUE, interface_infos)
        worker_manager.start()
        active_threads.append(worker_manager)
        
        # --- 修改: 确定要扫描的目录 ---
        source_dirs_to_scan = [os.path.realpath(DEFAULT_SOURCE_DIRECTORY)]
        if ENABLE_DRIVE_AUTO_SCAN and PSUTIL_AVAILABLE:
            log_message("[*] 正在扫描外部驱动器...")
            try:
                partitions = psutil.disk_partitions()
                for p in partitions:
                    # 跳过只读和虚拟文件系统
                    if 'ro' in p.opts or p.fstype in ['squashfs', 'tmpfs', 'devtmpfs', 'iso9660', 'udf']:
                        continue
                    
                    is_external = False
                    # Linux下的外部驱动器通常挂载在特定目录
                    if os.name != 'nt':
                        if p.mountpoint.startswith(('/media', '/run/media', '/mnt')):
                            is_external = True
                    # Windows下，排除系统盘 (通常是C:)
                    else:
                        system_drive = os.environ.get('SystemDrive', 'C:').upper()
                        if not p.device.upper().startswith(system_drive):
                            is_external = True
                    
                    if is_external:
                        real_path = os.path.realpath(p.mountpoint)
                        if real_path not in source_dirs_to_scan:
                            log_message(f"[*] 发现外部驱动器: {real_path}")
                            source_dirs_to_scan.append(real_path)
            except Exception as e:
                log_message(f"[!] 自动扫描驱动器失败: {e}")

        source_dirs_to_scan = sorted(list(set(source_dirs_to_scan))) # 去重并排序
        log_message(f"[*] 将扫描以下目录: {', '.join(source_dirs_to_scan)}")
        
        scanner = FileScannerThread(TASK_QUEUE, source_dirs_to_scan, files_to_exclude)
        scanner.start()
        active_threads.append(scanner)

        log_message(f"[*] 初始化完成。按 Ctrl+C 退出。")
        EXIT_FLAG.wait()
    finally:
        log_message("\n[*] 主线程检测到退出信号。正在清理...")
        if ENABLE_HOTSPOT_MODE: manage_hotspot(action='stop')
        for t in active_threads:
            if t.is_alive(): t.join(0.1)
        print("\n[*] 程序执行完毕。")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if not EXIT_FLAG.is_set():
            print(f"\n[!] 脚本发生未处理的致命错误: {e}")
            import traceback
            traceback.print_exc()
            EXIT_FLAG.set()
