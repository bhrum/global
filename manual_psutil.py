# psutil 最小替代品 - 仅提供基本功能
__version__ = "minimal-replacement"

class Process:
    def __init__(self, pid=None):
        self.pid = pid or 1
    
    def name(self):
        return "unknown"
    
    def cpu_percent(self):
        return 0.0

def disk_partitions(all=False):
    # 返回基本的分区信息
    import os
    partitions = []
    
    # 检查常见的挂载点
    mount_points = ['/']
    if os.path.exists('/home') and os.path.ismount('/home'):
        mount_points.append('/home')
    if os.path.exists('/var') and os.path.ismount('/var'):
        mount_points.append('/var')
    if os.path.exists('/usr') and os.path.ismount('/usr'):
        mount_points.append('/usr')
    if os.path.exists('/tmp') and os.path.ismount('/tmp'):
        mount_points.append('/tmp')
    
    for mp in mount_points:
        # 创建一个简单的分区对象
        class Partition:
            def __init__(self, device, mountpoint, fstype, opts):
                self.device = device
                self.mountpoint = mountpoint
                self.fstype = fstype
                self.opts = opts
        
        partitions.append(Partition(
            device='/dev/sda1',
            mountpoint=mp,
            fstype='ext4',
            opts='rw'
        ))
    
    return partitions

def cpu_percent():
    return 0.0

def virtual_memory():
    class Memory:
        total = 1024 * 1024 * 1024  # 1GB
        available = 512 * 1024 * 1024  # 512MB
        percent = 50.0
    return Memory()

def net_io_counters():
    class NetIO:
        bytes_sent = 0
        bytes_recv = 0
        packets_sent = 0
        packets_recv = 0
    return NetIO()

def boot_time():
    import time
    return time.time() - 3600  # 假设系统运行了1小时