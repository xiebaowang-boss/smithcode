import { useState } from "react";
import { Bot, Hand, Pencil } from "lucide-react";
import { serve } from "../session/serve";
import { Menu, type MenuEntry } from "../ui/Menu";

const MODES: { value: string; label: string; hint: string; icon: typeof Hand }[] = [
  { value: "smith", label: "请求批准", hint: "危险操作先确认", icon: Hand },
  { value: "accept_edits", label: "自动接受编辑", hint: "写文件不再确认", icon: Pencil },
  { value: "auto", label: "完全自动", hint: "所有工具直接执行", icon: Bot },
];

export function PermissionPicker({
  sessionId,
  mode,
  onChanged,
}: {
  sessionId: string | null;
  mode: string;
  onChanged: (mode: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const current = MODES.find((m) => m.value === mode) ?? MODES[0];
  const Icon = current.icon;

  async function pick(value: string) {
    if (!sessionId) return;
    try {
      const result = await serve.permission(sessionId, value);
      onChanged(result.mode);
      setError("");
    } catch (e) {
      setError(String(e));
    }
  }

  const items: MenuEntry[] = [
    { heading: "权限模式" },
    ...MODES.map((m) => ({
      label: m.label,
      icon: <m.icon size={13} />,
      hint: m.hint,
      current: m.value === mode,
      onSelect: () => void pick(m.value),
    })),
  ];

  return (
    <span className="popover-anchor">
      <button
        className="pill-btn"
        title={error || "权限模式"}
        disabled={!sessionId}
        onClick={() => setOpen(!open)}
      >
        <Icon size={14} />
        {current.label}
      </button>
      {open ? <Menu up items={items} onClose={() => setOpen(false)} /> : null}
    </span>
  );
}
