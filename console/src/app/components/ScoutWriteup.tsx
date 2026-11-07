import type { WriteupSection } from "../../lib/contract.gen";

/** A report's write-up: each section's heading over its text. */
export function ScoutWriteup({ sections }: { sections: WriteupSection[] }) {
  return (
    <div data-scout-writeup className="flex max-w-[70ch] flex-col gap-6">
      {sections.map((section, index) => (
        <section key={index} data-writeup-section>
          <h2 className="m-0 font-sans text-[15px] font-semibold leading-[1.4] text-ink [overflow-wrap:anywhere]">{section.heading}</h2>
          <p className="m-0 mt-1.5 whitespace-pre-line font-read text-[16px] leading-[1.6] text-ink-2 [overflow-wrap:anywhere]">{section.text}</p>
        </section>
      ))}
    </div>
  );
}
