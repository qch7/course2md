"""课程、来源解析与回收站的接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ... import responses as r
from ... import service
from ...schemas import (
    RefreshRequest,
    ResolveRequest,
)
from .deps import CurrentUser, get_manager

router = APIRouter()


@router.post("/resolve", tags=["课程"], summary="解析来源，列出课次并保存课程", response_model=r.ResolvedCourse)
def resolve(body: ResolveRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.resolve_source(get_manager(request), body.payload(), user)


@router.get("/courses", tags=["课程"], summary="已保存的课程", response_model=r.CoursesResponse)
def list_courses(request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"courses": service.list_courses(get_manager(request), user)}


@router.get("/courses/{course_id}", tags=["课程"], summary="课程详情与课次状态", response_model=r.CourseResponse)
def get_course(course_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    manager = get_manager(request)
    return {"course": manager.course_snapshot(service.get_course(manager, course_id, user))}


@router.delete(
    "/courses/{course_id}", tags=["课程"], summary="删除课程（移入回收站，30 天后永久删除）", response_model=r.Ok
)
def delete_course(course_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_course(get_manager(request), course_id, user)


@router.get("/trash", tags=["回收站"], summary="回收站中的课程与课次", response_model=r.TrashResponse)
def list_trash(request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.list_trash(get_manager(request), user)


@router.post("/trash/{kind}/{item_id}/restore", tags=["回收站"], summary="从回收站恢复课程或课次", response_model=r.Ok)
def restore_trash(kind: str, item_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.restore_trash(get_manager(request), kind, item_id, user)


@router.delete("/trash/{kind}/{item_id}", tags=["回收站"], summary="立即永久删除回收站中的项目", response_model=r.Ok)
def purge_trash(kind: str, item_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.purge_trash(get_manager(request), kind, item_id, user)


@router.post(
    "/courses/{course_id}/refresh", tags=["课程"], summary="重新解析已保存课程的课次", response_model=r.ResolvedCourse
)
def refresh_course(course_id: str, body: RefreshRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.refresh_course(get_manager(request), course_id, body.payload(), user)
