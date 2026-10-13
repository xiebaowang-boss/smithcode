import { useState } from "react";
import { serve } from "../session/serve";
import { Menu, type MenuEntry } from "../ui/Menu";

const EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

export function EffortPicker({
  sessionId,
  effort,
  onChanged,
}: {
  sessionId: string | null;
  effort: string;
  onChanged: (effort: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");

  async function pick(value: string) {
    if (!sessionId || value === effort) return;
    onChanged(value);
    setError("");
    try {
      await serve.dispatch(sessionId, `/effort ${value}`);
    } catch (e) {
      onChanged(effort);
      setError(String(e));
    }
  }

  const items: MenuEntry[] = [
    { heading: "思考强度" },
    ...EFFORTS.map((e) => ({
      label: e,
      current: e === effort,
      onSelect: () => void pick(e),
    })),
  ];

  return (
    <span className="popover-anchor">
      <button
        className="model-btn"
        title={error || "切换思考强度"}
        disabled={!sessionId}
        onClick={() => setOpen(!open)}
      >
        {effort || "思考强度"}
      </button>
      {open ? <Menu up alignRight items={items} onClose={() => setOpen(false)} /> : null}
    </span>
  );
}
