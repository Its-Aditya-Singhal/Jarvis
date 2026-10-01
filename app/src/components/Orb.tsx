import { useStore } from "../lib/store";

export type OrbMode = "idle" | "scanning" | "challenge" | "approved" | "denied" | "locked" | "offline";

interface Props {
  mode: OrbMode;
  /** unused (kept for callers): the old canvas ring followed a fixed level */
  level?: number;
  /** show when someone is talking or the assistant is answering */
  listen?: boolean;
  className?: string;
}

/**
 * The assistant's status mark: a ring in the state's colour. It used to be a canvas of
 * particles redrawn every frame (all day, even with nothing happening); now it is plain SVG
 * that only changes when the state does, with one gentle CSS pulse while speech is heard or
 * spoken (transform and opacity only, so the compositor does it without repainting).
 */
export default function Orb({ mode, listen = false, className = "" }: Props) {
  const { speaking, assistantSpeaking, thinking } = useStore();
  const live = listen && (speaking || assistantSpeaking || thinking);
  return (
    <div className={`orb orb-${mode} ${live ? "live" : ""} ${className}`} aria-hidden="true">
      <svg viewBox="0 0 100 100">
        <circle className="orb-track" cx="50" cy="50" r="46" />
        <circle className="orb-ring" cx="50" cy="50" r="46" />
        <circle className="orb-core" cx="50" cy="50" r="30" />
      </svg>
      <i className="orb-pulse" />
    </div>
  );
}
