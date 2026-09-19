"""LLM 客户端封装：OpenAI 兼容接口，统一走流式，重试策略见 `llm/retry.py`。"""
from __future__ import annotations

from contextlib import closing

from openai import BadRequestError, OpenAI

from .. import config, renderer

# 直接导入子模块：经包门面 `from ..agent import ...` 会惰性转发拉起整条重链（成环）
from ..agent.emitter import emit as emit_event
from ..agent.status import StatusChanged, StatusCleared
from ..cancel import current_token
from ..utils.proxy import normalize_proxy_env
from . import retry as retry_mod
from .request import ChatRequest, build_kwargs
from .retry import RetryPolicy
from .stream import parse_stream, usage_to_dict

# OpenAI SDK 的分页约定：`SyncPage.has_next_page/get_next_page`；旧版本或
# 兼容网关可能直接返回 list，`getattr` 为空即视为单页，退化为现状行为。
_MAX_MODEL_PAGES = 10


class LLMClient:
    """OpenAI 兼容客户端：显式构造参数 + `from_config()` 工厂。

    读全局 `config` 只发生在 `from_config()` 里：`TIMEOUT` / `URL` / 请求头
    在构造时固化（改完需重建 client）。`default_model` / `reasoning_effort`
    只是回退值——Agent 每轮 pin 一份 `TurnConfig` 经 `model` / `effort` 参数
    透传进来，轮级优先，构造值只在轮外调用（如后台标题）时生效。
    """

    # Agent 探测点：支持轮级参数（model / effort）透传的客户端置 True。
    # 测试替身（签名只有 messages/tools）保持 False，Agent 走旧 kwargs 组装。
    accepts_turn_params = True

    def __init__(self, *, api_key: str | None = None, base_url: str | None = None,
                 timeout: float = 120.0, default_model: str,
                 reasoning_effort: str | None = None,
                 custom_headers: dict | None = None,
                 session_id: str | None = None, client_factory=OpenAI):
        self.client = client_factory(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self.default_model = default_model
        self.reasoning_effort = reasoning_effort
        self._custom_headers = dict(custom_headers or {})
        self._session_id = session_id

    @classmethod
    def from_config(cls, client_factory=OpenAI) -> LLMClient:
        """从全局配置组装客户端：凭证缺失时给出人话指引，别让 SDK 抛裸异常。"""
        config.ensure_api_key()  # 凭证缺失时给出人话指引，别让 OpenAI SDK 抛裸异常
        normalize_proxy_env()  # 库被直接使用时（无 CLI 入口）同样兜底一次
        return cls(
            api_key=config.KEY,
            base_url=config.URL,
            timeout=config.LLM_TIMEOUT,
            default_model=config.MODEL,
            reasoning_effort=config.REASONING_EFFORT,
            custom_headers=config.load_provider_headers(),
            client_factory=client_factory,
        )

    def _resolved_headers(self) -> dict:
        """把配置的自定义请求头解析为实际值：{$session} 占位符替换为当前会话 id。

        每次请求现取现替换（而非构造时固化），保证 /new 轮换会话后仍发送新 id。
        """
        if not self._custom_headers:
            return {}
        session_id = self._session_id or config.SESSION_ID
        return {
            name: value.replace("{$session}", session_id)
            for name, value in self._custom_headers.items()
        }

    def list_models(self):
        """拉取服务商可用模型列表（OpenAI 兼容 `GET /models`）。

        网络异常、接口不支持、返回为空一律返回 None——模型列表只是 `/model`
        的候选，缺失不应影响对话本身。返回去重后的模型 id 列表。
        """
        try:
            page = self.client.models.list()
        except Exception:  # noqa: BLE001
            return None
        names = []
        seen = 0
        while page is not None and seen < _MAX_MODEL_PAGES:
            seen += 1
            for item in page:
                name = getattr(item, "id", None)
                if isinstance(name, str) and name and name not in names:
                    names.append(name)
            nxt = getattr(page, "get_next_page", None)
            has_nxt = getattr(page, "has_next_page", None)
            try:
                page = nxt() if callable(nxt) and callable(has_nxt) and has_nxt() else None
            except Exception:  # noqa: BLE001 翻页失败就用已收到的部分
                break
        return names or None

    def chat_stream(self, messages, tools=None, model: str | None = None,
                    effort: str | None = None, policy: RetryPolicy | None = None):
        """发起流式对话请求，逐段 yield 模型输出。

        model / effort 非空时覆盖构造值（Agent 每轮 pin 的 `TurnConfig` 经此
        透传，轮级优先）；为空时回退构造值（`from_config` 即 `config` 快照）。
        会话标题等后台小请求用 model 单独覆盖。policy 非空时覆盖重试策略
        （默认按 `config.MAX_RETRIES`）。

        yield 的元素为 (kind, payload)：
          ("reasoning", 文本)  — 模型思考内容（如有），仅供展示
          ("content", 文本)    — 正文片段
          ("message", dict)    — 流结束时组装好的完整 assistant 消息
          ("usage", dict)      — 流中携带的 token 用量（服务商支持才发）

        瞬时错误按 `llm.retry` 的策略自动重试（次数、退避、可重试判定都在那里），
        每次退避前上报 `retry_started` 供宿主显示进度。**正文已输出过也照常重试**：
        重放会重复打印已上屏的正文，但整轮报废的代价更大——上游（opencode /
        Codex / Claude Code）都是整请求重发，重复部分由上层按自己的消息模型
        处理（Agent 会把每次尝试的正文都记进同一条 assistant 消息）。思考内容
        只展示、不写入会话，流中断后可安全重算；工具调用在流结束前也不落历史。
        任务被取消时（取消令牌已触发）流在下一块数据到达前截停并关闭
        HTTP 连接，不再产出 message/usage，由 agent 侧拼装部分消息。
        """
        req = ChatRequest(
            messages=messages,
            tools=tools,
            model=model,
            reasoning_effort=effort or self.reasoning_effort,
            extra_headers=self._resolved_headers(),
        )
        kwargs = build_kwargs(req, default_model=self.default_model)

        view = renderer.current()
        owner = self  # 重试态的归属者：前台任务与后台标题各自清理，互不误清
        policy = policy or RetryPolicy(max_attempts=config.MAX_RETRIES + 1)

        def attempt():
            yield from self._stream_once(kwargs)

        def on_retry(state) -> None:
            # 优先走事件通道（前端可订阅 StatusChanged）；没有通道（单测直接调
            # 客户端、无 Agent 的调用方）则退回渲染器直调，两者可观测结果一致。
            if not emit_event(StatusChanged(
                kind="retry", text=state.text(), owner=owner, payload=state,
            )):
                view.retry_started(state, owner)

        def on_settled() -> None:
            # 成功或放弃都清掉重试态（owner 保证只清自己那条）
            if not emit_event(StatusCleared(kind="retry", owner=owner)):
                view.retry_finished(owner)

        yield from retry_mod.stream_with_retry(
            attempt, policy, on_retry=on_retry, on_settled=on_settled,
        )

    def _stream_once(self, kwargs):
        """消费一次流式响应：取消接线 + 解析委托给 `parse_stream`。

        取消即时生效：打开流后把 `stream.close` 登记为令牌监听——取消线程
        直接关流，即使正阻塞在等待下一块数据（模型静默期 / 网络慢）也会
        立即解除阻塞（读抛错或迭代结束），不必等下一块到达。关流引发的
        读错误按取消处理（吞掉），非取消的真实异常照常上抛；任务被中断时
        不产出 message/usage，由 agent 侧拼装部分消息。

        监听在流关闭后摘除——不摘除则每轮残留一个已关闭流的回调，长任务
        越积越多。`subscribe` 之后复查一次令牌：`_open_stream` 与订阅之间
        的窗口里触发的取消同样立即关流，不必等下一块数据。
        """
        token = current_token()
        if token is not None and token.cancelled:
            return  # 已取消：连新请求都不发起（避免中断后仍白跑一次调用）

        tail: list = []  # message / usage 只认完整跑完的尝试：取消后由 agent 侧拼装部分消息
        with closing(self._open_stream(kwargs)) as stream:
            close = stream.close
            if token is not None:
                token.subscribe(close)  # 取消线程直接关流，解除阻塞中的读
                if token.cancelled:
                    close()  # 订阅窗口期内已取消：不等下一块，直接关
            try:
                for kind, payload in parse_stream(_iter_cancellable(stream, token)):
                    if kind in ("content", "reasoning"):
                        yield kind, payload
                    else:
                        tail.append((kind, payload))
            except Exception:
                if token is not None and token.cancelled:
                    return  # 关流引发的读错误：本质是取消，非真实异常
                raise
            finally:
                if token is not None:
                    token.unsubscribe(close)

        if token is not None and token.cancelled:
            return  # 流恰好结束但已被取消：不产出 message/usage
        yield from tail

    def _open_stream(self, kwargs):
        """发起流式请求；个别兼容服务不认识 `stream_options` / `reasoning_effort` 时降级重连。

        降级后只是拿不到用量（或不用思考强度），对话本身不受影响。
        `pop` 就地修改 kwargs——重试复用同一份 kwargs，降级对后续尝试同样生效。
        """
        try:
            return self.client.chat.completions.create(**kwargs)
        except BadRequestError as e:
            if _mentions(e, "stream_options") and "stream_options" in kwargs:
                kwargs.pop("stream_options")
                return self.client.chat.completions.create(**kwargs)
            if _mentions(e, "reasoning_effort") and "reasoning_effort" in kwargs:
                kwargs.pop("reasoning_effort")
                return self.client.chat.completions.create(**kwargs)
            raise


def _iter_cancellable(stream, token):
    """逐块让出 chunk，每块之前查取消令牌：取消后不再产出 message/usage。"""
    for chunk in stream:
        if token is not None and token.cancelled:
            return
        yield chunk


def _mentions(exc: BaseException, param: str) -> bool:
    """400 错误是否在抱怨某个请求参数：状态码先行，错误文本子串匹配兜底。"""
    status = getattr(exc, "status_code", None)
    if status is not None and status != 400:
        return False
    text = str(exc)
    body = getattr(exc, "body", None) or ""
    return param.lower() in f"{text} {body}".lower()


def _usage_to_dict(usage):
    """旧入口保留：已搬至 `llm.stream.usage_to_dict`，此处 re-export。"""
    return usage_to_dict(usage)


__all__ = ["ChatRequest", "LLMClient", "build_kwargs"]
