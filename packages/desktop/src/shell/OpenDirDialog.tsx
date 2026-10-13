import { useState } from "react";
import { ModalManager } from "../modals/ModalManager";

/**
 * 「打开目录…」的手输兜底：登记一个项目目录并切成当前项目。
 *
 * 桌面壳走原生系统选择器（`pickDirectory()`），只有浏览器 dev 没有原生壳、
 * 或插件不可用时才落到这个对话框——手输路径在这里不是死路。
 */
export function OpenDirDialog({
  onClose,
  onSubmit,
}: {
  onClose: () => void;
  onSubmit: (path: string) => Promise<void>;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit() {
    const path = value.trim();
    if (!path) {
      setError("请输入目录路径");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await onSubmit(path);
      onClose();
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  return (
    <ModalManager onEscape={onClose}>
      <div className="modal-title">打开项目目录</div>
      <div className="modal-custom">
        <input
          value={value}
          autoFocus
          placeholder="/home/you/projects/foo"
          aria-label="项目目录路径"
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void submit();
            }
          }}
        />
      </div>
      <div className="modal-detail">目录必须存在；打开后新建会话会落在该目录内。</div>
      {error ? <div className="settings-err">{error}</div> : null}
      <div className="modal-options">
        <button className="primary" onClick={() => void submit()} disabled={busy}>
          {busy ? "打开中…" : "打开"}
        </button>
        <button onClick={onClose} disabled={busy}>
          取消
        </button>
      </div>
    </ModalManager>
  );
}
