import { useEffect, useRef } from "react";

export function ModalManager({
  children,
  wide,
  onEscape,
}: {
  children: React.ReactNode;
  wide?: boolean;
  onEscape?: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const prevFocus = useRef<Element | null>(null);
  const escapeRef = useRef(onEscape);
  escapeRef.current = onEscape;

  useEffect(() => {
    prevFocus.current = document.activeElement;
    const el = ref.current;
    const first = el?.querySelector("button, input, select, textarea") as HTMLElement | null;
    first?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && escapeRef.current) {
        e.stopPropagation();
        escapeRef.current();
        return;
      }
      if (e.key !== "Tab" || !el) return;
      const items = [...el.querySelectorAll("button, input, select, textarea")].filter(
        (n) => !(n as HTMLElement).hasAttribute("disabled"),
      ) as HTMLElement[];
      if (items.length === 0) return;
      const firstItem = items[0];
      const lastItem = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstItem) {
        e.preventDefault();
        lastItem.focus();
      } else if (!e.shiftKey && document.activeElement === lastItem) {
        e.preventDefault();
        firstItem.focus();
      }
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      (prevFocus.current as HTMLElement | null)?.focus?.();
    };
  }, []);

  return (
    <div className="modal-backdrop">
      <div ref={ref} className={`modal${wide ? " wide" : ""}`} role="dialog" aria-modal="true">
        {children}
      </div>
    </div>
  );
}
