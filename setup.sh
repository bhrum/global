#!/bin/bash

# --- 配置 ---
# 你的目标Python脚本的文件名
TARGET_SCRIPT="global_dandelion.py"

# systemd服务的名称 (可以自定义)
SERVICE_NAME="dandelion-startup"
# --- 结束配置 ---


# 步骤 1: 检查环境和权限

# 检查是否以root权限运行
if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 错误：此脚本需要以root权限运行。请使用 'sudo ./setup.sh'" >&2
  exit 1
fi

# 检查目标脚本是否存在于同一目录
if [ ! -f "$TARGET_SCRIPT" ]; then
    echo "❌ 错误：目标脚本 '$TARGET_SCRIPT' 在当前目录未找到。" >&2
    exit 1
fi

echo "✅ 环境检查通过。"


# 步骤 2: 自动获取所需信息

# 获取当前脚本所在的绝对目录路径
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
echo "📂 脚本目录: $SCRIPT_DIR"

# 获取Python 3解释器的绝对路径
PYTHON_PATH=$(command -v python3)
if [ -z "$PYTHON_PATH" ]; then
    echo "❌ 错误: 未在系统中找到 'python3'。请先安装Python 3。" >&2
    exit 1
fi
echo "🐍 Python 解释器路径: $PYTHON_PATH"

# 完整的脚本执行路径
SCRIPT_EXEC_PATH="$SCRIPT_DIR/$TARGET_SCRIPT"


# 步骤 3: 创建 systemd 服务和定时器文件

echo "⚙️  正在创建 systemd 服务文件 (已配置为不记录日志)..."

# 使用 cat 和 heredoc 创建 .service 文件
# 移除了 User 和 Group，使其以 root 权限运行
cat << EOF > /etc/systemd/system/${SERVICE_NAME}.service
[Unit]
Description=Autorun script ${TARGET_SCRIPT} with root privileges (no log)
After=network.target

[Service]
# 不指定User和Group，服务将默认以root身份运行
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${PYTHON_PATH} ${SCRIPT_EXEC_PATH}
Restart=always
RestartSec=10

# === 添加以下两行来禁用 systemd 日志 ===
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
EOF


echo "⚙️  正在创建 systemd 定时器文件..."

# 恢复使用 .timer 文件来实现延迟启动
cat << EOF > /etc/systemd/system/${SERVICE_NAME}.timer
[Unit]
Description=Run ${SERVICE_NAME} 30 seconds after boot

[Timer]
OnBootSec=30s
Unit=${SERVICE_NAME}.service

[Install]
WantedBy=timers.target
EOF

echo "✅ 文件创建成功。"


# 步骤 4: 重载 systemd 并启用定时器

echo "🚀 正在重载 systemd 并启用定时器..."
# 确保先停止可能存在的旧服务，避免冲突
systemctl stop ${SERVICE_NAME}.service >/dev/null 2>&1
systemctl disable ${SERVICE_NAME}.service >/dev/null 2>&1

# 重新加载配置并启用定时器
systemctl daemon-reload
systemctl enable --now ${SERVICE_NAME}.timer

echo "-------------------------------------------"
echo "🎉 全部完成！(无日志 + 延迟启动模式)"
echo "您的脚本 '$TARGET_SCRIPT' 将在每次开机30秒后以 root 权限静默运行。"
echo "所有输出都将被丢弃，不会生成日志文件。"
echo ""
echo "您可以使用以下命令检查定时器状态:"
echo "systemctl status ${SERVICE_NAME}.timer"
echo "-------------------------------------------"

# 显示定时器的初始状态
systemctl status ${SERVICE_NAME}.timer --no-pager