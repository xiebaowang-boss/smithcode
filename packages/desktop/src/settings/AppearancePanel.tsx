import { Laptop, Moon, Sun } from "lucide-react";
import type { ThemeMode } from "../app/useTheme";

const OPTIONS: { key: ThemeMode; label: string; swatch: string; icon: typeof Sun }[] = [
  { key: "light", label: "浅色", swatch: "light", icon: Sun },
  { key: "dark", label: "深色", swatch: "dark", icon: Moon },
  { key: "system", label: "跟随系统", swatch: "system", icon: Laptop },
];

export function AppearancePanel({
  mode,
  onChange,
}: {
  mode: ThemeMode;
  onChange: (mode: ThemeMode) => void;
}) {
  return (
    <div>
      <h2 className="settings-section-title">外观</h2>
      <p className="settings-section-hint">主题即时生效，选择会保存在本机。</p>
      <div className="theme-options">
        {OPTIONS.map(({ key, label, swatch, icon: Icon }) => (
          <button
            key={key}
            className={mode === key ? "active" : ""}
            aria-pressed={mode === key}
            onClick={() => onChange(key)}
          >
            <span className={`theme-swatch ${swatch}`} />
            <span>
              <Icon size={13} /> {label}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
