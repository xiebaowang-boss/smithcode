import { useState } from "react";
import {
  Expand,
  FileDiff,
  Folder,
  Globe,
  PanelRightClose,
  Plus,
  SquareTerminal,
  X,
} from "lucide-react";
import { toggleFullscreen } from "../app/desktop";
import { DiffPane } from "../panes/DiffPane";
import { FileBrowser } from "../panes/FileBrowser";
import { Menu } from "../ui/Menu";

export type RightPaneKind = "files" | "browser" | "terminal" | "diff" | null;

export interface SideWindow {
  id: number;
  kind: Exclude<RightPaneKind, null>;
}

interface KindMeta {
  kind: Exclude<RightPaneKind, null>;
  label: string;
  shortcut: string;
  icon: typeof Folder;
}

const PICKER: KindMeta[] = [
  { kind: "files", label: "Files", shortcut: "Ctrl+P", icon: Folder },
  { kind: "browser", label: "Browser", shortcut: "Ctrl+T", icon: Globe },
  { kind: "terminal", label: "Terminal", shortcut: "Ctrl+`", icon: SquareTerminal },
];

const META: Record<Exclude<RightPaneKind, null>, { label: string; icon: typeof Folder }> = {
  files: { label: "Files", icon: Folder },
  browser: { label: "Browser", icon: Globe },
  terminal: { label: "Terminal", icon: SquareTerminal },
  diff: { label: "更改", icon: FileDiff },
};

function PlaceholderPanel({ kind }: { kind: "browser" | "terminal" }) {
  const text = {
    browser: "浏览器面板将在二期实现：内嵌页面预览，便于对照本地开发服务。",
    terminal: "终端面板将在二期实现：PTY + xterm.js，直接在应用内跑命令。",
  };
  return (
    <div className="placeholder-panel">
      <span className="tag">二期</span>
      <div className="pane-empty">{text[kind]}</div>
    </div>
  );
}

export function RightSidebar({
  closing,
  windows,
  activeId,
  sessionId,
  onToggle,
  onActivate,
  onClose,
  onOpen,
  onAdd,
}: {
  closing: boolean;
  windows: SideWindow[];
  activeId: number | null;
  sessionId: string | null;
  onToggle: () => void;
  onActivate: (id: number) => void;
  onClose: (id: number) => void;
  onOpen: (kind: Exclude<RightPaneKind, null>) => void;
  onAdd: (kind: Exclude<RightPaneKind, null>) => void;
}) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const active = windows.find((w) => w.id === activeId) ?? null;

  return (
    <aside
      className={`rightside enter${closing ? " closing" : ""}${windows.length > 0 ? " has-windows" : ""}`}
      aria-label="侧边栏"
    >
      <div className="side-top">
        {windows.length > 0 ? (
          <div className="side-tabs">
            <div className="side-tabs-scroll">
              {windows.map((w) => {
                const meta = META[w.kind];
                const Icon = meta.icon;
                return (
                  <button
                    key={w.id}
                    className={`side-tab${w.id === activeId ? " active" : ""}`}
                    title={meta.label}
                    onClick={() => onActivate(w.id)}
                  >
                    <Icon size={14} />
                    <span className="label">{meta.label}</span>
                    <span
                      className="close"
                      role="button"
                      aria-label={`关闭 ${meta.label}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        onClose(w.id);
                      }}
                    >
                      <X size={12} />
                    </span>
                  </button>
                );
              })}
            </div>
            <span className="popover-anchor side-add">
              <button
                className="side-add-btn"
                title="新开窗口"
                onClick={() => setPickerOpen((v) => !v)}
              >
                <Plus size={14} />
              </button>
              {pickerOpen ? (
                <Menu
                  onClose={() => setPickerOpen(false)}
                  items={PICKER.map((item) => {
                    const Icon = item.icon;
                    return {
                      label: item.label,
                      icon: <Icon size={14} />,
                      hint: item.shortcut,
                      onSelect: () => onAdd(item.kind),
                    };
                  })}
                />
              ) : null}
            </span>
          </div>
        ) : (
          <span className="spacer" />
        )}
        <div className="side-actions">
          <button className="icon" title="全屏" onClick={() => void toggleFullscreen()}>
            <Expand size={15} />
          </button>
          <button className="icon active" title="收起侧栏" onClick={onToggle}>
            <PanelRightClose size={16} />
          </button>
        </div>
      </div>

      {windows.length === 0 ? (
        <div className="side-empty">
          {PICKER.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.kind} className="side-option" onClick={() => onOpen(item.kind)}>
                <Icon size={16} />
                <span>{item.label}</span>
                <span className="side-shortcut">{item.shortcut}</span>
              </button>
            );
          })}
        </div>
      ) : (
        <div className="side-body">
          {active?.kind === "files" ? (
            <FileBrowser key={`${active.id}:${sessionId ?? ""}`} sessionId={sessionId} />
          ) : null}
          {active?.kind === "diff" && sessionId ? <DiffPane sessionId={sessionId} /> : null}
          {active?.kind === "diff" && !sessionId ? (
            <div className="pane-empty">暂无会话，打开一个会话后可在此查看更改。</div>
          ) : null}
          {active?.kind === "browser" || active?.kind === "terminal" ? (
            <PlaceholderPanel kind={active.kind} />
          ) : null}
        </div>
      )}
    </aside>
  );
}
