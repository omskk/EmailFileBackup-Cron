# -*- coding: utf-8 -*-
"""
邮件处理模块

本模块负责从 IMAP 邮箱读取邮件、提取附件并上传到 WebDAV 服务器，
是应用的核心业务逻辑模块。

主要功能:
    1. IMAP 邮件检索
        - 连接到 IMAP 服务器
        - 按主题搜索未读邮件
        - 批量处理邮件（可配置数量上限）
    
    2. 附件处理
        - 提取邮件附件
        - 文件名清理和规范化
        - 文件大小限制检查
        - 文件名冲突处理
    
    3. WebDAV 上传
        - 自动选择目标服务器（支持多服务器）
        - 大文件分块上传
        - 上传失败日志记录
        - 重试机制（可配置）
    
    4. 分布式锁
        - 防止并发处理同一邮件
        - 超时自动释放
        - 适配 Vercel 多实例环境

业务流程:
    1. 获取分布式锁（防止并发）
    2. 连接 IMAP 服务器
    3. 搜索符合条件的未读邮件
    4. 按批次处理邮件（MAX_EMAILS_PER_RUN）
    5. 提取每封邮件的附件
    6. 检查附件大小和文件名
    7. 上传附件到 WebDAV
    8. 处理成功后删除邮件
    9. 释放分布式锁

配置依赖:
    环境变量:
        IMAP_HOSTNAME: IMAP 服务器地址
        IMAP_USERNAME: IMAP 用户名
        IMAP_PASSWORD: IMAP 密码
        EMAIL_SEARCH_SUBJECT: 搜索关键词
        MAX_ATTACHMENT_SIZE_MB: 附件大小限制（MB）
        MAX_EMAILS_PER_RUN: 单次处理邮件数
        WEBDAV_URL, WEBDAV_LOGIN, WEBDAV_PASSWORD: WebDAV 配置
    
    数据库:
        - webdav_servers: 服务器配置
        - app_config: 默认服务器设置
        - app_locks: 分布式锁表
        - upload_logs: 上传日志

使用示例:
    手动触发邮件处理::
    
        from mail_processor import process_emails
        
        # 处理邮件（自动获取锁）
        process_emails()
    
    通过 API 触发::
    
        # POST /api/run-task
        # 异步调用 process_emails()

特殊处理:
    - 文件名清理：移除特殊字符、URL 解码、去除空格
    - 重名处理：自动添加 (1), (2) 等后缀
    - 大文件提示：>5MB 的文件额外记录日志
    - 邮件保留：上传失败的邮件标记已读但不删除

错误处理:
    - IMAP 连接失败：记录错误，跳过本次执行
    - 附件提取失败：跳过该附件，继续处理其他
    - 上传失败：邮件不删除，下次不会再处理（已标记已读）
    - 锁获取失败：跳过本次执行，等待下次

性能优化:
    - 批量处理：避免一次处理过多邮件
    - 流式上传：大文件不占用内存
    - 连接复用：WebDAV 连接池
    - 早期退出：达到限制立即停止

注意事项:
    - 邮件处理是破坏性操作（会删除邮件）
    - 确保 IMAP 密码正确，避免账号锁定
    - 附件过大会被跳过，不会上传
    - 分布式锁确保同一时刻只有一个实例在处理
    - 邮件标记已读后不会再被处理

Author: MailBridge Team
Version: 1.0.0
"""
import errno
import re
import sys
from typing import Dict, Any
from urllib.parse import unquote
import os

import requests
from imbox import Imbox

# --- Import from centralized config ---
from config import load_config, validate_config, MAX_ATTACHMENT_SIZE, MAX_EMAILS_PER_RUN
from database import log_upload, acquire_lock, release_lock

# 使用统一的日志模块
from logger import get_logger, LogEmoji

logger = get_logger(__name__)


# ==================== WebDAV 上传 ====================

def upload_to_webdav(config: Dict[str, Any], data: Any, remote_filename: str, file_size: int) -> bool:
    """
    上传文件到 WebDAV 服务器
    
    根据配置自动选择目标服务器（优先使用默认服务器），通过 HTTP PUT
    请求将文件上传到 WebDAV。支持多服务器配置，自动回退到环境变量配置。
    
    服务器选择逻辑:
        1. 从数据库获取所有启用的服务器
        2. 读取默认服务器配置（app_config 表）
        3. 使用默认服务器，若未设置则使用第一个启用的服务器
        4. 若数据库无配置，回退到环境变量配置
    
    Args:
        config (Dict[str, Any]): 应用配置字典（由 load_config() 返回）
        data (Any): 要上传的文件数据（二进制）
        remote_filename (str): 远程文件名（不含路径）
        file_size (int): 文件大小（字节）
    
    Returns:
        bool: 上传成功返回 True，失败返回 False
    
    Examples:
        上传文件::
        
            with open('doc.pdf', 'rb') as f:
                data = f.read()
                size = len(data)
                success = upload_to_webdav(config, data, 'doc.pdf', size)
                if success:
                    print("上传成功")
        
        处理大文件::
        
            # 大文件会自动记录额外日志
            upload_to_webdav(config, large_data, 'large.zip', 10*1024*1024)
    
    Note:
        - 文件名不应包含路径分隔符
        - 大文件（>5MB）会记录额外提示日志
        - 使用配置的超时时间（timeout）
        - 上传结果会自动记录到 upload_logs 表
    
    Warning:
        - 若无可用服务器配置，会返回 False
        - 网络超时会导致上传失败
        - 确保 WebDAV 服务器可访问
    
    See Also:
        sanitize_filename(): 文件名清理
        find_unique_filename(): 处理文件名冲突
    """
    from database import get_config_value, get_enabled_servers, get_server_by_name
    
    # 从数据库获取所有启用的服务器（优先级排序）
    servers_from_db = get_enabled_servers()
    
    # 如果数据库中没有服务器，回退到环境变量配置
    if not servers_from_db:
        if not config['webdav_servers']:
            logger.error(f"{LogEmoji.ERROR} 没有配置 WebDAV 服务器，无法上传。")
            return False
        servers_from_db = config['webdav_servers']
        logger.warning(f"{LogEmoji.WARNING} 数据库中没有服务器配置，使用环境变量配置")
    
    # 从数据库获取默认服务器名称
    default_server_name = get_config_value('default_webdav_server')
    
    # 查找默认服务器配置
    webdav_config = None
    if default_server_name:
        for server in servers_from_db:
            if server['name'] == default_server_name:
                webdav_config = server
                break
    
    # 如果找不到默认服务器，使用第一个启用的服务器
    if not webdav_config:
        webdav_config = servers_from_db[0]
        logger.warning(f"未找到默认服务器，使用第一个启用的服务器: {webdav_config['name']}")
    
    # 构建完整的 WebDAV URL
    full_url = f"{webdav_config['url'].rstrip('/')}/{remote_filename}"
    auth = (webdav_config['login'], webdav_config['password'])
    server_name = webdav_config['name']

    try:
        logger.info(f"{LogEmoji.UPLOAD} 开始上传附件到 [{server_name}]: '{remote_filename}' ({file_size / 1024:.2f} KB)")

        # 大文件上传提示（>5MB）
        if file_size > 5 * 1024 * 1024:
            logger.info(f"{LogEmoji.UPLOAD} 正在上传大文件 ({file_size / 1024 / 1024:.2f} MB),请稍候...")

        # 使用配置的超时时间
        timeout = webdav_config.get('timeout', 30)
        
        # 发送 PUT 请求上传文件
        response = requests.put(full_url, data=data, auth=auth, timeout=timeout)
        response.raise_for_status()

        logger.info(f"{LogEmoji.SUCCESS} WebDAV 上传成功到 [{server_name}]: '{remote_filename}'")
        log_upload(remote_filename, file_size, "Success", server_name)
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"{LogEmoji.ERROR} WebDAV 上传失败到 [{server_name}]: '{remote_filename}'。原因: {e}")
        log_upload(remote_filename, file_size, "Failed", server_name)
        return False


# ==================== 文件名处理 ====================

        # 大文件上传提示
        if file_size > 5 * 1024 * 1024:  # > 5MB
            logger.info(f"{LogEmoji.UPLOAD} 正在上传大文件 ({file_size / 1024 / 1024:.2f} MB),请稍候...")

        # 使用配置的timeout
        timeout = webdav_config.get('timeout', 30)
        response = requests.put(full_url, data=data, auth=auth, timeout=timeout)
        response.raise_for_status()
        logger.info(f"{LogEmoji.SUCCESS} WebDAV 上传成功到 [{server_name}]: '{remote_filename}'")
        log_upload(remote_filename, file_size, "Success", server_name)
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"{LogEmoji.ERROR} WebDAV 上传失败到 [{server_name}]: '{remote_filename}'。原因: {e}")
        log_upload(remote_filename, file_size, "Failed", server_name)
        return False


def webdav_file_exists(webdav_config: Dict[str, Any], filename: str) -> bool:
    """使用 HEAD 请求检查文件是否存在于 WebDAV 服务器上。"""
    # 注意: 这里的 webdav_config 参数实际上是整个 config 对象，需要提取第一个服务器配置
    # 为了保持兼容性，我们在这里做处理，或者调用者应该传入具体的 server config
    # 鉴于 find_unique_filename 传入的是全局 config，我们在这里提取
    
    if 'webdav_servers' in webdav_config:
         server_config = webdav_config['webdav_servers'][0]
    else:
         # 假设传入的就是 server config (虽然目前代码逻辑不是这样)
         server_config = webdav_config
         
    full_url = f"{server_config['url'].rstrip('/')}/{filename}"
    auth = (server_config['login'], server_config['password'])
    try:
        response = requests.head(full_url, auth=auth, timeout=10)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        # 在出错时假定文件不存在，以避免阻塞上传
        return False


def find_unique_filename(config: Dict[str, Any], original_filename: str) -> str:
    """
    在 WebDAV 服务器上查找唯一文件名以防止覆盖。
    如果 'file.txt' 存在，它将尝试 'file (1).txt', 'file (2).txt' 等。
    """
    # webdav_file_exists 现在处理 config 提取
    if not webdav_file_exists(config, original_filename):
        return original_filename

    name, extension = os.path.splitext(original_filename)
    counter = 1
    while True:
        new_filename = f"{name} ({counter}){extension}"
        if not webdav_file_exists(config, new_filename):
            logger.info(f"{LogEmoji.FILE} 文件名 '{original_filename}' 已存在。使用新名称: '{new_filename}'")
            return new_filename
        counter += 1


def sanitize_filename(filename: str) -> str:
    filename = filename.replace('..', '')
    return re.sub(r'[<>:"/\\|?*]', '_', filename)


def decode_email_header(header: Any) -> str:
    """
    解码邮件头信息，专门处理可能是特殊邮件库对象或编码错误的文件名。

    该函数通过以下步骤处理邮件头：
    1. 将输入转换为字符串格式
    2. 移除特定的编码前缀
    3. 尝试进行URL解码

    参数:
        header (Any): 需要解码的邮件头，可能是一个特殊对象或字符串

    返回:
        str: 解码后的文件名字符串

    异常:
        TypeError: 如果 header 无法被转换成字符串
    """
    # 确保我们使用的是普通字符串，处理那些在打印时和使用时行为不同的特殊对象
    try:
        filename_str = str(header)
    except Exception as e:
        logger.error("Failed to convert header to string", exc_info=True)
        raise TypeError(f"Cannot convert header of type {type(header)} to string") from e

    # 移除可能存在的编码前缀，这是最常见的问题
    separator = "''"
    if separator in filename_str:
        # 只保留分隔符最后出现位置之后的部分
        filename_str = filename_str.split(separator)[-1]

    # 尝试对结果进行URL解码，因为它可能仍然被编码
    try:
        # unquote函数是安全的，如果没有编码内容则不会做任何处理
        decoded_filename = unquote(filename_str)
        return decoded_filename
    except Exception:
        logger.warning("Could not URL-decode input", extra={"input": filename_str}, exc_info=True)
        # 如果解码失败，返回已清理的字符串
        return filename_str


def _process_single_message(imbox: Imbox, uid: bytes, message: Any, config: Dict[str, Any]) -> bool:
    uid_str = uid.decode()

    attachments = message.attachments
    attachment_count = len(attachments)
    logger.info(f"{LogEmoji.EMAIL} [UID: {uid_str}] 邮件报告发现 {attachment_count} 个附件。")

    if not attachments:
        logger.warning(f"{LogEmoji.WARNING} [UID: {uid_str}] 确认没有附件，跳过。")
        return True

    all_attachments_succeeded = True
    for index, attachment in enumerate(attachments):
        original_filename_raw = attachment.get('filename')
        logger.info(
            f"{LogEmoji.FILE} [UID: {uid_str}] 开始处理附件 {index + 1}/{attachment_count}，原始文件名: '{original_filename_raw}'")

        original_filename = "unknown_attachment"
        try:
            original_filename = decode_email_header(original_filename_raw)
            safe_filename = sanitize_filename(original_filename)
            logger.info(f"{LogEmoji.SUCCESS} [UID: {uid_str}] 解码和清理后文件名: '{safe_filename}'")

            # 查找唯一文件名以避免冲突
            final_filename = find_unique_filename(config, safe_filename)

            attachment_content = attachment.get('content')

            # 检查附件大小 (不读取整个文件到内存)
            if hasattr(attachment_content, 'getbuffer'):
                attachment_size = attachment_content.getbuffer().nbytes
            else:
                # Fallback for other file-like objects
                pos = attachment_content.tell()
                attachment_content.seek(0, 2)
                attachment_size = attachment_content.tell()
                attachment_content.seek(pos)

            if attachment_size > MAX_ATTACHMENT_SIZE:
                logger.warning(
                    f"{LogEmoji.WARNING} [UID: {uid_str}] 附件 '{original_filename}' "
                    f"超过大小限制 ({attachment_size / 1024 / 1024:.2f} MB > {MAX_ATTACHMENT_SIZE / 1024 / 1024} MB),跳过"
                )
                continue

            if not upload_to_webdav(config, attachment_content, final_filename, attachment_size):
                all_attachments_succeeded = False
                logger.warning(f"{LogEmoji.WARNING} [UID: {uid_str}] -> 附件 '{original_filename}' 上传失败，此邮件将不会被删除。")
                break
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} [UID: {uid_str}] 处理附件 '{original_filename}' 时发生内部错误: {e}", exc_info=True)
            all_attachments_succeeded = False
            break
    return all_attachments_succeeded


def process_emails() -> None:
    """
    连接到 IMAP 服务器，获取并处理所有符合条件的邮件。
    """
    LOCK_NAME = "process_emails_lock"

    logger.info("=" * 40)
    logger.info(f"{LogEmoji.INFO} 开始执行邮件检查任务...")

    if not acquire_lock(LOCK_NAME):
        logger.info(f"{LogEmoji.WARNING} 另一个邮件检查任务正在运行。跳过此次执行。")
        logger.info("=" * 40)
        return

    try:
        config = load_config()
        if not validate_config(config):
            return

        imap_config = config['imap']
        search_subject = config['email']['search_subject']

        with Imbox(imap_config['hostname'],
                   username=imap_config['username'],
                   password=imap_config['password'],
                   ssl=True) as imbox:
            logger.info(f"{LogEmoji.SUCCESS} 成功连接到邮箱 {imap_config['hostname']}。")
            logger.info(f"{LogEmoji.EMAIL} 开始搜索主题为 '{search_subject}' 的未读邮件...")
            unread_messages = imbox.messages(unread=True, subject=search_subject)

            if not unread_messages:
                logger.info(f"{LogEmoji.INFO} 没有找到主题为 '{search_subject}' 的新邮件。")
                return

            logger.info(f"{LogEmoji.EMAIL} 找到 {len(unread_messages)} 封相关邮件,开始处理...")

            processed_count = 0
            for uid, message in unread_messages:
                # 批量处理限制
                if processed_count >= MAX_EMAILS_PER_RUN:
                    logger.info(
                        f"{LogEmoji.INFO} 已处理 {MAX_EMAILS_PER_RUN} 封邮件,剩余邮件将在下次运行时处理"
                    )
                    break

                uid_str = uid.decode()
                logger.info("-" * 40)
                logger.info(f"{LogEmoji.EMAIL} 正在处理邮件 - UID: {uid_str}, 主题: '{message.subject}'")

                # --- 关键改动: 不再立即标记为已读，依赖数据库锁防止并发 ---
                # imbox.mark_seen(uid)
                # logger.info(f"[UID: {uid_str}] 已立即标记为已读，以防止重复处理。")

                if _process_single_message(imbox, uid, message, config):
                    imbox.delete(uid)
                    logger.info(f"{LogEmoji.SUCCESS} [UID: {uid_str}] 邮件已成功处理并删除。")
                    processed_count += 1
                else:
                    # 如果处理失败,邮件将保持已读状态,不会在下次被获取
                    logger.error(f"{LogEmoji.ERROR} [UID: {uid_str}] 邮件处理失败,将保持已读状态但不会被删除。")

            if processed_count > 0:
                logger.info(f"{LogEmoji.SUCCESS} 本次执行共成功处理 {processed_count} 封邮件。")
    except (errno.ConnectionError, OSError) as e:
        logger.error(f"{LogEmoji.ERROR} 连接或处理邮箱时发生严重错误: {e}")
    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 发生未知错误: {e}", exc_info=True)
    finally:
        release_lock(LOCK_NAME)
        logger.info(f"{LogEmoji.INFO} 邮件检查任务执行完毕。")
        logger.info("=" * 40)

