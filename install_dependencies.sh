#!/bin/bash

# 如果任何命令执行失败，则立即中止脚本
set -e

# --- 检查是否以root权限运行 ---
if [ "$(id -u)" -ne 0 ]; then
  echo "错误：此脚本必须以root权限运行。请使用 'sudo ./install_dependencies.sh'" >&2
  exit 1
fi

echo "--- 开始为 global_dandelion_wifi.py 安装依赖 ---"

# --- 自动检测包管理器 ---
PKG_MANAGER=""
if command -v apt-get &> /dev/null; then
    PKG_MANAGER="apt-get"
elif command -v dnf &> /dev/null; then
    PKG_MANAGER="dnf"
elif command -v yum &> /dev/null; then
    PKG_MANAGER="yum"
elif command -v pacman &> /dev/null; then
    PKG_MANAGER="pacman"
else
    echo "错误：无法检测到支持的包管理器 (apt-get, dnf, yum, pacman)。" >&2
    exit 1
fi

echo "检测到包管理器: $PKG_MANAGER"

# --- 修复 EOL (End-of-Life) 发行版的软件源 ---
if [ "$PKG_MANAGER" = "apt-get" ] && [ -f /etc/os-release ]; then
    # shellcheck source=/dev/null
    . /etc/os-release
    
    # --- 确定发行版代号 ---
    # 优先使用 /etc/os-release, 如果失败则回退到 lsb_release
    CODENAME="${VERSION_CODENAME:-}"
    if [ -z "$CODENAME" ] && command -v lsb_release > /dev/null; then
        CODENAME=$(lsb_release -sc)
    fi

    if [ "${CODENAME}" = "stretch" ]; then
        echo "检测到 'stretch' (Debian 9) 版本，这是一个已停止支持的系统。"
        echo "软件源已失效，脚本将尝试切换到存档 (archive) 服务器。"
        
        # Backup original sources
        echo "正在备份原始软件源文件 (至 .bak 后缀)..."
        [ -f /etc/apt/sources.list ] && mv /etc/apt/sources.list /etc/apt/sources.list.bak
        if [ -d /etc/apt/sources.list.d ]; then
            # 避免移动一个空目录
            if [ -n "$(ls -A /etc/apt/sources.list.d 2>/dev/null)" ]; then
                mv /etc/apt/sources.list.d /etc/apt/sources.list.d.bak
            fi
        fi
        mkdir -p /etc/apt/sources.list.d

        echo "正在创建新的 Debian 存档软件源配置 (允许未签名)..."
        echo "检测到 'stretch' 版本，正在切换到国内存档 (archive) 镜像..."
        cat > /etc/apt/sources.list << EOF
deb [trusted=yes] http://mirrors.aliyun.com/debian-archive/debian/ stretch main contrib non-free
deb [trusted=yes] http://mirrors.aliyun.com/debian-archive/debian-security/ stretch/updates main contrib non-free
deb [trusted=yes] http://mirrors.aliyun.com/debian-archive/debian/ stretch-backports main contrib non-free
EOF

        # 仅当系统是 Armbian 时才添加 Armbian 的存档源
        if [ "${ID:-}" = "armbian" ]; then
            echo "正在创建新的 Armbian 存档软件源配置 (允许未签名)..."
            cat > /etc/apt/sources.list.d/armbian.list << EOF
deb [trusted=yes] https://mirrors.tuna.tsinghua.edu.cn/armbian/ stretch main utils
EOF
        fi

        echo "正在为存档仓库创建 APT 配置以禁用日期和签名检查..."
        cat > /etc/apt/apt.conf.d/99-allow-insecure << EOF
Acquire::Check-Valid-Until "false";
Acquire::Check-Date "false";
APT::Get::AllowUnauthenticated "true";
EOF

        echo "软件源配置已更新为指向存档服务器。"
    else
        # 对于非 EOL 的 Debian/Ubuntu，切换到国内镜像
        echo "检测到现代 Debian/Ubuntu 系统，尝试替换为国内镜像源..."
        SOURCES_LIST="/etc/apt/sources.list"
        if [ -f "$SOURCES_LIST" ]; then
            # 备份，但避免覆盖 stretch 的备份
            if [ ! -f "${SOURCES_LIST}.bak" ]; then
                cp "$SOURCES_LIST" "${SOURCES_LIST}.bak"
                echo "已备份 $SOURCES_LIST 至 ${SOURCES_LIST}.bak"
            fi

            echo "正在替换为阿里云镜像..."
            if [ "${ID}" = "ubuntu" ]; then
                sed -i 's|http://[a-z.]*archive.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' "$SOURCES_LIST"
                sed -i 's|http://security.ubuntu.com/ubuntu/|http://mirrors.aliyun.com/ubuntu/|g' "$SOURCES_LIST"
            elif [ "${ID}" = "debian" ]; then
                sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' "$SOURCES_LIST"
                sed -i 's|http://security.debian.org/debian-security|http://mirrors.aliyun.com/debian-security|g' "$SOURCES_LIST"
            fi
            echo "镜像源已更新。"
        else
            echo "警告: 未找到 /etc/apt/sources.list 文件。"
        fi
    fi
fi

# --- 更新软件包列表 ---
echo "正在更新软件包列表..."
case "$PKG_MANAGER" in
    apt-get)
        apt-get update
        ;;
    dnf|yum)
        if [ -f /etc/os-release ] && grep -q -i "centos" /etc/os-release; then
            echo "检测到 CentOS，尝试替换为阿里云镜像..."
            if [ ! -d /etc/yum.repos.d.bak ]; then
                cp -r /etc/yum.repos.d/ /etc/yum.repos.d.bak
                echo "已备份 /etc/yum.repos.d/ 至 /etc/yum.repos.d.bak/"
            fi
            # 禁用 mirrorlist 并将 baseurl 指向阿里云
            sed -i 's/mirrorlist/#mirrorlist/g' /etc/yum.repos.d/CentOS-*.repo
            sed -i 's|#baseurl=http://mirror.centos.org|baseurl=http://mirrors.aliyun.com|g' /etc/yum.repos.d/CentOS-*.repo
            echo "CentOS 镜像源已更新。"
        fi
        $PKG_MANAGER makecache
        ;;
    pacman)
        echo "检测到 Arch Linux，尝试添加国内镜像..."
        if [ -f /etc/pacman.d/mirrorlist ] && [ ! -f /etc/pacman.d/mirrorlist.bak ]; then
            cp /etc/pacman.d/mirrorlist /etc/pacman.d/mirrorlist.bak
            echo "已备份 /etc/pacman.d/mirrorlist"
        fi
        # 将清华大学镜像添加到文件顶部
        sed -i '1iServer = https://mirrors.tuna.tsinghua.edu.cn/archlinux/$repo/os/$arch' /etc/pacman.d/mirrorlist
        echo "Arch Linux 镜像源已更新。"
        pacman -Syy
        ;;
esac

# --- 安装系统依赖 ---
echo "正在安装所需的系统软件包 (python, pip, setuptools, wheel, network-manager) 及编译依赖..."

# --- 定义 Python 和 Pip 命令 ---
# 这些变量将根据检测到的系统和安装的 Python 版本进行设置
PYTHON_CMD="python3"
PIP_CMD="pip3"

case "$PKG_MANAGER" in
    apt-get)
        if [ -f /etc/os-release ] && grep -q -i "ubuntu" /etc/os-release; then
            echo "检测到 Ubuntu，正在添加 'deadsnakes' PPA 以获取最新的 Python..."
            apt-get install -y software-properties-common
            add-apt-repository -y ppa:deadsnakes/ppa
            apt-get update
            
            echo "正在为 Ubuntu 安装 Python 3.12..."
            apt-get install -y python3.12 python3.12-dev python3.12-venv python3-setuptools python3-wheel network-manager gcc
            PYTHON_CMD="python3.12"
            PIP_CMD="python3.12 -m pip"
        else
            # 适用于 Debian 或其他非 Ubuntu 的 apt 系统
            echo "正在为 Debian/其他系统安装默认的 python3..."
            apt-get install -y python3 python3-pip python3-setuptools python3-wheel network-manager gcc python3-dev
        fi
        ;;
    dnf|yum)
        # RHEL/CentOS 上的 Python 升级较为复杂，为保持稳定性，我们使用发行版提供的默认 python3
        $PKG_MANAGER install -y python3 python3-pip python3-setuptools python3-wheel NetworkManager gcc python3-devel
        # PYTHON_CMD 和 PIP_CMD 保持默认值 "python3" 和 "pip3"
        ;;
    pacman)
        # Arch Linux 通常提供非常新的 Python 版本
        pacman -S --noconfirm --needed python python-pip python-setuptools python-wheel networkmanager base-devel
        PYTHON_CMD="python"
        PIP_CMD="pip"
        ;;
esac

echo "系统软件包安装成功。"

# --- 安装Python依赖 ---
echo "正在通过 pip 安装 Python 库 'psutil' (使用国内镜像)..."
${PIP_CMD} install -i https://pypi.tuna.tsinghua.edu.cn/simple psutil
echo "'psutil' 安装成功。"

# --- 为主脚本设置执行权限 ---
if [ -f "global_dandelion_wifi.py" ]; then
    echo "正在为 global_dandelion_wifi.py 设置执行权限..."
    chmod +x global_dandelion_wifi.py
fi

# --- 最终说明 ---
echo ""
echo "--- 安装完成！ ---"
echo ""
echo "重要提示:"
echo "1. 请确保以下数据文件与脚本位于同一目录中:"
echo "   - GeoLite2-ASN-Blocks-IPv4.csv"
echo "   - GeoLite2-Country-Blocks-IPv4.csv"
echo "   - GeoLite2-Country-Locations-en.csv"
echo ""
echo "2. 要运行脚本并启用所有功能（如WiFi热点），请使用 sudo:"
echo "   sudo \${PYTHON_CMD:-python3} global_dandelion_wifi.py"
echo ""
echo "--------------------------------------------------------------------"