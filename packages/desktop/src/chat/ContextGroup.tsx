import { useState } from "react";
import { ChevronDown, ChevronRight, Search } from "lucide-react";
import type { ChatItem } from "./chatItems";
import { ToolCard } from "./ToolCard";

function summary(items: ChatItem[]): string {
  const reads = items.filter((i) => i.name === "read_file" || i.name === "list_dir").length;
  const searches = items.filter((i) => i.name === "glob" || i.name === "grep").length;
  const parts: string[] = [];
  if (reads > 0) parts.push(`${reads} 次读取`);
  if (searches > 0) parts.push(`${searches} 次搜索`);
  return parts.join("，") || `${items.length} 个工具`;
}

export function ContextGroup({ items }: { items: ChatItem[] }) {
  const [open, setOpen] = useState(false);
  const done = items.every((i) => !i.pending);
  return (
    <div className="ctxgroup">
      <div
        className="tool-head"
        onClick={() => setOpen(!open)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setOpen(!open);
          }
        }}
      >
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        <Search size={13} />
        <span className="tool-name">
          {done ? "已探索" : "探索中"} · {summary(items)}
        </span>
      </div>
      {open ? (
        <div className="ctxgroup-body">
          {items.map((i) => (
            <ToolCard key={i.key} item={i} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
