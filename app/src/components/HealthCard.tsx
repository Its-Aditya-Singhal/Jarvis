import { useState } from "react";
import { useStore } from "../lib/store";

/** Anything broken, in plain words, with the fix (safe to show anyone: no personal data). */
export default function HealthCard() {
  const { status } = useStore();
  const [open, setOpen] = useState<string | null>(null);
  const issues = status?.issues ?? [];
  if (!issues.length) return null;
  return (
    <div className="health">
      {issues.map((i) => (
        <button key={i.id} className={`health-item ${i.level}`} onClick={() => setOpen(open === i.id ? null : i.id)}>
          <span>
            {i.level === "error" ? "⚠" : "•"} {i.title}
          </span>
          {open === i.id ? <em>{i.fix}</em> : <em className="muted">How to fix ›</em>}
        </button>
      ))}
    </div>
  );
}
