import { useState } from "react";
import { ModalManager } from "../modals/ModalManager";

export function RenameDialog({
  initial,
  onClose,
  onSubmit,
}: {
  initial: string;
  onClose: () => void;
  onSubmit: (title: string) => Promise<void>;
}) {
  const [value, setValue] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit() {
    const title = value.trim();
    if (!title) {
      setError("名称不能为空");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await onSubmit(title);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false); // 成功时父组件已卸载；未关闭时必须复位，别卡在"保存中…"
    }
  }

  return (
    <ModalManager onEscape={onClose}>
      <div className="modal-title">重命名会话</div>
      <div className="modal-custom">
        <input
          value={value}
          autoFocus
          aria-label="会话名称"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void submit();
            }
          }}
        />
      </div>
      {error ? <div className="settings-err">{error}</div> : null}
      <div className="modal-options">
        <button className="primary" onClick={() => void submit()} disabled={busy}>
          {busy ? "保存中…" : "保存"}
        </button>
        <button onClick={onClose} disabled={busy}>
          取消
        </button>
      </div>
    </ModalManager>
  );
}
