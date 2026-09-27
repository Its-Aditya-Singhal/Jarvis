import { useState } from "react";
import { ApiError, post } from "../lib/backend";
import { dropSuggestion, useStore } from "../lib/store";

/** "Save to memory?" offers after you mention something personal. Nothing is saved without a tap. */
export default function SuggestionChips() {
  const { suggestions } = useStore();
  const [error, setError] = useState<string | null>(null);
  if (!suggestions.length) return null;
  const answer = async (id: string, accept: boolean) => {
    setError(null);
    try {
      await post(`/api/memory/suggestions/${id}`, { accept });
      dropSuggestion(id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Backend unreachable");
    }
  };
  return (
    <div className="suggestions">
      {suggestions.map((s) => (
        <div key={s.id} className="suggestion-chip">
          <span className="suggestion-label">SAVE TO MEMORY?</span>
          <span className="suggestion-text">{s.text}</span>
          <button className="mini ok" onClick={() => answer(s.id, true)}>
            SAVE
          </button>
          <button className="mini" onClick={() => answer(s.id, false)} title="Dismiss">
            ✕
          </button>
        </div>
      ))}
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
