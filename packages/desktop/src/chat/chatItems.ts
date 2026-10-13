import type { SmithEnvelope } from "../session/protocol";
import { dataOf } from "../session/protocol";

export interface ChatItem {
  key: string;
  kind:
    | "user"
    | "assistant"
    | "thinking"
    | "tool"
    | "notice"
    | "plan"
    | "queue"
    | "footer";
  text: string;
  summary?: string;
  level?: string;
  toolId?: string;
  name?: string;
  pending?: boolean;
  isError?: boolean;
  expand?: boolean;
  /** 正在流式增长、尚未收口的正文段：打字光标只钉在它上面，绝不钉历史正文 */
  live?: boolean;
}

export function reduceChatItems(events: SmithEnvelope[]): ChatItem[] {
  const ordered = byPublishTime(events);
  const items: ChatItem[] = [];
  // tool 配对不用"可变共享对象"：MAX_EVENTS 裁剪可能先吃掉 started，
  // 那时 ended 必须合成一张卡而不是静默丢失；同名 tool_call_id 重试也不能覆盖。
  // 索引存下标（不可变替换），key 带 seq 保证跨 clearEvents 不复用。
  const tools = new Map<string, number>();
  let streamKey = "";
  let streamText = "";
  let thinkingKey = "";
  let thinkingText = "";
  // 本段流缓冲已上屏的气泡下标：stream.ended 的收口发生在 message.ended 之前（实测顺序），
  // 不记住就会再按 ended 开一条，同一段正文渲染两遍
  let flushedIdx: number | null = null;

  function flushStream(live = false) {
    if (streamKey && streamText) {
      items.push({ key: streamKey, kind: "assistant", text: streamText, live: live || undefined });
      flushedIdx = items.length - 1;
    }
    streamKey = "";
    streamText = "";
  }

  function flushThinking() {
    if (thinkingKey && thinkingText) {
      items.push({ key: thinkingKey, kind: "thinking", text: thinkingText });
    }
    thinkingKey = "";
    thinkingText = "";
  }

  // 文本上限：断线丢了 stream.ended 时，半截文本不能无限攒进下一轮
  const STREAM_MAX = 200_000;
  function appendStream(dst: "stream" | "thinking", delta: string) {
    if (dst === "stream") {
      if (streamText.length + delta.length > STREAM_MAX) {
        flushStream();
      }
      streamText += delta;
    } else {
      if (thinkingText.length + delta.length > STREAM_MAX) {
        flushThinking();
      }
      thinkingText += delta;
    }
  }

  for (const env of ordered) {
    switch (env.type) {
      case "session.message.ended": {
        const msg = dataOf(env, "session.message.ended");
        const role = (msg?.message as { role?: string })?.role;
        const content = textOf((msg?.message as { content?: unknown })?.content);
        if (role === "user") {
          flushStream();
          flushThinking();
          items.push({ key: `${env.id}@${env.seq ?? "live"}`, kind: "user", text: content });
          flushedIdx = null;
        } else if (role === "assistant") {
          if (streamKey) {
            // 流缓冲还没收口：正文以 ended 为准（补上中途丢掉的尾巴），就地收口
            if (content) streamText = content;
            flushStream();
          } else if (flushedIdx !== null && content && items[flushedIdx]?.kind === "assistant") {
            // stream.ended 先收的口（实测顺序）：就地换成权威正文，不再开一条
            items[flushedIdx] = { ...items[flushedIdx], text: content };
          } else if (content) {
            // 重放路径：本轮没有易失的 delta / stream.ended，只有这条 durable 事件。
            // 正文为空的不垫气泡（纯 tool_calls 的回合）：空 assistant 会把打字光标
            // 引到历史中部去，还会在时间线里留一块空白
            items.push({ key: `${env.id}@${env.seq ?? "live"}`, kind: "assistant", text: content });
          }
          flushedIdx = null;
        }
        break;
      }
      case "session.message.delta": {
        const d = dataOf(env, "session.message.delta");
        if (d?.kind === "reasoning") {
          if (!thinkingKey) thinkingKey = `${env.id}@${env.seq ?? "live"}`;
          appendStream("thinking", d.delta);
        } else {
          if (!streamKey) {
            flushThinking();
            streamKey = `${env.id}@${env.seq ?? "live"}`;
          }
          appendStream("stream", d?.delta ?? "");
        }
        break;
      }
      case "session.stream.ended":
      case "session.prompt.finished":
      case "session.inbox.delivered":
        // prompt.finished 是 durable（重放能补）：断线丢了 stream.ended 时，
        // 靠它把半截文本强制落盘到本轮，不算进下一轮
        flushStream();
        flushThinking();
        break;
      case "session.tool.started": {
        const t = dataOf(env, "session.tool.started");
        const toolId = t?.tool_call_id ?? env.id;
        const item: ChatItem = {
          key: `${env.id}@${env.seq ?? "live"}`,
          kind: "tool",
          text: "",
          summary: t?.line ?? t?.name ?? "",
          toolId,
          name: t?.name,
          pending: true,
        };
        // 重试的同名 tool_call_id 不覆盖旧卡：旧卡保留，新卡下标覆盖索引
        tools.set(toolId, items.length);
        items.push(item);
        break;
      }
      case "session.tool.ended": {
        const t = dataOf(env, "session.tool.ended");
        const idx = tools.get(t?.tool_call_id ?? "");
        if (idx !== undefined && items[idx]?.kind === "tool") {
          items[idx] = {
            ...items[idx],
            pending: false,
            isError: t?.is_error,
            expand: t?.expand,
            text: t?.result ?? "",
          };
        } else {
          // 孤儿 ended（started 被裁剪/重连丢了）：合成一张已完成的卡，
          // 结果不能静默丢失
          items.push({
            key: `${env.id}@${env.seq ?? "live"}`,
            kind: "tool",
            text: t?.result ?? "",
            summary: t?.tool_call_id ?? "工具",
            toolId: t?.tool_call_id ?? undefined,
            pending: false,
            isError: t?.is_error,
            expand: true,
          });
        }
        break;
      }
      case "session.plan.updated": {
        const p = dataOf(env, "session.plan.updated");
        if (p?.created) {
          items.push({ key: `${env.id}@${env.seq ?? "live"}`, kind: "plan", text: p.rendered });
        }
        break;
      }
      case "session.notice": {
        const n = dataOf(env, "session.notice");
        items.push({
          key: `${env.id}@${env.seq ?? "live"}`,
          kind: "notice",
          text: n?.text ?? "",
          level: n?.level,
        });
        break;
      }
      case "session.inbox.delivered": {
        const q = dataOf(env, "session.inbox.delivered");
        const text = (q?.item as { text?: string })?.text ?? "";
        items.push({ key: `${env.id}@${env.seq ?? "live"}`, kind: "queue", text });
        break;
      }
      case "session.execution.succeeded":
      case "session.execution.failed":
      case "session.execution.interrupted": {
        flushStream();
        flushThinking();
        break;
      }
      default:
        break;
    }
  }
  if (streamKey && streamText) {
    // 每次重算的收尾：未收口的流缓冲以上屏，标 live——打字光标只认这个标记
    items.push({ key: streamKey, kind: "assistant", text: streamText, live: true });
  }
  flushThinking();
  return items;
}

/**
 * 还原发布顺序。
 *
 * 重连 / 新开 tab 时先收到 durable 重放、后收到直播队列里的易失事件（delta /
 * stream.ended），同一条消息会被拼成「先 ended、后 delta」——正文按 ended 上屏一次、
 * 再按 delta 收口一次，就是截图里那两段一模一样的话。按发布时间（信封注入，日志也
 * 落盘）排序即可还原真实先后；时间戳缺失时保持原样，宁可不排也不错排。
 */
function byPublishTime(events: SmithEnvelope[]): SmithEnvelope[] {
  if (events.length < 2) return events;
  for (const e of events) {
    if (typeof e.created !== "number" || !Number.isFinite(e.created)) return events;
  }
  // 稳定排序（ES2019+）：同一时刻的事件保持到达顺序
  return [...events].sort((a, b) => a.created - b.created);
}

export function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .filter((p) => typeof p === "object" && p !== null && (p as { type?: string }).type === "text")
      .map((p) => (p as { text?: string }).text ?? "")
      .join(" ");
  }
  return "";
}
