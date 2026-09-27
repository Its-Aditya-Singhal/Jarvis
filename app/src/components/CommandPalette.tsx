import { useEffect, useMemo, useRef, useState } from "react";

export interface PaletteItem {
  id: string;
  label: string;
  hint?: string;
  keys?: string;
  run: () => void;
}

/** Fuzzy-ish match: every typed character appears in order. */
function matches(label: string, q: string): boolean {
  let i = 0;
  const l = label.toLowerCase();
  for (const ch of q.toLowerCase().replace(/\s+/g, "")) {
    i = l.indexOf(ch, i);
    if (i < 0) return false;
    i++;
  }
  return true;
}

/** ⌘K: jump to any view or action from the keyboard. */
export default function CommandPalette({ items, onClose }: { items: PaletteItem[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const shown = useMemo(() => items.filter((it) => matches(`${it.label} ${it.hint ?? ""}`, q)), [items, q]);
  const current = Math.min(sel, Math.max(shown.length - 1, 0));

  useEffect(() => input.current?.focus(), []);

  const run = (it: PaletteItem | undefined) => {
    if (!it) return;
    onClose();
    it.run();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((current + 1) % Math.max(shown.length, 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((current - 1 + shown.length) % Math.max(shown.length, 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(shown[current]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div
        className="palette panel"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={onKey}
      >
        <input
          ref={input}
          className="palette-input"
          value={q}
          placeholder="Go to a view or run an action…"
          role="combobox"
          aria-expanded="true"
          aria-controls="palette-list"
          aria-activedescendant={shown[current] ? `palette-${shown[current].id}` : undefined}
          onChange={(e) => {
            setQ(e.target.value);
            setSel(0);
          }}
        />
        <ul id="palette-list" className="palette-list" role="listbox" aria-label="Results">
          {shown.map((it, i) => (
            <li
              key={it.id}
              id={`palette-${it.id}`}
              role="option"
              aria-selected={i === current}
              className={i === current ? "on" : ""}
              onMouseEnter={() => setSel(i)}
              onClick={() => run(it)}
            >
              <span>{it.label}</span>
              {it.hint && <em>{it.hint}</em>}
              {it.keys && <kbd>{it.keys}</kbd>}
            </li>
          ))}
          {!shown.length && <li className="palette-empty">Nothing matches “{q}”.</li>}
        </ul>
      </div>
    </div>
  );
}
