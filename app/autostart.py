"""开机自启（登录后自动运行）——写当前用户的 Run 注册表项。

为什么用 Run 键而不是 Windows 服务：
* 本程序是**带界面的托盘程序**，需要和当前登录用户会话绑定（托盘图标、桌面目录、
  Edge 用户配置都依赖用户会话）；真正的 Windows 服务跑在 session 0，弹不出界面、
  也访问不到用户桌面。
* Run 键在 `HKCU`，**不需要管理员权限**，随时可开可关。

自启时的行为：加 `--autostart` 参数启动 → 不显示主窗口，直接最小化到托盘并自动启动 FTP。

另外留了手动开关的兜底：即使界面勾选状态和注册表不一致，以注册表为准（`is_enabled`）。
"""

from __future__ import annotations

import os
import sys
from typing import Tuple

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ScanLink"
AUTOSTART_ARG = "--autostart"


def _project_root() -> str:
    """源码运行时是项目根目录；打包运行时是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def command() -> str:
    """自启要执行的命令行。

    * 打包版：直接跑 exe，带 --autostart
    * 源码版：优先用 pythonw.exe（不弹黑框），带 main.py 和 --autostart
    """
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}" {AUTOSTART_ARG}'

    python = sys.executable or "python.exe"
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.isfile(pythonw):
        python = pythonw
    script = os.path.join(_project_root(), "main.py")
    return f'"{python}" "{script}" {AUTOSTART_ARG}'


def is_supported() -> bool:
    return sys.platform.startswith("win")


def is_enabled() -> bool:
    """注册表里是否已有自启项。"""
    if not is_supported():
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except (OSError, ImportError, FileNotFoundError, ValueError):
        return False


def current_command() -> str:
    """读回注册表里实际写入的命令（用于界面提示/排障）。"""
    if not is_supported():
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except (OSError, ImportError, FileNotFoundError, ValueError):
        return ""


def enable() -> Tuple[bool, str]:
    """开启开机自启。返回 (是否成功, 说明)。"""
    if not is_supported():
        return False, "只有 Windows 支持开机自启"
    try:
        import winreg

        cmd = command()
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, cmd)
    except (OSError, ImportError) as exc:
        return False, f"写入注册表失败：{exc}"

    # 读回确认，避免"以为写进去了"却因为权限/策略没生效
    if not is_enabled():
        return False, "写入注册表后读回为空，可能被组策略拦截"
    return True, f"已开启开机自启（登录后最小化到托盘）\n{cmd}"


def disable() -> Tuple[bool, str]:
    """关闭开机自启。"""
    if not is_supported():
        return False, "只有 Windows 支持开机自启"
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        return True, "开机自启本来就是关闭的"
    except (OSError, ImportError) as exc:
        return False, f"删除注册表项失败：{exc}"
    if is_enabled():
        return False, "删除后读回仍存在，可能被组策略拦截"
    return True, "已关闭开机自启"


def toggle() -> Tuple[bool, bool, str]:
    """切换开关，返回 (是否成功, 切换后的状态, 说明)。"""
    if is_enabled():
        ok, detail = disable()
        return ok, False, detail
    ok, detail = enable()
    return ok, True, detail
