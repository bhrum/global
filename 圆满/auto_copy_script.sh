#!/bin/bash

# --- 脚本主体 (无需手动配置) ---

# 1. 获取脚本所在的目录
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
echo "脚本目录: $SCRIPT_DIR"

# 2. 计算脚本所在目录及所有子目录下需要复制的文件的总大小 (以字节为单位)
#    - find "$SCRIPT_DIR" -type f: 查找脚本目录及其子目录下的所有文件
#    - ! -name "...": 排除脚本自身
#    - du -cb: 计算总大小 (以字节为单位)
#    移除了 -maxdepth 1 限制，以包含所有子目录
echo "正在计算需要复制的文件总大小 (包括子目录)..."
TOTAL_SIZE_BYTES=$(find "$SCRIPT_DIR" -type f ! -name "$(basename "${BASH_SOURCE[0]}")" -print0 | du -cb --files0-from=- | tail -n 1 | awk '{print $1}')

if [ -z "$TOTAL_SIZE_BYTES" ] || [ "$TOTAL_SIZE_BYTES" -eq 0 ]; then
    echo "信息: 脚本目录中没有需要复制的文件。"
    # logger "Auto-copy script: No files to copy from $SCRIPT_DIR."
    exit 0
fi
echo "每次复制需要的文件总大小: $TOTAL_SIZE_BYTES 字节"

# 3. 自动查找最佳的外接硬盘
BEST_DRIVE=""
MAX_SPACE=0

echo "正在扫描已挂载的外部驱动器..."

# 使用 lsblk 读取设备列表，-b 表示以字节为单位，-n 表示无标题，-o 指定输出列
# 我们寻找已挂载的设备分区(TYPE=part)，并排除根目录(/)和/boot分区
while read -r MOUNTPOINT; do
    # 确保挂载点存在且可写
    if [ -n "$MOUNTPOINT" ] && [ -d "$MOUNTPOINT" ] && [ -w "$MOUNTPOINT" ]; then
        # 获取该挂载点的可用空间 (以字节为单位)
        AVAILABLE_SPACE=$(df --output=avail -B1 "$MOUNTPOINT" | tail -n 1)
        
        echo "  - 发现驱动器: '$MOUNTPOINT', 可用空间: $AVAILABLE_SPACE 字节"

        # 如果当前驱动器的空间比已记录的最大空间要大，则更新最佳选择
        if [ "$AVAILABLE_SPACE" -gt "$MAX_SPACE" ]; then
            MAX_SPACE=$AVAILABLE_SPACE
            BEST_DRIVE=$MOUNTPOINT
        fi
    fi
done <<< "$(lsblk -bno MOUNTPOINT,TYPE | awk '$2=="part" && $1!="/" && $1!~"^/boot" {print $1}')"

# 4. 检查是否找到了合适的目标驱动器
if [ -z "$BEST_DRIVE" ]; then
    echo "错误: 未找到合适的外接硬盘或没有写入权限。"
    # logger "Auto-copy script: No suitable external drive found."
    exit 1
fi

echo "-----------------------------------------------------"
echo "选择的目标驱动器: '$BEST_DRIVE'"
echo "该驱动器初始可用空间: $MAX_SPACE 字节"
echo "-----------------------------------------------------"

# 5. 开始循环复制，直到空间不足
COPY_COUNT=0
while true; do
    # 在每次循环开始时，重新检查当前可用空间
    CURRENT_AVAILABLE_SPACE=$(df --output=avail -B1 "$BEST_DRIVE" | tail -n 1)
    echo "当前驱动器可用空间: $CURRENT_AVAILABLE_SPACE 字节"

    # 比较空间大小
    if [ "$CURRENT_AVAILABLE_SPACE" -gt "$TOTAL_SIZE_BYTES" ]; then
        COPY_COUNT=$((COPY_COUNT + 1))
        TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
        # 创建一个带时间戳的目标目录，以防重复
        DEST_DIR="$BEST_DRIVE/backup_${TIMESTAMP}"
        
        echo "空间充足，开始第 $COPY_COUNT 次复制到新目录: $DEST_DIR"
        
        # 使用 rsync 进行复制
        rsync -avh --progress --exclude "$(basename "${BASH_SOURCE[0]}")" "$SCRIPT_DIR/" "$DEST_DIR/"
        
        echo "第 $COPY_COUNT 次复制完成。"
        echo "-----------------------------------------------------"
        # 可以选择在这里暂停几秒
        # sleep 5
    else
        echo "驱动器空间不足，无法进行下一次复制。停止操作。"
        # logger "Auto-copy script: Not enough space on $BEST_DRIVE for another copy. Aborting."
        break # 退出 while 循环
    fi
done

echo "脚本执行完毕。总共完成了 $COPY_COUNT 次复制。"
exit 0

