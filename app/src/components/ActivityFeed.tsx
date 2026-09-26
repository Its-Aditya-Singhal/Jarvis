import { useEffect, useRef } from "react";
import { useStore } from "../lib/store";

export default function ActivityFeed() {
  const { activity } = useStore();
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [activity.length]);

  return (
    <aside className="activity panel">
      <div className="panel-title">ACTIVITY</div>
      <div className="activity-list">
        {activity.length === 0 && <div className="muted">Waiting for events…</div>}
        {activity.map((a, i) => (
          <div key={`${a.ts}-${i}`} className={`activity-item level-${a.level}`}>
            <time>{new Date(a.ts * 1000).toLocaleTimeString([], { hour12: false })}</time>
            <span>{a.text}</span>
          </div>
        ))}
        <div ref={endRef} />
      </div>
    </aside>
  );
}
