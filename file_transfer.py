#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超大文件断点续传搬运器 v2.2
支持: 大文件分块传输、断点续传、重试机制、心跳检测、进度管理、高DPI自适应
"""

import os
import sys
import json
import hashlib
import threading
import time
import shutil
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Optional, Dict, List, Any, Tuple


def format_file_size(size_bytes: int) -> str:
    """格式化文件大小显示"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    elif size_bytes < 1024 * 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024 * 1024):.2f} TB"


# Windows高DPI支持
if sys.platform == 'win32':
    try:
        import ctypes
        # 告知Windows进程是DPI感知的
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except:
        try:
            # 兼容旧版Windows
            ctypes.windll.user32.SetProcessDPIAware()
        except:
            pass  # DPI设置失败，继续运行


class FileTransferConfig:
    """传输配置"""
    # 默认值
    DEFAULT_CHUNK_SIZE = 1024 * 1024 * 8  # 8MB
    DEFAULT_MAX_RETRY = 3
    DEFAULT_HEARTBEAT_INTERVAL = 10
    PROGRESS_FILE = ".transfer_progress.json"

    def __init__(self):
        """初始化实例配置（避免多实例冲突）"""
        self.CHUNK_SIZE = self.DEFAULT_CHUNK_SIZE
        self.MAX_RETRY = self.DEFAULT_MAX_RETRY
        self.HEARTBEAT_INTERVAL = self.DEFAULT_HEARTBEAT_INTERVAL

    def validate(self):
        """验证配置有效性"""
        if self.CHUNK_SIZE < 1024 * 1024:  # 最小1MB
            raise ValueError("分块大小不能小于1MB")
        if self.MAX_RETRY < 0:
            raise ValueError("重试次数不能小于0")
        if self.HEARTBEAT_INTERVAL < 1:
            raise ValueError("心跳间隔不能小于1秒")


class TransferProgress:
    """传输进度管理"""
    
    def __init__(self, progress_file: str):
        self.progress_file = progress_file
        self.progress = {}
        self.load()
    
    def load(self):
        """加载进度文件"""
        if os.path.exists(self.progress_file):
            try:
                with open(self.progress_file, 'r', encoding='utf-8') as f:
                    self.progress = json.load(f)
            except:
                self.progress = {}
    
    def save(self):
        """保存进度文件"""
        with open(self.progress_file, 'w', encoding='utf-8') as f:
            json.dump(self.progress, f, indent=2, ensure_ascii=False)
    
    def get_file_progress(self, file_key: str):
        """获取文件传输进度"""
        return self.progress.get(file_key, {
            'chunks_completed': [],
            'total_chunks': 0,
            'completed': False,
            'file_size': 0,
            'md5': None
        })
    
    def update_chunk(self, file_key: str, chunk_index: int):
        """更新块进度"""
        if file_key not in self.progress:
            self.progress[file_key] = {
                'chunks_completed': [],
                'total_chunks': 0,
                'completed': False,
                'file_size': 0,
                'md5': None
            }
        if chunk_index not in self.progress[file_key]['chunks_completed']:
            self.progress[file_key]['chunks_completed'].append(chunk_index)
            self.save()
    
    def set_complete(self, file_key: str, md5: str):
        """标记文件完成"""
        if file_key in self.progress:
            self.progress[file_key]['completed'] = True
            self.progress[file_key]['md5'] = md5
            self.save()
    
    def is_completed(self, file_key: str) -> bool:
        """检查是否已完成"""
        return self.progress.get(file_key, {}).get('completed', False)


class FileTransfer:
    """文件传输核心类"""
    
    def __init__(self, progress_callback=None, status_callback=None, log_callback=None):
        self.config = FileTransferConfig()
        self.progress_manager = None
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.log_callback = log_callback
        self._is_paused = False
        self._is_stopped = False
        self._last_activity = None
        self._heartbeat_thread = None
        self._heartbeat_stop_event = threading.Event()  # 使用事件确保线程安全退出
        self._transfer_lock = threading.Lock()  # 传输状态锁
        
    def _update_activity(self):
        """更新最后活跃时间（线程安全）"""
        with self._transfer_lock:
            self._last_activity = datetime.now()
    
    def _heartbeat_worker(self):
        """心跳检测线程（使用事件安全退出）"""
        while not self._heartbeat_stop_event.is_set():
            # 使用wait代替sleep，可以被立即唤醒退出
            if self._heartbeat_stop_event.wait(self.config.HEARTBEAT_INTERVAL):
                break
            if not self._is_paused and not self._is_stopped:
                self._update_activity()
                if self.log_callback:
                    self.log_callback(f"[心跳] 传输中... 最后活跃: {self._last_activity.strftime('%H:%M:%S')}")
    
    def set_progress_file(self, progress_file: str):
        """设置进度文件路径"""
        self.progress_manager = TransferProgress(progress_file)
    
    def pause(self):
        """暂停传输"""
        self._is_paused = True
        if self.log_callback:
            self.log_callback("[暂停] 传输已暂停")
    
    def resume(self):
        """继续传输"""
        self._is_paused = False
        self._update_activity()
        if self.log_callback:
            self.log_callback("[继续] 恢复传输")
    
    def stop(self):
        """停止传输（线程安全）"""
        with self._transfer_lock:
            self._is_stopped = True
        # 停止心跳线程
        self._heartbeat_stop_event.set()
        if self.log_callback:
            self.log_callback("[停止] 传输已停止")

    def pause(self):
        """暂停传输（线程安全）"""
        with self._transfer_lock:
            self._is_paused = True
        if self.log_callback:
            self.log_callback("[暂停] 传输已暂停")

    def resume(self):
        """继续传输（线程安全）"""
        with self._transfer_lock:
            self._is_paused = False
        self._update_activity()
        if self.log_callback:
            self.log_callback("[继续] 恢复传输")
    
    def calculate_md5(self, file_path: str) -> str:
        """计算文件MD5（使用更大的buffer提升性能）"""
        md5_hash = hashlib.md5()
        buffer_size = 1024 * 1024 * 8  # 8MB 缓冲
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(buffer_size)
                if not chunk:
                    break
                md5_hash.update(chunk)
        return md5_hash.hexdigest()
    
    def get_file_key(self, src_path: str, dst_path: str) -> str:
        """生成文件唯一标识"""
        src = os.path.abspath(src_path)
        dst = os.path.abspath(dst_path)
        file_stat = os.stat(src_path)
        key_str = f"{src}_{dst}_{file_stat.st_size}_{file_stat.st_mtime}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def transfer_chunk_with_retry(self, src_path: str, dst_path: str, 
                                   chunk_index: int, total_chunks: int,
                                   file_key: str, dst_dir_checked: bool = False) -> bool:
        """传输单个块（带重试机制）
        
        Args:
            src_path: 源文件路径
            dst_path: 目标文件路径
            chunk_index: 块索引
            total_chunks: 总块数
            file_key: 文件唯一标识
            dst_dir_checked: 目标目录是否已检查（避免重复IO）
        """
        chunk_size = self.config.CHUNK_SIZE
        start_pos = chunk_index * chunk_size
        
        # 只在第一次检查目录
        if not dst_dir_checked:
            dst_dir = os.path.dirname(dst_path)
            if dst_dir:
                os.makedirs(dst_dir, exist_ok=True)
        
        for attempt in range(self.config.MAX_RETRY):
            try:
                with open(src_path, 'rb') as src_file:
                    src_file.seek(start_pos)
                    chunk_data = src_file.read(chunk_size)
                
                # 写入目标文件
                mode = 'r+b' if os.path.exists(dst_path) else 'w+b'
                with open(dst_path, mode) as dst_file:
                    dst_file.seek(start_pos)
                    dst_file.write(chunk_data)
                    dst_file.flush()
                    os.fsync(dst_file.fileno())
                
                self.progress_manager.update_chunk(file_key, chunk_index)
                self._update_activity()
                return True
                
            except Exception as e:
                if attempt < self.config.MAX_RETRY - 1:
                    if self.log_callback:
                        self.log_callback(f"[重试 {attempt+1}] 块 {chunk_index}: {str(e)}")
                    time.sleep(1)
                else:
                    if self.log_callback:
                        self.log_callback(f"[失败] 块 {chunk_index}: {str(e)} (已达最大重试次数)")
        
        return False
    
    def check_progress(self, src_path: str, dst_path: str) -> dict:
        """检查文件传输进度"""
        if not os.path.exists(src_path):
            return {'error': '源文件不存在'}
        
        if not self.progress_manager:
            return {'error': '未加载进度文件'}
        
        file_size = os.path.getsize(src_path)
        total_chunks = (file_size + self.config.CHUNK_SIZE - 1) // self.config.CHUNK_SIZE
        file_key = self.get_file_key(src_path, dst_path)
        
        progress = self.progress_manager.get_file_progress(file_key)
        completed_chunks = len(progress['chunks_completed'])
        percent = (completed_chunks / total_chunks * 100) if total_chunks > 0 else 0
        
        return {
            'file': os.path.basename(src_path),
            'file_size': file_size,
            'total_chunks': total_chunks,
            'completed_chunks': completed_chunks,
            'percent': percent,
            'completed': progress['completed'],
            'md5': progress.get('md5', ''),
            'last_activity': self._last_activity
        }
    
    def transfer_file(self, src_path: str, dst_path: str) -> dict:
        """传输单个文件（支持断点续传）"""
        if not os.path.exists(src_path):
            raise FileNotFoundError(f"源文件不存在: {src_path}")
        
        file_key = self.get_file_key(src_path, dst_path)
        
        if not self.progress_manager:
            # 使用默认进度文件
            default_progress = os.path.join(os.path.dirname(src_path), self.config.PROGRESS_FILE)
            self.progress_manager = TransferProgress(default_progress)
        
        file_size = os.path.getsize(src_path)
        total_chunks = (file_size + self.config.CHUNK_SIZE - 1) // self.config.CHUNK_SIZE
        
        # 检查是否已完成
        if self.progress_manager.is_completed(file_key):
            # 验证目标文件
            if os.path.exists(dst_path) and os.path.getsize(dst_path) == file_size:
                if self.status_callback:
                    self.status_callback(f"文件已完成: {os.path.basename(src_path)}")
                return {'success': True, 'skipped': True, 'file': src_path}
        
        # 获取已完成的块
        progress = self.progress_manager.get_file_progress(file_key)
        completed_chunks = set(progress['chunks_completed'])
        
        # 初始化目标文件（如果不存在）- 使用稀疏文件方式，避免预分配大文件耗时
        if not os.path.exists(dst_path):
            dst_dir = os.path.dirname(dst_path)
            if dst_dir:
                os.makedirs(dst_dir, exist_ok=True)
            # 创建空文件，不预分配空间（避免TB级文件初始化耗时）
            if file_size > 0:
                with open(dst_path, 'wb') as f:
                    # 使用 seek+write 创建稀疏文件（支持大文件快速创建）
                    f.seek(file_size - 1)
                    f.write(b'\0')
                    f.flush()
                    os.fsync(f.fileno())
            else:
                # 空文件
                open(dst_path, 'wb').close()
        
        # 计算需要传输的块
        chunks_to_transfer = [i for i in range(total_chunks) if i not in completed_chunks]
        total_transfer = len(chunks_to_transfer)
        
        if self.log_callback:
            if total_transfer == total_chunks:
                self.log_callback(f"[开始] {os.path.basename(src_path)} ({total_chunks} 块, {format_file_size(file_size)})")
            else:
                self.log_callback(f"[继续] {os.path.basename(src_path)} (剩余 {total_transfer} 块)")
        
        # 启动心跳线程（安全重置）
        with self._transfer_lock:
            self._is_stopped = False
            self._is_paused = False
            self._heartbeat_stop_event.clear()
        self._update_activity()
        # 确保旧线程已停止
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_stop_event.set()
            self._heartbeat_thread.join(timeout=1.0)
        # 启动新线程
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_worker, daemon=True)
        self._heartbeat_thread.start()
        
        # 单线程传输
        completed = len(completed_chunks)
        failed_chunks = []
        
        for chunk_idx in chunks_to_transfer:
            if self._is_stopped:
                break
                
            # 等待暂停解除（使用事件更高效）
            while True:
                with self._transfer_lock:
                    if not self._is_paused or self._is_stopped:
                        break
                time.sleep(0.1)
            # 双重检查停止状态
            with self._transfer_lock:
                if self._is_stopped:
                    break
            
            # 传输块（带重试），只在第一次检查目录
            first_chunk = (chunk_idx == chunks_to_transfer[0])
            success = self.transfer_chunk_with_retry(
                src_path, dst_path, chunk_idx, total_chunks, file_key, first_chunk
            )
            
            if success:
                completed += 1
                if self.progress_callback:
                    self.progress_callback(src_path, completed, total_chunks, file_size)
            else:
                failed_chunks.append(chunk_idx)
        
        # 停止心跳（安全清理）
        self._heartbeat_stop_event.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=2.0)  # 最多等待2秒
        
        was_stopped = self._is_stopped
        with self._transfer_lock:
            self._is_stopped = True
        
        # 如果被停止，返回停止状态
        if was_stopped and not failed_chunks:
            return {'success': False, 'stopped': True, 'file': src_path}
        
        # 检查是否全部完成
        if not failed_chunks:
            # 最终验证 - MD5校验
            if self.log_callback:
                self.log_callback(f"[校验] {os.path.basename(src_path)}")
            
            src_md5 = self.calculate_md5(src_path)
            dst_md5 = self.calculate_md5(dst_path)
            
            if src_md5 != dst_md5:
                if self.log_callback:
                    self.log_callback(f"[失败] MD5校验不匹配")
                return {'success': False, 'md5_mismatch': True, 'file': src_path}
            
            self.progress_manager.set_complete(file_key, src_md5)
            
            if self.log_callback:
                self.log_callback(f"[完成] {os.path.basename(src_path)}")
            
            return {
                'success': True,
                'file': src_path,
                'size': file_size,
                'md5': src_md5
            }
        
        return {'success': False, 'failed_chunks': failed_chunks, 'file': src_path}


class ModernStyle:
    """现代深色主题配色"""
    # 主色调 - 深蓝色系
    BG_PRIMARY = "#1a1b26"
    BG_SECONDARY = "#24283b"
    BG_CARD = "#292e42"
    BG_HOVER = "#3b4261"
    
    # 文字颜色
    TEXT_PRIMARY = "#c0caf5"
    TEXT_SECONDARY = "#a9b1d6"
    TEXT_MUTED = "#565f89"
    
    # 强调色
    ACCENT_PRIMARY = "#7aa2f7"
    ACCENT_SUCCESS = "#9ece6a"
    ACCENT_WARNING = "#e0af68"
    ACCENT_DANGER = "#f7768e"
    ACCENT_INFO = "#7dcfff"
    
    # 边框
    BORDER_COLOR = "#414868"
    BORDER_RADIUS = 8
    
    # 字体
    FONT_FAMILY = "微软雅黑"
    FONT_MONO = "Consolas"


class RoundedButton(tk.Canvas):
    """圆角按钮组件（支持DPI自适应）"""
    def __init__(self, parent, text, command, width=120, height=36, 
                 bg=ModernStyle.ACCENT_PRIMARY, fg=ModernStyle.BG_PRIMARY,
                 hover_bg=ModernStyle.BG_HOVER, disabled=False, dpi_scale=1.0, **kwargs):
        # DPI缩放
        self.dpi_scale = dpi_scale
        scaled_width = DPIHelper.scale(width, dpi_scale)
        scaled_height = DPIHelper.scale(height, dpi_scale)
        scaled_radius = DPIHelper.scale(8, dpi_scale)
        
        # 使用按钮本身的颜色作为Canvas背景，消除边界
        super().__init__(parent, width=scaled_width, height=scaled_height, 
                        bg=bg, highlightthickness=0, bd=0, **kwargs)
        self._text = text
        self.command = command
        self.bg = bg
        self.hover_bg = hover_bg
        self.fg = fg
        self.disabled = disabled
        self.width = scaled_width
        self.height = scaled_height
        self.radius = scaled_radius
        
        self.current_bg = self.bg if not disabled else ModernStyle.TEXT_MUTED
        self.draw_button()
        
        if not disabled:
            # 绑定Canvas级别的事件（覆盖整个Canvas区域）
            self.bind("<Enter>", self.on_enter)
            self.bind("<Leave>", self.on_leave)
            self.bind("<Button-1>", self.on_click)
            self.bind("<ButtonRelease-1>", self.on_release)
    
    def draw_button(self):
        self.delete("all")
        # 圆角背景
        self.create_rounded_rect(0, 0, self.width, self.height, 
                                self.radius, fill=self.current_bg, outline="")
        # Canvas背景也同步更新，消除边缘缝隙
        self.configure(bg=self.current_bg)
        # 文字（DPI缩放）- 只显示，不需要单独绑定事件
        # Canvas级别的事件已经覆盖整个区域
        font_size = DPIHelper.get_scaled_font_size(10)
        self.create_text(self.width//2, self.height//2, text=self._text, 
                        fill=self.fg, font=(ModernStyle.FONT_FAMILY, font_size, "bold"))
    
    def create_rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self.create_polygon(points, **kwargs, smooth=True)
    
    def on_enter(self, event):
        if self.disabled:
            return
        self.current_bg = self.hover_bg
        self.draw_button()
    
    def on_leave(self, event):
        if self.disabled:
            return
        self.current_bg = self.bg
        self.draw_button()
    
    def on_click(self, event=None):
        if self.disabled:
            return
        # 点击时轻微变暗
        self.current_bg = self.bg
        self.draw_button()
    
    def on_release(self, event=None):
        if self.disabled:
            return
        self.current_bg = self.hover_bg
        self.draw_button()
        if self.command:
            self.command()
    
    def set_state(self, disabled):
        """设置按钮状态"""
        self.disabled = disabled
        self.current_bg = self.bg if not disabled else ModernStyle.TEXT_MUTED
        self.draw_button()
        # 解绑和重新绑定事件
        events = ["<Enter>", "<Leave>", "<Button-1>", "<ButtonRelease-1>"]
        for event in events:
            self.unbind(event)
        if not disabled:
            self.bind("<Enter>", self.on_enter)
            self.bind("<Leave>", self.on_leave)
            self.bind("<Button-1>", self.on_click)
            self.bind("<ButtonRelease-1>", self.on_release)


class ModernProgressbar(tk.Canvas):
    """现代风格进度条（支持DPI自适应）"""
    def __init__(self, parent, width=760, height=20, dpi_scale=1.0, **kwargs):
        # DPI缩放
        scaled_width = DPIHelper.scale(width, dpi_scale)
        scaled_height = DPIHelper.scale(height, dpi_scale)
        scaled_radius = DPIHelper.scale(10, dpi_scale)
        
        super().__init__(parent, width=scaled_width, height=scaled_height, 
                        bg=ModernStyle.BG_CARD, highlightthickness=0, **kwargs)
        self.width = scaled_width
        self.height = scaled_height
        self.value = 0
        self.radius = scaled_radius
        self.dpi_scale = dpi_scale
        self.draw_bar()
    
    def draw_bar(self):
        self.delete("all")
        # 背景
        self.create_rounded_rect(0, 0, self.width, self.height, 
                                self.radius, fill=ModernStyle.BG_SECONDARY, outline="")
        # 进度
        if self.value > 0:
            fill_width = int((self.value / 100) * self.width)
            if fill_width > self.radius * 2:
                self.create_rounded_rect(0, 0, fill_width, self.height, 
                                        self.radius, fill=ModernStyle.ACCENT_SUCCESS, outline="")
            elif fill_width > 0:
                self.create_oval(0, 0, fill_width, self.height, 
                                fill=ModernStyle.ACCENT_SUCCESS, outline="")
    
    def create_rounded_rect(self, x1, y1, x2, y2, radius, **kwargs):
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self.create_polygon(points, **kwargs, smooth=True)
    
    def set_value(self, value):
        self.value = max(0, min(100, value))
        self.draw_bar()


class DPIHelper:
    """DPI自适应辅助类"""
    
    _cached_scale = None  # 缓存缩放比例
    
    @staticmethod
    def get_scaling_factor():
        """获取系统DPI缩放比例（带缓存）"""
        if DPIHelper._cached_scale is not None:
            return DPIHelper._cached_scale
            
        if sys.platform != 'win32':
            DPIHelper._cached_scale = 1.0
            return 1.0
        
        try:
            import ctypes
            user32 = ctypes.windll.user32
            
            # 获取主显示器DPI
            hdc = user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            user32.ReleaseDC(0, hdc)
            
            # 计算缩放比例（基准96 DPI = 100%）
            scaling = dpi / 96.0
            DPIHelper._cached_scale = max(1.0, scaling)
            return DPIHelper._cached_scale
        except:
            DPIHelper._cached_scale = 1.0
            return 1.0
    
    @staticmethod
    def scale(value, factor=None):
        """根据DPI缩放数值"""
        if factor is None:
            factor = DPIHelper.get_scaling_factor()
        return int(value * factor)
    
    @staticmethod
    def get_scaled_font_size(base_size):
        """获取缩放后的字体大小"""
        factor = DPIHelper.get_scaling_factor()
        return int(base_size * factor)
    
    @staticmethod
    def reset_cache():
        """重置缓存（用于DPI变更时）"""
        DPIHelper._cached_scale = None


class FileTransferGUI:
    """图形界面类 - 现代深色主题 + DPI自适应"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("超大文件断点续传搬运器 v2.2")
        
        # 获取DPI缩放比例
        self.dpi_scale = DPIHelper.get_scaling_factor()
        
        # 根据DPI缩放窗口尺寸
        base_width = 850
        base_height = 620
        base_min_width = 800
        base_min_height = 550
        
        scaled_width = DPIHelper.scale(base_width, self.dpi_scale)
        scaled_height = DPIHelper.scale(base_height, self.dpi_scale)
        scaled_min_width = DPIHelper.scale(base_min_width, self.dpi_scale)
        scaled_min_height = DPIHelper.scale(base_min_height, self.dpi_scale)
        
        self.root.geometry(f"{scaled_width}x{scaled_height}")
        self.root.minsize(scaled_min_width, scaled_min_height)
        self.root.configure(bg=ModernStyle.BG_PRIMARY)
        
        # 配置样式
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure('TScrollbar', background=ModernStyle.BG_SECONDARY, 
                           troughcolor=ModernStyle.BG_CARD, bordercolor=ModernStyle.BG_SECONDARY,
                           arrowcolor=ModernStyle.TEXT_PRIMARY)
        self.style.map('TScrollbar', background=[('active', ModernStyle.BG_HOVER)])
        
        self.transfer = None
        self.transfer_thread = None
        
        # 变量
        self.source_path = tk.StringVar()
        self.dest_path = tk.StringVar()
        self.chunk_size = tk.StringVar(value="8")  # MB
        self.retry_count = tk.StringVar(value="3")
        self.heartbeat = tk.StringVar(value="10")
        self.status_text = tk.StringVar(value="就绪")
        self.last_activity_text = tk.StringVar(value="最后活跃: --")
        
        self.build_ui()
        
    def create_card(self, parent, padx=20, pady=15):
        """创建卡片容器（DPI自适应）"""
        scaled_padx = DPIHelper.scale(padx, self.dpi_scale)
        scaled_pady = DPIHelper.scale(pady, self.dpi_scale)
        card = tk.Frame(parent, bg=ModernStyle.BG_CARD, padx=scaled_padx, pady=scaled_pady)
        return card
    
    def create_input(self, parent, textvariable, width=None, **kwargs):
        """创建现代输入框（DPI自适应）"""
        font_size = DPIHelper.get_scaled_font_size(10)
        entry = tk.Entry(parent, textvariable=textvariable,
                        font=(ModernStyle.FONT_FAMILY, font_size),
                        bg=ModernStyle.BG_SECONDARY,
                        fg=ModernStyle.TEXT_PRIMARY,
                        insertbackground=ModernStyle.TEXT_PRIMARY,
                        relief=tk.FLAT,
                        bd=0,
                        **kwargs)
        if width:
            entry.config(width=width)
        return entry
    
    def build_ui(self):
        """构建现代风格界面（DPI自适应）"""
        # 主容器
        scaled_padx = DPIHelper.scale(25, self.dpi_scale)
        scaled_pady = DPIHelper.scale(20, self.dpi_scale)
        main_container = tk.Frame(self.root, bg=ModernStyle.BG_PRIMARY, padx=scaled_padx, pady=scaled_pady)
        main_container.pack(fill=tk.BOTH, expand=True)
        
        # === 标题区域 ===
        header_frame = tk.Frame(main_container, bg=ModernStyle.BG_PRIMARY)
        header_pady = (0, DPIHelper.scale(20, self.dpi_scale))
        header_frame.pack(fill=tk.X, pady=header_pady)
        
        # 主标题
        title_font_size = DPIHelper.get_scaled_font_size(18)
        title_label = tk.Label(header_frame, text="📦 超大文件断点续传搬运器", 
                               font=(ModernStyle.FONT_FAMILY, title_font_size, "bold"),
                               bg=ModernStyle.BG_PRIMARY,
                               fg=ModernStyle.TEXT_PRIMARY,
                               anchor="w")
        title_label.pack(fill=tk.X)
        
        # 副标题
        subtitle_font_size = DPIHelper.get_scaled_font_size(10)
        subtitle_label = tk.Label(header_frame, text="支持断点续传 · MD5校验 · 多线程传输",
                                 font=(ModernStyle.FONT_FAMILY, subtitle_font_size),
                                 bg=ModernStyle.BG_PRIMARY,
                                 fg=ModernStyle.TEXT_MUTED,
                                 anchor="w")
        subtitle_pady = (DPIHelper.scale(5, self.dpi_scale), 0)
        subtitle_label.pack(fill=tk.X, pady=subtitle_pady)
        
        # === 文件选择卡片 ===
        file_card = self.create_card(main_container)
        file_card_pady = (0, DPIHelper.scale(15, self.dpi_scale))
        file_card.pack(fill=tk.X, pady=file_card_pady)
        
        # 源文件行
        row1 = tk.Frame(file_card, bg=ModernStyle.BG_CARD)
        row1_pady = (0, DPIHelper.scale(12, self.dpi_scale))
        row1.pack(fill=tk.X, pady=row1_pady)
        
        src_label_font = DPIHelper.get_scaled_font_size(10)
        tk.Label(row1, text="📄 源文件", 
                font=(ModernStyle.FONT_FAMILY, src_label_font),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_SECONDARY,
                width=12, anchor="w").pack(side=tk.LEFT)
        
        self.source_entry = self.create_input(row1, self.source_path)
        source_padx = (0, DPIHelper.scale(10, self.dpi_scale))
        self.source_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=source_padx)
        
        self.browse_src_btn = RoundedButton(row1, text="浏览", command=self.browse_source,
                                           width=80, height=32,
                                           bg=ModernStyle.ACCENT_INFO,
                                           hover_bg=ModernStyle.BG_HOVER,
                                           dpi_scale=self.dpi_scale)
        self.browse_src_btn.pack(side=tk.LEFT)
        
        # 目标路径行
        row2 = tk.Frame(file_card, bg=ModernStyle.BG_CARD)
        row2_pady = (DPIHelper.scale(12, self.dpi_scale), 0)
        row2.pack(fill=tk.X, pady=row2_pady)
        
        dst_label_font = DPIHelper.get_scaled_font_size(10)
        tk.Label(row2, text="🎯 目标路径", 
                font=(ModernStyle.FONT_FAMILY, dst_label_font),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_SECONDARY,
                width=12, anchor="w").pack(side=tk.LEFT)
        
        self.dest_entry = self.create_input(row2, self.dest_path)
        dest_padx = (0, DPIHelper.scale(10, self.dpi_scale))
        self.dest_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=dest_padx)
        
        self.browse_dst_btn = RoundedButton(row2, text="浏览", command=self.browse_dest,
                                           width=80, height=32,
                                           bg=ModernStyle.ACCENT_INFO,
                                           hover_bg=ModernStyle.BG_HOVER,
                                           dpi_scale=self.dpi_scale)
        self.browse_dst_btn.pack(side=tk.LEFT)
        
        # === 高级参数卡片 ===
        params_card = self.create_card(main_container)
        params_pady = (0, DPIHelper.scale(15, self.dpi_scale))
        params_card.pack(fill=tk.X, pady=params_pady)
        
        params_header = tk.Frame(params_card, bg=ModernStyle.BG_CARD)
        params_header_pady = (0, DPIHelper.scale(12, self.dpi_scale))
        params_header.pack(fill=tk.X, pady=params_header_pady)
        
        params_title_font = DPIHelper.get_scaled_font_size(11)
        tk.Label(params_header, text="⚙️ 高级参数",
                font=(ModernStyle.FONT_FAMILY, params_title_font, "bold"),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_PRIMARY,
                anchor="w").pack(side=tk.LEFT)
        
        params_row = tk.Frame(params_card, bg=ModernStyle.BG_CARD)
        params_row.pack(fill=tk.X)
        
        # 块大小
        param_label_font = DPIHelper.get_scaled_font_size(9)
        param1_padx = (0, DPIHelper.scale(25, self.dpi_scale))
        param1_label_pady = (0, DPIHelper.scale(4, self.dpi_scale))
        
        param1 = tk.Frame(params_row, bg=ModernStyle.BG_CARD)
        param1.pack(side=tk.LEFT, padx=param1_padx)
        tk.Label(param1, text="块大小 (MB)",
                font=(ModernStyle.FONT_FAMILY, param_label_font),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_MUTED).pack(anchor="w", pady=param1_label_pady)
        self.chunk_entry = self.create_input(param1, self.chunk_size, width=10)
        self.chunk_entry.pack()
        
        # 重试次数
        param2_padx = (0, DPIHelper.scale(25, self.dpi_scale))
        param2 = tk.Frame(params_row, bg=ModernStyle.BG_CARD)
        param2.pack(side=tk.LEFT, padx=param2_padx)
        tk.Label(param2, text="重试次数",
                font=(ModernStyle.FONT_FAMILY, param_label_font),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_MUTED).pack(anchor="w", pady=param1_label_pady)
        self.retry_entry = self.create_input(param2, self.retry_count, width=10)
        self.retry_entry.pack()
        
        # 心跳间隔
        param3_padx = (0, DPIHelper.scale(25, self.dpi_scale))
        param3 = tk.Frame(params_row, bg=ModernStyle.BG_CARD)
        param3.pack(side=tk.LEFT, padx=param3_padx)
        tk.Label(param3, text="心跳间隔 (秒)",
                font=(ModernStyle.FONT_FAMILY, param_label_font),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_MUTED).pack(anchor="w", pady=param1_label_pady)
        self.heartbeat_entry = self.create_input(param3, self.heartbeat, width=10)
        self.heartbeat_entry.pack()
        
        # 提示
        param_hint_font = DPIHelper.get_scaled_font_size(9)
        param_hint_pady = (DPIHelper.scale(20, self.dpi_scale), 0)
        tk.Label(params_row, text="💡 传输数天建议将心跳改为300秒",
                font=(ModernStyle.FONT_FAMILY, param_hint_font),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_MUTED).pack(side=tk.LEFT, pady=param_hint_pady)
        
        # === 控制按钮区域 ===
        button_container = tk.Frame(main_container, bg=ModernStyle.BG_PRIMARY)
        button_pady = (0, DPIHelper.scale(15, self.dpi_scale))
        button_container.pack(pady=button_pady)
        
        scaled_padx = DPIHelper.scale(6, self.dpi_scale)
        
        self.start_btn = RoundedButton(button_container, text="▶ 开始", command=self.start_transfer,
                                      width=110, height=38,
                                      bg=ModernStyle.ACCENT_SUCCESS,
                                      hover_bg="#7ecf5e",
                                      dpi_scale=self.dpi_scale)
        self.start_btn.pack(side=tk.LEFT, padx=scaled_padx)
        
        self.pause_btn = RoundedButton(button_container, text="⏸ 暂停", command=self.pause_transfer,
                                      width=110, height=38,
                                      bg=ModernStyle.ACCENT_WARNING,
                                      hover_bg="#d49f56", disabled=True,
                                      dpi_scale=self.dpi_scale)
        self.pause_btn.pack(side=tk.LEFT, padx=scaled_padx)
        
        self.continue_btn = RoundedButton(button_container, text="▶ 继续", command=self.continue_transfer,
                                         width=110, height=38,
                                         bg=ModernStyle.ACCENT_PRIMARY,
                                         hover_bg=ModernStyle.BG_HOVER,
                                         dpi_scale=self.dpi_scale)
        self.continue_btn.pack(side=tk.LEFT, padx=scaled_padx)
        
        self.check_btn = RoundedButton(button_container, text="🔍 检查", command=self.check_progress,
                                      width=110, height=38,
                                      bg=ModernStyle.ACCENT_INFO,
                                      hover_bg=ModernStyle.BG_HOVER,
                                      dpi_scale=self.dpi_scale)
        self.check_btn.pack(side=tk.LEFT, padx=scaled_padx)
        
        self.load_btn = RoundedButton(button_container, text="📂 加载", command=self.load_progress,
                                     width=110, height=38,
                                     bg=ModernStyle.BORDER_COLOR,
                                     hover_bg=ModernStyle.BG_HOVER,
                                     fg=ModernStyle.TEXT_PRIMARY,
                                     dpi_scale=self.dpi_scale)
        self.load_btn.pack(side=tk.LEFT, padx=scaled_padx)
        
        # === 进度条区域 ===
        progress_card = self.create_card(main_container, pady=12)
        progress_pady = (0, DPIHelper.scale(15, self.dpi_scale))
        progress_card.pack(fill=tk.X, pady=progress_pady)
        
        # 进度条
        self.progress_bar = ModernProgressbar(progress_card, width=780, height=24,
                                             dpi_scale=self.dpi_scale)
        progress_bar_pady = (0, DPIHelper.scale(10, self.dpi_scale))
        self.progress_bar.pack(fill=tk.X, pady=progress_bar_pady)
        
        # 状态行
        status_row = tk.Frame(progress_card, bg=ModernStyle.BG_CARD)
        status_row.pack(fill=tk.X)
        
        status_font_size = DPIHelper.get_scaled_font_size(11)
        tk.Label(status_row, textvariable=self.status_text,
                font=(ModernStyle.FONT_FAMILY, status_font_size, "bold"),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.ACCENT_PRIMARY,
                anchor="w").pack(side=tk.LEFT)
        
        activity_font_size = DPIHelper.get_scaled_font_size(9)
        tk.Label(status_row, textvariable=self.last_activity_text,
                font=(ModernStyle.FONT_FAMILY, activity_font_size),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_MUTED,
                anchor="e").pack(side=tk.RIGHT)
        
        # === 日志区域 ===
        log_card = self.create_card(main_container)
        log_card.pack(fill=tk.BOTH, expand=True)
        
        log_header = tk.Frame(log_card, bg=ModernStyle.BG_CARD)
        log_header_pady = (0, DPIHelper.scale(10, self.dpi_scale))
        log_header.pack(fill=tk.X, pady=log_header_pady)
        
        log_title_font = DPIHelper.get_scaled_font_size(11)
        tk.Label(log_header, text="📋 传输日志",
                font=(ModernStyle.FONT_FAMILY, log_title_font, "bold"),
                bg=ModernStyle.BG_CARD,
                fg=ModernStyle.TEXT_PRIMARY,
                anchor="w").pack(side=tk.LEFT)
        
        # 日志文本框
        log_container = tk.Frame(log_card, bg=ModernStyle.BG_SECONDARY)
        log_container.pack(fill=tk.BOTH, expand=True)
        
        log_font_size = DPIHelper.get_scaled_font_size(9)
        log_padx = DPIHelper.scale(10, self.dpi_scale)
        log_pady = DPIHelper.scale(10, self.dpi_scale)
        
        self.log_text = tk.Text(log_container, height=10,
                               font=(ModernStyle.FONT_MONO, log_font_size),
                               bg=ModernStyle.BG_SECONDARY,
                               fg=ModernStyle.TEXT_SECONDARY,
                               insertbackground=ModernStyle.TEXT_PRIMARY,
                               relief=tk.FLAT,
                               bd=0,
                               wrap=tk.WORD,
                               padx=log_padx,
                               pady=log_pady)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        log_scroll = ttk.Scrollbar(log_container, orient=tk.VERTICAL, 
                                  command=self.log_text.yview)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scroll.set)
        
        # 初始化
        self.log("✨ 系统就绪，等待传输任务")
    
    def log(self, message: str):
        """添加日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()
    
    def browse_source(self):
        """选择源文件"""
        file_path = filedialog.askopenfilename(
            title="选择源文件",
            filetypes=[("所有文件", "*.*")]
        )
        if file_path:
            self.source_path.set(file_path)
            self.log(f"📄 已选择源文件: {os.path.basename(file_path)}")
    
    def browse_dest(self):
        """选择目标路径"""
        # 先问是文件还是目录
        if self.source_path.get() and os.path.isfile(self.source_path.get()):
            # 如果源是文件，让用户选择保存文件名
            file_path = filedialog.asksaveasfilename(
                title="选择目标文件",
                initialfile=os.path.basename(self.source_path.get())
            )
            if file_path:
                self.dest_path.set(file_path)
                self.log(f"🎯 已选择目标文件: {file_path}")
        else:
            directory = filedialog.askdirectory(title="选择目标目录")
            if directory:
                self.dest_path.set(directory)
                self.log(f"📂 已选择目标目录: {directory}")
    
    def check_progress(self):
        """检查当前传输进度（GUI方法）"""
        self._do_check_progress()
    
    def _do_check_progress(self):
        """执行进度检查"""
        src_file = self.source_path.get()
        dst_file = self.dest_path.get()
        
        if not src_file or not dst_file:
            messagebox.showwarning("警告", "请先设置源文件和目标路径!")
            return
        
        if not os.path.exists(src_file):
            messagebox.showerror("错误", "源文件不存在!")
            return
        
        # 创建传输实例并检查
        if not self.transfer:
            self.transfer = FileTransfer(None, None, self.log)
            # 使用源文件目录下的进度文件
            progress_file = os.path.join(os.path.dirname(src_file), FileTransferConfig.PROGRESS_FILE)
            self.transfer.set_progress_file(progress_file)
        
        result = self.transfer.check_progress(src_file, dst_file)
        
        if 'error' in result:
            self.log(f"❌ 检查失败: {result['error']}")
            return
        
        # 更新界面
        percent = result['percent']
        self.progress_bar.set_value(percent)
        self.status_text.set(f"已完成: {percent:.1f}% ({result['completed_chunks']}/{result['total_chunks']} 块)")
        
        if result['last_activity']:
            self.last_activity_text.set(f"最后活跃: {result['last_activity'].strftime('%Y-%m-%d %H:%M:%S')}")
        
        self.log(f"🔍 [检查] {result['file']}: {percent:.1f}% 已完成")
        
        if result['completed']:
            self.log(f"  ✓ 文件传输已完成，MD5: {result['md5']}")
    
    def load_progress(self):
        """加载进度文件"""
        progress_file = filedialog.askopenfilename(
            title="选择进度文件",
            filetypes=[("进度文件", "*.json"), ("所有文件", "*.*")],
            initialdir=os.path.dirname(self.source_path.get()) if self.source_path.get() else "."
        )
        
        if progress_file:
            if not self.transfer:
                self.transfer = FileTransfer(None, None, self.log)
            
            self.transfer.set_progress_file(progress_file)
            self.log(f"📂 已加载进度文件: {os.path.basename(progress_file)}")
            
            # 自动检查
            if self.source_path.get() and self.dest_path.get():
                self._do_check_progress()
    
    def update_progress(self, filepath: str, completed: int, total: int, file_size: int):
        """更新进度"""
        percent = (completed / total * 100) if total > 0 else 0
        
        self.progress_bar.set_value(percent)
        self.status_text.set(f"传输中: {percent:.1f}% ({completed}/{total} 块)")
        
        # 更新最后活跃时间
        if self.transfer and self.transfer._last_activity:
            self.last_activity_text.set(f"最后活跃: {self.transfer._last_activity.strftime('%H:%M:%S')}")
        
        self.root.update_idletasks()
    
    def _transfer_worker(self):
        """传输工作线程"""
        src_file = self.source_path.get()
        dst_file = self.dest_path.get()
        
        if not src_file or not dst_file:
            return
        
        # 设置配置（使用实例变量避免影响全局配置）
        try:
            chunk_size = int(self.chunk_size.get()) * 1024 * 1024
            max_retry = int(self.retry_count.get())
            heartbeat_interval = int(self.heartbeat.get())
        except ValueError:
            self.log("❌ 参数错误，请输入有效数字")
            self.transfer_finished()
            return
        
        # 初始化传输
        if not self.transfer:
            self.transfer = FileTransfer(self.update_progress, self.log, self.log)
        
        # 设置传输参数
        self.transfer.config.CHUNK_SIZE = chunk_size
        self.transfer.config.MAX_RETRY = max_retry
        self.transfer.config.HEARTBEAT_INTERVAL = heartbeat_interval
        
        # 验证配置
        try:
            self.transfer.config.validate()
        except ValueError as e:
            self.log(f"❌ 配置错误: {str(e)}")
            messagebox.showerror("配置错误", str(e))
            self.transfer_finished()
            return
        
        # 设置进度文件（源文件目录下）
        progress_file = os.path.join(os.path.dirname(src_file), FileTransferConfig.PROGRESS_FILE)
        self.transfer.set_progress_file(progress_file)
        
        # 目标如果是目录，自动添加文件名
        if os.path.isdir(dst_file):
            dst_file = os.path.join(dst_file, os.path.basename(src_file))
            self.dest_path.set(dst_file)
        
        try:
            result = self.transfer.transfer_file(src_file, dst_file)
            
            if result.get('success', False):
                self.status_text.set("✓ 传输完成")
                self.progress_bar.set_value(100)
                messagebox.showinfo("完成", "文件传输完成！")
            elif result.get('stopped', False):
                self.status_text.set("已停止")
            else:
                self.status_text.set("✗ 传输失败")
                messagebox.showerror("失败", "文件传输失败，请查看日志")
                
        except Exception as e:
            self.log(f"❌ 错误: {str(e)}")
            messagebox.showerror("错误", str(e))
        
        self.transfer_finished()
    
    def start_transfer(self):
        """开始传输"""
        if not self.source_path.get():
            messagebox.showwarning("警告", "请先选择源文件!")
            return
        
        if not self.dest_path.get():
            messagebox.showwarning("警告", "请先选择目标路径!")
            return
        
        if not os.path.exists(self.source_path.get()):
            messagebox.showerror("错误", "源文件不存在!")
            return
        
        # 更新按钮状态
        self.start_btn.set_state(True)
        self.pause_btn.set_state(False)
        self.continue_btn.set_state(True)
        
        self.transfer_thread = threading.Thread(target=self._transfer_worker, daemon=True)
        self.transfer_thread.start()
    
    def pause_transfer(self):
        """暂停传输"""
        if self.transfer:
            self.transfer.pause()
            self.pause_btn.set_state(True)
            self.continue_btn.set_state(False)
            self.status_text.set("⏸ 已暂停")
    
    def continue_transfer(self):
        """继续传输"""
        if self.transfer:
            self.transfer.resume()
            self.pause_btn.set_state(False)
            self.continue_btn.set_state(True)
            self.status_text.set("▶ 继续传输...")
    
    def transfer_finished(self):
        """传输完成后的界面更新"""
        self.start_btn.set_state(False)
        self.pause_btn.set_state(True)
        self.continue_btn.set_state(False)


def main():
    root = tk.Tk()
    app = FileTransferGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
