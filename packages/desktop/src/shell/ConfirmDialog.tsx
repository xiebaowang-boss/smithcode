import { useState } from "react";
import { ModalManager } from "../modals/ModalManager";

export function ConfirmDialog({
  title,
  detail,
  confirmLabel = "确认",
  danger,
  onClose,
  onConfirm,
}: {
  title: string;
  detail?: string;
  confirmLabel?: string;
  danger?: boolean;
  onClose: () => void;
  onConfirm: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function run() {
    setBusy(true);
    setError("");
    try {
      await onConfirm();
    } catch (e) {
      setError(String(e));
    } finally {
      // 成功时父组件通常已卸载本弹窗；失败/未关闭时必须复位 busy，
      // 否则按钮永远停在"处理中…"（React 18 对已卸载组件的 setState 是空操作）
      setBusy(false);
    }
  }

  return (
    <ModalManager onEscape={onClose}>
      <div className="modal-title">{title}</div>
      {detail ? <div className="modal-detail">{detail}</div> : null}
      {error ? <div className="settings-err">{error}</div> : null}
      <div className="modal-options">
        <button
          className={danger ? "primary danger" : "primary"}
          onClick={() => void run()}
          disabled={busy}
        >
          {busy ? "处理中…" : confirmLabel}
        </button>
        <button onClick={onClose} disabled={busy}>
          取消
        </button>
      </div>
    </ModalManager>
  );
}
