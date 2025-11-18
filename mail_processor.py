# -*- coding: utf-8 -*-
import errno
import hashlib
import json
import re
import sys
import time
import logging
import os
from typing import Dict, Any
from email.header import decode_header
from urllib.parse import unquote

import requests
from imbox import Imbox
from dotenv import load_dotenv

# --- 1. 日志配置 ---
# 在应用启动时加载 .env 文件
load_dotenv()

from database import log_upload, acquire_lock, release_lock

# 配置日志记录器 (现在只输出到控制台，文件日志由数据库替代)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def load_config() -> Dict[str, Any]:
    """
    从环境变量加载配置，并为缺失的值提供默认值。
    """
    logger.info("正在从环境变量加载配置...")
    config = {
        "webdav": {
            "url": os.getenv("WEBDAV_URL", ""),
            "login": os.getenv("WEBDAV_LOGIN", ""),
            "password": os.getenv("WEBDAV_PASSWORD", ""),
        },
        "imap": {
            "hostname": os.getenv("IMAP_HOSTNAME", ""),
            "username": os.getenv("IMAP_USERNAME", ""),
            "password": os.getenv("IMAP_PASSWORD", ""),
        },
        "email": {
            "search_subject": os.getenv("EMAIL_SEARCH_SUBJECT", ""),
        },
        "upload": {
            "retry_count": int(os.getenv("UPLOAD_RETRY_COUNT", 3)),
            "retry_delay": int(os.getenv("UPLOAD_RETRY_DELAY", 5)),
        },
        "api": {
            "secret_key": os.getenv("API_SECRET_KEY", "")
        }
    }
    logger.info("✅ 环境变量加载完毕。")
    return config


def validate_config(config: Dict[str, Any]) -> bool:
    """
    验证配置字典中是否包含所有必需的配置项。
    """
    logger.info("正在验证配置信息...")
    required_keys = [
        config['webdav']['url'], config['webdav']['login'], config['webdav']['password'],
        config['imap']['hostname'], config['imap']['username'], config['imap']['password'],
        config['email']['search_subject'], config['api']['secret_key']
    ]
    if not all(required_keys):
        logger.error("❌ 配置信息不完整。请检查 .env 文件或环境变量是否缺少关键配置。")
        return False
    logger.info("✅ 配置信息完整。")
    return True


def upload_to_webdav(config: Dict[str, Any], data: bytes, remote_filename: str) -> bool:
    """
    将数据单次上传到 WebDAV 服务器。
    如果上传失败，记录错误并返回 False。
    """
    webdav_config = config['webdav']
    full_url = f"{webdav_config['url'].rstrip('/')}/{remote_filename}"
    auth = (webdav_config['login'], webdav_config['password'])
    file_size = len(data)

    try:
        logger.info(f"开始上传附件到: {full_url}")
        response = requests.put(full_url, data=data, auth=auth, timeout=30)
        response.raise_for_status()
        logger.info(f"✅ WebDAV 上传成功: '{remote_filename}'")
        log_upload(remote_filename, file_size, "Success")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ WebDAV 上传失败: '{remote_filename}'。原因: {e}")
        log_upload(remote_filename, file_size, "Failed")
        return False


def webdav_file_exists(webdav_config: Dict[str, Any], filename: str) -> bool:
    """使用 HEAD 请求检查文件是否存在于 WebDAV 服务器上。"""
    full_url = f"{webdav_config['url'].rstrip('/')}/{filename}"
    auth = (webdav_config['login'], webdav_config['password'])
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
    webdav_config = config['webdav']
    if not webdav_file_exists(webdav_config, original_filename):
        return original_filename

    name, extension = os.path.splitext(original_filename)
    counter = 1
    while True:
        new_filename = f"{name} ({counter}){extension}"
        if not webdav_file_exists(webdav_config, new_filename):
            logger.info(f"文件名 '{original_filename}' 已存在。使用新名称: '{new_filename}'")
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
    logger.info(f"[UID: {uid_str}] 邮件报告发现 {attachment_count} 个附件。")

    if not attachments:
        logger.warning(f"[UID: {uid_str}] 确认没有附件，跳过。")
        return True

    all_attachments_succeeded = True
    for index, attachment in enumerate(attachments):
        original_filename_raw = attachment.get('filename')
        logger.info(
            f"[UID: {uid_str}] 开始处理附件 {index + 1}/{attachment_count}，原始文件名: '{original_filename_raw}'")

        original_filename = "unknown_attachment"
        try:
            original_filename = decode_email_header(original_filename_raw)
            safe_filename = sanitize_filename(original_filename)
            logger.info(f"[UID: {uid_str}] 解码和清理后文件名: '{safe_filename}'")

            # 查找唯一文件名以避免冲突
            final_filename = find_unique_filename(config, safe_filename)

            attachment_content = attachment.get('content').read()

            if not upload_to_webdav(config, attachment_content, final_filename):
                all_attachments_succeeded = False
                logger.warning(f"[UID: {uid_str}] -> 附件 '{original_filename}' 上传失败，此邮件将不会被删除。")
                break
        except Exception as e:
            logger.error(f"[UID: {uid_str}] 处理附件 '{original_filename}' 时发生内部错误: {e}", exc_info=True)
            all_attachments_succeeded = False
            break
    return all_attachments_succeeded


def process_emails() -> None:
    """
    连接到 IMAP 服务器，获取并处理所有符合条件的邮件。
    """
    LOCK_NAME = "process_emails_lock"

    logger.info("=" * 40)
    logger.info("开始执行邮件检查任务...")

    if not acquire_lock(LOCK_NAME):
        logger.info("另一个邮件检查任务正在运行。跳过此次执行。")
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
            logger.info(f"✅ 成功连接到邮箱 {imap_config['hostname']}。")
            logger.info(f"开始搜索主题为 '{search_subject}' 的未读邮件...")
            unread_messages = imbox.messages(unread=True, subject=search_subject)

            if not unread_messages:
                logger.info(f"没有找到主题为 '{search_subject}' 的新邮件。")
                return

            logger.info(f"找到 {len(unread_messages)} 封相关邮件，开始处理...")
            for uid, message in unread_messages:
                uid_str = uid.decode()
                logger.info("-" * 40)
                logger.info(f"正在处理邮件 - UID: {uid_str}, 主题: '{message.subject}'")

                # --- 关键改动: 立即标记为已读，防止并发 ---
                imbox.mark_seen(uid)
                logger.info(f"[UID: {uid_str}] 已立即标记为已读，以防止重复处理。")

                if _process_single_message(imbox, uid, message, config):
                    imbox.delete(uid)
                    logger.info(f"✅ [UID: {uid_str}] 邮件已成功处理并删除。")
                else:
                    # 如果处理失败，邮件将保持已读状态，不会在下次被获取
                    logger.error(f"❌ [UID: {uid_str}] 邮件处理失败，将保持已读状态但不会被删除。")
    except (errno.ConnectionError, OSError) as e:
        logger.error(f"❌ 连接或处理邮箱时发生严重错误: {e}")
    except Exception as e:
        logger.error(f"❌ 发生未知错误: {e}", exc_info=True)
    finally:
        release_lock(LOCK_NAME)
        logger.info("邮件检查任务执行完毕。")
        logger.info("=" * 40)
