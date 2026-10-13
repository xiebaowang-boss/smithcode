import { useMemo, useState } from "react";
import { PencilLine, X } from "lucide-react";
import { useSessionStore } from "../session/sessionStore";
import { serve } from "../session/serve";

export function QueuePanel({
  sessionId,
  onTakeBack,
}: {
  sessionId: string;
  /** 取回编辑：把排队文本交回输入框（由 Composer 填充） */
  onTakeBack?: (text: string) => void;
}) {
  const events = useSessionStore(
    (s) => s.tabs.find((t) => t.sessionId === sessionId)?.events ?? [],
  );
  const [error, setError] = useState("");

  // 事件折叠成"当前还在排队的项"：每次 render 重建是 O(events)，useMemo 收敛
  const open = useMemo(() => {
    const map = new Map<string, string>();
    for (const env of events) {
      if (env.type === "session.inbox.enqueued") {
        const item = (env.data as { item?: { id?: string; text?: string } }).item;
        if (item?.id) map.set(item.id, item.text ?? "");
      } else if (env.type === "session.inbox.cancelled") {
        const id = (env.data as { item_id?: string }).item_id;
        if (id) map.delete(id);
      } else if (env.type === "session.inbox.cleared") {
        map.clear();
      }
    }
    return map;
  }, [events]);

  if (open.size === 0) return null;

  async function takeBack(id: string) {
    setError("");
    try {
      const item = await serve.takeQueued(sessionId, id);
      onTakeBack?.(item.text);
    } catch (e) {
      setError(String(e));
    }
  }

  async function revoke(id: string) {
    setError("");
    try {
      await serve.cancelQueued(sessionId, id);
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="queue-chips" aria-live="polite">
      <span className="queue-title">排队中（{open.size}）</span>
      {[...open.entries()].map(([id, text]) => (
        <span key={id} className="queue-chip">
          <span title={text}>↳ {text}</span>
          <button title="取回编辑" onClick={() => void takeBack(id)}>
            <PencilLine size={12} />
          </button>
          <button title="撤销" onClick={() => void revoke(id)}>
            <X size={12} />
          </button>
        </span>
      ))}
      {error ? <span className="queue-error">{error}</span> : null}
    </div>
  );
}
