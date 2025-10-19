#!/bin/bash


# --- 配置 ---
# 已更新为您的 Worker 链接和新文件名
R2_WORKER_URL="https://pub-1687c02b123649f2813adcb31f346698.r2.dev/%E8%B4%A2%E7%A5%9E%E5%8A%A8%E6%80%81%E5%92%92%E8%BD%AE%E8%AF%AD%E9%9F%B3%E7%AB%8B%E4%BD%93%E7%9F%A9%E9%98%B5%E5%9D%9B%E5%9F%8E.amitabha"


# 每次下载之间休眠的秒数，以防止请求过于频繁
SLEEP_INTERVAL=1
# --- 配置结束 ---


# 检查 R2_WORKER_URL 是否已设置为默认值
if [ "$R2_WORKER_URL" == "https://your-worker-name.your-subdomain.workers.dev/your-file-key" ]; then
  echo "错误：请先编辑此脚本，将 R2_WORKER_URL 变量设置为您自己的 Worker 地址。"
  exit 1
fi


echo "脚本已启动。按 CTRL+C 停止。"
echo "将从以下地址循环下载: $R2_WORKER_URL"
echo "每次下载间隔: ${SLEEP_INTERVAL}秒"
echo "------------------------------------"


# 初始化计数器
SUCCESS_COUNT=0
TOTAL_BYTES=0


# 函数：将字节转换为可读格式 (KB, MB, GB)
format_bytes() {
    local bytes=$1
    if (( bytes < 1024 )); then
        # 小于 1KB，直接显示 B
        echo "${bytes} B"
    elif (( bytes < 1024 * 1024 )); then
        # 小于 1MB，显示 KB
        printf "%.2f KB\n" $(echo "$bytes / 1024" | bc -l)
    elif (( bytes < 1024 * 1024 * 1024 )); then
        # 小于 1GB，显示 MB
        printf "%.2f MB\n" $(echo "$bytes / 1024 / 1024" | bc -l)
    else
        # 大于等于 1GB，显示 GB
        printf "%.2f GB\n" $(echo "$bytes / 1024 / 1024 / 1024" | bc -l)
    fi
}




# 无限循环
while true; do
  echo "$(date +'%Y-%m-%d %H:%M:%S') - 开始第 $((SUCCESS_COUNT + 1)) 次下载..."


  # 使用 curl 的 -w (write-out) 选项来捕获下载统计信息
  STATS=$(curl -s -L -w "%{size_download},%{time_total}" -o /dev/null "$R2_WORKER_URL")
  CURL_EXIT_CODE=$?


  # 检查 curl 是否成功执行并且返回了统计数据
  if [ $CURL_EXIT_CODE -eq 0 ] && [[ "$STATS" == *","* ]]; then
    # 从 STATS 变量中解析数据
    DOWNLOAD_BYTES=$(echo "$STATS" | cut -d',' -f1)
    DOWNLOAD_TIME=$(echo "$STATS" | cut -d',' -f2)


    # 检查下载大小是否过小 (例如小于 100 字节)，这通常意味着返回了错误消息
    if [ "$DOWNLOAD_BYTES" -lt 100 ]; then
        echo "$(date +'%Y-%m-%d %H:%M:%S') - 下载成功，但文件大小异常 (${DOWNLOAD_BYTES} B)，可能收到了错误消息。"
        echo "                      正在尝试显示服务器返回的具体内容..."
        # 重新执行 curl，这次不丢弃输出，以便查看错误消息
        SERVER_RESPONSE=$(curl -s -L "$R2_WORKER_URL")
        echo "                      服务器响应: '${SERVER_RESPONSE}'"
        echo "                      请检查您的 Worker 日志或 R2 文件路径/权限。"
    else
        # 更新统计数据
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
        TOTAL_BYTES=$((TOTAL_BYTES + DOWNLOAD_BYTES))


        # 格式化为可读单位
        READABLE_SIZE=$(format_bytes "$DOWNLOAD_BYTES")
        TOTAL_READABLE_SIZE=$(format_bytes "$TOTAL_BYTES")


        echo "$(date +'%Y-%m-%d %H:%M:%S') - 下载成功! 本次: ${READABLE_SIZE}, 耗时: ${DOWNLOAD_TIME}s."
        echo "                      总计成功 ${SUCCESS_COUNT} 次, 总下载量: ${TOTAL_READABLE_SIZE}."
    fi
  else
    echo "$(date +'%Y-%m-%d %H:%M:%S') - 下载失败 (curl 退出码: $CURL_EXIT_CODE)。请检查 URL 或网络连接。"
  fi


  echo "------------------------------------"
  sleep $SLEEP_INTERVAL
done


