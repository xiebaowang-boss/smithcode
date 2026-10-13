import { useMemo, useState } from "react";
import { FileDiff, MessageSquarePlus } from "lucide-react";
import { useSessionStore } from "../session/sessionStore";
import { serve } from "../session/serve";

interface DiffLine {
  kind: "head" | "hunk" | "add" | "del" | "ctx";
  text: string;
}

interface DiffFile {
  name: string;
  lines: DiffLine[];
}

function parseUnified(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  for (const raw of text.split("\n")) {
    if (raw.startsWith("--- a/") || raw.startsWith("--- ")) {
      continue;
    }
    if (raw.startsWith("+++ ")) {
      const name = raw.slice(4).replace(/^b\//, "").trim() || "diff";
      current = { name, lines: [] };
      files.push(current);
      continue;
    }
    if (!current) {
      current = { name: "diff", lines: [] };
      files.push(current);
    }
    if (raw.startsWith("@@")) current.lines.push({ kind: "hunk", text: raw });
    else if (raw.startsWith("+")) current.lines.push({ kind: "add", text: raw });
    else if (raw.startsWith("-")) current.lines.push({ kind: "del", text: raw });
    else current.lines.push({ kind: "ctx", text: raw });
  }
  return files.filter((f) => f.lines.length > 0);
}

function collectDiffs(events: { type: string; data: unknown }[]): DiffFile[] {
  const out: DiffFile[] = [];
  for (const env of events) {
    if (env.type !== "session.tool.ended") continue;
    const result = (env.data as { result?: string }).result ?? "";
    if (!result.includes("--- ") || !result.includes("+++ ")) continue;
    out.push(...parseUnified(result));
  }
  return out;
}

export function hasDiffs(events: { type: string; data: unknown }[]): boolean {
  return events.some((env) => {
    if (env.type !== "session.tool.ended") return false;
    const result = (env.data as { result?: string }).result ?? "";
    return result.includes("--- ") && result.includes("+++ ");
  });
}

export function DiffPane({ sessionId }: { sessionId: string }) {
  const events = useSessionStore(
    (s) => s.tabs.find((t) => t.sessionId === sessionId)?.events ?? [],
  );
  const [commentFile, setCommentFile] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const files = useMemo(() => collectDiffs(events), [events]);

  async function sendComment() {
    if (!commentFile || !comment.trim()) return;
    await serve.prompt(sessionId, `关于 ${commentFile} 的 diff 评论：${comment.trim()}`).catch(() => {});
    setComment("");
    setCommentFile(null);
  }

  if (files.length === 0) {
    return (
      <div className="pane-empty">
        暂无 diff。写/编辑工具执行后，变更预览会自动聚合到这里；点行尾图标可针对文件评论。
      </div>
    );
  }
  return (
    <div className="diffpane">
      {files.map((f, fi) => (
        <details key={`${f.name}-${fi}`} open={fi === 0}>
          <summary>
            <FileDiff size={13} /> {f.name}
            <button
              title="评论此文件"
              onClick={(e) => {
                e.preventDefault();
                setCommentFile(f.name);
              }}
            >
              <MessageSquarePlus size={12} />
            </button>
          </summary>
          <pre className="diff-hunk">
            {f.lines.slice(0, 200).map((l, i) => (
              <span key={i} className={l.kind === "add" ? "diff-add" : l.kind === "del" ? "diff-del" : l.kind === "hunk" ? "diff-hunk-line" : undefined}>
                {l.text + "\n"}
              </span>
            ))}
          </pre>
        </details>
      ))}
      {commentFile ? (
        <div className="diff-comment">
          <div>评论 {commentFile}</div>
          <textarea value={comment} onChange={(e) => setComment(e.target.value)} rows={2} />
          <div className="modal-options">
            <button onClick={() => void sendComment()} disabled={!comment.trim()}>发送</button>
            <button onClick={() => setCommentFile(null)}>取消</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
