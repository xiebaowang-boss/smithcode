import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, Wrench, XCircle } from "lucide-react";
import type { ChatItem } from "./chatItems";

const COLLAPSE_LINES = 12;

function clipped(text: string): { shown: string; hidden: number } {
  const lines = text.split("\n");
  if (lines.length <= COLLAPSE_LINES) return { shown: text, hidden: 0 };
  return { shown: lines.slice(0, COLLAPSE_LINES).join("\n"), hidden: lines.length - COLLAPSE_LINES };
}

export function ToolCard({ item }: { item: ChatItem }) {
  const shouldExpand = item.isError === true || item.expand === true;
  const [open, setOpen] = useState(shouldExpand);
  const [userToggled, setUserToggled] = useState(false);
  // SSE 把同一张卡从 pending 推成 done 时（不可变替换），没手动折叠过就跟随状态
  useEffect(() => {
    if (!userToggled && shouldExpand) setOpen(true);
    if (!item.pending && !shouldExpand && !userToggled) setOpen(false);
  }, [item.pending, shouldExpand, userToggled]);
  const { shown, hidden } = clipped(item.text);
  const body = open || hidden === 0 ? item.text : `${shown}\n…（+${hidden} 行，点击展开）`;
  const label = item.summary || item.name || "工具";
  return (
    <div className={`tool ${item.pending ? "pending" : ""} ${item.isError ? "error" : ""}`}>
      <div
        className="tool-head"
        title={item.name}
        onClick={() => {
          setUserToggled(true);
          setOpen(!open);
        }}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setUserToggled(true);
            setOpen(!open);
          }
        }}
      >
        {item.pending ? (
          <Loader2 size={13} className="spin" />
        ) : open ? (
          <ChevronDown size={13} />
        ) : (
          <ChevronRight size={13} />
        )}
        {item.isError ? <XCircle size={13} /> : <Wrench size={13} />}
        <span className="tool-name">{label}</span>
        {!open && hidden > 0 ? <span className="tool-lines">+{hidden} 行</span> : null}
      </div>
      {(open || item.pending) && body ? <pre className="tool-body">{body}</pre> : null}
    </div>
  );
}
