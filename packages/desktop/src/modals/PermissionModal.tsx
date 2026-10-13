import { serve, type PendingPrompt } from "../session/serve";
import { ModalManager } from "./ModalManager";

type Answer = { outcome: string; value?: string; values?: string[] };

export function PermissionModal({
  sessionId,
  pending,
}: {
  sessionId: string;
  pending: PendingPrompt;
}) {
  async function reply(answer: Answer) {
    await serve.reply(sessionId, pending.id, answer).catch(() => {});
  }

  const descriptions = (pending.payload?.descriptions as Record<string, string>) ?? {};
  const content = pending.payload?.content as string | undefined;
  return (
    <ModalManager onEscape={() => void reply({ outcome: "denied" })}>
      <div>
        <div className="modal-title">{pending.title}</div>
        {(pending.detail ?? []).map((line, i) => (
          <div key={i} className="modal-detail">{line}</div>
        ))}
        {content ? <pre className="modal-content">{content}</pre> : null}
        <div className="modal-options">
          {pending.options.map((opt) => (
            <button
              key={opt}
              className={opt === pending.options[0] ? "primary" : ""}
              onClick={() => void reply({ outcome: "answered", value: opt })}
              title={descriptions[opt] ?? ""}
            >
              {opt} {descriptions[opt] ? `· ${descriptions[opt]}` : ""}
            </button>
          ))}
          <button onClick={() => void reply({ outcome: "denied" })}>拒绝</button>
        </div>
      </div>
    </ModalManager>
  );
}
