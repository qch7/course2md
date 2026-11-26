# 服务器部署

服务器形态让同学们共用一个 Hub：每人一个账号，课程、任务与笔记互相隔离；转写可以由服务器上的显卡、
或大家自己电脑上的计算节点承担。

## 组成

| 服务 | 作用 |
|---|---|
| `hub` | course2md Hub（`course2md server`），不含转写依赖 |
| `db` | PostgreSQL 17 |
| `caddy` | 反向代理，自动申请 HTTPS 证书 |
| `worker`（可选，`--profile gpu`） | 服务器上的 GPU 计算节点 |

## 步骤

1. 准备一台有公网地址的 Linux 服务器，安装 Docker 与 Docker Compose，把域名解析到服务器，开放 80 / 443 端口。
2. 获取代码并填写配置：

   ```bash
   git clone <仓库地址> course2md && cd course2md/deploy
   cp .env.example .env
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # 分别生成数据库口令与密钥
   ```

   `.env` 中必填 `COURSE2MD_DOMAIN`、`POSTGRES_PASSWORD`、`COURSE2MD_SECRET_KEY`。**备份好密钥**：
   课程链接中的凭证用它加密保存，丢失后无法解密。

3. 启动：`docker compose up -d`，然后从日志中取**初始化令牌**：

   ```bash
   docker compose logs hub | grep -A1 "初始化令牌"
   ```

   打开 `https://你的域名`，填入令牌并设置用户名、口令，创建管理员账号。令牌形如 `c2s_…`，
   只存在于 Hub 进程内存，创建成功后立即作废；重启 Hub 会生成新的令牌，所以没有任何人能在你拿到令牌前抢先成为管理员。
   不用 Docker 时，令牌打印在 `course2md server` 的标准输出中。
4. 默认关闭自助注册（`COURSE2MD_REGISTRATION=closed`），管理员在侧边栏「管理」页为同学创建账号、
   修改角色、停用账号或重置口令；设为 `open` 则任何人都可以在登录页注册普通账号。

## 计算节点

服务器没有显卡时，由各自电脑上的节点转写：

1. 登录后在「计算服务」中生成节点令牌。普通用户生成的是**个人节点**，只处理自己的任务；
   管理员生成的是**共享节点**，处理所有人的任务。
2. 在自己的电脑上：

   ```powershell
   uv tool install "course2md[asr,cuda]"
   course2md worker --hub https://你的域名 --token c2m_xxx
   ```

服务器本身有 NVIDIA 显卡（已安装 NVIDIA Container Toolkit）时，把管理员生成的令牌填入 `.env` 的
`COURSE2MD_WORKER_TOKEN`，然后 `docker compose --profile gpu up -d`。

整理阶段可以使用管理员在 `.env` 中配置的大模型接口（所有用户共用），也可以交给装有 DSH / Codex 的个人节点。

## 回收站

在左侧课程列表删除课程（或单个课次）是逻辑删除：立即从列表消失，进行中的任务先被取消，
保留 30 天，期间可在「回收站」恢复；到期后 Hub 自动永久删除数据库记录、阶段记录、笔记版本，
并删除该用户目录 `users/<用户编号>/` 下对应的输出文件。也可以在回收站里立即永久删除。
本地应用只清理记录，不会删除用户自己磁盘上的笔记文件。

## 来源解析插件

解析课次与下载视频都由计算节点完成（见「计算节点」），所以课程平台只需要节点能访问，Hub 不必。
在 `.env` 的 `COURSE2MD_PLUGINS` 中列出包名（空格分隔），重新构建：`docker compose build && docker compose up -d`。
服务器不允许从网页安装插件，插件由管理员在部署配置中声明，保证可重现。

## 安全边界

- 所有接口都需要登录；会话 Cookie 为 HttpOnly、Secure、SameSite=Lax，脚本可以使用 API 令牌（`Authorization: Bearer`）。
- 连续登录失败会被暂时拒绝（同一地址 10 分钟内 10 次）；初始化令牌的错误尝试同样计入。
- 首个管理员只能凭启动日志中的初始化令牌创建，公网暴露的新实例不会被抢注。
- 普通用户不能读取服务器上的本地文件、不能指定输出路径、不能运行自定义整理命令、不能更换大模型接口地址。
- 课程链接加密保存在数据库中，只在派发下载任务时解密交给节点；个人节点只会收到主人的任务。
- 节点令牌、登录令牌在数据库中只保存 SHA-256 摘要。

## 备份与升级

- 数据在 `db-data`（数据库）与 `hub-data`（笔记、截图与产物）两个卷中，定期备份这两个卷以及 `.env`。
- 升级：`git pull && docker compose build && docker compose up -d`，数据库结构在 Hub 启动时自动迁移。
- 前端在镜像构建时由 `deploy/Dockerfile` 的 `web` 阶段（Node 22 + pnpm）自动构建，部署机不需要安装 Node，
  也不必手动上传 `web_dist`；构建上下文里本地已有的 `src/course2md/web_dist` 会被 `.dockerignore` 排除，不会混进镜像。

## 不用 Docker

```bash
pip install "course2md[server]"
export COURSE2MD_SECRET_KEY=... COURSE2MD_DATABASE_URL=postgresql+psycopg://...
course2md server --host 127.0.0.1 --port 8765   # 默认纯 Hub；单机自带算力可加 --with-node
```

不设置 `COURSE2MD_DATABASE_URL` 时使用输出目录中的 SQLite，适合人数较少的场景。
