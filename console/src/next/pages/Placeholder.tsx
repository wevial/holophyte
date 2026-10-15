import { Link, useSearch } from "@tanstack/react-router";
import { keepScope } from "../scope";

export function Placeholder({
  title,
  sentence,
  classic,
}: {
  title: string;
  sentence: string;
  classic: { href: string; label: string };
}) {
  const { project } = useSearch({ from: "__root__" });
  return (
    <section aria-labelledby="page-title" className="flex max-w-[640px] flex-col gap-3 px-8 py-7">
      <div>
        <h1 id="page-title" className="text-[20px] font-semibold leading-tight text-ink">{title}</h1>
        <p data-scope className="font-mono text-[12px] text-ink-3">{project ?? "All projects"}</p>
      </div>
      <p className="text-[14px] text-ink-2">{sentence}</p>
      <a href={classic.href} className="self-start text-[13px] font-medium text-act underline underline-offset-2">
        {classic.label}
      </a>
    </section>
  );
}

export function NotFound() {
  return (
    <section className="flex flex-col gap-3 px-8 py-7">
      <p className="text-[14px] text-ink-2">There is no page here.</p>
      <Link
        to="/"
        search={keepScope}
        className="self-start text-[13px] font-medium text-act underline underline-offset-2"
      >
        Go to Overview
      </Link>
    </section>
  );
}
