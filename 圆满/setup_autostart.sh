#!/bin/bash

# --- 开机自启设置脚本 ---
#
# 功能:
# 1. 检查是否以 root 权限运行。
# 2. 找到 'auto_copy_script.sh' 脚本并赋予其执行权限。
# 3. 创建一个 systemd 服务文件，配置为开机启动。
# 4. 将所有标准输出和错误输出重定向到 /dev/null，实现无日志运行。
# 5. 重新加载 systemd 配置并启用新创建的服务。

# --- 脚本主体 ---

# 检查是否以 root 身份运行
if [ "$EUID" -ne 0 ]; then
  echo "错误: 请使用 root 权限运行此脚本 (例如: sudo ./setup_autostart.sh)"
  exit 1
fi

# 获取此设置脚本所在的目录
SETUP_SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
# 主脚本的绝对路径
MAIN_SCRIPT_PATH="$SETUP_SCRIPT_DIR/auto_copy_script.sh"

# 检查主脚本是否存在
if [ ! -f "$MAIN_SCRIPT_PATH" ]; then
    echo "错误: 主脚本 'auto_copy_script.sh' 未在当前目录找到。"
    echo "请确保 'setup_autostart.sh' 和 'auto_copy_script.sh' 在同一个文件夹下。"
    exit 1
fi

# 确保主脚本有执行权限
chmod +x "$MAIN_SCRIPT_PATH"
echo "成功赋予 'auto_copy_script.sh' 执行权限。"

# 定义 systemd 服务文件的内容
# - Description: 服务的描述。
# - After: 确保在系统进入多用户模式后运行，此时外部驱动器通常已挂载。
# - Type=simple: 适用于长时间运行的脚本。
# - User=root: 以 root 身份运行，以确保有权限读取所有驱动器和写入数据。
# - ExecStart: 要执行的脚本的绝对路径。
# - StandardOutput=null / StandardError=null: 这是实现无日志的关键，将所有输出丢弃。
# - WantedBy: 将服务挂载到多用户目标下，实现开机自启。
SERVICE_FILE_CONTENT="[Unit]
Description=Auto Copy Script to External Drive on Boot
After=multi-user.target

[Service]
Type=simple
User=root
ExecStart=$MAIN_SCRIPT_PATH
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target"

# 定义 systemd 服务文件的路径
SERVICE_FILE_PATH="/etc/systemd/system/auto_copy.service"

# 创建服务文件
echo "正在创建 systemd 服务文件: $SERVICE_FILE_PATH"
echo "$SERVICE_FILE_CONTENT" > "$SERVICE_FILE_PATH"

# 重新加载 systemd 配置，使其识别新的服务
echo "重新加载 systemd daemon..."
systemctl daemon-reload

# 启用服务，使其开机自启
echo "启用服务 'auto_copy.service'..."
systemctl enable auto_copy.service

echo "-----------------------------------------------------"
echo "设置完成！"
echo "备份脚本现在会在每次系统启动时自动运行。"
echo ""
echo "常用命令:"
echo "  - 查看服务状态: systemctl status auto_copy.service"
echo "  - 手动启动服务: sudo systemctl start auto_copy.service"
echo "  - 手动停止服务: sudo systemctl stop auto_copy.service"
echo "  - 禁止开机自启: sudo systemctl disable auto_copy.service"
echo "-----------------------------------------------------"

exit 0
