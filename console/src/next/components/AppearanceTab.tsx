import type { ReactNode } from "react";
import { useReadingFont, useTheme, type ReadingFont } from "../appearance";
import type { Theme } from "../../lib/theme";

const THEMES: { value: Theme; name: string; line: string; colors: string[] }[] = [
  { value: "dark", name: "Night", line: "deep blue, pink for you", colors: ["#05070f", "#0b1020", "#f5a3c7"] },
  { value: "light", name: "Dawn", line: "light lavender paper", colors: ["#eceaf3", "#fbfafd", "#b02f6b"] },
  { value: "system", name: "System", line: "follows your computer", colors: ["#05070f", "#eceaf3", "#0b1020", "#fbfafd", "#f5a3c7", "#b02f6b"] },
];

const FONTS: { value: ReadingFont; name: string; line: string; face: string }[] = [
  { value: "sans", name: "Plex Sans", line: "default", face: '"IBM Plex Sans", Arial, sans-serif' },
  { value: "serif", name: "Plex Serif", line: "for long reads", face: '"IBM Plex Serif", Georgia, serif' },
  { value: "hyperlegible", name: "Hyperlegible", line: "clearest letterforms", face: '"Atkinson Hyperlegible Next", Arial, sans-serif' },
];

function Row({ title, help, children }: { title: string; help: string; children: ReactNode }) {
  const id = title === "Theme" ? "appearance-theme" : "appearance-font";
  return (
    <div className="grid gap-5 border-t border-rule-2 py-5 min-[860px]:grid-cols-[minmax(140px,0.8fr)_minmax(0,1.4fr)]">
      <div>
        <h3 id={id} className="font-sans text-[15px] font-semibold">{title}</h3>
        <p id={id + "-help"} className="mt-1.5 text-[14px] text-ink-2" style={{ fontFamily: '"IBM Plex Sans", Arial, sans-serif' }}>{help}</p>
      </div>
      <div role="radiogroup" aria-labelledby={id} aria-describedby={id + "-help"} className="grid min-w-0 gap-3 min-[1100px]:grid-cols-2">
        {children}
      </div>
    </div>
  );
}

function Card({ group, name, checked, onChange, children, swatch }: {
  group: string; name: string; checked: boolean; onChange: () => void; children: ReactNode; swatch?: ReactNode;
}) {
  return (
    <label className={`min-w-0 cursor-pointer border p-3 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-act ${checked ? "border-act bg-act-wash" : "border-rule bg-paper-2 hover:border-ink-3"}`}>
      {swatch}
      <span className="flex items-center gap-2 font-mono text-[12px] font-medium uppercase tracking-[0.06em]">
        <input type="radio" name={group} aria-label={name} checked={checked} onChange={onChange} className="size-3 shrink-0 accent-act" />
        {name}
      </span>
      {children}
    </label>
  );
}

export function AppearanceTab() {
  const { theme, setTheme, resolvedTheme } = useTheme();
  const { font, setFont } = useReadingFont();
  return (
    <div className="mt-5">
      <Row title="Theme" help="System follows your computer's light or dark setting.">
        {THEMES.map((item) => (
          <Card key={item.value} group="theme" name={item.name} checked={theme === item.value} onChange={() => setTheme(item.value)}
            swatch={<span aria-hidden="true" className="mb-2 flex h-11 border border-rule">{item.colors.map((color) => <span key={color} className="flex-1" style={{ backgroundColor: color }} />)}</span>}>
            <p className="mt-2 font-mono text-[12px] text-ink-3">{item.value === "system" && theme === "system" ? `now ${resolvedTheme === "dark" ? "Night" : "Dawn"}, from your computer` : item.line}</p>
          </Card>
        ))}
      </Row>
      <Row title="Reading font" help="For titles, summaries and agent text. Keys, times and counts stay in Plex Mono.">
        {FONTS.map((item) => (
          <Card key={item.value} group="reading-font" name={item.name} checked={font === item.value} onChange={() => setFont(item.value)}>
            <p className="mt-2 text-[15px]" style={{ fontFamily: item.face }}>The sweep frees a silent lease.</p>
            <p className="mt-3 font-mono text-[12px] text-ink-3">{item.line}</p>
          </Card>
        ))}
      </Row>
    </div>
  );
}
