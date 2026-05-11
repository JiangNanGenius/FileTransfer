#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包脚本 - 使用 PyInstaller 将程序打包为独立 EXE
"""

import os
import sys
import subprocess
import shutil

def main():
    # 检查 PyInstaller 是否安装
    try:
        import PyInstaller
        print("✓ PyInstaller 已安装")
    except ImportError:
        print("正在安装 PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
    
    # 打包命令
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",  # 打包为单个文件
        "--windowed",  # 不显示控制台窗口
        "--name", "超大文件断点续传搬运器",
        "--icon", "NONE",  # 可以替换为 ico 文件路径
        "--add-data", "README.md;." if sys.platform == 'win32' else "README.md:.",
        "--clean",
        "--noconfirm",
        "file_transfer.py"
    ]
    
    print("\n开始打包...")
    print("命令:", " ".join(cmd))
    print()
    
    # 执行打包
    result = subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))
    
    if result.returncode == 0:
        print("\n✓ 打包成功!")
        exe_path = os.path.join("dist", "超大文件断点续传搬运器.exe")
        if os.path.exists(exe_path):
            size_mb = os.path.getsize(exe_path) / (1024 * 1024)
            print(f"EXE 文件: {exe_path}")
            print(f"文件大小: {size_mb:.1f} MB")
            
            # 复制到上级目录方便测试
            dest_path = "C:\\超大文件断点续传搬运器_v2.1.exe"
            shutil.copy2(exe_path, dest_path)
            print(f"已复制到: {dest_path}")
    else:
        print("\n✗ 打包失败!")
        sys.exit(1)

if __name__ == "__main__":
    main()
