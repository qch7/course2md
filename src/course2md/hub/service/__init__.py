"""Hub 的业务逻辑，按领域拆分在各子模块里，这里统一导出给接口层使用。

与 HTTP 框架无关，接口层（:mod:`course2md.hub.api`）只负责解析请求与返回结果。
校验失败抛出 ValueError / ResolverError（400），需要用户确认时抛出 ConfirmationRequired（409），
资源不存在时抛出 NotFound（404）。

新增业务时放进对应领域的子模块，再在这里补一行导出；不要把无关的函数堆回同一个文件。
"""

from __future__ import annotations

from .common import (
    IMAGE_SUFFIXES,
    Forbidden,
    NotFound,
    get_course,
    get_job,
    has_local_node,
    job_file,
    output_root,
    request_cancel,
    server_mode,
    visible,
)
from .course_transfer import (
    create_transfer_peer,
    delete_transfer_peer,
    export_course_bundle_file,
    export_job_bundle_file,
    import_bundle_file,
    list_transfer_peers,
    remote_catalog,
    transfer_pull,
    transfer_push,
    update_transfer_peer,
)
from .creation import (
    create_job,
    create_targets,
)
from .hub_links import (
    connect_hub_link,
    create_hub_link,
    delete_hub_link,
    disconnect_hub_link,
    list_hub_links,
    update_hub_link,
)
from .jobs import (
    STAGE_ORDER,
    cancel_job,
    job_tasks,
    list_courses,
    list_jobs,
    preview,
    rerun_job,
    send_message,
)
from .local_nodes import (
    create_local_node,
    delete_local_node,
    list_local_nodes,
    update_local_node,
)
from .server_config import (
    available_agents,
    config_payload,
    resolve_frame_selector,
)
from .sources import (
    SESSIONS_WAIT_SECONDS,
    check_source,
    list_sessions,
    refresh_course,
    resolve_source,
    stored_sessions,
)
from .trash import (
    delete_course,
    delete_job,
    list_trash,
    purge_trash,
    restore_trash,
)
from .versions import (
    note_version_text,
    note_versions,
    restore_note_version,
)
from .workers import (
    create_worker_token,
    delete_worker,
    disable_worker,
    list_workers,
    require_queue,
)

__all__ = [
    "IMAGE_SUFFIXES",
    "SESSIONS_WAIT_SECONDS",
    "STAGE_ORDER",
    "Forbidden",
    "NotFound",
    "available_agents",
    "cancel_job",
    "check_source",
    "config_payload",
    "connect_hub_link",
    "create_hub_link",
    "create_job",
    "create_local_node",
    "create_targets",
    "create_transfer_peer",
    "create_worker_token",
    "delete_course",
    "delete_hub_link",
    "delete_job",
    "delete_local_node",
    "delete_transfer_peer",
    "delete_worker",
    "disable_worker",
    "disconnect_hub_link",
    "export_course_bundle_file",
    "export_job_bundle_file",
    "get_course",
    "get_job",
    "has_local_node",
    "import_bundle_file",
    "job_file",
    "job_tasks",
    "list_courses",
    "list_hub_links",
    "list_jobs",
    "list_local_nodes",
    "list_sessions",
    "list_transfer_peers",
    "list_trash",
    "list_workers",
    "note_version_text",
    "note_versions",
    "output_root",
    "preview",
    "purge_trash",
    "refresh_course",
    "remote_catalog",
    "request_cancel",
    "require_queue",
    "rerun_job",
    "resolve_frame_selector",
    "resolve_source",
    "restore_note_version",
    "restore_trash",
    "send_message",
    "server_mode",
    "stored_sessions",
    "transfer_pull",
    "transfer_push",
    "update_hub_link",
    "update_local_node",
    "update_transfer_peer",
    "visible",
]
