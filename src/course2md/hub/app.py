"""Hub 的 FastAPI 应用：REST /api/v1、SSE 与前端页面。"""

from __future__ import annotations

import mimetypes
import threading
import traceback
import webbrowser
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..core.accounts import AccountError
from ..resolvers import ResolverError
from .api import v1_router, worker_router
from .auth import router as auth_router
from .security import LocalOriginGuard
from .service import Forbidden, NotFound
from .transfer import TransferError

DEFAULT_PORT = 8765
WEB_DIST = Path(__file__).resolve().parent.parent / "web_dist"  # 前端构建产物（web/ 中 pnpm build 生成）
UNBUILT = (
    "<!doctype html><meta charset=utf-8><title>course2md</title>"
    "<p>前端尚未构建。在仓库的 web 目录中运行 <code>pnpm install</code> 与 <code>pnpm build</code> 后刷新；"
    "接口文档见 <a href=/api/docs>/api/docs</a>。</p>"
)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _validation_message(exc: RequestValidationError) -> str:
    for error in exc.errors():
        if error.get("type") == "json_invalid":
            return "请求内容不是有效的 JSON"
        if error.get("loc") == ("body",):
            return "请求内容必须是 JSON 对象"
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(part) for part in first.get("loc", ())[1:]) or "请求"
    return f"{field} 取值无效：{first.get('msg', '格式错误')}"


def create_app(
    manager=None,
    *,
    bind_host: str = "127.0.0.1",
    local_guard: bool = True,
    accounts=None,
    web_dist: Path | None = None,
) -> FastAPI:
    """manager 为任务管理器。

    accounts 为空时是本地模式：不需要登录，local_guard 检查 Host / Origin 防止其它网页调用本机服务。
    传入 :class:`course2md.core.accounts.Accounts` 时是服务器模式：所有接口需要登录，只检查写请求的来源。
    """
    from ..jobs.manager import ConfirmationRequired

    app = FastAPI(
        title="course2md",
        version=__version__,
        description="课程回放转 Markdown 笔记的 Hub 接口。所有路径位于 /api/v1 下，进度可通过 SSE 订阅。",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    app.state.manager = manager
    app.state.accounts = accounts
    if manager is not None and accounts is not None:
        manager.mode = "server"
    app.state.web_dist = web_dist if web_dist is not None else WEB_DIST

    app.include_router(v1_router, prefix="/api/v1")
    app.include_router(worker_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")

    @app.exception_handler(ConfirmationRequired)
    async def _confirmation(_request: Request, exc: ConfirmationRequired) -> JSONResponse:
        return JSONResponse(exc.payload, status_code=409)

    @app.exception_handler(NotFound)
    async def _not_found(_request: Request, exc: NotFound) -> JSONResponse:
        return _error(str(exc), 404)

    @app.exception_handler(Forbidden)
    async def _forbidden(_request: Request, exc: Forbidden) -> JSONResponse:
        return _error(str(exc), 403)

    @app.exception_handler(AccountError)
    async def _account(_request: Request, exc: AccountError) -> JSONResponse:
        return _error(str(exc), 400)

    @app.exception_handler(ValueError)
    async def _bad_value(_request: Request, exc: ValueError) -> JSONResponse:
        return _error(str(exc), 400)

    @app.exception_handler(ResolverError)
    async def _resolver(_request: Request, exc: ResolverError) -> JSONResponse:
        return _error(str(exc), 400)

    @app.exception_handler(TransferError)
    async def _transfer(_request: Request, exc: TransferError) -> JSONResponse:
        return _error(str(exc), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(_validation_message(exc), 400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404 and exc.detail == "Not Found":
            return _error("未知路径", 404)
        if exc.detail == "There was an error parsing the body":
            return _error("请求内容不是有效的 JSON", 400)
        return _error(str(exc.detail), exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
        traceback.print_exception(exc)
        return _error(str(exc) or exc.__class__.__name__, 500)

    def _file(root_dir: Path, name: str, cache: bool = False) -> Response:
        root = Path(root_dir).resolve()
        target = (root / unquote(name)).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return _error("not found", 404)
        headers = {"Cache-Control": "public, max-age=31536000, immutable"} if cache else {"Cache-Control": "no-cache"}
        return Response(
            target.read_bytes(),
            media_type=mimetypes.guess_type(target.name)[0] or "text/plain; charset=utf-8",
            headers=headers,
        )

    def _spa_available() -> bool:
        return app.state.web_dist is not None and (Path(app.state.web_dist) / "index.html").is_file()

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index() -> Response:
        if _spa_available():
            return _file(app.state.web_dist, "index.html")
        return Response(UNBUILT, media_type="text/html; charset=utf-8", status_code=503)

    @app.get("/assets/{name:path}", include_in_schema=False)
    def assets(name: str) -> Response:
        if not _spa_available():
            return _error("not found", 404)
        return _file(Path(app.state.web_dist) / "assets", name, cache=True)

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        """前端的客户端路由（/job/…、/workers 等）都返回同一个页面。"""
        if path.startswith("api/") or not _spa_available():
            return _error("未知路径", 404)
        return _file(app.state.web_dist, "index.html")

    if local_guard or accounts is not None:
        # 节点接口使用令牌鉴权，不受页面来源检查限制，局域网或公网节点也能接入。
        # 服务器模式经反向代理以公开域名访问，只检查写请求的来源与格式，不限制 Host。
        app.add_middleware(
            LocalOriginGuard, bind_host=bind_host, exempt_prefixes=("/api/v1/worker/",), check_host=accounts is None
        )
    return app


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    """启动本地应用：任务管理器 + Hub 接口 + 网页。"""
    import uvicorn

    from ..config import bootstrap_runtime, load_settings
    from ..jobs.manager import JobManager
    from ..resolvers import load_errors
    from ..util.winacl import ACL_HINT, acl_grant_problem

    load_settings()
    bootstrap_runtime()
    manager = JobManager()
    for error in load_errors():
        manager.warnings.append(error)
        print(error)
    problem = acl_grant_problem(manager.store.path.parent)
    if problem:
        warning = f"{ACL_HINT}（检查结果：{problem}）"
        manager.warnings.append(warning)
        print(warning)

    app = create_app(manager, bind_host=host)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    def announce() -> None:
        while not server.started and not server.should_exit:
            threading.Event().wait(0.05)
        if not server.started:
            return
        actual = server.servers[0].sockets[0].getsockname()[1] if server.servers else port
        url = f"http://{host}:{actual}"
        started = manager.start_hub_links(
            local_origins={url, f"http://127.0.0.1:{actual}", f"http://localhost:{actual}"}
        )
        print(f"course2md 界面已启动：{url}（Ctrl+C 退出）")
        if started:
            print(f"已自动连接 {len(started)} 个外部 Hub（可在「计算服务」页查看状态）")
        if open_browser:
            webbrowser.open(url)

    threading.Thread(target=announce, daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        manager.close()
    if not server.started:
        raise SystemExit(f"端口 {port} 无法监听。换一个端口：--port {port + 1}")
    print("\n已停止")


def serve_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT, with_node: bool = False) -> None:
    """服务器形态：默认以纯 Hub 运行（需要登录、按用户隔离），计算任务由连入的节点承担。"""
    import os

    import uvicorn

    from ..config import bootstrap_runtime, env, load_settings
    from ..core.accounts import Accounts
    from ..core.crypto import FieldCipher
    from ..jobs.manager import JobManager
    from ..resolvers import load_errors

    load_settings()
    if not env("COURSE2MD_SECRET_KEY"):
        raise SystemExit(
            "服务器形态必须设置 COURSE2MD_SECRET_KEY（用于加密保存来源链接等凭证），例如：\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    FieldCipher.from_env()  # 缺少 cryptography 时尽早报错
    bootstrap_runtime()
    manager = JobManager(database_url=env("COURSE2MD_DATABASE_URL"), builtin_worker=with_node, mode="server")
    manager.hub_links.default_work_dir = (manager.data_dir / "hub-links").resolve()
    manager.start_hub_links(
        local_origins={f"http://{host}:{port}", f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    )
    for error in load_errors():
        manager.warnings.append(error)
        print(error)
    accounts = Accounts(manager.store.db, registration=env("COURSE2MD_REGISTRATION", "closed"))
    app = create_app(manager, bind_host=host, accounts=accounts)
    trusted = env("COURSE2MD_TRUSTED_PROXIES", "127.0.0.1")
    setup_token = accounts.begin_setup()
    if setup_token:
        # 令牌只打印这一次、只存于内存：谁能读到服务日志，谁就是部署者，可以创建首个管理员。
        print(
            "\n" + "=" * 64 + "\n"
            "还没有任何账号。请打开网页，用下面的初始化令牌创建管理员：\n\n"
            f"    {setup_token}\n\n"
            "令牌仅在本次运行内有效，创建成功后立即作废；重启服务会生成新的令牌。\n" + "=" * 64 + "\n"
        )
    mode_hint = "含本机节点" if with_node else "纯 Hub，需连接计算节点"
    print(f"course2md 服务器已启动：http://{host}:{port}（{mode_hint}，数据库 {manager.store.db.url.split('@')[-1]}）")
    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="info",
            proxy_headers=True,
            forwarded_allow_ips=trusted,
            server_header=False,
        )
    finally:
        manager.close()
        os.environ.pop("COURSE2MD_SECRET_KEY", None)
