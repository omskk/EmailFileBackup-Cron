import os
import requests
from functools import wraps
from flask import Flask, render_template, request, jsonify, abort, Response, url_for
from mail_processor import process_emails, load_config
from database import get_logs_paginated, get_total_log_count
from math import ceil, floor, log

app = Flask(__name__)

# --- 认证 ---
def check_auth(username, password):
    """检查用户名和密码是否正确。"""
    web_user = os.getenv("WEB_AUTH_USER", "admin")
    web_pass = os.getenv("WEB_AUTH_PASSWORD", "")
    return username == web_user and password == web_pass

def authenticate():
    """发送一个 401 响应，请求认证。"""
    return Response(
        '需要认证才能访问。\n'
        '请输入您的用户名和密码。', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# --- 路由 ---
@app.route('/')
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

def format_size(size_bytes):
    """將字節轉換為KB、MB、GB等。"""
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB")
    i = int(floor(log(size_bytes, 1024)))
    p = pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_name[i]}"

@app.route('/api/run-task', methods=['POST'])
def run_task():
    """
    公开的 API 端点，用于异步触发邮件处理任务。
    """
    config = load_config()
    api_secret_key = config.get("api", {}).get("secret_key")
    internal_api_key = os.getenv("INTERNAL_API_KEY")

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
    internal_api_key = os.getenv("INTERNAL_API_KEY")

    # 验证内部 API 密钥
    auth_header = request.headers.get('Authorization')
    if not auth_header or auth_header != f"Bearer {internal_api_key}":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    try:
        process_emails()
        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5001)
