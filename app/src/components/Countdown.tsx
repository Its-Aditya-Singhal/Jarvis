import { useEffect, useState } from "react";

/** Time left until ``due`` (ISO), ticking every second: "4:05", "1:02:09". */
export default function Countdown({ due }: { due: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const left = Math.max(0, Math.round((new Date(due).getTime() - now) / 1000));
  const h = Math.floor(left / 3600);
  const m = Math.floor((left % 3600) / 60);
  const s = left % 60;
  return <>{h ? `${h}:${String(m).padStart(2, "0")}` : m}:{String(s).padStart(2, "0")}</>;
}
