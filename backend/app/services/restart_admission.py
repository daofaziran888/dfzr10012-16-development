"""重启准入业务规则：重启前对检查点与状态后端做联合准入。

口径约束（与业务验收保持一致）：
- 先完成检查点可恢复、状态后端生效、节点 slot 容量全部校验，再决定是否占用 slot；
  任何一项不满足都只写处理日志与准入清单，不允许先占 slot 再补做检查点校验。
- 同一重启请求号（幂等键）并发提交只生效一次，重复提交返回首次生成的准入单。
- 准入结论与检查点版本在生成时固化为快照，列表与详情读取同一份快照，重新查询结论不变。
- 恢复失败时准入清单结论回退、实例状态与节点 slot 占用一起回退，处理日志保留可追溯。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "restart_admission"
JOBO_MODULE = "jobops"
CHEC_MODULE = "checkpoint"
STAT_MODULE = "state"
NODE_MODULE = "node_capacity"
LOG_MODULE = "log"

# 检查点只有处于可恢复状态才允许作为恢复入口。
RECOVERABLE_CHEC_STATUS = "已创建"
RECOVERABLE_CHEC_RECORD_STATUS = "可恢复"
# 状态后端必须处于启用态，保证检查点快照有落地载体。
EFFECTIVE_STAT_STATUS = "已启用"

CONCLUSION_PASS = "准入通过"
CONCLUSION_DENY = "准入驳回"
CONCLUSION_ROLLBACK = "准入回退"

# 同一进程内的串行化锁：内存仓库下保证并发提交的判定与落库是一个临界区。
_submit_lock = threading.RLock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class RestartAdmissionService:
    """重启准入数据清单的查询、准入判定与回退都收敛在本服务。"""

    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        conclusion: str | None = None,
        instance_no: str | None = None,
        node: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """条件筛选 + 关键词检索 + 分页定位，返回的是固化后的准入快照。"""
        rows = store.rows(MODULE)
        if conclusion:
            rows = [row for row in rows if row.get("准入结论") == conclusion]
        if instance_no:
            rows = [row for row in rows if instance_no in str(row.get("实例编号", ""))]
        if node:
            rows = [row for row in rows if node in str(row.get("运行节点", ""))]
        if keyword:
            # 关键词覆盖单号、请求号、实例与检查点编号，便于跨链路追溯。
            fields = ["准入单号", "重启请求号", "实例编号", "检查点编号", "检查点版本", "作业名称"]
            rows = [
                row
                for row in rows
                if any(keyword in str(row.get(field, "")) for field in fields)
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def get_by_request_no(self, request_no: str) -> dict[str, Any] | None:
        """按重启请求号（幂等键）定位首次准入单。"""
        for row in store.rows(MODULE):
            if row.get("重启请求号") == request_no:
                return row
        return None

    def evaluate_instance(self, entry_id: int) -> dict[str, Any] | None:
        """作业实例侧的只读核对：按运行节点、重启次数与恢复条件核对当前生效准入结论。"""
        instance = store.find(JOBO_MODULE, entry_id)
        if instance is None:
            return None
        latest = self._latest_for_instance(str(instance.get("实例编号", "")))
        return {
            "实例编号": instance.get("实例编号"),
            "运行节点": instance.get("运行节点"),
            "重启次数": instance.get("重启次数"),
            "恢复条件": instance.get("恢复条件"),
            "实例状态": instance.get("实例状态"),
            "当前准入单号": latest.get("准入单号") if latest else None,
            "准入结论": latest.get("准入结论") if latest else "暂无准入记录",
            "检查点版本": latest.get("检查点版本") if latest else None,
            "缺失slot": latest.get("缺失slot") if latest else None,
            "快照版本": latest.get("快照版本") if latest else None,
        }

    def latest_for_instance(self, instance_no: str) -> dict[str, Any] | None:
        """返回某实例最新一张准入单，供作业运维等跨链路消费侧核对当前生效结论。"""
        return self._latest_for_instance(instance_no)

    def _latest_for_instance(self, instance_no: str) -> dict[str, Any] | None:
        matched = [row for row in store.rows(MODULE) if row.get("实例编号") == instance_no]
        if not matched:
            return None
        return max(matched, key=lambda row: int(row.get("id", 0)))

    # ------------------------------------------------------------------ 准入
    def submit(
        self,
        *,
        instance_id: int,
        request_no: str,
        checkpoint_id: int | None = None,
        state_id: int | None = None,
        required_slots: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """提交一次重启准入。

        返回 (准入单, 消息, 是否首次生效)。重复请求号不会再次生效，返回首次准入单。
        全程在锁内完成“读取—校验—落库—占用”，保证并发提交只生效一次。
        """
        request_no = (request_no or "").strip()
        if not request_no:
            return None, "重启请求号不能为空，无法生成可审计的准入单", False

        with _submit_lock:
            existed = self.get_by_request_no(request_no)
            if existed is not None:
                return existed, f"重启请求 {request_no} 已提交过，按幂等口径返回首次准入单 {existed.get('准入单号')}", False

            instance = store.find(JOBO_MODULE, instance_id)
            if instance is None:
                return None, f"作业实例 {instance_id} 不存在或已归档，无法发起重启准入", False

            # 入参缺省时回退到实例当前绑定的检查点/状态后端，保证联合读取口径统一。
            checkpoint = self._resolve_checkpoint(instance, checkpoint_id)
            state_conf = self._resolve_state(instance, state_id)
            need = int(required_slots if required_slots is not None else instance.get("需要slot数", 0) or 0)
            if need <= 0:
                return None, "需要 slot 数必须为正整数，无法做容量准入", False

            node = self._find_node(str(instance.get("运行节点", "")))
            free_slots = int(node.get("空闲slot", 0)) if node else 0
            missing_slots = max(need - free_slots, 0)

            checks = self._joint_checks(instance, checkpoint, state_conf, node, need)
            passed = all(item["passed"] for item in checks)
            conclusion = CONCLUSION_PASS if passed else CONCLUSION_DENY
            reason = "；".join(item["reason"] for item in checks if not item["passed"]) or (
                "检查点可恢复且状态后端生效，节点 slot 满足恢复需求"
            )

            snapshot = self._next_snapshot_version()
            admission = self._build_admission(
                instance=instance,
                request_no=request_no,
                checkpoint=checkpoint,
                state_conf=state_conf,
                node=node,
                need=need,
                free_slots=free_slots,
                missing_slots=missing_slots,
                conclusion=conclusion,
                reason=reason,
                snapshot=snapshot,
            )

            if passed:
                # 全部校验通过后才允许占用 slot，杜绝先占用再补校验。
                self._occupy_slots(node, need)
                admission["节点空闲slot"] = int(node["空闲slot"])
                self._advance_instance(instance, restarting=True)
                admission["生效时间"] = _now()
            else:
                # 驳回：只写处理日志，实例状态与 slot 账本保持不动。
                admission["生效时间"] = "—"

            store.rows(MODULE).append(admission)
            log_entry = self._write_log(admission, denied=not passed)
            admission["处理日志编号"] = log_entry["记录编号"]
            instance["最近准入单号"] = admission["准入单号"]
            return admission, f"{conclusion}：{reason}", True

    def _joint_checks(
        self,
        instance: dict[str, Any],
        checkpoint: dict[str, Any] | None,
        state_conf: dict[str, Any] | None,
        node: dict[str, Any] | None,
        need: int,
    ) -> list[dict[str, Any]]:
        """联合准入：检查点、状态后端、节点容量逐项校验，任一项失败即驳回。"""
        checks: list[dict[str, Any]] = []

        if checkpoint is None:
            checks.append({"passed": False, "reason": "实例未绑定检查点，缺少恢复入口"})
        else:
            chec_ok = (
                checkpoint.get("status") == RECOVERABLE_CHEC_STATUS
                and checkpoint.get("记录状态") == RECOVERABLE_CHEC_RECORD_STATUS
            )
            checks.append({
                "passed": chec_ok,
                "reason": "检查点处于可恢复状态" if chec_ok
                else f"检查点 {checkpoint.get('记录编号')} 状态为 {checkpoint.get('记录状态')}，不满足恢复条件",
            })

        if state_conf is None:
            checks.append({"passed": False, "reason": "实例未绑定状态后端，快照无落地载体"})
        else:
            stat_ok = state_conf.get("status") == EFFECTIVE_STAT_STATUS
            checks.append({
                "passed": stat_ok,
                "reason": f"状态后端 {state_conf.get('配置编号')} 生效中" if stat_ok
                else f"状态后端 {state_conf.get('配置编号')} 状态为 {state_conf.get('status')}，未生效",
            })

        if node is None:
            checks.append({"passed": False, "reason": f"运行节点 {instance.get('运行节点')} 未登记容量账本"})
        else:
            free = int(node.get("空闲slot", 0))
            cap_ok = free >= need
            checks.append({
                "passed": cap_ok,
                "reason": f"节点 {node.get('节点名称')} 空闲 {free} 个 slot，满足需要的 {need} 个" if cap_ok
                else f"节点 {node.get('节点名称')} 仅 {node.get('slot总数')} 个 slot、空闲 {free} 个，缺少 {need - free} 个 slot",
            })
        return checks

    # ------------------------------------------------------------------ 回退
    def rollback_recovery(self, admission_id: int, *, fail_reason: str) -> tuple[dict[str, Any] | None, str]:
        """恢复失败：准入结论回退，实例状态与 slot 占用一起退回，日志保留。"""
        with _submit_lock:
            admission = store.find(MODULE, admission_id)
            if admission is None:
                return None, f"准入单 {admission_id} 不存在，无法回退"
            if admission.get("准入结论") != CONCLUSION_PASS:
                return None, f"准入单 {admission.get('准入单号')} 当前结论为 {admission.get('准入结论')}，只有准入通过的单据可回退"

            instance = self._find_by_biz_no(JOBO_MODULE, "实例编号", str(admission.get("实例编号")))
            node = self._find_node(str(admission.get("运行节点")))
            if instance is not None:
                self._advance_instance(instance, restarting=False)
                instance["重启次数"] = max(int(instance.get("重启次数", 0)) - 1, 0)
            if node is not None:
                self._release_slots(node, int(admission.get("需要slot数", 0)))
                admission["节点空闲slot"] = int(node["空闲slot"])

            admission["准入结论"] = CONCLUSION_ROLLBACK
            admission["status"] = CONCLUSION_ROLLBACK
            admission["abnormal"] = True
            admission["回退原因"] = fail_reason or "恢复失败，准入结论与实例状态一并回退"
            admission["生效时间"] = "—"
            self._write_log(admission, denied=True, fail_reason=fail_reason)
            return admission, f"准入单 {admission.get('准入单号')} 已回退，实例状态与 slot 占用已恢复"

    # ------------------------------------------------------------------ 内部
    def _resolve_checkpoint(
        self, instance: dict[str, Any], checkpoint_id: int | None
    ) -> dict[str, Any] | None:
        if checkpoint_id is not None:
            return store.find(CHEC_MODULE, checkpoint_id)
        return self._find_by_biz_no(CHEC_MODULE, "记录编号", str(instance.get("关联检查点", "")))

    def _resolve_state(
        self, instance: dict[str, Any], state_id: int | None
    ) -> dict[str, Any] | None:
        if state_id is not None:
            return store.find(STAT_MODULE, state_id)
        return self._find_by_biz_no(STAT_MODULE, "配置编号", str(instance.get("关联状态后端", "")))

    def _find_node(self, node_name: str) -> dict[str, Any] | None:
        for row in store.rows(NODE_MODULE):
            if row.get("节点名称") == node_name:
                return row
        return None

    def _find_by_biz_no(self, module: str, field: str, value: str) -> dict[str, Any] | None:
        if not value:
            return None
        for row in store.rows(module):
            if row.get(field) == value:
                return row
        return None

    def _occupy_slots(self, node: dict[str, Any], need: int) -> None:
        node["已占用slot"] = int(node.get("已占用slot", 0)) + need
        node["空闲slot"] = int(node.get("slot总数", 0)) - int(node["已占用slot"])

    def _release_slots(self, node: dict[str, Any], need: int) -> None:
        node["已占用slot"] = max(int(node.get("已占用slot", 0)) - need, 0)
        node["空闲slot"] = int(node.get("slot总数", 0)) - int(node["已占用slot"])

    def _advance_instance(self, instance: dict[str, Any], *, restarting: bool) -> None:
        if restarting:
            instance["重启次数"] = int(instance.get("重启次数", 0)) + 1
            instance["实例状态"] = "重启中"
            instance["status"] = "运行中"
        else:
            instance["实例状态"] = "运行中"
            instance["status"] = "运行中"

    def _next_admission_no(self) -> str:
        rows = store.rows(MODULE)
        return f"RST-{len(rows) + 1:04d}"

    def _next_snapshot_version(self) -> str:
        rows = store.rows(MODULE)
        return f"ADM-SNAP-{len(rows) + 1:04d}"

    def _build_admission(
        self,
        *,
        instance: dict[str, Any],
        request_no: str,
        checkpoint: dict[str, Any] | None,
        state_conf: dict[str, Any] | None,
        node: dict[str, Any] | None,
        need: int,
        free_slots: int,
        missing_slots: int,
        conclusion: str,
        reason: str,
        snapshot: str,
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        return {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": conclusion,
            "pending": conclusion == CONCLUSION_PASS,
            "abnormal": conclusion != CONCLUSION_PASS,
            "准入单号": self._next_admission_no(),
            "重启请求号": request_no,
            "实例编号": instance.get("实例编号"),
            "作业名称": instance.get("作业名称"),
            "运行节点": instance.get("运行节点"),
            "重启次数": int(instance.get("重启次数", 0)) + (1 if conclusion == CONCLUSION_PASS else 0),
            "恢复条件": instance.get("恢复条件"),
            "检查点编号": checkpoint.get("记录编号") if checkpoint else "—",
            # 检查点版本在准入时固化，后续检查点再打点也不改变本单结论。
            "检查点版本": checkpoint.get("检查点版本") if checkpoint else "—",
            "状态后端编号": state_conf.get("配置编号") if state_conf else "—",
            "状态后端版本": state_conf.get("配置版本") if state_conf else "—",
            "需要slot数": need,
            "节点slot总数": int(node.get("slot总数", 0)) if node else 0,
            "节点空闲slot": free_slots,
            # 可核对结果：缺失 slot 必须保留在清单里。
            "缺失slot": missing_slots,
            "准入结论": conclusion,
            "准入说明": reason,
            "快照版本": snapshot,
            "处理日志编号": "—",
            "创建时间": _now(),
            "生效时间": "—",
            "回退原因": "—",
        }

    def _write_log(
        self,
        admission: dict[str, Any],
        *,
        denied: bool,
        fail_reason: str | None = None,
    ) -> dict[str, Any]:
        """把准入处理结果写入共享日志中心，驳回/回退同样留痕。"""
        rows = store.rows(LOG_MODULE)
        if fail_reason is not None:
            level, content = "ERROR", (
                f"恢复失败回退：准入单 {admission.get('准入单号')} 结论退回为{CONCLUSION_ROLLBACK}，"
                f"原因：{fail_reason}；实例状态与 slot 占用已恢复"
            )
        elif denied:
            level = "WARN"
            content = (
                f"重启准入驳回：请求 {admission.get('重启请求号')} 对应实例 {admission.get('实例编号')}，"
                f"检查点 {admission.get('检查点编号')}（{admission.get('检查点版本')}），"
                f"节点 {admission.get('运行节点')} 缺失 {admission.get('缺失slot')} 个 slot；"
                f"仅写处理日志，不占用 slot、不变更实例状态。原因：{admission.get('准入说明')}"
            )
        else:
            level = "INFO"
            content = (
                f"重启准入通过：准入单 {admission.get('准入单号')}，检查点 {admission.get('检查点编号')}"
                f"（{admission.get('检查点版本')}）可恢复，节点 {admission.get('运行节点')} "
                f"分配 {admission.get('需要slot数')} 个 slot，实例进入重启中"
            )
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": "已记录",
            "pending": True,
            "abnormal": denied,
            "记录编号": f"LOG-{len(rows) + 1:04d}",
            "流作业名": admission.get("实例编号"),
            "日志级别": level,
            "日志内容": content,
            "发生时间": _now(),
            "异常类型": fail_reason or ("准入驳回" if denied else "—"),
            "关联准入单": admission.get("准入单号"),
            "处理状态": "待分析",
            "记录状态": "已记录",
        }
        rows.append(entry)
        return entry


service = RestartAdmissionService()
