import { useEffect, useState } from "react";
import { serve, type ProviderSnapshot } from "../session/serve";

export function ProviderPanel() {
  const [snap, setSnap] = useState<ProviderSnapshot | null>(null);
  const [url, setUrl] = useState("");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [budget, setBudget] = useState("");
  const [msg, setMsg] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function load() {
    setLoading(true);
    try {
      const s = await serve.settings();
      setSnap(s);
      setUrl(s.url);
      setModel(s.model);
      setBudget(String(s.budget));
      setKey("");
      setMsg("");
      setErrors({});
    } catch (e) {
      setMsg(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  function validate(): boolean {
    const next: Record<string, string> = {};
    if (url && !/^https?:\/\//.test(url.trim())) next.url = "接口地址应以 http(s):// 开头";
    if (budget && (!/^\d+$/.test(budget.trim()) || Number(budget) <= 0)) next.budget = "预算应为正整数";
    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function save() {
    if (!validate()) return;
    setSaving(true);
    try {
      const { changed } = await serve.applyProvider({
        url: url.trim(),
        model: model.trim(),
        key: key.trim(),
        budget: Number(budget) || 0,
      });
      const names = Object.keys(changed).join("、") || "无变化";
      setMsg(`已保存：${names}（下一轮生效）`);
      setKey("");
    } catch (e) {
      setMsg(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div aria-live="polite">
      <h2 className="settings-section-title">模型服务</h2>
      <p className="settings-section-hint">
        任何 OpenAI 兼容接口均可接入；密钥写入本机凭据库，不回显。
      </p>
      <div className="settings-field">
        <span className="label">接口地址</span>
        <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…" />
        {errors.url ? <span className="settings-err">{errors.url}</span> : null}
      </div>
      <div className="settings-field">
        <span className="label">模型</span>
        <input value={model} onChange={(e) => setModel(e.target.value)} />
      </div>
      <div className="settings-field">
        <span className="label">API Key</span>
        <input
          type="password"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          autoComplete="off"
          placeholder={snap?.key_configured ? "已配置（留空保留）" : "未配置"}
        />
      </div>
      <div className="settings-field">
        <span className="label">上下文预算（token）</span>
        <input value={budget} onChange={(e) => setBudget(e.target.value)} inputMode="numeric" />
        {errors.budget ? <span className="settings-err">{errors.budget}</span> : null}
      </div>
      <div className="modal-options">
        <button className="primary" onClick={() => void save()} disabled={saving}>
          {saving ? "保存中…" : "保存"}
        </button>
        <button onClick={() => void load()} disabled={loading}>
          {loading ? "加载中…" : "重新加载"}
        </button>
      </div>
      {msg ? <div className="settings-msg">{msg}</div> : null}
    </div>
  );
}
