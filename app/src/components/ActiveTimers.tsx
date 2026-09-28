import { useEffect, useState } from "react";
import { ApiError, api, post } from "../lib/backend";
import { useStore } from "../lib/store";
import Countdown from "./Countdown";

interface Item {
  key: string;
  kind: "TIMER" | "LATER";
  label: string;
  due: string;
  cancel: string;
}

interface ToolsState {
  alarms: { id: number; kind: "alarm" | "timer"; due: string; label: string; status: string }[];
  delayed?: { id: string; due: string; summary: string }[];
}

/** Running timers and delayed actions ("close it after 10 seconds") on the main screen. */
export default function ActiveTimers() {
  const { toolsVersion } = useStore();
  const [items, setItems] = useState<Item[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    let live = true;
    api<ToolsState>("/api/tools")
      .then((d) => {
        if (!live) return;
        const timers: Item[] = d.alarms
          .filter((a) => a.kind === "timer" && a.status === "pending")
          .map((a) => ({ key: `t${a.id}`, kind: "TIMER", label: a.label, due: a.due, cancel: `/api/alarms/${a.id}/cancel` }));
        const later: Item[] = (d.delayed ?? []).map((x) => ({
          key: `d${x.id}`,
          kind: "LATER",
          label: x.summary,
          due: x.due,
          cancel: `/api/delayed/${x.id}/cancel`,
        }));
        setItems([...timers, ...later].sort((a, b) => a.due.localeCompare(b.due)));
        setError(null);
      })
      .catch(() => live && setItems([]));
    return () => {
      live = false;
    };
  }, [toolsVersion]);

  // drop finished ones without waiting for the next refetch
  const running = items.filter((i) => new Date(i.due).getTime() > now - 1000);
  useEffect(() => {
    if (!items.length) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [items.length]);
  if (!running.length) return null;

  const cancel = (i: Item) =>
    post(i.cancel)
      .then(() => setItems((all) => all.filter((x) => x.key !== i.key)))
      .catch((e) => setError(e instanceof ApiError ? e.message : "Backend unreachable"));

  return (
    <div className="timers" aria-label="Running timers">
      {running.map((i) => (
        <div key={i.key} className="timer-chip">
          <b className="tag">{i.kind}</b>
          <span className="timer-left">
            <Countdown due={i.due} />
          </span>
          {i.label && <span className="timer-label">{i.label}</span>}
          <button className="timer-cancel" aria-label={`Cancel ${i.label || "timer"}`} onClick={() => cancel(i)}>
            ✕
          </button>
        </div>
      ))}
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
