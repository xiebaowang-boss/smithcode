import { PanelRightOpen } from "lucide-react";

export function ChatTopActions({ onToggleRight }: { onToggleRight: () => void }) {
  return (
    <div className="chat-top-actions">
      <button className="icon" title="展开侧栏" onClick={onToggleRight}>
        <PanelRightOpen size={16} />
      </button>
    </div>
  );
}
