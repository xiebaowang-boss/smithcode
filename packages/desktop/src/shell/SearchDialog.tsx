import { useEffect, useMemo, useState } from "react";
import type { HistorySession } from "../session/serve";
import { ModalManager } from "../modals/ModalManager";
import { relTime } from "../ui/format";

export function SearchDialog({
  history,
  onPick,
  onClose,
}: {
  history: HistorySession[];
  onPick: (item: HistorySession) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);

  const results = useMemo(() => {
    const q = query.trim();
    const list = q
      ? history.filter(
          (h) =>
            h.title.includes(q) || h.first_prompt.includes(q) || h.id.startsWith(q),
        )
      : history;
    return list.slice(0, 30);
  }, [history, query]);

  useEffect(() => {
    setIndex(0);
  }, [query]);

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setIndex((i) => Math.min(i + 1, Math.max(results.length - 1, 0)));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setIndex((i) => Math.max(i - 1, 0));
      return;
    }
    if (e.key === "Enter") {
      const item = results[index];
      if (!item) return;
      e.preventDefault();
      onClose();
      onPick(item);
    }
  }

  return (
    <ModalManager onEscape={onClose}>
      <div onKeyDown={onKey}>
        <div className="modal-custom">
          <input
            value={query}
            autoFocus
            placeholder="搜索会话…"
            aria-label="搜索会话"
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="select-list" role="listbox" aria-label="搜索结果">
          {results.map((h, i) => (
            <button
              key={h.id}
              role="option"
              aria-selected={i === index}
              className={i === index ? "current" : ""}
              onMouseEnter={() => setIndex(i)}
              onClick={() => {
                onClose();
                onPick(h);
              }}
            >
              {h.title || h.first_prompt || h.id.slice(0, 8)}
              <span className="desc">{relTime(h.updated)}</span>
            </button>
          ))}
          {results.length === 0 ? <div className="pane-empty">没有匹配的会话</div> : null}
        </div>
      </div>
    </ModalManager>
  );
}
