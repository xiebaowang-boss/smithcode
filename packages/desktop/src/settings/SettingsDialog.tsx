import type { ReactNode } from "react";
import { Blocks, Info, Palette, Plug, Settings2, X } from "lucide-react";
import { ModalManager } from "../modals/ModalManager";
import type { ThemeMode } from "../app/useTheme";
import type { WorkspaceInfo } from "../session/serve";
import { AboutPanel } from "./AboutPanel";
import { AppearancePanel } from "./AppearancePanel";
import { MCPPanel } from "./MCPPanel";
import { ProviderPanel } from "./ProviderPanel";
import { SkillsPanel } from "./SkillsPanel";
import type { SettingsSection } from "./sections";

const NAV: { key: SettingsSection; label: string; icon: ReactNode }[] = [
  { key: "appearance", label: "外观", icon: <Palette size={15} /> },
  { key: "provider", label: "模型服务", icon: <Settings2 size={15} /> },
  { key: "mcp", label: "MCP 服务器", icon: <Plug size={15} /> },
  { key: "skills", label: "技能", icon: <Blocks size={15} /> },
  { key: "about", label: "关于", icon: <Info size={15} /> },
];

export function SettingsDialog({
  section,
  onSection,
  onClose,
  mode,
  onTheme,
  workspace,
  sessionId,
  onLoadSkill,
  onRescanSkills,
}: {
  section: SettingsSection;
  onSection: (section: SettingsSection) => void;
  onClose: () => void;
  mode: ThemeMode;
  onTheme: (mode: ThemeMode) => void;
  workspace: WorkspaceInfo | null;
  sessionId: string | null;
  onLoadSkill: (name: string) => void;
  onRescanSkills: () => void;
}) {
  return (
    <ModalManager wide onEscape={onClose}>
      <div className="settings-dialog">
        <nav className="settings-nav" aria-label="设置分节">
          <div className="settings-nav-title">设置</div>
          {NAV.map(({ key, label, icon }) => (
            <button
              key={key}
              className={section === key ? "active" : ""}
              aria-current={section === key}
              onClick={() => onSection(key)}
            >
              {icon}
              {label}
            </button>
          ))}
          <span className="spacer" />
          <button onClick={onClose}>
            <X size={15} />
            关闭
          </button>
        </nav>
        <div className="settings-body">
          {section === "appearance" ? <AppearancePanel mode={mode} onChange={onTheme} /> : null}
          {section === "provider" ? <ProviderPanel /> : null}
          {section === "mcp" ? <MCPPanel /> : null}
          {section === "skills" ? (
            <SkillsPanel
              sessionId={sessionId}
              onLoadSkill={onLoadSkill}
              onRescan={onRescanSkills}
            />
          ) : null}
          {section === "about" ? <AboutPanel workspace={workspace} /> : null}
        </div>
      </div>
    </ModalManager>
  );
}
