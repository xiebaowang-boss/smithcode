import { useState } from "react";
import { serve, type PendingPrompt } from "../session/serve";
import { ModalManager } from "./ModalManager";

interface Question {
  question: string;
  options: string[];
  descriptions: string[];
  multiple: boolean;
}

export function AskUserModal({
  sessionId,
  pending,
}: {
  sessionId: string;
  pending: PendingPrompt;
}) {
  const questions = (pending.payload?.questions as Question[]) ?? [];
  const [answers, setAnswers] = useState<string[]>([]);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [custom, setCustom] = useState("");

  async function reply(answer: { outcome: string; value?: string; values?: string[] }) {
    await serve.reply(sessionId, pending.id, answer).catch(() => {});
  }

  const cancel = () => void reply({ outcome: "cancelled" });
  const idx = answers.length;
  const q = questions[idx];

  if (questions.length === 0 || !q) {
    return (
      <ModalManager onEscape={cancel}>
        <div className="modal-title">确认提交</div>
        {questions.map((item, i) => (
          <div key={i} className="modal-review">
            {item.question} → {answers[i] || "（未答）"}
          </div>
        ))}
        <div className="modal-options">
          <button
            className="primary"
            onClick={() => void reply({ outcome: "answered", values: answers, value: answers[0] ?? "" })}
          >
            提交
          </button>
          <button onClick={cancel}>取消</button>
        </div>
      </ModalManager>
    );
  }

  function commit(value: string) {
    setAnswers([...answers, value]);
    setChecked(new Set());
    setCustom("");
  }

  return (
    <ModalManager onEscape={cancel}>
      <div className="modal-title">
        ({idx + 1}/{questions.length}) {q.question}
      </div>
      {q.options.map((opt, i) => (
        <label key={opt} className="askuser-opt">
          <input
            type={q.multiple ? "checkbox" : "radio"}
            name={`q-${idx}`}
            checked={q.multiple ? checked.has(opt) : undefined}
            onChange={() => {
              if (!q.multiple) {
                commit(opt);
                return;
              }
              const next = new Set(checked);
              if (next.has(opt)) next.delete(opt);
              else next.add(opt);
              setChecked(next);
            }}
          />
          {opt}
          {q.descriptions[i] ? <span className="askuser-desc">{q.descriptions[i]}</span> : null}
        </label>
      ))}
      {q.multiple ? (
        <div className="modal-options">
          <button
            className="primary"
            onClick={() => {
              const picked = q.options.filter((o) => checked.has(o));
              commit(custom ? [...picked, custom].join("，") : picked.join("，") || "（未选择）");
            }}
          >
            下一题
          </button>
        </div>
      ) : null}
      <div className="modal-custom">
        <input
          value={custom}
          onChange={(e) => setCustom(e.target.value)}
          placeholder="自定义回答…（回车提交本题）"
          onKeyDown={(e) => {
            if (e.key === "Enter" && custom.trim()) {
              e.stopPropagation();
              commit(custom.trim());
            }
          }}
        />
      </div>
      <div className="modal-options">
        <button onClick={cancel}>取消</button>
      </div>
    </ModalManager>
  );
}
