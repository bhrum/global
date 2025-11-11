# -*- coding: utf-8 -*-
import socket
import os
import random
import time
import hashlib
import struct
import threading
import signal
import sys

# 配置参数
CHUNK_SIZE = 512
WORKER_COUNT = 1
SEND_INTERVAL = 0.01
SOURCE_DIRECTORY = os.path.dirname(os.path.abspath(__file__))

# 全局控制
EXIT_FLAG = threading.Event()

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def send_file(sock, file_path, target_ips):
    """发送单个文件到目标IP列表（每个数据块发送到一个随机IP）"""
    try:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            return
        
        with open(file_path, 'rb') as f:
            chunk_index = 0
            while not EXIT_FLAG.is_set():
                chunk_data = f.read(CHUNK_SIZE)
                if not chunk_data:
                    break
                
                packet = struct.pack('!I', chunk_index) + hashlib.md5(chunk_data).digest() + chunk_data
                
                # 每个数据块只发送到一个随机IP
                ip = random.choice(target_ips)
                try:
                    sock.sendto(packet, (ip, random.randint(10240, 65535)))
                except:
                    pass
                
                chunk_index += 1
                time.sleep(SEND_INTERVAL)
        
        log(f"完成: {os.path.basename(file_path)}")
    except Exception as e:
        log(f"错误: {e}")

def worker(sock, file_list, target_ips):
    """工作线程 - 循环发送"""
    while not EXIT_FLAG.is_set():
        for file_path in file_list:
            if EXIT_FLAG.is_set():
                break
            send_file(sock, file_path, target_ips)
        if not EXIT_FLAG.is_set():
            log("一轮完成，重新开始...")
            time.sleep(3)

def scan_files(directory):
    """扫描目录获取文件列表（包括子目录）"""
    script_name = os.path.basename(__file__)
    files = []
    for root, dirs, filenames in os.walk(directory):
        # 排除隐藏目录
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for filename in filenames:
            if not filename.startswith('.') and filename != script_name:
                files.append(os.path.join(root, filename))
    return files

def generate_random_ips(count=100):
    """生成随机IP地址"""
    return [f"{random.randint(1,223)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}" 
            for _ in range(count)]

def main():
    signal.signal(signal.SIGINT, lambda s, f: EXIT_FLAG.set())
    signal.signal(signal.SIGTERM, lambda s, f: EXIT_FLAG.set())
    
    log("全球发送 - 极简版")
    log(f"源目录: {SOURCE_DIRECTORY}")
    log(f"并发数: {WORKER_COUNT}")
    
    # 创建套接字
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # 生成目标IP池
    target_ips = generate_random_ips(100)
    
    # 扫描文件
    log("扫描文件...")
    file_list = scan_files(SOURCE_DIRECTORY)
    log(f"找到 {len(file_list)} 个文件")
    
    # 启动工作线程
    threads = []
    for i in range(WORKER_COUNT):
        t = threading.Thread(target=worker, args=(sock, file_list, target_ips), daemon=True)
        t.start()
        threads.append(t)
    
    log("开始循环发送... (Ctrl+C 停止)")
    
    # 持续运行直到中断
    try:
        EXIT_FLAG.wait()
    except KeyboardInterrupt:
        EXIT_FLAG.set()
    
    log("停止中...")
    for t in threads:
        t.join(timeout=2)
    
    sock.close()
    log("完成")

if __name__ == "__main__":
    main()