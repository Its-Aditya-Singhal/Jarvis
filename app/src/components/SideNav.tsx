export type View = "system" | "auth" | "memory" | "tools" | "security" | "settings";

const ITEMS: { id: View; label: string; phase?: number }[] = [
  { id: "system", label: "System Status" },
  { id: "auth", label: "Authentication" },
  { id: "memory", label: "Memory", phase: 8 },
  { id: "tools", label: "Tools", phase: 6 },
  { id: "security", label: "Security" },
  { id: "settings", label: "Settings" },
];

export default function SideNav({ view, onChange }: { view: View; onChange: (v: View) => void }) {
  return (
    <nav className="sidenav panel">
      <div className="panel-title">NAVIGATION</div>
      {ITEMS.map((it) => (
        <button
          key={it.id}
          className={`nav-item ${view === it.id ? "active" : ""}`}
          disabled={it.phase !== undefined}
          onClick={() => onChange(it.id)}
          title={it.phase ? `Arrives in phase ${it.phase}` : undefined}
        >
          <i className="dot" />
          <span>{it.label}</span>
          {it.phase && <em className="soon">P{it.phase}</em>}
        </button>
      ))}
    </nav>
  );
}
