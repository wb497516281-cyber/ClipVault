# -*- mode: python ; coding: utf-8 -*-
# packaging.spec — PyInstaller 打包配置（ClipVault 托盘常驻版：tkinter GUI + 采集）。
#
# 构建：
#   pip install -e ".[build]"        # 安装 pyinstaller
#   python make_icon.py              # 生成 assets/icon.ico（已生成可跳过）
#   pyinstaller packaging.spec
#
# 产物：dist/ClipVault/ClipVault.exe（onedir 模式，启动快、杀毒误报率低于 onefile）
# 说明：
#   - 入口是 tray.py（托盘 + 采集 + tkinter GUI 一体）；
#   - assets/ 作为数据文件打进去（托盘/窗口图标）；
#   - pywin32 的 DLL 与 pystray 的后端靠 hiddenimports / 默认 hook 处理；
#   - console=False：无黑窗；排障时可临时改 True 看日志。

block_cipher = None

a = Analysis(
    ['tray.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets', 'assets'),   # 托盘/窗口图标
    ],
    hiddenimports=[
        # pywin32 常见模块（托盘、剪贴板、窗口标题都要用）
        'win32api',
        'win32con',
        'win32gui',
        'win32clipboard',
        'win32process',
        'win32ui',
        'win32timezone',
        'pywintypes',
        'pythoncom',
        # pystray Windows 后端
        'pystray',
        'pystray._win32',
        # 本项目模块（扁平布局，PyInstaller 有时识别不到）
        'gui',
        'storage',
        'watcher',
        'clipwriter',
        'ai_client',
        'autostart',
        'config',
        'make_icon',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 用不到的大依赖，裁剪体积
        'numpy',
        'pandas',
        'matplotlib',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ClipVault',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,               # 无控制台窗口；排障时改 True
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    icon='assets/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='ClipVault',
)
