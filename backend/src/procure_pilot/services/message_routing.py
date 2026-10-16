"""Route user messages before touching requirement state or resuming a checkpoint."""

from __future__ import annotations

import asyncio
import hashlib
import re
from uuid import uuid4

from procure_pilot.agents.tools import AgentRuntimeContext
from procure_pilot.domain.conversation import (
    MessageRoute,
    MessageRouteProposal,
    MessageRouteRecord,
    MessageRouteResult,
)
from procure_pilot.domain.execution import AgentExecution, ExecutionStatus
from procure_pilot.domain.models import RunResponse, RunState
from procure_pilot.harness.runtime import HarnessBudgetExceeded, RunNotFoundError


def classify_message_rules(message: str, category: str | None) -> MessageRouteProposal:
    """Conservative offline baseline. Ambiguity never resets an existing requirement."""
    text = re.sub(r"\s+", "", message.lower())
    if re.search(
        r"(?:(?:不要|不用|不想|别)(?:再|现在)?(?:重新|重来|新需求|新采购|另开|换需求)|(?:不是|并非)(?:要)?(?:新需求|新采购|另开|换需求))",
        text,
    ):
        return MessageRouteProposal(
            route="CONTINUE", confidence=1, reason="用户明确表示不重新开始采购"
        )
    if re.search(
        r"(?:新(?:的)?(?:采购)?需求|新(?:的)?采购|重新(?:采购|开始|买)|另开|另外(?:采购|买)|换(?:个|一个)?需求|改(?:成)?买)",
        text,
    ):
        return MessageRouteProposal(
            route="NEW", confidence=1, reason="用户明确提出独立采购或替换当前需求"
        )
    if re.search(
        r"(?:这批|这单|原来|之前|上一批|上一个).{0,8}(?:不要了|不买了|先不买|取消|先放一边)", text
    ):
        return MessageRouteProposal(
            route="NEW", confidence=1, reason="用户明确放下当前采购，重新收集新需求"
        )
    if re.search(
        r"(?:预算|数量|内存|存储|硬盘|交期|交付|地址|城市).{0,4}(?:改成|改为|调整为|换成)", text
    ):
        return MessageRouteProposal(
            route="CONTINUE", confidence=1, reason="用户明确修改当前采购字段"
        )
    if re.search(r"解释|为什么|理由|依据|对比|比较|区别|接受方案|同意方案|采用方案|便宜|价格优先|成本优先|快点|快一点|保修优先|综合优先|重试查询|第[一二三123]款|库存|保修|质保", text):
        return MessageRouteProposal(route="CONTINUE", confidence=0.95, reason="用户对当前推荐或协商方案继续对话")
    if re.search(r"(?:换|推荐|选)(?:另)?(?:一个|个|其他)?(?:型号|款|品牌)|其他候选", text):
        return MessageRouteProposal(
            route="CONTINUE", confidence=0.95, reason="用户在当前采购中切换候选或型号"
        )
    if re.search(r"(?:再买|再采购|另外一批|换一个|换个|换一批)", text):
        return MessageRouteProposal(
            route="CLARIFY", confidence=0.5, reason="需要区分调整当前采购与开始另一笔采购"
        )
    categories = {
        "laptop": r"笔记本|电脑|laptop",
        "monitor": r"显示器|monitor",
        "phone": r"手机|phone",
    }
    mentioned = {key for key, pattern in categories.items() if re.search(pattern, text)}
    if category is None and len(mentioned) == 1:
        return MessageRouteProposal(
            route="CONTINUE", confidence=1, reason="用户回答当前待澄清的商品品类"
        )
    if category and len(mentioned) == 1 and category not in mentioned:
        if re.search(r"采购|购买|买\d|买一|买几|买台", text):
            return MessageRouteProposal(route="NEW", confidence=0.95, reason="用户提出采购不同品类")
        return MessageRouteProposal(
            route="CLARIFY", confidence=0.5, reason="提到了其他品类，但尚不明确是否切换采购"
        )
    if re.search(r"制度|公司标准|团队标准|研发标准|历史|上次|参考订单|HIST-|确认需求|^确认[。！!]?$|^可以[。！!]?$", message, re.I):
        return MessageRouteProposal(route="CONTINUE", confidence=1, reason="继续当前采购的参考依据、历史选择或需求确认")
    if re.search(
        r"预算|含税|未税|内存|硬盘|存储|交付|到货|送到|送雅|雅加达|上海|深圳|公司标准|团队标准|数量|还是|继续|补充|不换|\d+\s*(?:台|件|gb|tb|元)",
        text,
    ):
        return MessageRouteProposal(
            route="CONTINUE", confidence=0.95, reason="用户补充或调整当前采购条件"
        )
    return MessageRouteProposal(
        route="CLARIFY", confidence=0.4, reason="尚无法判断是在继续当前采购还是开始新需求"
    )


class ProcurementMessageRouter:
    def __init__(self, engine, runtime):
        self.engine = engine
        self.runtime = runtime
        self._locks: dict[str, asyncio.Lock] = {}

    async def route(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        message: str,
        scenario: str | None = None,
        request_id: str | None = None,
        route_choice: str | None = None,
        expected_requirement_version: int | None = None,
        expected_recommendation_version: int | None = None,
        expected_resolution_draft_id: str | None = None,
        expected_resolution_version: int | None = None,
    ) -> RunResponse:
        async with self._locks.setdefault(run_id, asyncio.Lock()):
            run = self.engine.get(run_id, tenant_id=tenant_id, user_id=user_id)
            if route_choice not in {None, "CONTINUE", "NEW"}:
                raise ValueError("Invalid route choice")
            request_id = request_id or f"route_{uuid4().hex}"
            record = next(
                (item for item in run.message_routes if item.request_id == request_id), None
            )
            if run.state not in {
                RunState.WAITING_USER,
                RunState.WAITING_CONFIRMATION,
                RunState.FAILED,
                RunState.DRAFT_CREATED,
            } and (record is None or record.status not in {"DISPATCHING", "COMPLETED"}):
                raise ValueError(
                    "Workflow cannot route a message while a procurement step is running"
                )
            if record is not None:
                if (record.message != message or record.scenario != scenario
                    or record.expected_requirement_version != expected_requirement_version
                    or record.expected_recommendation_version != expected_recommendation_version
                    or record.expected_resolution_draft_id != expected_resolution_draft_id
                    or record.expected_resolution_version != expected_resolution_version):
                    raise ValueError("Message request_id was already used for different input")
                if record.status == "COMPLETED":
                    if route_choice is not None and route_choice != record.proposal.route:
                        raise ValueError("This request already has a different routing decision")
                    return self._completed_response(run, record, tenant_id, user_id)
                if (
                    record.status == "DISPATCHING"
                    and route_choice is not None
                    and route_choice != record.proposal.route
                ):
                    raise ValueError("This request is already dispatching to a different target")
                if record.status == "CLARIFY" and route_choice is None:
                    return self._clarification_response(run, record)
            elif route_choice is not None:
                raise ValueError("A route choice requires the matching pending request")
            else:
                if expected_requirement_version is not None and expected_requirement_version != run.requirement.version:
                    raise ValueError("Requirement version is stale; refresh before replying")
                if expected_recommendation_version is not None and expected_recommendation_version != run.recommendation_version:
                    raise ValueError("Recommendation version is stale; refresh before replying")
                if expected_resolution_draft_id is not None and (run.resolution_draft is None or run.resolution_draft.draft_id != expected_resolution_draft_id):
                    raise ValueError("Requirement resolution draft is stale")
                if expected_resolution_version is not None and (run.resolution_draft is None or run.resolution_draft.version != expected_resolution_version):
                    raise ValueError("Requirement resolution draft version is stale")
                proposal, source = await self._classify(run, message, request_id)
                if proposal.confidence < 0.8:
                    proposal.route = MessageRoute.CLARIFY
                if (
                    run.state in {RunState.DRAFT_CREATED, RunState.FAILED}
                    and proposal.route == MessageRoute.CONTINUE
                ):
                    proposal = MessageRouteProposal(
                        route="CLARIFY",
                        confidence=1,
                        reason="当前采购已经结束，请确认是否建立新采购",
                    )
                record = MessageRouteRecord(
                    request_id=request_id,
                    message=message,
                    scenario=scenario,
                    message_id=f"msg_{uuid4().hex[:12]}",
                    base_state_version=run.state_version,
                    base_requirement_version=run.requirement.version,
                    base_recommendation_version=run.recommendation_version,
                    expected_requirement_version=expected_requirement_version,
                    expected_recommendation_version=expected_recommendation_version,
                    expected_resolution_draft_id=expected_resolution_draft_id,
                    expected_resolution_version=expected_resolution_version,
                    base_resolution_draft_id=run.resolution_draft.draft_id if run.resolution_draft else None,
                    base_resolution_version=run.resolution_draft.version if run.resolution_draft else 0,
                    proposal=proposal,
                    source=source,
                    status="CLARIFY" if proposal.route == MessageRoute.CLARIFY else "DISPATCHING",
                )
                run.message_routes.append(record)

            if route_choice is not None and record.status == "CLARIFY":
                if record.status != "CLARIFY" or route_choice not in {"CONTINUE", "NEW"}:
                    raise ValueError("This message is not awaiting a route choice")
                if (
                    run.state_version != record.base_state_version
                    or run.requirement.version != record.base_requirement_version
                    or run.recommendation_version != record.base_recommendation_version
                    or (run.resolution_draft.draft_id if run.resolution_draft else None) != record.base_resolution_draft_id
                    or (run.resolution_draft.version if run.resolution_draft else 0) != record.base_resolution_version
                ):
                    raise ValueError(
                        "The procurement changed; resend the message against the latest state"
                    )
                if route_choice == "CONTINUE" and run.state in {
                    RunState.DRAFT_CREATED,
                    RunState.FAILED,
                }:
                    raise ValueError("A completed procurement cannot accept requirement changes")
                record.proposal = MessageRouteProposal(
                    route=route_choice, confidence=1, reason="用户明确选择消息归属"
                )
                record.source = "user"
                record.status = "DISPATCHING"

            if record.proposal.route == MessageRoute.CLARIFY:
                self.engine.harness.trace(
                    run,
                    actor="Message Router",
                    kind="SERVICE",
                    name="message.route_clarification",
                    detail={"request_id": request_id, "route": "CLARIFY", "source": record.source},
                )
                self.engine.repository.save(run)
                return self._clarification_response(run, record)
            if record.target_run_id is None:
                record.target_run_id = (
                    f"run_{uuid4().hex[:12]}"
                    if record.proposal.route == MessageRoute.NEW
                    else run.run_id
                )
            self.engine.harness.trace(
                run,
                actor="Message Router",
                kind="SERVICE",
                name="message.route_decided",
                detail={
                    "request_id": request_id,
                    "route": record.proposal.route,
                    "target_run_id": record.target_run_id,
                    "source": record.source,
                },
            )
            self.engine.repository.save(run)
            response = await self._dispatch(run, record, tenant_id, user_id)
            # The workflow may have changed this Run, so reload before saving the receipt.
            source_run = self.engine.get(run_id, tenant_id=tenant_id, user_id=user_id)
            saved = next(
                item for item in source_run.message_routes if item.request_id == request_id
            )
            saved.status = "COMPLETED"
            saved.assistant_message = response.assistant_message
            self.engine.repository.save(source_run)
            if response.run.run_id == source_run.run_id:
                response.run = source_run
            response.message_route = self._result(source_run, saved)
            return response

    async def _classify(self, run, message, request_id):
        category = (
            run.requirement_spec.category.normalized_value
            if run.requirement_spec and run.requirement_spec.category
            else None
        )
        if run.requirement_spec and any(
            item.field == "category" for item in run.requirement_spec.missing_fields
        ):
            category = None
        rules = classify_message_rules(message, category)
        if run.recommendation and message.strip() in {"那款", "这款", "这个", "那个"}:
            rules = MessageRouteProposal(route="CONTINUE", confidence=1, reason="继续当前推荐，具体候选指代由对话阶段澄清")
        from procure_pilot.services.requirement_resolution import is_confirmation_message
        if run.wait_reason is not None and run.wait_reason.value == "REQUIREMENT_CONFIRMATION" and is_confirmation_message(message):
            rules = MessageRouteProposal(route="CONTINUE", confidence=1, reason="确认当前展示的需求草案")
        method = getattr(self.engine.planner, "classify_message", None)
        if method is None:
            return rules, "deterministic"
        payload = {
            "request_id": request_id,
            "message": message,
            "category": category,
            "requirement": run.requirement.model_dump(mode="json"),
            "has_recommendation": run.recommendation is not None,
            "has_negotiation_options": bool(run.negotiation_options),
            "recent_messages": [
                {"role": item.role, "content": item.content} for item in run.messages[-6:]
            ],
        }
        fingerprint = hashlib.sha256(repr(payload).encode()).hexdigest()
        execution = self.engine.execution_repository.claim_agent(
            AgentExecution(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.run_id,
                node_name="message_router",
                agent_kind="REQUIREMENT",
                task_id=f"route_{request_id}"[:80],
                state_version=run.state_version,
                input_fingerprint=fingerprint,
                idempotency_key=f"message-route:{run.run_id}:{request_id}:{run.state_version}:{run.requirement.version}",
            )
        )
        if execution.status == ExecutionStatus.SUCCEEDED and execution.result_json is not None:
            return MessageRouteProposal.model_validate(execution.result_json), "model"
        context = AgentRuntimeContext(
            run=run,
            catalog=self.engine.catalog,
            enterprise=self.engine.enterprise,
            harness=self.engine.harness,
            roles=self.engine.enterprise.identity(run.tenant_id, run.user_id).roles,
            tool_gateway=self.engine.tool_gateway,
            model_gateway=self.engine.model_gateway,
            agent_kind="REQUIREMENT",
            requirement_spec=run.requirement_spec,
        )
        try:
            with self.engine.harness.agent_execution(execution.execution_id):
                proposal = await self.engine.harness.execute_agent_turn(
                    run, lambda: method(payload, context=context)
                )
            proposal = MessageRouteProposal.model_validate(proposal)
            # Negations and explicit fresh-start statements cannot be reversed by a model.
            if rules.confidence == 1:
                proposal = rules
            self.engine.execution_repository.complete_agent(
                execution.execution_id, proposal.model_dump(mode="json")
            )
            return proposal, "model"
        except Exception as exc:
            self.engine.execution_repository.fail_agent(execution.execution_id, type(exc).__name__)
            self.engine.harness.trace(
                run,
                actor="Message Router",
                kind="SERVICE",
                name="message.route_fallback",
                detail={"error_type": type(exc).__name__},
                status="DEGRADED",
            )
            if isinstance(exc, HarnessBudgetExceeded):
                return MessageRouteProposal(
                    route="CLARIFY",
                    confidence=1,
                    reason="当前任务的模型额度不足，请明确选择消息归属",
                ), "fallback"
            return rules, "fallback"

    async def _dispatch(self, run, record, tenant_id, user_id):
        try:
            # The target ID is generated by this router and stored in an authorized receipt.
            # SQL scoped reads intentionally hide absence as PermissionError; distinguish
            # absence internally, then perform the normal owner-scoped read before use.
            self.engine.repository.get(record.target_run_id)
        except RunNotFoundError:
            target = None
        else:
            target = self.engine.get(record.target_run_id, tenant_id=tenant_id, user_id=user_id)
        accepted = target is not None and any(
            item.message_id == record.message_id for item in target.messages
        )
        if target is not None and (record.proposal.route == MessageRoute.NEW or accepted):
            if target.state in {
                RunState.WAITING_USER,
                RunState.WAITING_CONFIRMATION,
                RunState.DRAFT_CREATED,
                RunState.FAILED,
            }:
                return RunResponse(
                    run=target,
                    assistant_message=next(
                        (
                            item.content
                            for item in reversed(target.messages)
                            if item.role == "assistant"
                        ),
                        target.error or "采购任务已恢复。",
                    ),
                )
            return await self.runtime.resume_pending(
                run_id=target.run_id, tenant_id=tenant_id, user_id=user_id
            )
        if record.proposal.route == MessageRoute.NEW:
            return await self.runtime.start_run(
                run_id=record.target_run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                message=record.message,
                scenario=record.scenario,
            )
        if (
            run.state_version != record.base_state_version
            or run.requirement.version != record.base_requirement_version
                    or run.recommendation_version != record.base_recommendation_version
                    or (run.resolution_draft.draft_id if run.resolution_draft else None) != record.base_resolution_draft_id
                    or (run.resolution_draft.version if run.resolution_draft else 0) != record.base_resolution_version
        ):
            raise ValueError("The procurement changed before this message was accepted")
        return await self.runtime.continue_run(
            run_id=run.run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            message=record.message,
            message_id=record.message_id,
            scenario=record.scenario,
        )

    @staticmethod
    def _result(run, record):
        choices = (
            (
                ["NEW"]
                if run.state in {RunState.DRAFT_CREATED, RunState.FAILED}
                else ["CONTINUE", "NEW"]
            )
            if record.status == "CLARIFY"
            else []
        )
        return MessageRouteResult(
            request_id=record.request_id,
            route=record.proposal.route,
            origin_run_id=run.run_id,
            target_run_id=record.target_run_id,
            reason=record.proposal.reason,
            source=record.source,
            choices=choices,
        )

    def _clarification_response(self, run, record):
        message = "这是补充／调整当前采购，还是开始一笔新的采购？新采购不会继承当前条件。"
        if run.state in {RunState.DRAFT_CREATED, RunState.FAILED}:
            message = "当前采购已经结束。是否用这条消息开始一笔新的采购？"
        return RunResponse(
            run=run, assistant_message=message, message_route=self._result(run, record)
        )

    def _completed_response(self, run, record, tenant_id, user_id):
        target = self.engine.get(record.target_run_id, tenant_id=tenant_id, user_id=user_id)
        return RunResponse(
            run=target,
            assistant_message=next(
                (item.content for item in reversed(target.messages) if item.role == "assistant"),
                target.error or record.assistant_message,
            ),
            message_route=self._result(run, record),
        )
