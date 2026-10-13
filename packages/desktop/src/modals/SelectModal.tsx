import { ModalManager } from "./ModalManager";

export interface SelectItem {
  label: string;
  value: string;
  current: boolean;
  description?: string;
  trailing?: string;
}

export interface SelectState {
  title: string;
  command: string;
  items: SelectItem[];
  readonly?: boolean;
}

export function SelectModal({
  state,
  onPick,
  onClose,
}: {
  state: SelectState;
  onPick: (value: string) => void;
  onClose: () => void;
}) {
  return (
    <ModalManager onEscape={onClose}>
      <div className="modal-title">{state.title}</div>
      <div className="select-list" role="listbox" aria-label={state.title}>
        {state.items.map((item) => (
          <button
            key={item.value}
            role="option"
            aria-selected={item.current}
            className={item.current ? "current" : ""}
            disabled={state.readonly}
            onClick={() => onPick(item.value)}
          >
            {item.trailing ? <span className="trailing">{item.trailing}</span> : null}
            {item.label} {item.current ? "（当前）" : ""}
            {item.description ? <span className="desc">{item.description}</span> : null}
          </button>
        ))}
      </div>
      <div className="modal-options">
        <button onClick={onClose}>关闭</button>
      </div>
    </ModalManager>
  );
}
