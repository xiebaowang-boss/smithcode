import { useLayoutEffect, useRef, useState } from "react";
import { ArrowDown, Loader2 } from "lucide-react";
import type { ChatItem } from "./chatItems";
import { Markdown } from "./Markdown";
import { ContextGroup } from "./ContextGroup";
import { ThinkingBlock } from "./ThinkingBlock";
import { ToolCard } from "./ToolCard";

function Notice({ item }: { item: ChatItem }) {
  // notice.text 是服务端/工具可控字符串（命令回执、跳过提示、错误）——
  // 按纯文本渲染，不过 markdown 管线，避免不可信内容进 HTML
  return (
    <div className={`msg notice ${item.level ?? "info"}`}>
      <span className="notice-text">{item.text}</span>
    </div>
  );
}

export function MessageList({ items, streaming }: { items: ChatItem[]; streaming?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [stuck, setStuck] = useState(true);

  // useLayoutEffect：在浏览器绘制前贴底，避免流式增量时的抖动
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || !stuck) return;
    el.scrollTop = el.scrollHeight;
  }, [items, stuck]);

  function onScroll() {
    const el = ref.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setStuck(nearBottom);
  }

  function scrollToBottom() {
    const el = ref.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    setStuck(true);
  }

  const nodes: React.ReactNode[] = [];
  let contextBuf: ChatItem[] = [];
  function flushContext() {
    if (contextBuf.length > 0) {
      // key 取首条工具的稳定 key（含 seq），不用数组下标——插入会让下标漂移、
      // 让 React 复用错 DOM
      nodes.push(<ContextGroup key={`ctx-${contextBuf[0].key}`} items={contextBuf} />);
      contextBuf = [];
    }
  }

  // 打字光标只钉在「正在流式增长的正文」（chatItems 标 live 的那段）上。按
  // 「最后一条正文」判会把光标钉到上一轮的历史正文上——新一轮刚开始还没吐字时，
  // 历史中部就会凭空悬着一个 ▍。
  const liveIdx = items.findIndex((it) => it.live);

  items.forEach((item, i) => {
    if (item.kind === "tool" && isContextTool(item.name)) {
      contextBuf.push(item);
      return;
    }
    flushContext();
    switch (item.kind) {
      case "user":
        nodes.push(
          <div key={item.key} className="msg user">
            {item.text}
          </div>,
        );
        break;
      case "assistant":
        nodes.push(
          <div key={item.key} className="msg assistant">
            <Markdown text={item.text} />
            {streaming && i === liveIdx ? <span className="cursor">▍</span> : null}
          </div>,
        );
        break;
      case "thinking":
        nodes.push(<ThinkingBlock key={item.key} text={item.text} />);
        break;
      case "tool":
        nodes.push(<ToolCard key={item.key} item={item} />);
        break;
      case "plan":
        nodes.push(
          <div key={item.key} className="msg plan">
            <Markdown text={item.text} />
          </div>,
        );
        break;
      case "notice":
        nodes.push(<Notice key={item.key} item={item} />);
        break;
      case "queue":
        nodes.push(
          <div key={item.key} className="msg user queued">
            ↳ {item.text}
          </div>,
        );
        break;
      case "footer":
        nodes.push(
          <div key={item.key} className="msg footer">
            {item.text}
          </div>,
        );
        break;
      default:
        break;
    }
  });
  flushContext();

  return (
    <div className="messages-wrap">
      <div className="messages" ref={ref} onScroll={onScroll}>
        <div className="thread">
          {nodes}
          {streaming ? (
            <div className="msg streaming-hint">
              <Loader2 size={12} className="spin" /> 生成中…
            </div>
          ) : null}
        </div>
      </div>
      {!stuck && nodes.length > 0 ? (
        <button className="to-latest" onClick={scrollToBottom}>
          <ArrowDown size={14} /> 回到最新
        </button>
      ) : null}
    </div>
  );
}

function isContextTool(name?: string): boolean {
  return name === "read_file" || name === "list_dir" || name === "glob" || name === "grep";
}
