import { Link } from "@tanstack/react-router";
import { keepScope } from "../scope";

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
