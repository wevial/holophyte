import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import type { runPage } from "../run";

export const CRUMB_LINK = "border-b border-rule text-ink-3 no-underline hover:border-ink hover:text-ink";

export function zoneName(at: number): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" }).formatToParts(at);
  return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
}

export function Page({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="px-6 pb-8 pt-5 max-[720px]:px-4">
      <section aria-label={label} className="mx-auto max-w-[1180px]">
        {children}
      </section>
    </div>
  );
}

export function Notice({ label, text, back }: { label: string; text: string; back?: ReturnType<typeof runPage> }) {
  return (
    <Page label={label}>
      <p className="m-0 font-mono text-[13px] text-ink-3">{text}</p>
      <Link {...(back ?? { to: "/runs", search: {} })} className="mt-3 inline-block font-sans text-[13px] font-medium text-act underline underline-offset-2">
        {back ? "Back to the run" : "Back to Runs"}
      </Link>
    </Page>
  );
}
