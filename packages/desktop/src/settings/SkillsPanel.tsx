import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Loader2, RefreshCw } from "lucide-react";
import { serve, type SkillsPayload } from "../session/serve";

export function SkillsPanel({
  sessionId,
  onLoadSkill,
  onRescan,
}: {
  sessionId: string | null;
  onLoadSkill: (name: string) => void;
  onRescan: () => void;
}) {
  const [data, setData] = useState<SkillsPayload | null>(null);
  const [error, setError] = useState("");
  const [openName, setOpenName] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function refresh() {
    if (!sessionId) return;
    setLoading(true);
    try {
      setData(await serve.skills(sessionId));
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!sessionId) {
    return (
      <div>
        <h2 className="settings-section-title">技能</h2>
        <p className="settings-section-hint">技能是会话级的：开始一个会话后可查看与载入。</p>
      </div>
    );
  }

  return (
    <div>
      <h2 className="settings-section-title">技能</h2>
      <p className="settings-section-hint">
        项目技能来自 <code>.agents/skills</code>，用户技能来自 <code>~/.smithcode/skills</code>。
        载入后技能正文会作为消息进入对话。
      </p>
      <div className="modal-options" style={{ marginTop: 0 }}>
        <button onClick={() => void refresh()} disabled={loading}>
          {loading ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />} 刷新
        </button>
        <button onClick={onRescan}>重新扫描磁盘</button>
      </div>
      {error ? <div className="settings-err">{error}</div> : null}

      <div className="list-panel" style={{ marginTop: 14 }}>
        {data && data.skills.length === 0 ? (
          <div className="pane-empty">还没有发现技能。</div>
        ) : null}
        {data?.skills.map((s) => (
          <div key={s.name} className="list-row">
            <div className="grow">
              <div className="title">
                <button
                  className="ghost"
                  style={{ padding: 0 }}
                  onClick={() => setOpenName(openName === s.name ? null : s.name)}
                  aria-expanded={openName === s.name}
                >
                  {openName === s.name ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
                {s.name}
                <span className="tag">{s.scope}</span>
                {s.active ? <span className="tag">已载入</span> : null}
                {s.disabled ? <span className="tag warn">已禁用</span> : null}
                {!s.model_invocable ? <span className="tag">仅手动</span> : null}
              </div>
              <div className="desc">{s.description || s.location}</div>
              {openName === s.name ? (
                <pre className="log-box">{s.body || "（无正文）"}</pre>
              ) : null}
            </div>
            <div className="actions">
              <button onClick={() => onLoadSkill(s.name)} disabled={s.disabled || s.active}>
                载入
              </button>
            </div>
          </div>
        ))}
      </div>
      {data?.diagnostics?.length ? (
        <pre className="log-box">{data.diagnostics.join("\n")}</pre>
      ) : null}
    </div>
  );
}
