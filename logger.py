# -*- coding: utf-8 -*-
"""
统一日志管理模块

本模块为 mail_to_webdav 项目提供统一的日志配置和管理功能，确保整个应用
的日志输出格式一致、级别可控、易于调试和监控。

主要功能:
    - 环境变量驱动的日志级别配置
    - 多种预定义日志格式（console/detailed/simple）
    - 统一的 Emoji 使用规范（LogEmoji 类）
    - 自动配置根 logger
    - 简洁的 API 接口

设计理念:
    - 单一职责：只负责日志配置，不处理业务逻辑
    - 即插即用：import 即自动配置
    - 环境感知：自动适配开发/生产环境
    - 一致性：统一的 emoji 和格式规范

环境变量:
    LOG_LEVEL: 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL）
        - 默认: INFO
        - 开发环境建议: DEBUG
        - 生产环境建议: INFO 或 WARNING
    
    LOG_FORMAT: 日志格式（console/detailed/simple）
        - 默认: console
        - console: 易读格式，适合本地调试
        - detailed: 包含文件名和行号，适合生产环境排查
        - simple: 最简格式，适合日志聚合系统

使用示例:
    基本用法::
    
        from logger import get_logger, LogEmoji
        
        # 获取 logger 实例
        logger = get_logger(__name__)
        
        # 使用标准化的 emoji
        logger.info(f"{LogEmoji.SUCCESS} 操作成功")
        logger.error(f"{LogEmoji.ERROR} 操作失败", exc_info=True)
        logger.warning(f"{LogEmoji.WARNING} 注意事项")
    
    自定义配置::
    
        # 通过环境变量配置
        import os
        os.environ['LOG_LEVEL'] = 'DEBUG'
        os.environ['LOG_FORMAT'] = 'detailed'
        
        # 或在 .env 文件中设置
        # LOG_LEVEL=DEBUG
        # LOG_FORMAT=detailed

性能考虑:
    - Logger 实例会被缓存，避免重复创建
    - 字符串格式化使用 f-string，性能最优
    - 根 logger 只在模块导入时配置一次

注意事项:
    - 不要在日志中输出敏感信息（密码、密钥等）
    - DEBUG 级别会产生大量日志，影响性能
    - 生产环境建议使用 INFO 或以上级别
    - 所有 emoji 应使用 LogEmoji 类常量，不要硬编码

Author: MailBridge Team
Version: 1.0.0
License: MIT
"""
import os
import sys
import logging
from typing import Optional

# ==================== 配置常量 ====================

# 日志级别映射表：将字符串映射到 logging 模块的常量
LOG_LEVELS = {
    'DEBUG': logging.DEBUG,      # 调试信息（10）
    'INFO': logging.INFO,        # 常规信息（20）
    'WARNING': logging.WARNING,  # 警告信息（30）
    'ERROR': logging.ERROR,      # 错误信息（40）
    'CRITICAL': logging.CRITICAL # 严重错误（50）
}

# 预定义日志格式
LOG_FORMATS = {
    # console: 标准格式，包含时间、模块名、级别和消息
    'console': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    
    # detailed: 详细格式，额外包含文件名和行号，便于定位问题
    'detailed': '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    
    # simple: 简洁格式，只包含级别和消息，适合日志聚合系统
    'simple': '%(levelname)s - %(message)s'
}


# ==================== 配置函数 ====================

def get_log_level() -> int:
    """
    从环境变量获取日志级别
    
    读取 LOG_LEVEL 环境变量并转换为 logging 模块的级别常量。
    如果环境变量未设置或值无效，返回默认级别 INFO。
    
    Returns:
        int: logging 模块的日志级别常量 (10-50)
            - DEBUG: 10
            - INFO: 20（默认）
            - WARNING: 30
            - ERROR: 40
            - CRITICAL: 50
    
    Examples:
        >>> os.environ['LOG_LEVEL'] = 'DEBUG'
        >>> get_log_level()
        10
        
        >>> os.environ['LOG_LEVEL'] = 'invalid'
        >>> get_log_level()  # 返回默认值
        20
    
    Note:
        - 环境变量值不区分大小写
        - 无效值会被忽略，使用默认值 INFO
    """
    level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
    return LOG_LEVELS.get(level_str, logging.INFO)


def get_log_format() -> str:
    """
    从环境变量获取日志格式
    
    读取 LOG_FORMAT 环境变量并返回对应的格式字符串。
    如果环境变量未设置或值无效，返回默认格式 console。
    
    Returns:
        str: 日志格式字符串，符合 logging.Formatter 规范
    
    Examples:
        >>> os.environ['LOG_FORMAT'] = 'detailed'
        >>> get_log_format()
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
    
    Note:
        - 支持的格式: console, detailed, simple
        - 无效值会使用默认值 console
    """
    format_type = os.getenv('LOG_FORMAT', 'console').lower()
    return LOG_FORMATS.get(format_type, LOG_FORMATS['console'])


def setup_logging(
    name: Optional[str] = None,
    level: Optional[int] = None,
    format_str: Optional[str] = None
) -> logging.Logger:
    """
    设置并返回一个配置好的 logger 实例
    
    这是一个底层函数，通常不直接使用。推荐使用更简洁的 get_logger() 函数。
    
    配置内容:
        - 设置日志级别（从参数或环境变量）
        - 创建控制台处理器（输出到 stdout）
        - 设置日志格式
        - 避免重复配置（检查 handlers）
    
    Args:
        name (str, optional): Logger 名称，通常使用模块的 __name__
            默认: None（使用根 logger）
        level (int, optional): 日志级别常量
            默认: None（从环境变量读取）
        format_str (str, optional): 日志格式字符串
            默认: None（从环境变量读取）
    
    Returns:
        logging.Logger: 配置好的 logger 实例
    
    Note:
        - 如果 logger 已有 handlers，会跳过配置（避免重复）
        - Handler 输出到 stdout，不输出到 stderr
        - 同一个 name 多次调用会返回同一个实例（logging 模块特性）
    
    See Also:
        get_logger(): 推荐使用的简化接口
    """
    logger = logging.getLogger(name)
    
    # 如果 logger 已经有 handlers，说明已经配置过，直接返回
    # 这避免了重复配置导致日志重复输出
    if logger.handlers:
        return logger
    
    # 设置日志级别
    log_level = level if level is not None else get_log_level()
    logger.setLevel(log_level)
    
    # 创建控制台处理器（输出到 stdout）
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    
    # 设置日志格式
    log_format = format_str if format_str is not None else get_log_format()
    formatter = logging.Formatter(log_format)
    console_handler.setFormatter(formatter)
    
    # 添加处理器到 logger
    logger.addHandler(console_handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    获取一个配置好的 logger 实例（推荐使用）
    
    这是获取 logger 的推荐方式，简单易用，自动读取环境变量配置。
    
    Args:
        name (str): Logger 名称，推荐使用 __name__
            使用 __name__ 可以在日志中显示模块路径，便于定位问题
    
    Returns:
        logging.Logger: 已配置的 logger 实例，可直接使用
    
    Examples:
        基本使用::
        
            from logger import get_logger, LogEmoji
            
            logger = get_logger(__name__)
            logger.info(f"{LogEmoji.SUCCESS} 应用启动成功")
        
        在不同模块中使用::
        
            # app.py
            logger = get_logger(__name__)  # 日志显示 app
            
            # database.py
            logger = get_logger(__name__)  # 日志显示 database
    
    Note:
        - 同一个 name 多次调用返回同一个实例（单例模式）
        - Logger 会自动从环境变量读取配置
        - 不需要手动配置 handlers 和 formatters
    """
    return setup_logging(name)


# ==================== Emoji 规范类 ====================

class LogEmoji:
    """
    统一的日志 Emoji 规范类
    
    定义了项目中所有日志使用的 emoji 常量，确保整个应用的日志风格一致。
    使用常量而不是硬编码 emoji，便于统一修改和维护。
    
    分类:
        - 状态类: SUCCESS, ERROR, WARNING, INFO 等
        - 功能类: DATABASE, NETWORK, EMAIL 等  
        - 操作类: UPLOAD, DOWNLOAD, DELETE 等
        - 系统类: LOCK, UNLOCK, CLEAN 等
    
    Attributes:
        SUCCESS (str): ✅ 成功操作
        INFO (str): ℹ️ 信息提示
        WARNING (str): ⚠️ 警告
        CAUTION (str): 🟡 注意
        ERROR (str): ❌ 错误
        CRITICAL (str): 🚨 严重错误
        FAILED (str): 🔴 失败
        DEBUG (str): 🔍 调试
        LOCK (str): 🔒 锁定
        UNLOCK (str): 🔓 解锁
        DATABASE (str): 💾 数据库操作
        NETWORK (str): 🌐 网络操作
        EMAIL (str): 📧 邮件操作
        FILE (str): 📁 文件操作
        UPLOAD (str): ⬆️ 上传
        DOWNLOAD (str): ⬇️ 下载
        DELETE (str): 🗑️ 删除
        CLEAN (str): 🧹 清理
    
    Examples:
        使用预定义的 emoji::
        
            from logger import get_logger, LogEmoji
            
            logger = get_logger(__name__)
            
            # 成功操作
            logger.info(f"{LogEmoji.SUCCESS} 数据保存成功")
            
            # 数据库操作
            logger.info(f"{LogEmoji.DATABASE} 连接到数据库")
            
            # 错误处理
            logger.error(f"{LogEmoji.ERROR} 连接失败: {e}")
    
    Note:
        - 使用常量而不是直接写 emoji 字符串
        - 保持日志 emoji 使用的一致性
        - 不同操作类型使用对应的 emoji
        - 可以组合使用多个 emoji
    """
    # 状态和级别
    SUCCESS = "✅"      # 操作成功
    INFO = "ℹ️"         # 一般信息
    WARNING = "⚠️"     # 警告
    CAUTION = "🟡"     # 注意/小心
    ERROR = "❌"        # 错误
    CRITICAL = "🚨"    # 严重错误/紧急
    FAILED = "🔴"      # 失败
    DEBUG = "🔍"       # 调试信息
    
    # 系统操作
    LOCK = "🔒"        # 获取锁
    UNLOCK = "🔓"      # 释放锁
    CLEAN = "🧹"       # 清理操作
    
    # 功能模块
    DATABASE = "💾"    # 数据库操作
    NETWORK = "🌐"     # 网络请求
    EMAIL = "📧"       # 邮件处理
    FILE = "📁"        # 文件操作
    
    # 文件操作
    UPLOAD = "⬆️"       # 上传
    DOWNLOAD = "⬇️"     # 下载
    DELETE = "🗑️"      # 删除


# ==================== 初始化 ====================

def configure_root_logger():
    """
    配置根 logger
    
    在模块导入时自动调用，确保即使其他模块不使用 get_logger()，
    也能有合适的默认日志配置。
    
    配置内容:
        - 从环境变量读取日志级别
        - 创建控制台处理器
        - 设置日志格式
    
    Note:
        - 只配置一次（检查 handlers）
        - 影响所有未显式配置的 logger
        - 在模块导入时自动执行
    
    Warning:
        此函数会在模块导入时自动执行，不需要手动调用。
    """
    root_logger = logging.getLogger()
    
    # 如果已经配置过，跳过
    if root_logger.handlers:
        return
    
    log_level = get_log_level()
    root_logger.setLevel(log_level)
    
    # 创建控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    
    # 设置格式
    formatter = logging.Formatter(get_log_format())
    console_handler.setFormatter(formatter)
    
    root_logger.addHandler(console_handler)


# 模块导入时自动配置根 logger
# 这确保了即使其他模块使用标准的 logging.getLogger()，也能有合适的格式
configure_root_logger()
