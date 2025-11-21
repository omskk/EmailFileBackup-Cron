import os
import secrets
import requests
from functools import wraps
from flask import Flask, render_template, request, jsonify, abort, Response, url_for, session, redirect
from flask_session import Session
from math import ceil, floor, log
from webdav3.client import Client
from urllib.parse import quote, unquote, urlparse

# --- Import from centralized config ---
from config import load_config, validate_config
from mail_processor import process_emails

# 导入from database
from database import get_logs_paginated, get_total_log_count, get_log_count_by_status, get_db_connection, init_db, \
    cleanup_stale_locks, get_config_value, set_config_value, seed_servers_from_env, \
    get_all_servers, get_enabled_servers, get_server_by_name, get_server_by_id, add_server, update_server, delete_server

# 使用统一的日志模块
from logger import get_logger, LogEmoji

logger = get_logger(__name__)

app = Flask(__name__)

# Flask-Session 配置
app.secret_key = os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = '/tmp/flask_session'  # 使用/tmp目录（Vercel可写）
app.config['SESSION_PERMANENT'] = False
app.config['SESSION_USE_SIGNER'] = True

# 初始化 Flask-Session
Session(app)

# Load Config
config = load_config()

# ================= WebDAV 配置 =================
# 这里的配置现在是动态获取的，不再是全局常量
# WEBDAV_SERVERS = config['webdav_servers']

# ================= 初始化数据库 =================
try:
    init_db()
    cleanup_stale_locks()  # 启动时无条件清理所有锁（新实例，旧锁无效）
    seed_servers_from_env()  # 从环境变量导入服务器配置（首次启动）
except Exception as e:
    logger.error(f"{LogEmoji.ERROR} 数据库初始化失败: {e}")


# --- 配置验证 ---
def validate_api_keys():
    """验证 API 密钥长度是否足够安全"""
    api_key = config['api']['secret_key']
    internal_key = config['api']['internal_key']
    web_pass = config['web']['password']

    if len(api_key) < 32:
        logger.warning(f"{LogEmoji.WARNING} API_SECRET_KEY 长度少于32字符,建议使用更强的密钥!")
    if len(internal_key) < 32:
        logger.warning(f"{LogEmoji.WARNING} INTERNAL_API_KEY 长度少于32字符,建议使用更强的密钥!")
    if not web_pass:
        logger.error(f"{LogEmoji.ERROR} WEB_AUTH_PASSWORD 未设置,Web界面将无法访问!")


# 应用启动时验证配置
validate_api_keys()


# --- 认证 ---
def check_auth(username, password):
    """检查用户名和密码是否正确,使用时序攻击安全的比较。"""
    web_user = config['web']['user']
    web_pass = config['web']['password']

    # 使用 secrets.compare_digest 防止时序攻击
    return (secrets.compare_digest(username, web_user) and
            secrets.compare_digest(password, web_pass))


def authenticate():
    """发送一个 401 响应，请求认证。"""
    return Response(
        '需要认证才能访问。\n'
        '请输入您的用户名和密码。', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})


def requires_auth(f):
    """要求用户登录的装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated


# --- WebDAV 辅助函数 ---
def get_server_config(server_name):
    """根据名称获取服务器配置（从数据库）"""
    server = get_server_by_name(server_name)
    if server:
        # 转换数据库格式为应用所需格式
        return {
            'name': server['name'],
            'url': server['url'],
            'login': server['login'],
            'password': server['password'],
            'timeout': server.get('timeout', 60),
            'chunk_size': server.get('chunk_size', 8192)
        }
    return None

def get_webdav_client(server_config):
    """获取WebDAV客户端实例"""
    parsed_url = urlparse(server_config['url'])
    host = f"{parsed_url.scheme}://{parsed_url.netloc}"
    root_path = parsed_url.path.rstrip('/')
    
    return Client({
        'webdav_hostname': host,
        'webdav_login': server_config['login'],
        'webdav_password': server_config['password'],
        'webdav_root': root_path
    })

def format_size(size_bytes):
    """将字节转换为KB、MB、GB等。"""
    if not size_bytes:
        return "0B"
    try:
        size_bytes = int(size_bytes)
        if size_bytes == 0:
            return "0B"
        size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
        i = int(floor(log(size_bytes, 1024)))
        p = pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"
    except (ValueError, TypeError):
        return "-"

def format_date(date_str):
    """格式化日期 (简化)"""
    if not date_str:
        return "-"
    return date_str


# --- 路由 ---

# === 认证路由 ===
@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录页面"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if check_auth(username, password):
            session['logged_in'] = True
            session['username'] = username
            logger.info(f"{LogEmoji.SUCCESS} 用户 {username} 登录成功")
            
            # 跳转到next参数指定的页面，或默认首页
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('home'))
        else:
            logger.warning(f"{LogEmoji.WARNING} 用户 {username} 登录失败")
            return render_template('login.html', error='用户名或密码错误')
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    """登出"""
    username = session.get('username', '未知用户')
    session.clear()
    logger.info(f"{LogEmoji.INFO} 用户 {username} 已登出")
    return redirect(url_for('login'))


# === 页面路由 ===
@app.route('/')
@requires_auth
def home():
    return render_template('index.html')


@app.route('/logs')
@requires_auth
def view_logs():
    """
    從資料庫獲取分頁日誌並顯示，支持搜索。
    """
    page = request.args.get('page', 1, type=int)
    search_query = request.args.get('q', None)
    per_page = 20

    logs = get_logs_paginated(page, per_page, search_query)
    total_logs = get_total_log_count(search_query)
    total_pages = ceil(total_logs / per_page)

    # 將檔案大小從字節轉換為更易讀的格式
    for log in logs:
        log['size_readable'] = format_size(log['size_bytes'])

    return render_template('logs.html',
                           logs=logs,
                           page=page,
                           total_pages=total_pages,
                           search_query=search_query)


@app.route('/files')
@app.route('/files/<path:subpath>')
@requires_auth
def webdav_index(subpath=None):
    """WebDAV 文件浏览器 - 支持多服务器"""
    
    # 1. 根路径 /files: 显示服务器列表
    if subpath is None:
        servers = []
        db_servers = get_enabled_servers()  # 从数据库获取启用的服务器
        for s in db_servers:
            servers.append({
                'name': s['name'],
                'display_path': s['name'], # 链接到服务器根目录
                'isdir': True,
                'size_str': '-',
                'mtime': '-',
                'is_server_root': True # 标记为服务器根节点
            })
        return render_template(
            'webdav.html',
            files=servers,
            display_path='/',
            parent_path=None, # 根目录没有父级
            is_root=True
        )

    # 2. 解析路径 /files/<server_name>/<path...>
    parts = subpath.split('/', 1)
    server_name = parts[0]
    req_path = '/' + parts[1] if len(parts) > 1 else '/'
    
    server_config = get_server_config(server_name)
    if not server_config:
        return "Server not found", 404
        
    logger.info(f"{LogEmoji.NETWORK} WebDAV 浏览请求: Server={server_name}, Path={req_path}")

    try:
        client = get_webdav_client(server_config)
        items = client.list(req_path, get_info=True)
        
        # 获取 WebDAV Root Path 用于路径处理
        parsed_url = urlparse(server_config['url'])
        webdav_root_path = parsed_url.path.rstrip('/')
        
        file_list = []
        for item in items:
            item_full_path = item['path']
            
            # 构造当前目录的完整路径来比较，过滤掉当前目录本身
            current_full_path = webdav_root_path + req_path
            current_full_path = current_full_path.replace('//', '/')
            
            if item_full_path.rstrip('/') == current_full_path.rstrip('/'):
                continue
                
            # 计算显示路径 (相对于 WebDAV Root)
            if webdav_root_path and item_full_path.startswith(webdav_root_path):
                rel_path = item_full_path[len(webdav_root_path):]
            else:
                rel_path = item_full_path
            
            if not rel_path.startswith('/'):
                rel_path = '/' + rel_path
                
            # 最终显示路径需要包含 server_name 前缀: /files/<server_name>/<rel_path>
            # 但在模板中 url_for 会自动处理，我们只需要给 webdav_index 传 subpath
            # subpath = server_name + rel_path
            # 这里我们为了方便模板，直接构造完整的 subpath
            
            display_subpath = f"{server_name}{rel_path}"

            name = os.path.basename(item_full_path.rstrip('/'))
            if not name: 
                continue
            
            # 过滤系统文件
            if name in ['.DS_Store', '._.DS_Store']:
                continue

            file_list.append({
                'name': unquote(name),
                'display_path': display_subpath, # 传递给 url_for('webdav_index', subpath=...)
                'isdir': item['isdir'],
                'size_str': format_size(item.get('size', 0)) if not item['isdir'] else '-',
                'mtime': format_date(item.get('modified', '')),
                'size': int(item.get('size', 0)) if not item['isdir'] and item.get('size') else 0
            })

        file_list.sort(key=lambda x: (not x['isdir'], x['name'].lower()))
        
        # 计算父目录
        if req_path == '/':
            # 如果当前是服务器根目录，父目录是 /files (即服务器列表)
            parent_path = '' # 空字符串表示返回服务器列表
        else:
            # 否则父目录是上一级
            parent_dir = os.path.dirname(req_path.rstrip('/'))
            parent_path = f"{server_name}{parent_dir}"
            
        return render_template(
            'webdav.html', 
            files=file_list, 
            display_path=f"/{server_name}{req_path}", 
            parent_path=parent_path,
            is_root=False
        )
        
    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} WebDAV 浏览失败: {str(e)}")
        return f"无法连接到 WebDAV 服务器 '{server_name}': {str(e)}", 500


@app.route('/files/download/<path:subpath>')
@requires_auth
def webdav_download(subpath):
    """WebDAV 文件下载"""
    # 2. 解析路径 /files/<server_name>/<path...>
    parts = subpath.split('/', 1)
    server_name = parts[0]
    req_path = '/' + parts[1].lstrip('/') if len(parts) > 1 else '/'
    
    server_config = get_server_config(server_name)
    if not server_config:
        return "Server not found", 404
        
    parsed_url = urlparse(server_config['url'])
    webdav_host = f"{parsed_url.scheme}://{parsed_url.netloc}"
    webdav_root_path = parsed_url.path.rstrip('/')

    # 尝试解码路径，解决部分服务器/客户端编码不一致问题
    req_path_decoded = unquote(req_path)
    
    logger.info(f"{LogEmoji.DOWNLOAD} WebDAV 下载请求: Server={server_name}, RawPath={req_path}, DecodedPath={req_path_decoded}")

    # 优先使用解码后的路径，如果失败则尝试原始路径
    target_path = req_path_decoded
    
    full_webdav_path = webdav_root_path + target_path
    full_webdav_path = full_webdav_path.replace('//', '/')

    try:
        # 1. 获取文件元数据 (准确的大小和修改时间)
        # 注意: 即使获取失败也继续尝试下载，因为 info 可能因为权限等原因失败但 get 成功
        file_size = None
        last_modified = None
        
        client = get_webdav_client(server_config)
        try:
            info = client.info(target_path)
            file_size = int(info.get('size', 0))
            last_modified = info.get('modified')
        except Exception as e:
            logger.warning(f"{LogEmoji.WARNING} 获取文件信息失败 (尝试继续下载): {e}, Path: {target_path}")
            # 备选：尝试使用未解码的路径
            if req_path != req_path_decoded:
                try:
                    logger.info(f"尝试使用原始路径获取信息: {req_path}")
                    info = client.info(req_path)
                    file_size = int(info.get('size', 0))
                    last_modified = info.get('modified')
                    target_path = req_path # 如果原始路径成功，下载也用原始路径
                    full_webdav_path = webdav_root_path + target_path
                    full_webdav_path = full_webdav_path.replace('//', '/')
                except Exception as e2:
                    logger.warning(f"{LogEmoji.WARNING} 原始路径获取信息也失败: {e2}")

        
        # 2. 构建下载链接
        encoded_path = quote(full_webdav_path, safe='/')
        file_url = f"{webdav_host}{encoded_path}"
        
        # 3. 发起请求 (流式下载)
        headers = {'User-Agent': 'WebDAV-Browser'}
        upstream_res = requests.get(
            file_url,
            auth=(server_config['login'], server_config['password']),
            stream=True,
            headers=headers,
            timeout=server_config['timeout']
        )
        
        # 检查响应状态，如果是错误页面(如404/500)，内容可能很短(比如12字节)
        if upstream_res.status_code != 200:
            logger.error(f"{LogEmoji.ERROR} WebDAV 下载响应错误: {upstream_res.status_code}")
            return f"WebDAV Error: {upstream_res.status_code}", upstream_res.status_code

        def generate():
            try:
                for chunk in upstream_res.iter_content(chunk_size=server_config['chunk_size']):
                    if chunk:
                        yield chunk
            except Exception as e:
                logger.error(f"{LogEmoji.ERROR} 下载流中断: {e}")

        filename = os.path.basename(target_path)
        try:
            filename_utf8 = quote(filename)
        except:
            filename_utf8 = filename

        # 4. 构建响应头 (使用 info 中的准确信息)
        resp_headers = {
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename_utf8}",
            "Content-Length": file_size,
            "Content-Type": "application/octet-stream" # 默认类型
        }
        
        # 尝试从 info 或 upstream headers 获取更准确的 Content-Type
        if 'Content-Type' in upstream_res.headers:
             resp_headers['Content-Type'] = upstream_res.headers['Content-Type']
            
        if last_modified:
             resp_headers['Last-Modified'] = format_date(last_modified) # 确保格式正确
        
        return Response(generate(), headers=resp_headers)
        
    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} WebDAV 下载失败: {str(e)}")
        return "下载失败", 500


@app.route('/api/run-task', methods=['POST'])
def run_task():
    """
    公开的 API 端点，用于异步触发邮件处理任务。
    """
    # Reload config to ensure latest values
    current_config = load_config()
    api_secret_key = current_config['api']['secret_key']
    internal_api_key = current_config['api']['internal_key']

    if not api_secret_key or not internal_api_key:
        return jsonify({"status": "error", "message": "Server Error: API keys 未完全配置。"}), 500

    # 验证外部 API 密钥
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {api_secret_key}":
        return jsonify({"status": "error", "message": "Unauthorized: 无效或缺失的 API 密钥。"}), 401

    try:
        # 异步调用内部 worker 端点
        worker_url = url_for('internal_worker', _external=True)
        headers = {'Authorization': f'Bearer {internal_api_key}'}

        # 使用 timeout 实现 "fire-and-forget"
        requests.post(worker_url, headers=headers, timeout=0.5)

    except requests.exceptions.ReadTimeout:
        # 这是预期的行为，因为我们不等待 worker 响应
        pass
    except Exception as e:
        return jsonify({"status": "error", "message": f"触发 worker 失败: {e}"}), 500

    return jsonify({"status": "success", "message": "邮件处理任务已成功异步触发。"}), 202


@app.route('/api/internal/worker', methods=['POST'])
def internal_worker():
    """
    内部 worker 端点，实际执行耗时任务。
    """
    internal_api_key = config['api']['internal_key']

    # 验证内部 API 密钥
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {internal_api_key}":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        process_emails()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        # 记录详细错误但不暴露给客户端
        logger.error(f"{LogEmoji.ERROR} 邮件处理失败: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "邮件处理失败,请查看日志"}), 500


@app.route('/servers')
@requires_auth
def server_management():
    """服务器管理页面"""
    servers = get_all_servers()  # 从数据库获取所有服务器
    default_server = get_config_value('default_webdav_server')
    
    return render_template(
        'servers.html',
        servers=servers,
        default_server=default_server
    )


@app.route('/servers/set-default', methods=['POST'])
@requires_auth
def set_default_server():
    """设置默认上传服务器"""
    server_name = request.form.get('server_name')
    
    if not server_name:
        return jsonify({"status": "error", "message": "服务器名称不能为空"}), 400
    
    # 验证服务器是否存在（从数据库）
    server = get_server_by_name(server_name)
    if not server:
        return jsonify({"status": "error", "message": "服务器不存在"}), 404
    
    # 保存到数据库
    if set_config_value('default_webdav_server', server_name):
        return jsonify({"status": "success", "message": f"默认服务器已设置为: {server_name}"}), 200
    else:
        return jsonify({"status": "error", "message": "保存失败"}), 500


@app.route('/servers/add', methods=['POST'])
@requires_auth  
def add_webdav_server():
    """添加新的 WebDAV 服务器"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        url = data.get('url', '').strip()
        login = data.get('login', '').strip()
        password = data.get('password', '').strip()
        enabled = data.get('enabled', True)
        priority = int(data.get('priority', 0))
        
        if not all([name, url, login, password]):
            return jsonify({"status": "error", "message": "所有字段都必须填写"}), 400
        
        # 检查名称是否已存在
        if get_server_by_name(name):
            return jsonify({"status": "error", "message": f"服务器名称 '{name}' 已存在"}), 400
        
        if add_server(name, url, login, password, enabled, priority):
            return jsonify({"status": "success", "message": f"服务器 '{name}' 添加成功"}), 200
        else:
            return jsonify({"status": "error", "message": "添加失败"}), 500
            
    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 添加服务器失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/servers/edit/<int:server_id>', methods=['POST'])
@requires_auth
def edit_webdav_server(server_id):
    """编辑 WebDAV 服务器"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        url = data.get('url', '').strip()
        login = data.get('login', '').strip()
        password = data.get('password', '').strip()
        enabled = data.get('enabled', True)
        priority = int(data.get('priority', 0))
        
        if not all([name, url, login, password]):
            return jsonify({"status": "error", "message": "所有字段都必须填写"}), 400
        
        # 检查服务器是否存在
        existing = get_server_by_id(server_id)
        if not existing:
            return jsonify({"status": "error", "message": "服务器不存在"}), 404
        
        # 如果修改了名称,检查新名称是否已被其他服务器使用
        if name != existing['name']:
            if get_server_by_name(name):
                return jsonify({"status": "error", "message": f"服务器名称 '{name}' 已被使用"}), 400
        
        if update_server(server_id, name, url, login, password, enabled, priority):
            return jsonify({"status": "success", "message": f"服务器 '{name}' 更新成功"}), 200
        else:
            return jsonify({"status": "error", "message": "更新失败"}), 500
            
    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 编辑服务器失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/servers/delete/<int:server_id>', methods=['POST'])
@requires_auth
def delete_webdav_server(server_id):
    """删除 WebDAV 服务器"""
    try:
        # 检查服务器是否存在
        server = get_server_by_id(server_id)
        if not server:
            return jsonify({"status": "error", "message": "服务器不存在"}), 404
        
        # 检查是否是默认服务器
        default_server = get_config_value('default_webdav_server')
        if server['name'] == default_server:
            return jsonify({"status": "error", "message": "不能删除默认服务器，请先选择其他默认服务器"}), 400
        
        if delete_server(server_id):
            return jsonify({"status": "success", "message": f"服务器 '{server['name']}' 删除成功"}), 200
        else:
            return jsonify({"status": "error", "message": "删除失败"}), 500
            
    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 删除服务器失败: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/servers/test/<int:server_id>', methods=['POST'])
@requires_auth
def test_webdav_server(server_id):
    """测试 WebDAV 服务器连接"""
    try:
        # 获取服务器配置
        server = get_server_by_id(server_id)
        if not server:
            return jsonify({"status": "error", "message": "服务器不存在"}), 404
        
        # 构建服务器配置
        server_config = {
            'name': server['name'],
            'url': server['url'],
            'login': server['login'],
            'password': server['password']
        }
        
        # 尝试连接
        client = get_webdav_client(server_config)
        client.list('/', get_info=False)
        
        return jsonify({
            "status": "success", 
            "message": f"✅ 服务器 '{server['name']}' 连接成功！"
        }), 200
        
    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 测试服务器连接失败: {e}")
        return jsonify({
            "status": "error", 
            "message": f"❌ 连接失败: {str(e)}"
        }), 200  # 返回 200 但 status 为 error，方便前端处理


@app.route('/health')
def health_check():
    """健康检查端点,用于监控和自动化检测"""
    status = {
        "status": "healthy",
        "database": "unknown",
        "webdav": "unknown"
    }
    
    # 检查数据库连接
    try:
        conn = get_db_connection()
        if conn:
            conn.close()
            status["database"] = "connected"
        else:
            status["database"] = "disconnected"
            status["status"] = "unhealthy"
    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 数据库健康检查失败: {e}")
        status["database"] = "error"
        status["status"] = "unhealthy"

    # 检查 WebDAV 连接 (检查所有配置的服务器)
    webdav_status = "connected"
    for server in config['webdav_servers']:
        try:
            client = get_webdav_client(server)
            client.list('/', get_info=False)
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} WebDAV 健康检查失败 ({server['name']}): {e}")
            webdav_status = "disconnected"
            status["status"] = "unhealthy"
            
    status["webdav"] = webdav_status

    code = 200 if status["status"] == "healthy" else 503
    return jsonify(status), code


if __name__ == '__main__':
    app.run(debug=True, port=5001)

