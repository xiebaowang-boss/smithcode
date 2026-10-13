import { useCallback, useEffect, useState } from "react";
import { ChevronLeft, FileCode, FolderClosed, FolderOpen, Loader2, RefreshCw } from "lucide-react";
import { serve, type DirEntry } from "../session/serve";
import { fileSize } from "../ui/format";

interface Preview {
  path: string;
  content: string;
  truncated: boolean;
  size: number;
}

export function FileBrowser({ sessionId }: { sessionId?: string | null }) {
  const [dir, setDir] = useState("");
  const [entries, setEntries] = useState<DirEntry[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [file, setFile] = useState<Preview | null>(null);

  const loadDir = useCallback(async (path: string) => {
    setLoading(true);
    setError("");
    try {
      const data = await serve.listDir(path, sessionId);
      setEntries(data.entries);
      setTruncated(data.truncated);
      setDir(data.path);
      setFile(null);
    } catch (e) {
      setError(String(e));
      setEntries([]);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void loadDir("");
  }, [loadDir]);

  async function openFile(name: string) {
    const path = dir ? `${dir}/${name}` : name;
    setLoading(true);
    setError("");
    try {
      const data = await serve.readFile(path, sessionId);
      setFile({ path, content: data.content, truncated: data.truncated, size: data.size });
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  const crumbs = dir ? dir.split("/") : [];

  if (file) {
    const lines = file.content.split("\n");
    return (
      <div className="filebrowser">
        <div className="filebrowser-crumb">
          <button
            onClick={() => {
              setFile(null);
            }}
          >
            <ChevronLeft size={12} /> 返回目录
          </button>
          <span className="sep">/</span>
          <span title={file.path}>{file.path}</span>
        </div>
        <div className="filebrowser-hint">
          只读预览 · {fileSize(file.size)}
          {file.truncated ? " · 内容过长已截断" : ""}
        </div>
        <pre className="filebrowser-preview">
          {lines.map((line, i) => (
            <span key={i} className="filebrowser-line">
              <span className="filebrowser-no">{i + 1}</span>
              {line + "\n"}
            </span>
          ))}
        </pre>
      </div>
    );
  }

  return (
    <div className="filebrowser">
      <div className="filebrowser-crumb">
        <button onClick={() => void loadDir("")} title="工作区根目录">
          <FolderOpen size={12} /> 工作区
        </button>
        {crumbs.map((part, i) => {
          const path = crumbs.slice(0, i + 1).join("/");
          return (
            <span key={path} style={{ display: "inline-flex", alignItems: "center", gap: 2 }}>
              <span className="sep">/</span>
              <button onClick={() => void loadDir(path)} title={path}>
                {part}
              </button>
            </span>
          );
        })}
        <span className="spacer" />
        <button onClick={() => void loadDir(dir)} disabled={loading} aria-label="刷新目录">
          {loading ? <Loader2 size={12} className="spin" /> : <RefreshCw size={12} />}
        </button>
      </div>
      {error ? <div className="settings-err">{error}</div> : null}
      <div className="filebrowser-list">
        {entries.map((entry) =>
          entry.type === "dir" ? (
            <button
              key={entry.name}
              className="filebrowser-row"
              onClick={() => void loadDir(dir ? `${dir}/${entry.name}` : entry.name)}
            >
              <FolderClosed size={14} />
              <span className="grow">{entry.name}</span>
            </button>
          ) : (
            <button
              key={entry.name}
              className="filebrowser-row"
              onClick={() => void openFile(entry.name)}
            >
              <FileCode size={14} />
              <span className="grow">{entry.name}</span>
              <span className="size">{fileSize(entry.size)}</span>
            </button>
          ),
        )}
        {entries.length === 0 && !loading && !error ? (
          <div className="pane-empty">空目录</div>
        ) : null}
        {truncated ? <div className="filebrowser-hint">条目过多，仅显示前 400 项</div> : null}
      </div>
    </div>
  );
}
