"""接口响应模型，用于 OpenAPI 文档与前端生成的 TypeScript 类型。

字段都有默认值并允许额外字段（extra="allow"），服务层新增字段不会破坏现有客户端。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class FileEntry(_Model):
    name: str = ""
    rel: str = ""
    size: str = ""
    kind: str = ""


class JobEvent(_Model):
    seq: int = 0
    time: float = 0.0
    kind: str = ""
    text: str = ""
    truncated: bool = False


class ChatMessage(_Model):
    id: str = ""
    role: str = ""
    text: str = ""
    status: str = ""
    html: str = ""


class Job(_Model):
    id: str
    source: str = ""
    title: str = ""
    out_dir: str = ""
    status: str = ""
    stage: str = ""
    course: str = ""
    session_date: str = ""
    week: int | None = None
    course_id: str = ""
    session_id: str = ""
    progress: float = 0.0
    sub: float | None = None
    message: str = ""
    logs: list[str] = []
    log_offset: int = 0
    files: list[FileEntry] = []
    error: str = ""
    agent: str = ""
    agent_model: str = ""
    timings: dict[str, float] = {}
    started_at: float = 0.0
    events: list[JobEvent] = []
    event_offset: int = 0
    messages: list[ChatMessage] = []
    pending_messages: int = 0
    revision: int = 0
    can_chat: bool = False


class ExistingResult(_Model):
    path: str = ""
    complete: bool = False
    updated_at: float | None = None


class Session(_Model):
    id: str = ""
    title: str = ""
    date: str = ""
    week: int | None = None
    views: int | None = None
    folder: str = ""
    existing: ExistingResult | None = None
    job_id: str = ""
    status: str = ""


class ViewOption(_Model):
    id: int | str = ""
    label: str = ""
    default: bool = False


class Course(_Model):
    id: str
    title: str = ""
    source: str = ""
    out_dir: str = ""
    course: str = ""
    resolver: str = ""
    resolver_label: str = ""
    sessions: list[Session] = []
    term_start: str = ""
    term_source: str = ""
    view_options: list[ViewOption] = []
    updated_at: float = 0.0


class ResolvedCourse(Course):
    course_id: str = ""


class AgentModelOptions(_Model):
    default_provider: str = ""
    default_model: str = ""
    providers: dict[str, list[str]] = {}


class ResolverInfo(_Model):
    name: str = ""
    label: str = ""
    hint: str = ""


class Plugin(_Model):
    name: str
    label: str = ""
    hint: str = ""
    version: str = ""
    origin: str = ""
    resolvers: list[str] = []
    removable: bool = False


class PluginsResponse(_Model):
    plugins: list[Plugin] = []
    errors: list[str] = []
    can_manage: bool = False
    restart_required: bool = False
    message: str = ""


class FrameSelectorInfo(_Model):
    name: str
    label: str = ""
    description: str = ""
    builtin: bool = False


class Config(_Model):
    model: str = ""
    device: str = ""
    has_llm_key: bool = False
    llm_base: str = ""
    llm_model: str = ""
    llm_vision: bool = False
    max_images: int = 12
    default_out: str | None = None
    term_start: str = ""
    default_formats: list[str] = []
    proofread: bool = True
    detail: bool = True
    frames: bool = True
    frame_interval: float = 5.0
    frame_selector: str = "classroom"
    frame_selectors: list[FrameSelectorInfo] = []
    delete_video: bool = True
    startup_warnings: list[str] = []
    formats: list[str] = []
    devices: list[str] = []
    agents: dict[str, Any] = {}
    agent_default: str = ""
    agent_models: dict[str, AgentModelOptions] = {}
    agent_choices: dict[str, dict[str, str]] = {}
    has_local_node: bool = True
    resolvers: list[ResolverInfo] = []
    server: bool = False


class Worker(_Model):
    id: str
    name: str = ""
    kind: str = ""
    capabilities: list[str] = []
    version: str = ""
    last_seen: float = 0.0
    disabled: bool = False
    owner_id: str | None = None
    online: bool = False


class TaskProgress(_Model):
    stage: str = ""
    fraction: float = 0.0
    message: str = ""
    sub: float | None = None
    seq: int = 0


class Task(_Model):
    id: str
    job_id: str = ""
    stage: str = ""
    status: str = ""
    requires: list[str] = []
    affinity: str | None = None
    result: str | None = None
    warnings: list[str] = []
    error: str = ""
    error_type: str = ""
    worker_id: str | None = None
    attempts: int = 0
    progress: TaskProgress = TaskProgress()
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None


class NoteVersion(_Model):
    version: int
    label: str = ""
    model: str = ""
    created_at: float = 0.0
    has_brief: bool = False


class NoteVersionText(_Model):
    version: int
    kind: str = "detailed"
    text: str = ""


class UserInfo(_Model):
    id: str
    username: str = ""
    role: str = "user"
    disabled: bool = False
    created_at: float | None = None


class JobsResponse(_Model):
    jobs: list[Job] = []


class JobResponse(_Model):
    job: Job


class CreateJobsResponse(_Model):
    jobs: list[Job] = []
    id: str = ""
    job: Job | None = None
    course: Course | None = None


class CoursesResponse(_Model):
    courses: list[Course] = []


class CourseResponse(_Model):
    course: Course


class WorkersResponse(_Model):
    workers: list[Worker] = []


class WorkerToken(_Model):
    worker_id: str
    name: str = ""
    token: str = ""
    shared: bool = False


class TasksResponse(_Model):
    tasks: list[Task] = []


class VersionsResponse(_Model):
    versions: list[NoteVersion] = []


class Preview(_Model):
    name: str = ""
    text: str | None = None
    html: str | None = None
    image_url: str | None = None


class Ok(_Model):
    ok: bool = True


class TrashItem(_Model):
    kind: str = "course"  # course 或 job
    id: str
    title: str = ""
    course: str = ""
    deleted_at: float = 0.0
    purge_at: float = 0.0
    job_count: int = 0


class TrashResponse(_Model):
    items: list[TrashItem] = []
    retention_days: int = 30
    removes_files: bool = False


class AuthStatus(_Model):
    mode: str = "local"
    registration: str = "closed"
    has_users: bool = True
    needs_setup: bool = False


class UserResponse(_Model):
    user: UserInfo


class LoginResponse(UserResponse):
    token: str = ""


class TokenResponse(_Model):
    token: str


class UsersResponse(_Model):
    users: list[UserInfo] = []


class HubLinkTask(_Model):
    id: str = ""
    job_id: str = ""
    stage: str = ""
    started_at: float = 0.0


class HubLink(_Model):
    id: str
    hub_url: str = ""
    name: str = ""
    work_dir: str = ""
    has_token: bool = False
    token_hint: str = ""
    auto_connect: bool = True
    no_agents: bool = False
    without: list[str] = []
    source: str = "manual"
    status: str = "disconnected"
    worker_id: str = ""
    capabilities: list[str] = []
    connected_at: float | None = None
    last_heartbeat: float | None = None
    last_error: str = ""
    retry_at: float | None = None
    current_task: HubLinkTask | None = None
    completed_tasks: int = 0
    failed_tasks: int = 0
    logs: list[str] = []
    created_at: float = 0.0


class HubLinkResponse(_Model):
    link: HubLink


class HubLinksResponse(_Model):
    links: list[HubLink] = []
    can_manage: bool = False
    capabilities: list[str] = []
    default_name: str = ""
    default_work_dir: str = ""


class LocalNode(_Model):
    id: str
    name: str = ""
    builtin: bool = False
    enabled: bool = True
    auto: bool = True
    selected: list[str] = []
    capabilities: list[str] = []
    online: bool = False
    busy: bool = False
    current_task: str | None = None
    created_at: float = 0.0


class LocalNodeResponse(_Model):
    node: LocalNode


class LocalNodesResponse(_Model):
    nodes: list[LocalNode] = []
    can_manage: bool = False
    available_capabilities: list[str] = []


class TransferSkipped(_Model):
    title: str = ""
    session_id: str = ""
    reason: str = ""


class BundleImportResponse(_Model):
    course: Course | None = None
    imported: list[Job] = []
    overwritten: list[Job] = []
    skipped: list[TransferSkipped] = []
    warnings: list[str] = []


class TransferPeer(_Model):
    id: str
    name: str = ""
    hub_url: str = ""
    has_token: bool = False
    token_hint: str = ""
    created_at: float = 0.0


class TransferPeerResponse(_Model):
    peer: TransferPeer


class TransferPeersResponse(_Model):
    peers: list[TransferPeer] = []


class RemoteJob(_Model):
    id: str
    title: str = ""
    course: str = ""
    course_id: str = ""
    session_id: str = ""
    session_date: str = ""
    week: int | None = None
    status: str = ""
    agent: str = ""
    agent_model: str = ""
    started_at: float = 0.0
    file_count: int = 0
    transferable: bool = False


class RemoteCourse(_Model):
    id: str = ""
    title: str = ""
    course: str = ""
    source: str = ""
    updated_at: float = 0.0
    jobs: list[RemoteJob] = []


class RemoteCatalogResponse(_Model):
    hub_url: str = ""
    mode: str = "local"
    courses: list[RemoteCourse] = []


class TransferResultResponse(_Model):
    direction: str = "push"
    hub_url: str = ""
    courses: list[Course] = []
    imported: list[Job] = []
    overwritten: list[Job] = []
    skipped: list[TransferSkipped] = []
    warnings: list[str] = []
