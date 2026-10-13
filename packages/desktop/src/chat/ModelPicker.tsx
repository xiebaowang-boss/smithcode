import { useEffect, useState } from "react";
import { serve } from "../session/serve";
import { Menu, type MenuEntry } from "../ui/Menu";

export function ModelPicker({
  sessionId,
  model,
  onChanged,
}: {
  sessionId: string | null;
  model: string;
  onChanged: (model: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [models, setModels] = useState<string[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!sessionId) return;
      try {
        const result = await serve.dispatch(sessionId, "/model");
        if (!cancelled && result.select) setModels(result.select.items.map((i) => i.value));
      } catch {
        // serve 未起时列表留空，仅显示当前值
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  async function pick(value: string) {
    if (!sessionId || value === model) return;
    onChanged(value);
    setError("");
    try {
      await serve.dispatch(sessionId, `/model ${value}`);
    } catch (e) {
      onChanged(model);
      setError(String(e));
    }
  }

  const items: MenuEntry[] = [
    { heading: "模型" },
    ...(models.length > 0
      ? models.map((m) => ({
          label: m,
          current: m === model,
          onSelect: () => void pick(m),
        }))
      : [{ label: "（会话未运行，无模型列表）", disabled: true }]),
  ];

  return (
    <span className="popover-anchor">
      <button
        className="model-btn"
        title={error || "切换模型"}
        disabled={!sessionId}
        onClick={() => setOpen(!open)}
      >
        {model || "模型"}
      </button>
      {open ? <Menu up alignRight items={items} onClose={() => setOpen(false)} /> : null}
    </span>
  );
}
