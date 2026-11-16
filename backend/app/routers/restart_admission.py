"""重启准入接口：重启前联合检查点与状态后端做准入，输出可审计的重启准入数据清单。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.restart_admission import (
    CONCLUSION_DENY,
    CONCLUSION_PASS,
    CONCLUSION_ROLLBACK,
    RestartAdmissionService,
)

router = APIRouter(prefix="/api/restart-admission", tags=["重启准入"])

service = RestartAdmissionService()

LIST_FIELDS = [
    "准入单号", "重启请求号", "实例编号", "作业名称", "运行节点", "重启次数",
    "恢复条件", "检查点编号", "检查点版本", "状态后端编号", "需要slot数",
    "节点slot总数", "节点空闲slot", "缺失slot", "准入结论", "快照版本", "创建时间",
]
CONCLUSIONS = [CONCLUSION_PASS, CONCLUSION_DENY, CONCLUSION_ROLLBACK]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按准入单号、请求号、实例编号、检查点编号/版本检索"),
    conclusion: str | None = Query(default=None, description="准入通过、准入驳回、准入回退"),
    instance_no: str | None = Query(default=None, description="按实例编号筛选"),
    node: str | None = Query(default=None, description="按运行节点筛选"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """重启准入数据清单：条件筛选、关键词检索与分页定位，读取固化快照。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if conclusion and conclusion not in CONCLUSIONS:
        raise HTTPException(status_code=400, detail=f"准入结论仅支持：{'、'.join(CONCLUSIONS)}")
    items, total = service.list_entries(
        keyword=keyword,
        conclusion=conclusion,
        instance_no=instance_no,
        node=node,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出重启准入数据清单：跨业务链路共享同一份全量快照。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "restart_admission", "total": total, "items": items}


@router.get("/request/{request_no}", response_model=dict)
def get_by_request_no(request_no: str) -> dict:
    """按重启请求号核对幂等结论：重复提交前后读到的是同一份准入快照。"""
    entry = service.get_by_request_no(request_no)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"重启请求 {request_no} 尚未生成准入单")
    return entry


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """准入单详情：与列表读取同一份检查点版本与快照，结论不会二次计算。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"准入单 {entry_id} 不存在或已归档")
    return entry


@router.post("/submit", response_model=ActionResult)
def submit_admission(payload: EntryPayload) -> ActionResult:
    """提交重启准入：联合校验检查点可恢复、状态后端生效与节点 slot 容量。

    必填 values：instance_id（作业实例 id）、request_no（重启请求号，幂等键）。
    选填：checkpoint_id、state_id、required_slots，缺省取实例当前绑定配置。
    驳回时 ok=False 但准入单与处理日志已落库；重复请求号只生效一次。
    """
    values = payload.values
    try:
        instance_id = int(values.get("instance_id"))
    except (TypeError, ValueError):
        return ActionResult(ok=False, message="缺少 instance_id 或其不是整数，无法定位作业实例")
    request_no = str(values.get("request_no") or "").strip()
    checkpoint_id = values.get("checkpoint_id")
    state_id = values.get("state_id")
    required_slots = values.get("required_slots")
    entry, message, first_hit = service.submit(
        instance_id=instance_id,
        request_no=request_no,
        checkpoint_id=int(checkpoint_id) if checkpoint_id is not None else None,
        state_id=int(state_id) if state_id is not None else None,
        required_slots=int(required_slots) if required_slots is not None else None,
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    ok = entry.get("准入结论") == CONCLUSION_PASS
    prefix = "" if first_hit else "（幂等命中，未重复生效）"
    return ActionResult(ok=ok, message=message + prefix, entry=entry)


@router.post("/{entry_id}/rollback", response_model=ActionResult)
def rollback_admission(entry_id: int, payload: EntryPayload) -> ActionResult:
    """恢复失败回退：准入清单结论、实例状态与节点 slot 占用一起退回。"""
    fail_reason = str(payload.values.get("fail_reason") or "恢复过程失败").strip()
    entry, message = service.rollback_recovery(entry_id, fail_reason=fail_reason)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
