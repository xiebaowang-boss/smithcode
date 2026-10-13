import { useState } from "react";
import { isTauri } from "../app/desktop";
import { useSseStatus } from "../session/useSseStatus";
import type { WorkspaceInfo } from "../session/serve";
import pkg from "../../package.json";

export function AboutPanel({ workspace }: { workspace: WorkspaceInfo | null }) {
  const { connected, lastSeq } = useSseStatus();
  const [msg, setMsg] = useState("");

  async function restart() {
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const ok = await invoke("sidecar_restart");
      setMsg(ok ? "已请求重启 sidecar，界面会自动重连…" : "没有运行中的 sidecar 进程");
    } catch (e) {
      setMsg(String(e));
    }
  }

  return (
    <div>
      <h2 className="settings-section-title">关于</h2>
      <p className="settings-section-hint">SmithCode 桌面端 · {pkg.version}</p>
      <div className="kv-list">
        <div className="kv-row">
          <span className="k">工作区</span>
          <span className="v">
            <code>{workspace?.cwd ?? "未知"}</code>
          </span>
        </div>
        <div className="kv-row">
          <span className="k">服务连接</span>
          <span className="v">
            <span className={`conn-dot ${connected ? "ok" : "down"}`} />{" "}
            {connected ? `已连接${lastSeq ? `（seq ${lastSeq}）` : ""}` : "未连接"}
          </span>
        </div>
        {workspace?.extra?.length ? (
          <div className="kv-row">
            <span className="k">附加目录</span>
            <span className="v">{workspace.extra.join("、")}</span>
          </div>
        ) : null}
      </div>
      {isTauri() ? (
        <div className="modal-options">
          <button onClick={() => void restart()}>重启 sidecar</button>
        </div>
      ) : null}
      {msg ? <div className="settings-msg">{msg}</div> : null}
    </div>
  );
}
