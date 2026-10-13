/** 自动生成（packages/core/scripts/gen_protocol.py），不要手改。来源：event/catalog.py 的 @declare 声明。 */

export interface SmithEnvelope<T = unknown> {
  id: string;
  type: string;
  version: number;
  created: number;
  session_id: string | null;
  durable: boolean;
  seq: number | null;
  data: T;
}

export type AgentMessage = Record<string, unknown>;

export interface StepUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface QueueItem {
  id: string;
  text: string;
  kind: QueueItemKind;
  images: unknown[] | null;
  created_at: number;
}

export type PromptKind = "permission" | "outside_access" | "skill_trust" | "ask_user" | "confirm";
export type PromptOutcome = "answered" | "cancelled" | "denied" | "error";
export type StatusKind = "working" | "retry" | "compaction" | "branch_summary" | "stopping";
export type NoticeLevel = "info" | "success" | "warning" | "error";
export type StreamKind = "content" | "reasoning";
export type QueueItemKind = "steer" | "follow_up";
export type InterruptReason = "user" | "shutdown" | "superseded" | "inactivity";
export type StepFinish = "stop" | "tool_calls" | "interrupted" | "error";

export interface SmithEventMap {
  "session.checkpointed": {
    state: Record<string, unknown>;
  };
  "session.compaction.ended": {
    before_tokens: number;
    after_tokens: number;
  };
  "session.compaction.failed": {
    reason: string;
  };
  "session.compaction.started": {
    before_tokens: number;
  };
  "session.created": {
    cwd: string;
    model: string;
    effort: string;
    app: string;
    oneshot: boolean;
  };
  "session.execution.failed": {
    status: string;
    reason: string;
  };
  "session.execution.interrupted": {
    reason: "user" | "shutdown" | "superseded" | "inactivity";
    partial: boolean;
  };
  "session.execution.started": Record<string, never>;
  "session.execution.succeeded": {
    status: string;
    text: string;
  };
  "session.history.compacted": {
    summary: AgentMessage;
    tail: AgentMessage[];
    before_tokens: number;
    after_tokens: number;
  };
  "session.idle": Record<string, never>;
  "session.inbox.cancelled": {
    item_id: string;
  };
  "session.inbox.cleared": Record<string, never>;
  "session.inbox.delivered": {
    item: QueueItem;
  };
  "session.inbox.enqueued": {
    item: QueueItem;
  };
  "session.message.delta": {
    message: AgentMessage;
    delta: string;
    kind: "content" | "reasoning";
  };
  "session.message.ended": {
    message: AgentMessage;
  };
  "session.message.started": {
    message: AgentMessage;
  };
  "session.model.selected": {
    model: string;
    effort: string;
  };
  "session.notice": {
    text: string;
    level: "info" | "success" | "warning" | "error";
  };
  "session.plan.updated": {
    summary: string;
    rendered: string;
    titles: string;
    created: boolean;
    tool_call_id: string | null;
  };
  "session.prompt.finished": {
    id: string;
    kind: "permission" | "outside_access" | "skill_trust" | "ask_user" | "confirm";
    outcome: "answered" | "cancelled" | "denied" | "error";
    value: string | null;
    values: string[];
    error: string | null;
  };
  "session.prompt.started": {
    id: string;
    kind: "permission" | "outside_access" | "skill_trust" | "ask_user" | "confirm";
    title: string;
    blocking: boolean;
    detail: string[];
    options: string[];
    payload: Record<string, unknown> | null;
  };
  "session.run.ended": {
    result: unknown;
  };
  "session.status.changed": {
    kind: "working" | "retry" | "compaction" | "branch_summary" | "stopping";
    text: string;
    owner: string | null;
    payload: unknown;
  };
  "session.status.cleared": {
    kind: "working" | "retry" | "compaction" | "branch_summary" | "stopping";
    owner: string | null;
  };
  "session.step.ended": {
    step_id: string;
    index: number;
    finish: "stop" | "tool_calls" | "interrupted" | "error";
    usage: StepUsage;
  };
  "session.step.started": {
    step_id: string;
    index: number;
  };
  "session.stream.ended": {
    message: AgentMessage;
  };
  "session.title.changed": {
    title: string;
    source: string;
  };
  "session.tool.ended": {
    tool_call_id: string | null;
    result: string;
    is_error: boolean;
    expand: boolean;
  };
  "session.tool.preview": {
    tool_call_id: string | null;
    detail: string;
  };
  "session.tool.started": {
    tool_call_id: string | null;
    name: string;
    line: string;
    display: string;
  };
  "session.usage.updated": {
    calls: number;
    total_input: number;
    total_output: number;
    total_tokens: number;
    cached_tokens: number;
    step: StepUsage;
  };
}

export type SmithEventType = keyof SmithEventMap;

/** durable=true：可回放骨架（重连 after_seq 重放）；volatile：重连可丢。 */
export const DURABLE_EVENTS: ReadonlySet<string> = new Set([
  "session.checkpointed",
  "session.created",
  "session.execution.failed",
  "session.execution.interrupted",
  "session.execution.started",
  "session.execution.succeeded",
  "session.history.compacted",
  "session.message.ended",
  "session.message.started",
  "session.model.selected",
  "session.plan.updated",
  "session.prompt.finished",
  "session.step.ended",
  "session.step.started",
  "session.title.changed",
  "session.tool.ended",
  "session.tool.started",
  "session.usage.updated",
]);
