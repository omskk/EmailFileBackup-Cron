import os
import logging
import mysql.connector
from mysql.connector import errorcode
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 从环境变量获取数据库连接 URL
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    """
    根据 DATABASE_URL 创建并返回一个数据库连接。
    """
    if not DATABASE_URL:
        logger.error("❌ 数据库连接失败: DATABASE_URL 环境变量未设置。")
        return None
    try:
        url = urlparse(DATABASE_URL)
        conn = mysql.connector.connect(
            host=url.hostname,
            port=url.port or 3306,
            user=url.username,
            password=url.password,
            database=url.path[1:]  # 去掉路径开头的 '/'
        )
        return conn
    except mysql.connector.Error as err:
        logger.error(f"❌ 数据库连接失败: {err}")
        return None

def init_db():
    """
    初始化数据库，如果 'upload_logs' 表不存在，则创建它。
    """
    conn = get_db_connection()
    if not conn:
        return

    try:
        cursor = conn.cursor()
        table_name = "upload_logs"
        create_table_query = f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            filename VARCHAR(255) NOT NULL,
            size_bytes INT NOT NULL,
            status VARCHAR(50) NOT NULL
        ) ENGINE=InnoDB;
        """
        cursor.execute(create_table_query)
        conn.commit()
        logger.info(f"✅ 数据库表 '{table_name}' 初始化成功。")
    except mysql.connector.Error as err:
        logger.error(f"❌ 创建数据库表失败: {err}")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

def log_upload(filename: str, size_bytes: int, status: str):
    """
    向数据库中插入一条附件上传记录。
    """
    conn = get_db_connection()
    if not conn:
        return

    try:
        cursor = conn.cursor()
        insert_query = """
        INSERT INTO upload_logs (filename, size_bytes, status)
        VALUES (%s, %s, %s)
        """
        cursor.execute(insert_query, (filename, size_bytes, status))
        conn.commit()
        logger.info(f"记录到数据库: {filename} ({size_bytes} bytes) - {status}")
    except mysql.connector.Error as err:
        logger.error(f"❌ 写入数据库失败: {err}")
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

# 在模块加载时自动初始化数据库
if __name__ != '__main__':
    init_db()
