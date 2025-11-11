#!/bin/bash

# --- 脚本主体 ---

# 1. 获取脚本所在的目录
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
echo "脚本目录 (源): $SCRIPT_DIR"

# 2. 定义统一的目标目录
DEST_DIR="$SCRIPT_DIR/all_copies"
echo "目标目录 (统一存放): $DEST_DIR"

# 3. 创建目标目录并设置 No-CoW (+C) 属性
#    (在循环开始前执行一次)
if [ ! -d "$DEST_DIR" ]; then
    echo "创建目标目录: $DEST_DIR"
    if ! mkdir "$DEST_DIR"; then
        echo "错误: 无法创建目录 $DEST_DIR。停止操作。"
        exit 1
    fi
    
    echo "正在为 $DEST_DIR 设置 'No CoW' (+C) 属性..."
    if ! chattr +C "$DEST_DIR" 2>/dev/null; then
        echo "警告: 无法设置 +C 属性。可能是文件系统不支持 (例如非Btrfs) 或权限不足。"
        echo "      将继续进行常规复制，但 CoW 可能未被禁用。"
    else
        echo "      No CoW (+C) 属性设置成功。"
    fi
else
    echo "目标目录 $DEST_DIR 已存在。"
    # 假设如果目录已存在，CoW 属性也已按需设置
fi

# 4. 获取所有需要复制的文件并按大小排序（从大到小）
echo "正在扫描并按大小排序文件..."
mapfile -t SOURCE_FILES < <(find "$SCRIPT_DIR" -mindepth 1 -type f ! -name "$(basename "${BASH_SOURCE[0]}")" ! -path "$DEST_DIR/*" -exec du -b {} + | sort -rn | cut -f2-)

if [ ${#SOURCE_FILES[@]} -eq 0 ]; then
    echo "信息: 脚本目录中没有需要复制的文件。"
    exit 0
fi

echo "找到 ${#SOURCE_FILES[@]} 个文件待复制"
echo "-----------------------------------------------------"

# 5. 检查剩余空间，如果所有文件都大于剩余空间则退出
CURRENT_AVAILABLE_SPACE=$(df --output=avail -B1 "$SCRIPT_DIR" | tail -n 1)
SMALLEST_FILE="${SOURCE_FILES[-1]}"
SMALLEST_SIZE=$(stat -c%s "$SMALLEST_FILE" 2>/dev/null || stat -f%z "$SMALLEST_FILE" 2>/dev/null)

if [ "$CURRENT_AVAILABLE_SPACE" -le "$SMALLEST_SIZE" ]; then
    echo "空间不足: 剩余 $(numfmt --to=iec-i --suffix=B $CURRENT_AVAILABLE_SPACE 2>/dev/null || echo "$CURRENT_AVAILABLE_SPACE 字节")，所有文件都无法复制（最小文件: $(numfmt --to=iec-i --suffix=B $SMALLEST_SIZE 2>/dev/null || echo "$SMALLEST_SIZE 字节")）。"
    echo "脚本退出。"
    exit 0
fi

# 6. 循环复制，逐个文件尝试，直到最小的文件也无法复制
COPY_COUNT=0
FILE_INDEX=0

while true; do
    # 检查当前可用空间
    CURRENT_AVAILABLE_SPACE=$(df --output=avail -B1 "$SCRIPT_DIR" | tail -n 1)
    
    # 尝试复制当前索引的文件
    CURRENT_FILE="${SOURCE_FILES[$FILE_INDEX]}"
    FILE_SIZE=$(stat -c%s "$CURRENT_FILE" 2>/dev/null || stat -f%z "$CURRENT_FILE" 2>/dev/null)
    
    if [ "$CURRENT_AVAILABLE_SPACE" -gt "$FILE_SIZE" ]; then
        # 空间足够，尝试复制文件
        FILENAME=$(basename "$CURRENT_FILE")
        
        if cp --reflink=never --backup=numbered "$CURRENT_FILE" "$DEST_DIR/" 2>/dev/null; then
            COPY_COUNT=$((COPY_COUNT + 1))
            echo "[$COPY_COUNT] 复制: $FILENAME ($(numfmt --to=iec-i --suffix=B $FILE_SIZE 2>/dev/null || echo "$FILE_SIZE 字节")) | 剩余空间: $(numfmt --to=iec-i --suffix=B $CURRENT_AVAILABLE_SPACE 2>/dev/null || echo "$CURRENT_AVAILABLE_SPACE 字节")"
        else
            echo "-----------------------------------------------------"
            echo "复制失败: 磁盘空间已满或发生错误。"
            break
        fi
        
        # 移动到下一个文件（循环回到开始）
        FILE_INDEX=$(( (FILE_INDEX + 1) % ${#SOURCE_FILES[@]} ))
    else
        # 当前文件无法复制，尝试下一个更小的文件
        FILE_INDEX=$(( (FILE_INDEX + 1) % ${#SOURCE_FILES[@]} ))
        
        # 如果已经尝试了所有文件都无法复制，退出
        SMALLEST_FILE="${SOURCE_FILES[-1]}"
        SMALLEST_SIZE=$(stat -c%s "$SMALLEST_FILE" 2>/dev/null || stat -f%z "$SMALLEST_FILE" 2>/dev/null)
        
        if [ "$CURRENT_AVAILABLE_SPACE" -le "$SMALLEST_SIZE" ]; then
            echo "-----------------------------------------------------"
            echo "空间不足: 剩余 $(numfmt --to=iec-i --suffix=B $CURRENT_AVAILABLE_SPACE 2>/dev/null || echo "$CURRENT_AVAILABLE_SPACE 字节")，连最小文件 ($(numfmt --to=iec-i --suffix=B $SMALLEST_SIZE 2>/dev/null || echo "$SMALLEST_SIZE 字节")) 也无法复制。"
            break
        fi
    fi
done

echo "脚本执行完毕。总共完成了 $COPY_COUNT 次文件复制。"
exit 0