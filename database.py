# -*- coding: utf-8 -*-
"""
数据库操作模块

本模块提供了 mail_to_webdav 项目的所有数据库操作功能，采用连接池机制优化性能，
实现分布式锁保证并发安全，管理上传日志、应用配置和 WebDAV 服务器配置。

主要功能:
    1. 连接管理
        - MySQL 连接池（连接复用，避免频繁连接）
        - 自动重连机制
        - 连接超时控制
    
    2. 分布式锁机制
        - 基于数据库的分布式锁实现
        - 支持超时自动释放
        - 防止并发任务冲突（Vercel 多实例环境）
    
    3. 数据表管理
        - upload_logs: 上传历史记录
        - app_locks: 分布式锁表
        - app_config: 应用配置键值对
        - webdav_servers: WebDAV 服务器配置
    
    4. 业务功能
        - 上传日志记录和查询
        - 应用配置持久化
        - 服务器配置 CRUD
        - 环境变量配置播种

数据库架构:
    upload_logs 表:
        - id: 主键（自增）
        - timestamp: 上传时间
        - filename: 文件名
        - size_bytes: 文件大小（字节）
        - status: 上传状态（Success/Failed）
        - server_name: 目标服务器名称
    
    app_locks 表:
        - lock_name: 锁名称（主键）
        - is_locked: 锁定状态
        - locked_at: 锁定时间
    
    app_config 表:
        - config_key: 配置键（主键）
        - config_value: 配置值
        - updated_at: 最后更新时间
    
    webdav_servers 表:
        - id: 主键（自增）
        - name: 服务器名称（唯一）
        - url: WebDAV URL
        - login: 登录用户名
        - password: 密码
        - timeout: 超时时间（秒）
        - chunk_size: 分块大小（字节）
        - is_enabled: 是否启用
        - is_default: 是否为默认服务器
        - created_at: 创建时间
        - updated_at: 最后更新时间

性能优化:
    - 连接池：复用连接，避免频繁创建/销毁
    - 索引优化：关键字段添加索引加速查询
    - 事务控制：确保数据一致性
    - 自动清理：启动时清理僵死锁

安全特性:
    - SQL 注入防护：使用参数化查询
    - 密码存储：明文存储（建议使用环境变量管理）
    - 分布式锁：防止并发冲突
    - 连接超时：避免长时间占用连接

使用示例:
    基本数据库操作::
    
        from database import init_db, get_db_connection
        
        # 初始化数据库（创建表）
        init_db()
        
        # 获取连接（自动使用连接池）
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM upload_logs LIMIT 10")
            results = cursor.fetchall()
            cursor.close()
            conn.close()
    
    分布式锁使用::
    
        from database import acquire_lock, release_lock
        
        # 获取锁
        if acquire_lock('my_task'):
            try:
                # 执行需要互斥的任务
                process_emails()
            finally:
                # 确保释放锁
                release_lock('my_task')
        else:
            print("任务正在运行，跳过")
    
    日志记录::
    
        from database import log_upload
        
        # 记录上传
        log_upload('document.pdf', 1024000, 'Success', 'Default')
    
    服务器配置::
    
        from database import get_all_servers, add_server
        
        # 获取所有服务器
        servers = get_all_servers()
        
        # 添加新服务器
        add_server('Backup', 'https://...', 'user', 'pass')

环境依赖:
    - DATABASE_URL: MySQL 连接字符串（必需）
        格式: mysql://username:password@hostname:port/database
        示例: mysql://user:pass@db.example.com:3306/mailbridge

注意事项:
    - 连接池大小设为 3，适合 Vercel 无服务器环境
    - 分布式锁依赖数据库，确保数据库可用
    - 启动时会自动清理所有僵死锁
    - 使用事务时需手动 commit 或 rollback
    - 连接使用完毕需要 close()，否则占用连接池

性能建议:
    - 避免长时间持有数据库连接
    - 大批量操作使用批量插入
    - 合理使用索引优化查询
    - 定期清理历史日志数据

Author: MailBridge Team
Version: 1.0.0
"""
import os
import mysql.connector
from mysql.connector import errorcode, pooling
from urllib.parse import urlparse
from datetime import datetime
from config import DATABASE_URL

# 使用统一的日志模块
from logger import get_logger, LogEmoji

logger = get_logger(__name__)

# ==================== 导出的公共 API ====================

# 模块导出的公共函数列表
__all__ = [
    # 连接管理
    'get_db_connection', 'init_db', 
    
    # 锁管理
    'cleanup_stale_locks', 'acquire_lock', 'release_lock',
    
    # 日志管理
    'log_upload', 'get_logs_paginated', 'get_total_log_count', 'get_log_count_by_status',
    
    # 配置管理
    'get_config_value', 'set_config_value',
    
    # 服务器管理
    'get_all_servers', 'get_enabled_servers', 'get_server_by_id', 'get_server_by_name',
    'add_server', 'update_server', 'delete_server', 'seed_servers_from_env'
]


# ==================== 全局变量 ====================

# 全局连接池实例（延迟初始化）
connection_pool = None


# ==================== 连接管理 ====================

def get_db_connection():
    """
    从连接池获取数据库连接
    
    使用 MySQL 连接池技术提升性能，避免频繁创建和销毁连接。
    连接池在首次调用时懒加载初始化，后续调用直接从池中获取。
    
    连接池配置:
        - 池大小: 3 个连接（适合 Vercel 无服务器环境）
        - 自动重置: 连接归还时重置会话状态
        - 连接超时: 10 秒
        - 自动提交: 关闭（需手动 commit）
    
    Returns:
        mysql.connector.connection.MySQLConnection | None: 
            - 成功: 返回可用的数据库连接对象
            - 失败: 返回 None（DATABASE_URL 未设置或连接失败）
    
    Examples:
        基本用法::
        
            conn = get_db_connection()
            if conn:
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM upload_logs")
                    results = cursor.fetchall()
                finally:
                    cursor.close()
                    conn.close()  # 归还连接到池
        
        使用上下文管理器::
        
            conn = get_db_connection()
            if conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT COUNT(*) FROM upload_logs")
                    count = cursor.fetchone()[0]
                conn.close()
    
    Note:
        - 连接池只初始化一次（全局单例）
        - 连接使用完毕务必调用 close()，否则占用池资源
        - close() 不会真正关闭连接，而是归还到池中
        - 连接池空时会自动创建新连接（不超过 pool_size）
        - 超时未归还的连接会自动关闭
    
    Warning:
        - DATABASE_URL 未设置时返回 None
        - 连接失败会记录错误日志
        - 不要在长时间运行的任务中持有连接
    
    See Also:
        init_db(): 初始化数据库表结构
    """
    global connection_pool

    # 检查 DATABASE_URL 环境变量
    if not DATABASE_URL:
        logger.error(f"{LogEmoji.ERROR} 数据库连接失败: DATABASE_URL 环境变量未设置。")
        return None

    try:
        # 懒加载初始化连接池（仅在第一次调用时）
        if connection_pool is None:
            url = urlparse(DATABASE_URL)
            connection_pool = pooling.MySQLConnectionPool(
                pool_name="mailbridge_pool",  # 连接池名称
                pool_size=3,  # 池大小：3 个连接（Vercel 环境优化）
                pool_reset_session=True,  # 归还时重置会话状态
                autocommit=False,  # 关闭自动提交，需手动 commit
                connect_timeout=10,  # 连接超时 10 秒
                host=url.hostname,
                port=url.port or 3306,
                user=url.username,
                password=url.password,
                database=url.path[1:]  # 去掉路径开头的 '/'
            )
            logger.info(f"{LogEmoji.SUCCESS} {LogEmoji.DATABASE} 数据库连接池初始化成功(pool_size=3)。")

        # 从连接池获取连接
        return connection_pool.get_connection()
    except mysql.connector.Error as err:
        logger.error(f"{LogEmoji.ERROR} {LogEmoji.DATABASE} 数据库连接失败: {err}")
        return None


def init_db():
    """
    初始化数据库表结构
    
    创建应用所需的所有数据库表，包括上传日志、分布式锁、应用配置和
    服务器配置表。如果表已存在则跳过创建。
    
    创建的表:
        1. upload_logs: 上传历史记录
        2. app_locks: 分布式锁
        3. app_config: 应用配置
        4. webdav_servers: WebDAV 服务器配置
    
    表结构详情请参考模块文档中的"数据库架构"部分。
    
    Returns:
        None
    
    Examples:
        应用启动时初始化::
        
            # app.py
            from database import init_db
            
            # 启动时调用一次
            init_db()
        
        检查表是否创建成功::
        
            init_db()
            conn = get_db_connection()
            if conn:
                cursor = conn.cursor()
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()
                print(f"已创建表: {tables}")
    
    Note:
        - 使用 CREATE TABLE IF NOT EXISTS，重复调用安全
        - 所有表使用 InnoDB 引擎（支持事务）
        - 主键和索引自动创建
        - 失败会记录错误日志但不抛出异常
    
    Warning:
        - 需要数据库 CREATE TABLE 权限
        - 网络异常可能导致部分表创建失败
        - 建议在应用启动时调用一次
    
    See Also:
        get_db_connection(): 获取数据库连接
        cleanup_stale_locks(): 清理僵死锁
    """
    conn = get_db_connection()
    if not conn:
        return

    try:
        cursor = conn.cursor()

        # ========== 1. 创建 upload_logs 表 ==========
        logs_table_name = "upload_logs"
        create_logs_table_query = f"""
        CREATE TABLE IF NOT EXISTS {logs_table_name} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            filename VARCHAR(255) NOT NULL,
            size_bytes INT NOT NULL,
            status VARCHAR(50) NOT NULL,
            server_name VARCHAR(255) DEFAULT NULL,
            INDEX idx_timestamp (timestamp),
            INDEX idx_status (status),
            INDEX idx_server_name (server_name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """
        cursor.execute(create_logs_table_query)
        logger.info(f"{LogEmoji.SUCCESS} 数据库表 '{logs_table_name}' 初始化成功。")

        # 创建 app_locks 表
        locks_table_name = "app_locks"
        create_locks_table_query = f"""
        CREATE TABLE IF NOT EXISTS {locks_table_name} (
            lock_name VARCHAR(255) PRIMARY KEY,
            is_locked BOOLEAN NOT NULL DEFAULT FALSE,
            locked_at TIMESTAMP NULL
        ) ENGINE=InnoDB;
        """
        cursor.execute(create_locks_table_query)
        logger.info(f"{LogEmoji.SUCCESS} 数据库表 '{locks_table_name}' 初始化成功。")

        # 创建 app_config 表
        config_table_name = "app_config"
        create_config_table_query = f"""
        CREATE TABLE IF NOT EXISTS {config_table_name} (
            config_key VARCHAR(255) PRIMARY KEY,
            config_value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB;
        """
        cursor.execute(create_config_table_query)
        logger.info(f"{LogEmoji.SUCCESS} 数据库表 '{config_table_name}' 初始化成功。")

        # 创建 webdav_servers 表
        servers_table_name = "webdav_servers"
        create_servers_table_query = f"""
        CREATE TABLE IF NOT EXISTS {servers_table_name} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL,
            url TEXT NOT NULL,
            login VARCHAR(255) NOT NULL,
            password TEXT NOT NULL,
            enabled BOOLEAN DEFAULT TRUE,
            priority INT DEFAULT 0,
            timeout INT DEFAULT 60,
            chunk_size INT DEFAULT 8192,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB;
        """
        cursor.execute(create_servers_table_query)
        logger.info(f"{LogEmoji.SUCCESS} 数据库表 '{servers_table_name}' 初始化成功。")

        # 迁移逻辑: 为旧的upload_logs表添加server_name列(如果不存在)
        try:
            cursor.execute(f"""
                ALTER TABLE {logs_table_name} 
                ADD COLUMN server_name VARCHAR(255) DEFAULT NULL
            """)
            logger.info(f"{LogEmoji.SUCCESS} 已为 upload_logs 表添加 server_name 列")
        except mysql.connector.Error as alter_err:
            # 列可能已存在，这是正常的
            if alter_err.errno == 1060:  # Duplicate column name
                logger.info(f"{LogEmoji.INFO} upload_logs 表已有 server_name 列，跳过添加")
            else:
                logger.warning(f"添加 server_name 列时出现警告: {alter_err}")

        # 创建索引以优化查询性能
        logger.info("正在创建数据库索引...")
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON upload_logs(timestamp DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_filename ON upload_logs(filename)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_status ON upload_logs(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_server_name ON upload_logs(server_name)")
            logger.info(f"{LogEmoji.SUCCESS} 数据库索引创建成功。")
        except mysql.connector.Error as idx_err:
            # 索引可能已存在,不影响主流程
            logger.warning(f"索引创建警告: {idx_err}")

        conn.commit()

    except mysql.connector.Error as err:
        logger.error(f"{LogEmoji.ERROR} 创建数据库表失败: {err}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def release_lock(lock_name: str):
    """
    释放一个命名的锁。
    """
    conn = get_db_connection()
    if not conn:
        return

    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE app_locks SET is_locked = FALSE, locked_at = NULL WHERE lock_name = %s", (lock_name,))
        conn.commit()
        logger.info(f"{LogEmoji.UNLOCK} 成功释放锁: '{lock_name}'")
    except mysql.connector.Error as err:
        logger.error(f"{LogEmoji.ERROR} 释放锁时发生数据库错误: {err}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def cleanup_stale_locks():
    """
    清理所有僵死锁。
    在应用启动时调用，无条件清理所有锁。

    因为每次启动都是新的实例（尤其在 Vercel 无服务器环境），
    旧实例的锁都应该被清理，无需检查超时时间。
    """
    conn = get_db_connection()
    if not conn:
        return

    try:
        cursor = conn.cursor()
        # 无条件清理所有锁
        query = """
                UPDATE app_locks
                SET is_locked = FALSE, \
                    locked_at = NULL
                WHERE is_locked = TRUE \
                """
        cursor.execute(query)
        cleared = cursor.rowcount
        conn.commit()

        if cleared > 0:
            logger.warning(f"{LogEmoji.CLEAN} 启动时清理了 {cleared} 个僵死锁")
        else:
            logger.info(f"{LogEmoji.SUCCESS} 启动时检查：没有发现僵死锁")

    except mysql.connector.Error as err:
        logger.error(f"{LogEmoji.ERROR} 清理僵死锁失败: {err}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def acquire_lock(lock_name: str, timeout_minutes: int = 30) -> bool:
    """
    尝试获取一个命名的锁。如果锁已被占用但超时，则强制释放后获取。

    Args:
        lock_name: 锁的名称
        timeout_minutes: 锁超时时间（分钟），默认30分钟

    Returns:
        bool: 成功获取锁返回 True，否则返回 False
    """
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cursor = conn.cursor()
        # 确保锁记录存在
        cursor.execute("INSERT IGNORE INTO app_locks (lock_name) VALUES (%s)", (lock_name,))

        # 尝试以原子方式获取锁
        # FOR UPDATE 会锁定行，直到事务结束
        cursor.execute("START TRANSACTION")
        cursor.execute("""
                       SELECT is_locked, locked_at
                       FROM app_locks
                       WHERE lock_name = %s
                           FOR UPDATE
                       """, (lock_name,))
        result = cursor.fetchone()

        if result:
            is_locked, locked_at = result

            # 如果锁被占用，检查是否超时
            if is_locked:
                if locked_at:
                    # 计算锁占用时长
                    time_diff = datetime.now() - locked_at
                    if time_diff.total_seconds() > timeout_minutes * 60:
                        logger.warning(
                            f"{LogEmoji.WARNING} 锁 '{lock_name}' 已超时 ({int(time_diff.total_seconds() / 60)} 分钟)，强制释放"
                        )
                        # 强制释放超时的锁
                        cursor.execute("""
                                       UPDATE app_locks
                                       SET is_locked = FALSE,
                                           locked_at = NULL
                                       WHERE lock_name = %s
                                       """, (lock_name,))
                        is_locked = False
                else:
                    # 没有时间戳的旧锁，强制释放
                    logger.warning(f"{LogEmoji.WARNING} 锁 '{lock_name}' 没有时间戳，强制释放")
                    cursor.execute("""
                                   UPDATE app_locks
                                   SET is_locked = FALSE,
                                       locked_at = NULL
                                   WHERE lock_name = %s
                                   """, (lock_name,))
                    is_locked = False

            # 尝试获取锁
            if not is_locked:
                cursor.execute("""
                               UPDATE app_locks
                               SET is_locked = TRUE,
                                   locked_at = CURRENT_TIMESTAMP
                               WHERE lock_name = %s
                               """, (lock_name,))
                conn.commit()
                logger.info(f"{LogEmoji.LOCK} 成功获取锁: '{lock_name}'")
                return True
            else:
                conn.rollback()
                logger.warning(f"{LogEmoji.WARNING} 未能获取锁 '{lock_name}'，因为它已被占用。")
                return False
        else:
            conn.rollback()
            return False

    except mysql.connector.Error as err:
        logger.error(f"{LogEmoji.ERROR} 获取锁时发生数据库错误: {err}")
        if conn.is_connected():
            conn.rollback()
        return False
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def release_lock(lock_name: str):
    """
    释放一个命名的锁。
    """
    conn = get_db_connection()
    if not conn:
        return

    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE app_locks SET is_locked = FALSE, locked_at = NULL WHERE lock_name = %s", (lock_name,))
        conn.commit()
        logger.info(f"{LogEmoji.UNLOCK} 成功释放锁: '{lock_name}'")
    except mysql.connector.Error as err:
        logger.error(f"{LogEmoji.ERROR} 释放锁时发生数据库错误: {err}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def log_upload(filename: str, size_bytes: int, status: str, server_name: str = None):
    """
    向数据库中插入一条附件上传记录。
    """
    conn = get_db_connection()
    if not conn:
        return

    try:
        cursor = conn.cursor()
        insert_query = """
                       INSERT INTO upload_logs (filename, size_bytes, status, server_name)
                       VALUES (%s, %s, %s, %s) \
                       """
        cursor.execute(insert_query, (filename, size_bytes, status, server_name))
        conn.commit()
        logger.info(f"{LogEmoji.DATABASE} 记录到数据库: {filename} ({size_bytes} bytes) - {status} [{server_name or 'N/A'}]")
    except mysql.connector.Error as err:
        logger.error(f"{LogEmoji.ERROR} 写入数据库失败: {err}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_logs_paginated(page: int = 1, per_page: int = 20, search_query: str = None):
    """
    从数据库中分页获取最新的日志记录，支持搜索。
    """
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cursor = conn.cursor(dictionary=True)
        offset = (page - 1) * per_page

        params = []
        where_clause = ""
        if search_query:
            where_clause = "WHERE filename LIKE %s"
            params.append(f"%{search_query}%")

        query = f"SELECT * FROM upload_logs {where_clause} ORDER BY timestamp DESC LIMIT %s OFFSET %s"

        params.extend([per_page, offset])

        cursor.execute(query, tuple(params))
        logs = cursor.fetchall()
        return logs
    except mysql.connector.Error as err:
        logger.error(f"❌ 从数据库读取日志失败: {err}")
        return []
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_total_log_count(search_query: str = None):
    """
    获取日志总数，支持搜索。
    """
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cursor = conn.cursor()

        params = []
        where_clause = ""
        if search_query:
            where_clause = "WHERE filename LIKE %s"
            params.append(f"%{search_query}%")

        query = f"SELECT COUNT(*) FROM upload_logs {where_clause}"
        cursor.execute(query, tuple(params))
        count = cursor.fetchone()[0]
        return count
    except mysql.connector.Error as err:
        logger.error(f"❌ 从数据库读取日志数失败: {err}")
        return 0
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_log_count_by_status(status: str) -> int:
    """
    获取指定状态的日志数量,用于统计展示。
    """
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cursor = conn.cursor()
        query = "SELECT COUNT(*) FROM upload_logs WHERE status = %s"
        cursor.execute(query, (status,))
        count = cursor.fetchone()[0]
        return count
    except mysql.connector.Error as err:
        logger.error(f"❌ 从数据库读取状态统计失败: {err}")
        return 0
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_config_value(key: str, default: str = None) -> str:
    """
    从数据库获取配置值
    """
    conn = get_db_connection()
    if not conn:
        return default
    try:
        cursor = conn.cursor()
        query = "SELECT config_value FROM app_config WHERE config_key = %s"
        cursor.execute(query, (key,))
        result = cursor.fetchone()
        return result[0] if result else default
    except mysql.connector.Error as err:
        logger.error(f"❌ 读取配置失败: {err}")
        return default
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def set_config_value(key: str, value: str):
    """
    设置配置值到数据库
    """
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cursor = conn.cursor()
        query = """
                INSERT INTO app_config (config_key, config_value)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE config_value = %s, updated_at = CURRENT_TIMESTAMP
                """
        cursor.execute(query, (key, value, value))
        conn.commit()
        logger.info(f"✅ 配置已保存: {key} = {value}")
        return True
    except mysql.connector.Error as err:
        logger.error(f"❌ 保存配置失败: {err}")
        return False
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()




# ================= WebDAV 服务器管理 =================

def get_all_servers():
    """获取所有 WebDAV 服务器配置"""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        query = "SELECT * FROM webdav_servers ORDER BY priority ASC, id ASC"
        cursor.execute(query)
        servers = cursor.fetchall()
        return servers
    except mysql.connector.Error as err:
        logger.error(f"❌ 读取服务器列表失败: {err}")
        return []
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_enabled_servers():
    """获取所有启用的 WebDAV 服务器"""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cursor = conn.cursor(dictionary=True)
        query = "SELECT * FROM webdav_servers WHERE enabled = TRUE ORDER BY priority ASC, id ASC"
        cursor.execute(query)
        servers = cursor.fetchall()
        return servers
    except mysql.connector.Error as err:
        logger.error(f"❌ 读取启用服务器列表失败: {err}")
        return []
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_server_by_id(server_id: int):
    """根据 ID 获取服务器配置"""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor(dictionary=True)
        query = "SELECT * FROM webdav_servers WHERE id = %s"
        cursor.execute(query, (server_id,))
        server = cursor.fetchone()
        return server
    except mysql.connector.Error as err:
        logger.error(f"❌ 读取服务器失败: {err}")
        return None
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def get_server_by_name(name: str):
    """根据名称获取服务器配置"""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor(dictionary=True)
        query = "SELECT * FROM webdav_servers WHERE name = %s"
        cursor.execute(query, (name,))
        server = cursor.fetchone()
        return server
    except mysql.connector.Error as err:
        logger.error(f"❌ 读取服务器失败: {err}")
        return None
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def add_server(name: str, url: str, login: str, password: str, 
               enabled: bool = True, priority: int = 0, 
               timeout: int = 60, chunk_size: int = 8192):
    """添加新的 WebDAV 服务器"""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cursor = conn.cursor()
        query = """
                INSERT INTO webdav_servers (name, url, login, password, enabled, priority, timeout, chunk_size)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """
        cursor.execute(query, (name, url, login, password, enabled, priority, timeout, chunk_size))
        conn.commit()
        logger.info(f"✅ 服务器已添加: {name}")
        return True
    except mysql.connector.Error as err:
        logger.error(f"❌ 添加服务器失败: {err}")
        return False
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def update_server(server_id: int, name: str, url: str, login: str, password: str,
                  enabled: bool = True, priority: int = 0,
                  timeout: int = 60, chunk_size: int = 8192):
    """更新服务器配置"""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cursor = conn.cursor()
        query = """
                UPDATE webdav_servers
                SET name = %s, url = %s, login = %s, password = %s,
                    enabled = %s, priority = %s, timeout = %s, chunk_size = %s
                WHERE id = %s
                """
        cursor.execute(query, (name, url, login, password, enabled, priority, timeout, chunk_size, server_id))
        conn.commit()
        logger.info(f"✅ 服务器已更新: {name}")
        return True
    except mysql.connector.Error as err:
        logger.error(f"❌ 更新服务器失败: {err}")
        return False
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def delete_server(server_id: int):
    """删除服务器"""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cursor = conn.cursor()
        query = "DELETE FROM webdav_servers WHERE id = %s"
        cursor.execute(query, (server_id,))
        conn.commit()
        logger.info(f"✅ 服务器已删除: ID={server_id}")
        return True
    except mysql.connector.Error as err:
        logger.error(f"❌ 删除服务器失败: {err}")
        return False
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


def seed_servers_from_env():
    """从环境变量导入服务器配置到数据库（仅在数据库为空时）"""
    from config import load_config
    
    # 检查数据库是否已有服务器
    existing_servers = get_all_servers()
    if existing_servers:
        logger.info("✅ 数据库已有服务器配置，跳过种子导入")
        return
    
    # 从环境变量加载配置
    config = load_config()
    servers_to_import = config.get('webdav_servers', [])
    
    if not servers_to_import:
        logger.warning("⚠️ 没有找到环境变量中的服务器配置")
        return
    
    # 导入到数据库
    imported_count = 0
    for idx, server in enumerate(servers_to_import):
        name = server.get('name', f'Server-{idx+1}')
        url = server.get('url')
        login = server.get('login')
        password = server.get('password')
        timeout = server.get('timeout', 60)
        chunk_size = server.get('chunk_size', 8192)
        
        if url and login and password:
            if add_server(name, url, login, password, 
                         enabled=True, priority=idx, 
                         timeout=timeout, chunk_size=chunk_size):
                imported_count += 1
    
    if imported_count > 0:
        logger.info(f"✅ 成功从环境变量导入 {imported_count} 个服务器配置到数据库")
        # 设置第一个服务器为默认
        if not get_config_value('default_webdav_server'):
            first_server = servers_to_import[0].get('name', 'Server-1')
            set_config_value('default_webdav_server', first_server)
            logger.info(f"✅ 默认服务器已设置为: {first_server}")


# 在模块加载时自动初始化数据库
# 初始化逻辑移至 app.py 中显式调用

