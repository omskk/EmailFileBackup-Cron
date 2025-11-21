# -*- coding: utf-8 -*-
"""
配置管理模块

本模块负责从环境变量加载和验证应用程序的所有配置项，支持多 WebDAV 服务器、
IMAP 邮箱、API 安全密钥等配置。

主要功能:
    - 从环境变量加载配置
    - 支持多 WebDAV 服务器配置（JSON 格式）
    - 提供配置验证功能
    - 设置合理的默认值
    - 统一的配置访问接口

配置来源:
    - .env 文件（通过 python-dotenv 加载）
    - 环境变量（优先级高于 .env）
    - 代码内置默认值（最低优先级）

配置分类:
    1. WebDAV 服务器配置（webdav_servers）
        - 默认服务器：通过 WEBDAV_URL 等变量配置
        - 额外服务器：通过 WEBDAV_SERVERS JSON 数组配置
    
    2. IMAP 邮箱配置（imap）
        - 服务器地址、用户名、密码
    
    3. 邮件检索配置（email）
        - 搜索关键词
    
    4. 上传配置（upload）
        - 重试次数和延迟
    
    5. API 安全配置（api）
        - 外部和内部 API 密钥
    
    6. Web 界面配置（web）
        - 登录用户名和密码
    
    7. 数据库配置（database）
        - MySQL 连接字符串

环境变量列表:
    核心配置:
        DATABASE_URL: MySQL 连接字符串
        WEBDAV_URL: 默认 WebDAV 服务器 URL
        WEBDAV_LOGIN: 默认 WebDAV 登录名
        WEBDAV_PASSWORD: 默认 WebDAV 密码
        IMAP_HOSTNAME: IMAP 服务器地址
        IMAP_USERNAME: IMAP 登录用户名
        IMAP_PASSWORD: IMAP 登录密码
        EMAIL_SEARCH_SUBJECT: 邮件搜索关键词
        API_SECRET_KEY: 外部 API 密钥
        INTERNAL_API_KEY: 内部 API 密钥
        WEB_AUTH_USER: Web 界面用户名
        WEB_AUTH_PASSWORD: Web 界面密码
    
    可选配置:
        MAX_ATTACHMENT_SIZE_MB: 附件大小限制（MB，默认 50）
        MAX_EMAILS_PER_RUN: 单次处理邮件数（默认 10）
        UPLOAD_RETRY_COUNT: 上传重试次数（默认 3）
        UPLOAD_RETRY_DELAY: 重试延迟秒数（默认 5）
        DOWNLOAD_TIMEOUT: 下载超时秒数（默认 60）
        CHUNK_SIZE: 下载分块大小（默认 8192）
        WEBDAV_SERVERS: 额外服务器列表（JSON 数组）

使用示例:
    加载配置::
    
        from config import load_config, validate_config
        
        # 加载配置
        config = load_config()
        
        # 验证配置
        if not validate_config(config):
            print("配置验证失败")
            exit(1)
        
        # 访问配置
        servers = config['webdav_servers']
        imap_host = config['imap']['hostname']
    
    多服务器配置::
    
        # 在 .env 文件中
        WEBDAV_SERVERS='[
            {"name": "Backup", "url": "...", "login": "...", "password": "..."},
            {"name": "Archive", "url": "...", "login": "...", "password": "..."}
        ]'

注意事项:
    - 密码和密钥不应硬编码，必须通过环境变量配置
    - API_SECRET_KEY 和 INTERNAL_API_KEY 建议使用 32+ 字符
    - DATABASE_URL 格式: mysql://user:password@host:port/database
    - WEBDAV_SERVERS 必须是有效的 JSON 数组格式
    - 配置加载失败会记录错误日志但不抛出异常

Author: MailBridge Team
Version: 1.0.0
"""
import os
import json
from typing import Dict, Any, List
from dotenv import load_dotenv

# ==================== 初始化 ====================

# Load environment variables from .env file
# 从 .env 文件加载环境变量（如果存在）
load_dotenv()

# 使用统一的日志模块
from logger import get_logger, LogEmoji

logger = get_logger(__name__)


# ==================== 常量配置 ====================

# 附件大小限制（字节）
# 从环境变量读取（MB），转换为字节，默认 50MB
MAX_ATTACHMENT_SIZE = int(os.getenv("MAX_ATTACHMENT_SIZE_MB", 50)) * 1024 * 1024

# 单次运行最多处理的邮件数量
# 避免单次运行时间过长，默认 10 封
MAX_EMAILS_PER_RUN = int(os.getenv("MAX_EMAILS_PER_RUN", 10))

# 数据库连接字符串（必需配置）
DATABASE_URL = os.getenv("DATABASE_URL")


# ==================== 配置加载函数 ====================

def load_config() -> Dict[str, Any]:
    """
    从环境变量加载应用配置
    
    读取所有必需和可选的环境变量，构建配置字典。支持多 WebDAV 服务器配置，
    通过 JSON 格式的环境变量 WEBDAV_SERVERS 添加额外服务器。
    
    配置结构:
        {
            'webdav_servers': [
                {
                    'name': 服务器名称,
                    'url': WebDAV URL,
                    'login': 登录名,
                    'password': 密码,
                    'timeout': 超时时间（秒）,
                    'chunk_size': 分块大小（字节）
                },
                ...
            ],
            'imap': {
                'hostname': IMAP 服务器,
                'username': 用户名,
                'password': 密码
            },
            'email': {
                'search_subject': 搜索关键词
            },
            'upload': {
                'retry_count': 重试次数,
                'retry_delay': 重试延迟（秒）
            },
            'api': {
                'secret_key': 外部 API 密钥,
                'internal_key': 内部 API 密钥
            },
            'web': {
                'user': 登录用户名,
                'password': 登录密码
            },
            'database': {
                'url': 数据库连接字符串
            }
        }
    
    Returns:
        Dict[str, Any]: 配置字典，包含所有应用配置项
    
    Examples:
        基本用法::
        
            config = load_config()
            
            # 访问 WebDAV 服务器配置
            for server in config['webdav_servers']:
                print(f"Server: {server['name']}")
            
            # 访问 IMAP 配置
            imap_host = config['imap']['hostname']
        
        多服务器配置::
        
            # 设置环境变量
            os.environ['WEBDAV_SERVERS'] = '[
                {"name": "Backup", "url": "...", "login": "user", "password": "pass"}
            ]'
            
            config = load_config()
            # config['webdav_servers'] 包含默认服务器和额外服务器
    
    Note:
        - 默认服务器只有在 WEBDAV_URL 配置时才会添加
        - WEBDAV_SERVERS 解析失败会记录错误但不影响默认服务器
        - 所有服务器都会设置默认的 timeout(60) 和 chunk_size(8192)
        - 未配置的可选项会使用默认值
    
    See Also:
        validate_config(): 验证配置完整性
    """
    # 从 WEBDAV_SERVERS 环境变量加载所有服务器（JSON 列表格式）
    # 示例: [{"name": "Main", "url": "...", "login": "...", "password": "..."}]
    webdav_servers = []
    servers_json = os.getenv("WEBDAV_SERVERS")
    
    if servers_json:
        try:
            servers = json.loads(servers_json)
            if isinstance(servers, list):
                for s in servers:
                    # 为每个服务器设置默认值（如果未指定）
                    s.setdefault("timeout", 60)
                    s.setdefault("chunk_size", 8192)
                    webdav_servers.append(s)
            else:
                logger.error(
                    f"{LogEmoji.ERROR} WEBDAV_SERVERS must be a JSON array. "
                    f"Example: '[{{\"name\": \"Main\", \"url\": \"...\", \"login\": \"...\", \"password\": \"...\"}}]'"
                )
        except json.JSONDecodeError as e:
            # JSON 解析失败，记录错误但不中断
            logger.error(
                f"{LogEmoji.ERROR} Failed to parse WEBDAV_SERVERS: {e}. "
                f"It must be a valid JSON array."
            )

    # 3. 构建完整配置字典
    config = {
        # WebDAV 服务器列表（可包含多个服务器）
        "webdav_servers": webdav_servers,
        
        # IMAP 邮箱配置
        "imap": {
            "hostname": os.getenv("IMAP_HOSTNAME", ""),
            "username": os.getenv("IMAP_USERNAME", ""),
            "password": os.getenv("IMAP_PASSWORD", ""),
        },
        
        # 邮件搜索配置
        "email": {
            "search_subject": os.getenv("EMAIL_SEARCH_SUBJECT", ""),
        },
        
        # 上传重试配置
        "upload": {
            "retry_count": int(os.getenv("UPLOAD_RETRY_COUNT", 3)),   # 默认重试 3 次
            "retry_delay": int(os.getenv("UPLOAD_RETRY_DELAY", 5)),   # 默认延迟 5 秒
        },
        
        # API 安全配置
        "api": {
            "secret_key": os.getenv("API_SECRET_KEY", ""),      # 外部 API 密钥
            "internal_key": os.getenv("INTERNAL_API_KEY", ""),  # 内部 API 密钥
        },
        
        # Web 界面认证配置
        "web": {
            "user": os.getenv("WEB_AUTH_USER", "admin"),  # 默认用户名 admin
            "password": os.getenv("WEB_AUTH_PASSWORD", ""),
        },
        
        # 数据库配置
        "database": {
            "url": DATABASE_URL  # MySQL 连接字符串
        }
    }
    
    return config


def validate_config(config: Dict[str, Any]) -> bool:
    """
    验证配置的完整性和正确性
    
    检查所有必需的配置项是否存在且有效，包括 WebDAV 服务器配置、
    IMAP 配置、API 密钥等。验证失败会记录详细的错误信息。
    
    验证规则:
        1. 至少配置一个 WebDAV 服务器
        2. 每个服务器必须有 url、login、password
        3. IMAP 配置必须完整（hostname、username、password）
        4. 邮件搜索关键词必须配置
        5. API 密钥必须配置
        6. Web 界面密码必须配置
        7. 数据库 URL 必须配置
    
    Args:
        config (Dict[str, Any]): 由 load_config() 返回的配置字典
    
    Returns:
        bool: 配置有效返回 True，否则返回 False
            - True: 所有必需配置项都存在且有效
            - False: 存在缺失或无效的配置项
    
    Examples:
        基本验证::
        
            config = load_config()
            if validate_config(config):
                print("配置有效，可以启动应用")
            else:
                print("配置无效，请检查环境变量")
                exit(1)
        
        在应用启动时使用::
        
            # app.py
            config = load_config()
            try:
                validate_config(config)
            except Exception as e:
                logger.error("配置验证失败")
                sys.exit(1)
    
    Note:
        - 验证失败会记录详细的缺失项列表
        - 不会抛出异常，只返回 bool 值
        - WebDAV 服务器按索引编号（从 1 开始）
        - 建议在应用启动时立即调用
    
    See Also:
        load_config(): 加载配置
    """
    # 1. 验证 WebDAV 服务器配置
    if not config['webdav_servers']:
        logger.error(f"{LogEmoji.ERROR} No WebDAV servers configured.")
        return False
        
    # 验证每个服务器的必需字段
    for i, server in enumerate(config['webdav_servers']):
        if not all([server.get('url'), server.get('login'), server.get('password')]):
            logger.error(
                f"{LogEmoji.ERROR} WebDAV server #{i+1} ({server.get('name', 'Unknown')}) "
                f"is missing url, login, or password."
            )
            return False

    # 2. 验证所有必需的配置项
    required_keys = [
        config['imap']['hostname'],
        config['imap']['username'],
        config['imap']['password'],
        config['email']['search_subject'],
        config['api']['secret_key'],
        config['api']['internal_key'],
        config['web']['password'],
        config['database']['url']
    ]
    
    if not all(required_keys):
        # 识别具体缺失的配置项，便于用户定位问题
        missing = []
        if not config['imap']['hostname']: missing.append("IMAP_HOSTNAME")
        if not config['imap']['username']: missing.append("IMAP_USERNAME")
        if not config['imap']['password']: missing.append("IMAP_PASSWORD")
        if not config['email']['search_subject']: missing.append("EMAIL_SEARCH_SUBJECT")
        if not config['api']['secret_key']: missing.append("API_SECRET_KEY")
        if not config['api']['internal_key']: missing.append("INTERNAL_API_KEY")
        if not config['web']['password']: missing.append("WEB_AUTH_PASSWORD")
        if not config['database']['url']: missing.append("DATABASE_URL")
        
        logger.error(
            f"{LogEmoji.ERROR} Configuration incomplete. Missing: {', '.join(missing)}"
        )
        return False
    
    # 所有验证通过
    return True
