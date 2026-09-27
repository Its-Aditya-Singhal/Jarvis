import { useState } from "react";
import { ApiError, VoiceGender, post } from "../lib/backend";

const OPTIONS: { id: VoiceGender; label: string; note: string }[] = [
  { id: "female", label: "FEMALE", note: "Warm, calm · Hindi voice included" },
  { id: "male", label: "MALE", note: "Steady, calm · Hindi voice included" },
];

/** Female / male voice choice with a spoken preview (synthesised locally). */
export default function VoicePicker({
  value,
  onChange,
  disabled = false,
}: {
  value: VoiceGender;
  onChange: (g: VoiceGender) => void;
  disabled?: boolean;
}) {
  const [error, setError] = useState<string | null>(null);
  const preview = async (g: VoiceGender) => {
    setError(null);
    try {
      await post("/api/speech/preview", { gender: g });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Backend unreachable");
    }
  };
  return (
    <div>
      <div className="voice-picker" role="radiogroup">
        {OPTIONS.map((o) => (
          <div
            key={o.id}
            role="radio"
            aria-checked={value === o.id}
            tabIndex={0}
            className={`voice-option ${value === o.id ? "active" : ""} ${disabled ? "disabled" : ""}`}
            onClick={() => !disabled && onChange(o.id)}
            onKeyDown={(e) => (e.key === " " || e.key === "Enter") && !disabled && onChange(o.id)}
          >
            <b>{o.label}</b>
            <span>{o.note}</span>
            <button
              type="button"
              className="btn ghost"
              disabled={disabled}
              onClick={(e) => {
                e.stopPropagation();
                preview(o.id);
              }}
            >
              ▶ PREVIEW
            </button>
          </div>
        ))}
      </div>
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
