# -*- coding: utf-8 -*-
import errno
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

from database import log_upload

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


def upload_to_webdav_with_retry(config: Dict[str, Any], data: bytes, remote_filename: str) -> bool:
    webdav_config = config['webdav']
    upload_config = config['upload']
    full_url = f"{webdav_config['url'].rstrip('/')}/{remote_filename}"
    auth = (webdav_config['login'], webdav_config['password'])
    file_size = len(data)

    for attempt in range(upload_config['retry_count'] + 1):
        try:
            action = "开始上传" if attempt == 0 else f"第 {attempt} 次重试上传"
            logger.info(f"{action}附件到: {full_url}")
            response = requests.put(full_url, data=data, auth=auth, timeout=30)
            response.raise_for_status()
            logger.info(f"✅ WebDAV 上传成功: '{remote_filename}'")
            log_upload(remote_filename, file_size, "Success")
            return True
        except requests.exceptions.RequestException as e:
            logger.warning(f"WebDAV 上传失败: {e}。")
            if attempt < upload_config['retry_count']:
                logger.info(f"等待 {upload_config['retry_delay']} 秒后重试...")
                time.sleep(upload_config['retry_delay'])

    logger.error(f"❌ WebDAV 上传在 {upload_config['retry_count']} 次重试后仍然失败: '{remote_filename}'。")
    log_upload(remote_filename, file_size, "Failed")
    return False


def sanitize_filename(filename: str) -> str:
    filename = filename.replace('..', '')
    return re.sub(r'[<>:"/\\|?*]', '_', filename)


def decode_email_header(header: str) -> str:
    """
    Decodes an email header, with special handling for RFC 2231 format.
    """
    if header:
        # Check for RFC 2231 format (e.g., utf-8''%E... or utf-8'en'%E...)
        rfc2231_match = re.match(r"([^']*)'([^']*)'(.*)", header)
        if rfc2231_match:
            try:
                charset, lang, encoded_text = rfc2231_match.groups()
                # URL-decode the text using the specified charset
                decoded_filename = unquote(encoded_text, encoding=charset)
                logger.info(f"Successfully decoded RFC 2231 header to '{decoded_filename}'")
                return decoded_filename
            except Exception as e:
                logger.warning(f"Failed to decode RFC 2231 header '{header}': {e}. Falling back.")
                # Fallback to standard decoding if RFC 2231 parsing fails

    # Fallback to standard RFC 2047 decoding for all other cases
    try:
        decoded_parts = []
        # The header might be None, so we guard against it
        for part, charset in decode_header(header or ""):
            try:
                if isinstance(part, bytes):
                    # If charset is None, 'us-ascii' is the default, but utf-8 is a safer bet
                    decoded_parts.append(part.decode(charset or 'utf-8', errors='ignore'))
                else:
                    decoded_parts.append(part)
            except (UnicodeDecodeError, LookupError):
                # If decoding fails, try with utf-8 as a last resort
                decoded_parts.append(part.decode('utf-8', errors='ignore') if isinstance(part, bytes) else part)

        # Guard against empty result, return original header if decode results in empty string
        decoded_header = "".join(decoded_parts)
        return decoded_header if decoded_header else header

    except Exception as e:
        logger.error(f"Generic failure in decoding header '{header}': {e}")
        return header  # Return original header as a last resort


def _process_single_message(imbox: Imbox, uid: bytes, message: Any, config: Dict[str, Any]) -> bool:
    uid_str = uid.decode()

    if not message.attachments:
        logger.warning(f"[UID: {uid_str}] 该邮件没有附件，跳过。")
        return True

    all_attachments_succeeded = True
    for attachment in message.attachments:
        original_filename = "unknown_attachment"
        try:
            original_filename = decode_email_header(attachment.get('filename'))
            safe_filename = sanitize_filename(original_filename)
            logger.info(f"[UID: {uid_str}] 发现附件: '{original_filename}' -> 清理后: '{safe_filename}'")
            attachment_content = attachment.get('content').read()
            if not upload_to_webdav_with_retry(config, attachment_content, safe_filename):
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
    logger.info("=" * 40)
    logger.info("开始执行邮件检查任务...")

    config = load_config()
    if not validate_config(config):
        return

    imap_config = config['imap']
    search_subject = config['email']['search_subject']

    try:
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
        logger.info("邮件检查任务执行完毕。")
        logger.info("=" * 40)
