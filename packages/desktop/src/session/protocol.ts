import type { SmithEnvelope, SmithEventMap, SmithEventType } from "../../../protocol/events";

export type { SmithEnvelope, SmithEventMap, SmithEventType };

export function isType<T extends SmithEventType>(
  env: SmithEnvelope,
  type: T,
): env is SmithEnvelope<SmithEventMap[T]> {
  return env.type === type;
}

export function dataOf<T extends SmithEventType>(
  env: SmithEnvelope,
  type: T,
): SmithEventMap[T] | null {
  if (env.type !== type) return null;
  return env.data as SmithEventMap[T];
}
