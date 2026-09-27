export type View = "system" | "auth" | "memory" | "tools" | "security" | "privacy" | "settings" | "about";

export const ITEMS: { id: View; label: string; phase?: number }[] = [
  { id: "system", label: "System Status" },
  { id: "auth", label: "Authentication" },
  { id: "memory", label: "Memory" },
  { id: "tools", label: "Tools" },
  { id: "security", label: "Security" },
  { id: "privacy", label: "Privacy" },
  { id: "settings", label: "Settings" },
  { id: "about", label: "About" },
];

/** ⌘1…⌘7 for the first seven views, ⌘I for About. */
export const shortcut = (id: View) => (id === "about" ? "⌘I" : `⌘${ITEMS.findIndex((i) => i.id === id) + 1}`);

export default function SideNav({ view, onChange }: { view: View; onChange: (v: View) => void }) {
  return (
    <nav className="sidenav panel" aria-label="Views">
      <div className="panel-title" aria-hidden="true">NAVIGATION</div>
      {ITEMS.map((it) => (
        <button
          key={it.id}
          className={`nav-item ${view === it.id ? "active" : ""}`}
          disabled={it.phase !== undefined}
          onClick={() => onChange(it.id)}
          aria-current={view === it.id ? "page" : undefined}
          title={it.phase ? `Arrives in phase ${it.phase}` : `${it.label} (${shortcut(it.id)})`}
        >
          <i className="dot" aria-hidden="true" />
          <span>{it.label}</span>
          <kbd className="nav-key" aria-hidden="true">{shortcut(it.id)}</kbd>
          {it.phase && <em className="soon">P{it.phase}</em>}
        </button>
      ))}
    </nav>
  );
}
