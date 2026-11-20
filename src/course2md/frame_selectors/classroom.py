"""用本地视觉特征挑选课件代表图，合并重复页与被完整页覆盖的动画状态。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from .api import SelectionOptions

_SIZE = (512, 288)
_KERNEL = np.ones((5, 5), np.uint8)


def _longest_run(mask: np.ndarray) -> tuple[int, int]:
    edges = np.diff(np.pad(mask.astype(np.int8), (1, 1)))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    if not len(starts):
        return 0, len(mask)
    index = int(np.argmax(ends - starts))
    return int(starts[index]), int(ends[index])


def _screen_region(gray: np.ndarray) -> tuple[float, float, float, float]:
    """在教室全景中寻找与暗色背景形成明显对比的投影区域。"""
    small = cv2.resize(gray, (320, 180))
    columns = np.median(small[:120], axis=0)
    low, high = np.percentile(columns, [15, 85])
    if high - low < 60:
        return 0, 0, 1, 1
    left, right = _longest_run(columns > (low + high) / 2)
    if not 80 <= right - left <= 295:
        return 0, 0, 1, 1
    rows = np.median(small[:, left:right], axis=1)
    low, high = np.percentile(rows, [15, 85])
    top, bottom = _longest_run(rows > (low + high) / 2)
    if not 36 <= bottom - top <= 155:
        return 0, 0, 1, 1
    top = 0 if top < 36 else max(0, top - 3)
    return left / 320, top / 180, right / 320, min(180, bottom + 3) / 180


@dataclass
class _Signature:
    ink: np.ndarray
    expanded: np.ndarray
    visible: np.ndarray
    coarse: np.ndarray
    coarse_visible: np.ndarray
    detail: np.ndarray
    quality: float
    reason: str = ""

    def distance(self, other: _Signature) -> float:
        shared = self.coarse_visible & other.coarse_visible
        if not shared.any():
            return math.inf
        difference = (self.coarse - other.coarse)[shared]
        return float(np.abs(difference - np.median(difference)).mean())

    def matches(self, other: _Signature) -> bool:
        if (self.reason == "blank") != (other.reason == "blank"):
            return False
        if self.distance(other) > 5:
            return False
        shared = self.visible & other.visible
        changed = ((self.ink & ~other.expanded) | (other.ink & ~self.expanded)) & shared
        if other.reason:
            return int(changed.sum()) <= 1000
        return int(changed.sum()) <= 150 and self.erased_ink(other) <= 20 and other.erased_ink(self) <= 20

    def erased_ink(self, other: _Signature) -> int:
        shared = self.coarse_visible & other.coarse_visible
        offset = float(np.median((self.coarse - other.coarse)[shared]))
        lightened = other.detail.astype(np.int16) - self.detail + offset > 35
        return int((self.ink & self.visible & other.visible & lightened).sum())

    def covered_by(self, other: _Signature) -> bool:
        if (
            other.ink.sum() < self.ink.sum()
            or other.visible.mean() < self.visible.mean() - 0.005
            or self.distance(other) > 5
        ):
            return False
        shared = self.visible & other.visible
        missing = int((self.ink & ~other.expanded & shared).sum())
        return missing <= 80 and missing <= int(self.ink[shared].sum()) * 0.015 and self.erased_ink(other) <= 20


@dataclass
class _Group:
    signature: _Signature
    representative: float
    members: list[float] = field(default_factory=list)
    covered_by: int | None = None


class ContentSelector:
    """同一机位逐帧分组，比较时遮挡时钟、页边与进入投影区域的人影。"""

    label = "课堂录播"
    description = "定位投影区域，忽略人影与时间戳，合并相同课件，过滤空白页和签到二维码"

    def __init__(self, _options: SelectionOptions | None = None) -> None:
        self.region: tuple[float, float, float, float] | None = None
        self.groups: list[_Group] = []
        self.last: int | None = None
        self.qr = cv2.QRCodeDetector()

    def _signature(self, gray: np.ndarray) -> _Signature:
        if self.region is None:
            self.region = _screen_region(gray)
        height, width = gray.shape
        left, top, right, bottom = self.region
        region = gray[round(top * height) : round(bottom * height), round(left * width) : round(right * width)]
        image = cv2.resize(region, _SIZE, interpolation=cv2.INTER_AREA)
        smooth = cv2.GaussianBlur(image, (3, 3), 0.6)
        background = cv2.GaussianBlur(smooth, (0, 0), 5)
        ink = background.astype(np.int16) - smooth > 5
        visible = np.ones(smooth.shape, np.uint8)
        projected = self.region != (0, 0, 1, 1)
        visible[: 18 if projected else 2] = 0
        visible[-20 if projected else -2 :] = 0
        visible[:, :4] = 0
        visible[:, -4:] = 0

        # 大块暗色人影从投影边缘进入。细小文字经开运算消除，避免把字当成人影。
        occluded = np.zeros(smooth.shape, np.uint8)
        if projected:
            dark = (smooth < np.median(smooth[visible > 0]) - 40).astype(np.uint8)
            dark[visible == 0] = 0
            dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
            count, labels, stats, _ = cv2.connectedComponentsWithStats(dark)
            for component in range(1, count):
                _, y, _, component_height, area = stats[component]
                if y + component_height >= 266 and area >= 80:
                    occluded[labels == component] = 1
            visible[cv2.dilate(occluded, np.ones((17, 17), np.uint8)) > 0] = 0

        reason = "blank" if int(ink[visible > 0].sum()) < 60 else ""
        found, points = self.qr.detect(image)
        if found:
            polygon = np.round(points[0]).astype(np.int32)
            cv2.fillConvexPoly(visible, polygon, 0)
            qr_area = cv2.contourArea(polygon) / image.size
            outside = int(ink[visible > 0].sum())
            if qr_area > 0.12 and outside < int(ink.sum()) * 0.4:
                reason = "qr"

        ink &= visible > 0
        expanded = cv2.dilate(ink.astype(np.uint8), _KERNEL) > 0
        coarse = cv2.resize(smooth, (64, 36)).astype(np.int16)
        coarse_visible = cv2.resize(visible, (64, 36), interpolation=cv2.INTER_NEAREST) > 0
        sharpness = float(np.mean(cv2.Laplacian(smooth, cv2.CV_32F) ** 2))
        quality = float(visible.mean() - occluded.mean()) + sharpness / 10000
        return _Signature(ink, expanded, visible > 0, coarse, coarse_visible, smooth, quality, reason)

    def add(self, key: float, gray: np.ndarray) -> None:
        signature = self._signature(gray)
        order = list(range(len(self.groups)))
        if self.last is not None:
            order.remove(self.last)
            order.insert(0, self.last)
        for index in order:
            group = self.groups[index]
            if signature.matches(group.signature):
                group.members.append(key)
                if signature.quality > group.signature.quality:
                    if group.signature.reason == "qr":
                        signature.reason = "qr"
                    group.signature = signature
                    group.representative = key
                self.last = index
                return
        self.groups.append(_Group(signature, key, [key]))
        self.last = len(self.groups) - 1

    def selected(self) -> list[float]:
        useful = [index for index, group in enumerate(self.groups) if not group.signature.reason]
        for group in self.groups:
            group.covered_by = None
        for index in useful:
            group = self.groups[index]
            for other_index in useful:
                other = self.groups[other_index]
                if index != other_index and other.covered_by is None and group.signature.covered_by(other.signature):
                    group.covered_by = other_index
                    break
        return sorted(
            group.representative for group in self.groups if not group.signature.reason and group.covered_by is None
        )

    def report(self) -> dict:
        return {
            "region": self.region,
            "groups": [
                {
                    "representative": group.representative,
                    "members": group.members,
                    "reason": group.signature.reason,
                    "covered_by": group.covered_by,
                }
                for group in self.groups
            ],
        }
