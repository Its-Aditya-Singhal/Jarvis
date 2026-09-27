import { useRef } from "react";

/** Ref that always holds the latest value (for callbacks used inside timers). */
export function useLatest<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}
