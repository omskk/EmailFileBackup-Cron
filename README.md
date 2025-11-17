# MailBridge: IMAP to WebDAV Sync

一款专为 Vercel 平台设计的自动化工具。它能作为一座桥梁，监控指定的 IMAP 邮箱，并将符合特定主题邮件的附件自动同步到 WebDAV 服务器。

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fomskk%2Fmail2webdav.git&env=DATABASE_URL,IMAP_HOSTNAME,IMAP_USERNAME,IMAP_PASSWORD,WEBDAV_URL,WEBDAV_LOGIN,WEBDAV_PASSWORD,EMAIL_SEARCH_SUBJECT,API_SECRET_KEY,INTERNAL_API_KEY,WEB_AUTH_USER,WEB_AUTH_PASSWORD&project-name=mailbridge-imap-to-webdav&repository-name=mailbridge-imap-to-webdav)

## ✨ 主要功能

- **一键部署**: 通过 Vercel 按钮，只需几步即可完成部署。
- **无服务器架构**: 完美运行在 Vercel 上，无需管理自己的服务器。
- **邮件监控**: 通过 IMAP 连接邮箱，实时监控新邮件。
- **关键字过滤**: 只处理邮件主题包含特定关键字的邮件。
- **附件同步**: 自动提取附件并上传到 WebDAV 服务器。
- **日志记录**: 所有同步活动都会记录在 MySQL 数据库中，方便追踪和搜索。
- **Web 界面**: 提供一个受密码保护的简洁 Web 页面，用于查看和搜索同步日志。
- **Webhook 触发**: 可由 Vercel Cron Jobs 或任何第三方定时任务服务调用。
- **并发安全**: 通过独特的邮件状态管理，确保高频调用下的稳定性和数据一致性。

## 🚀 一键部署指南

1.  **点击上方 "Deploy with Vercel" 按钮。**
2.  **授权 Vercel**: 登录并授权 Vercel 访问您的 GitHub 账户，它将为您创建一个新的仓库。
3.  **配置环境变量**: 在 Vercel 的配置界面，仔细填写所有必需的环境变量。这是部署过程中最重要的一步。

| 变量名                | 描述                                           |
| --------------------- | ---------------------------------------------- |
| `DATABASE_URL`        | 你的 MySQL 数据库连接字符串。                 |
| `IMAP_HOSTNAME`       | 你的邮箱 IMAP 服务器地址。                     |
| `IMAP_USERNAME`       | 你的邮箱账户。                                 |
| `IMAP_PASSWORD`       | 你的邮箱密码或应用专用密码。                   |
| `WEBDAV_URL`          | 你的 WebDAV 服务器的完整 URL。                 |
| `WEBDAV_LOGIN`        | WebDAV 的用户名。                              |
| `WEBDAV_PASSWORD`     | WebDAV 的密码。                                |
| `EMAIL_SEARCH_SUBJECT`| 用于匹配邮件主题的关键字。                     |
| `API_SECRET_KEY`      | 保护 API 的密钥，请使用一个长而随机的字符串。  |
| `INTERNAL_API_KEY`    | 内部 API 调用的密钥，请使用另一个长而随机的字符串。|
| `WEB_AUTH_USER`       | 访问 Web 日志页面的用户名。                    |
| `WEB_AUTH_PASSWORD`   | 访问 Web 日志页面的密码。                      |

4.  **部署**: 点击 "Deploy"，Vercel 将自动完成所有工作。

## 🕹️ 设置定时任务

部署成功后，您需要设置一个定时任务来定期调用 API，以实现自动化同步。

- **推荐方式**: 使用 [Vercel Cron Jobs](https://vercel.com/docs/cron-jobs)。
- **其他方式**: 可以使用任何第三方 Cron 服务（如 Uptime Kuma, Cron-job.org）或通过 GitHub Actions 设置。

**配置示例 (Vercel Cron Jobs)**:
在您 Vercel 项目的 `vercel.json` 文件中，添加如下配置，表示每分钟执行一次。

```json
{
  "crons": [
    {
      "path": "/api/run-task",
      "schedule": "* * * * *"
    }
  ]
}
```

**重要**: 调用此端点时，必须包含正确的 `Authorization` 头。

- **URL**: `https://<your-project-url>.vercel.app/api/run-task`
- **Method**: `POST`
- **Header**: `Authorization: Bearer <your_api_secret_key>`

## 💻 本地开发

1.  克隆您自己的项目仓库。
2.  安装依赖: `pip install -r requirements.txt`。
3.  创建并填写 `.env` 文件 (参考 `.env.example`)。
4.  运行开发服务器: `python app.py`。

## 🛠️ 技术栈

- **框架**: Python, Flask
- **部署**: Vercel
- **数据库**: MySQL
- **主要库**:
  - `imbox`: IMAP 邮件处理
  - `requests`: WebDAV 上传
  - `mysql-connector-python`: 数据库连接
