import { useEffect, useRef, type ReactNode, type RefObject } from "react";

export interface MenuItem {
  label: string;
  icon?: ReactNode;
  hint?: string;
  danger?: boolean;
  current?: boolean;
  disabled?: boolean;
  onSelect?: () => void;
}

export type MenuEntry = MenuItem | { heading: string };

function isHeading(entry: MenuEntry): entry is { heading: string } {
  return "heading" in entry;
}

export function useDismiss(ref: RefObject<HTMLElement | null>, onClose: () => void) {
  useEffect(() => {
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key !== "Escape") return;
      // capture 阶段先于 ModalManager 的 document（bubble）处理器。菜单开着时
      // Esc 归菜单（关菜单、不关背后的 modal/不停任务），stopPropagation 阻断
      // 后续所有阶段；菜单没开时这里不注册，modal 照常收 Esc。
      e.stopPropagation();
      onClose();
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [ref, onClose]);
}

export function Menu({
  items,
  onClose,
  label,
  up,
  alignRight,
}: {
  items: MenuEntry[];
  onClose: () => void;
  label?: string;
  up?: boolean;
  alignRight?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, onClose);
  return (
    <div
      ref={ref}
      role="menu"
      className={`popover${up ? " up" : ""}${alignRight ? " align-right" : ""}`}
    >
      {label ? <div className="popover-label">{label}</div> : null}
      {items.map((entry, i) =>
        isHeading(entry) ? (
          <div key={`h-${i}`} className="popover-label">
            {entry.heading}
          </div>
        ) : (
          <button
            key={`${entry.label}-${i}`}
            role="menuitem"
            className={`popover-item${entry.current ? " current" : ""}${entry.danger ? " danger" : ""}`}
            disabled={entry.disabled}
            onClick={() => {
              onClose();
              entry.onSelect?.();
            }}
          >
            {entry.icon}
            <span className="grow">{entry.label}</span>
            {entry.hint ? <span className="desc">{entry.hint}</span> : null}
          </button>
        ),
      )}
    </div>
  );
}
