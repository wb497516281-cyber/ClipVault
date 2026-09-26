"""autostart.py — Windows 开机自启动管理（HKCU 运行项）。

依赖：仅标准库（winreg 为 Windows 内置模块）。
用法：
  python autostart.py install    写入开机启动项（启动 ClipVault 托盘常驻）
  python autostart.py remove     移除开机启动项
  python autostart.py status     查看当前状态

安全性说明：
  - 只写 HKCU（当前用户）键值，不需要管理员权限，也不影响其它用户；
  - 命令固定为「pythonw + tray.py」，不执行任何外部下载内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # 非 Windows 环境直接给出提示
    winreg = None  # type: ignore[assignment]

#: 开机启动项在注册表中的名称
APP_NAME = "ClipVault"

#: HKCU 下的 Run 键（当前用户登录时运行）
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: 项目根目录与托盘入口
BASE_DIR = Path(__file__).resolve().parent
TRAY_SCRIPT = BASE_DIR / "tray.py"


def _pythonw() -> str:
    """优先用 pythonw.exe（无黑窗），找不到就退回当前解释器。"""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else sys.executable


def _command() -> str:
    """开机启动执行的完整命令行（路径带引号，防止空格问题）。

    源码模式：pythonw + tray.py；
    打包模式（sys.frozen）：exe 自身就是入口，只写 exe 路径 ——
    拼包内不存在的 tray.py 参数属于未定义行为。
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}"'
    return f'"{_pythonw()}" "{TRAY_SCRIPT}"'


def is_installed() -> bool:
    """是否已安装开机启动项。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except FileNotFoundError:
        return False


def install() -> str:
    """写入开机启动项，返回写入的命令行。"""
    if winreg is None:
        raise RuntimeError("开机自启动仅支持 Windows")
    command = _command()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
    return command


def remove() -> None:
    """移除开机启动项（不存在时静默成功）。"""
    if winreg is None:
        raise RuntimeError("开机自启动仅支持 Windows")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass


def status() -> dict:
    """当前状态详情。"""
    installed = is_installed()
    command = ""
    if installed and winreg is not None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            command, _ = winreg.QueryValueEx(key, APP_NAME)
    return {"installed": installed, "command": command, "key": f"HKCU\\{RUN_KEY_PATH}"}


def main() -> None:
    """命令行入口：install / remove / status。"""
    if winreg is None:
        print("开机自启动仅支持 Windows。")
        return
    action = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if action == "install":
        print(f"已写入开机启动项：\n  {install()}")
    elif action == "remove":
        remove()
        print("已移除开机启动项。")
    elif action == "status":
        info = status()
        state = "已安装" if info["installed"] else "未安装"
        print(f"ClipVault 开机自启动：{state}")
        if info["command"]:
            print(f"  注册表位置：{info['key']}")
            print(f"  启动命令：{info['command']}")
    else:
        print("用法：python autostart.py [install|remove|status]")


if __name__ == "__main__":
    main()
