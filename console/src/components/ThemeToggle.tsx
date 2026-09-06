import { THEMES, type Theme } from "../lib/theme";

const LABELS: Record<Theme, string> = { system: "System", light: "Light", dark: "Dark" };

/** Three-way theme control at the foot of the rail. */
export function ThemeToggle({ theme, onChange }: { theme: Theme; onChange: (theme: Theme) => void }) {
  return (
    <div role="group" aria-label="Theme" className="flex gap-0.5">
      {THEMES.map((option) => (
        <button
          key={option}
          type="button"
          aria-pressed={theme === option}
          onClick={() => onChange(option)}
          className={`flex-1 rounded-button px-2 py-1 text-[11px] font-semibold ${
            theme === option ? "bg-rail-selected text-paper" : "text-rail-text hover:bg-rail-card"
          }`}
        >
          {LABELS[option]}
        </button>
      ))}
    </div>
  );
}
