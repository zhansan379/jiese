"""链路追踪（参考 ragent framework/trace + RagTraceAspect + StreamChatTraceRunner）。

- 两级模型：一次问答 = 一个 Run（trace_runs），管线各阶段 = Node（trace_nodes）
- TraceContext：contextvars 保存 traceId 与节点栈，等价 ragent 的
  TransmittableThreadLocal（asyncio task 与 run_in_threadpool 工作线程均继承上下文）
- 节点先落 RUNNING 行、结束时更新状态与耗时（SUCCESS/ERROR），与 ragent
  startNode/finishNode 一致；Run 收尾记录总耗时与异常
- Run 状态机：RUNNING / SUCCESS / ERROR / CANCELLED（客户端断开）
- 节点类型对齐 ragent：REWRITE / INTENT / RETRIEVE / LLM / USER_TTFT
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool

from .config import settings
from .db import conn_ctx

STATUS_RUNNING = "RUNNING"
STATUS_SUCCESS = "SUCCESS"
STATUS_ERROR = "ERROR"
STATUS_CANCELLED = "CANCELLED"

_trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)
_node_stack_var: ContextVar[list[str] | None] = ContextVar("node_stack", default=None)


class TraceContext:
    """traceId + 节点栈的请求上下文（push/pop 决定节点的父节点与深度）。"""

    @staticmethod
    def get_trace_id() -> str | None:
        return _trace_id_var.get()

    @staticmethod
    def set_trace_id(trace_id: str | None) -> None:
        _trace_id_var.set(trace_id)

    @staticmethod
    def depth() -> int:
        stack = _node_stack_var.get()
        return len(stack) if stack else 0

    @staticmethod
    def current_node() -> str | None:
        stack = _node_stack_var.get()
        return stack[-1] if stack else None

    @staticmethod
    def push_node(node_id: str) -> None:
        stack = list(_node_stack_var.get() or [])
        stack.append(node_id)
        _node_stack_var.set(stack)

    @staticmethod
    def pop_node() -> None:
        stack = _node_stack_var.get()
        if stack:
            _node_stack_var.set(stack[:-1])

    @staticmethod
    def clear() -> None:
        _trace_id_var.set(None)
        _node_stack_var.set(None)


def _truncate_error(error: str | None) -> str | None:
    if error is None:
        return None
    message = str(error)
    return message[: settings.trace_max_error_length]


# ---------------- 同步落库（线程池中执行，短事务 autocommit） ----------------

def _db_start_run(row: dict) -> None:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trace_runs (trace_id, trace_name, entry_method, session_id,"
            " status, start_time, extra_data)"
            " VALUES (%(trace_id)s, %(trace_name)s, %(entry_method)s, %(session_id)s,"
            " 'RUNNING', %(start_time)s, %(extra_data)s)"
            " ON CONFLICT (trace_id) DO NOTHING",
            row,
        )


def _db_finish_run(args: tuple) -> None:
    trace_id, status, error, end_time, duration_ms = args
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trace_runs SET status=%s, error_message=%s, end_time=%s, duration_ms=%s"
            " WHERE trace_id=%s",
            (status, _truncate_error(error), end_time, duration_ms, trace_id),
        )


def _db_start_node(row: dict) -> None:
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO trace_nodes (trace_id, node_id, parent_node_id, depth,"
            " node_type, node_name, status, start_time, extra_data)"
            " VALUES (%(trace_id)s, %(node_id)s, %(parent_node_id)s, %(depth)s,"
            " %(node_type)s, %(node_name)s, 'RUNNING', %(start_time)s, %(extra_data)s)"
            " ON CONFLICT (trace_id, node_id) DO NOTHING",
            row,
        )


def _db_finish_node(args: tuple) -> None:
    trace_id, node_id, status, error, end_time, duration_ms, extra = args
    with conn_ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE trace_nodes SET status=%s, error_message=%s, end_time=%s,"
            " duration_ms=%s, extra_data = extra_data || %s::jsonb"
            " WHERE trace_id=%s AND node_id=%s",
            (status, _truncate_error(error), end_time, duration_ms,
             json.dumps(extra, ensure_ascii=False) if extra else "{}", trace_id, node_id),
        )


@dataclass
class NodeHandle:
    """节点句柄：执行期间可通过 set_extra 补充 extra_data（finish 时合并落库）。"""

    extras: dict = field(default_factory=dict)

    def set_extra(self, **kv) -> None:
        self.extras.update(kv)


@dataclass
class _NoopHandle:
    def set_extra(self, **kv) -> None:  # noqa: ARG002 追踪关闭时的空实现
        pass


_NOOP_HANDLE = _NoopHandle()


class TraceRecorder:
    """一次问答链路的记录器（ragent StreamChatTraceRunner 的对应物）。

    追踪关闭时 chat 管线拿到 None，trace_node 直接 no-op，零开销。
    """

    def __init__(self, *, trace_name: str, entry_method: str, session_id: str | None,
                 extra: dict | None = None) -> None:
        self.trace_id = uuid.uuid4().hex
        self.trace_name = trace_name
        self.entry_method = entry_method
        self.session_id = session_id
        self.run_extra = extra or {}
        self._t0 = time.monotonic()
        self._start_time = datetime.now(timezone.utc)
        self._finished = False
        self._ttft_done = False

    async def start_run(self) -> None:
        TraceContext.set_trace_id(self.trace_id)
        try:
            await run_in_threadpool(_db_start_run, {
                "trace_id": self.trace_id,
                "trace_name": self.trace_name,
                "entry_method": self.entry_method,
                "session_id": self.session_id,
                "start_time": self._start_time,
                "extra_data": json.dumps(self.run_extra, ensure_ascii=False),
            })
        except Exception as e:  # noqa: BLE001  追踪写入绝不影响主链路
            print(f"[trace] 写入 Run 失败: {e}")

    async def record_ttft(self) -> None:
        """用户感知首包 TTFT：Run 开始到第一条内容推给前端（ragent user-first-packet 节点）。"""
        if self._ttft_done or self._finished:
            return
        self._ttft_done = True
        now = datetime.now(timezone.utc)
        duration_ms = max(0, int((time.monotonic() - self._t0) * 1000))
        node_id = uuid.uuid4().hex
        try:
            await run_in_threadpool(_db_start_node, {
                "trace_id": self.trace_id,
                "node_id": node_id,
                "parent_node_id": None,
                "depth": 0,
                "node_type": "USER_TTFT",
                "node_name": "user-first-packet",
                "start_time": self._start_time,
                "extra_data": "{}",
            })
            await run_in_threadpool(_db_finish_node, (
                self.trace_id, node_id, STATUS_SUCCESS, None, now, duration_ms, {},
            ))
        except Exception as e:  # noqa: BLE001
            print(f"[trace] 写入 TTFT 节点失败: {e}")

    async def finish(self, status: str, error: str | None = None) -> None:
        """收尾 Run。落库是同步短写（无 await，不设取消点）：

        客户端断开时 Starlette 已取消所在 task，任何 await 都会立即再抛
        CancelledError 导致收尾写不进去，Run 永远停在 RUNNING——同步写保证
        CANCELLED/ERROR/SUCCESS 状态总能落库（单条 UPDATE，事件循环阻塞可忽略）。
        """
        if self._finished:
            return
        self._finished = True
        duration_ms = max(0, int((time.monotonic() - self._t0) * 1000))
        try:
            _db_finish_run((self.trace_id, status, error, datetime.now(timezone.utc), duration_ms))
        except Exception as e:  # noqa: BLE001
            print(f"[trace] 收尾 Run 失败: {e}")
        finally:
            TraceContext.clear()


@asynccontextmanager
async def trace_node(recorder: "TraceRecorder | None", name: str, node_type: str,
                     extra: dict | None = None):
    """节点级追踪上下文（ragent @RagTraceNode AOP 的显式等价物）。

    recorder 为 None（追踪关闭）时整个块 no-op；节点先落 RUNNING 行，
    正常结束 SUCCESS、抛异常 ERROR（记录异常摘要后原样上抛）。
    """
    if recorder is None:
        yield _NOOP_HANDLE
        return

    node_id = uuid.uuid4().hex
    start_time = datetime.now(timezone.utc)
    t0 = time.monotonic()
    handle = NodeHandle(extras=dict(extra or {}))
    try:
        await run_in_threadpool(_db_start_node, {
            "trace_id": recorder.trace_id,
            "node_id": node_id,
            "parent_node_id": TraceContext.current_node(),
            "depth": TraceContext.depth(),
            "node_type": node_type,
            "node_name": name,
            "start_time": start_time,
            "extra_data": json.dumps(handle.extras, ensure_ascii=False),
        })
    except Exception as e:  # noqa: BLE001
        print(f"[trace] 写入节点失败: {e}")

    TraceContext.push_node(node_id)
    try:
        yield handle
    except asyncio.CancelledError:
        await _finish_node(recorder, node_id, STATUS_CANCELLED, None, t0, handle)
        raise
    except Exception as e:  # noqa: BLE001
        await _finish_node(recorder, node_id, STATUS_ERROR, f"{type(e).__name__}: {e}", t0, handle)
        raise
    else:
        await _finish_node(recorder, node_id, STATUS_SUCCESS, None, t0, handle)
    finally:
        TraceContext.pop_node()


async def _finish_node(recorder: TraceRecorder, node_id: str, status: str,
                       error: str | None, t0: float, handle: NodeHandle) -> None:
    duration_ms = max(0, int((time.monotonic() - t0) * 1000))
    try:
        await run_in_threadpool(_db_finish_node, (
            recorder.trace_id, node_id, status, error, datetime.now(timezone.utc), duration_ms, handle.extras,
        ))
    except Exception as e:  # noqa: BLE001
        print(f"[trace] 更新节点失败: {e}")
