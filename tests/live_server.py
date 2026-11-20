"""在后台线程中运行 uvicorn，供需要真实 HTTP 连接的测试使用。"""

from __future__ import annotations

import threading
import time

import uvicorn


class LiveServer:
    def __init__(self, app, host: str = "127.0.0.1") -> None:
        config = uvicorn.Config(app, host=host, port=0, log_level="error", access_log=False, lifespan="off")
        self.server = uvicorn.Server(config)
        self.server.install_signal_handlers = lambda: None  # 后台线程不能注册信号处理
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("测试服务未能启动")
            time.sleep(0.01)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
