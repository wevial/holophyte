import { useContext } from "react";
import { Link, useNavigate, useRouter, useRouterState, useSearch } from "@tanstack/react-router";
import { storeScope, switcherRows } from "../projects";
import { keepScope, useConsolePeers } from "../scope";
import { hostsNeedingAttention, settingsHost, SETTINGS_TABS } from "../settings";
import { SettingsMarksContext } from "../settingsDraft";
import { BackIcon } from "./icons";

const GROUPS = ["You", "Factory", "Project"] as const;

const GROUP_LABEL = "font-mono text-[11px] leading-[1.3] font-medium uppercase tracking-[0.1em] text-ink-3";

const SECTION_LINK =
  "relative flex min-h-11 items-center gap-3 border-l-2 px-4 text-[12px] font-medium uppercase tracking-[0.12em]";

function BackRow({ back }: { back: string | null }) {
  const router = useRouter();
  const className = "flex min-h-11 w-full items-center gap-2 border border-rule bg-paper px-3 text-[13px] font-medium text-ink hover:bg-paper-2";
  const body = <><BackIcon /><span>Back</span></>;
  return (
    <div className="px-3 pt-3 pb-1">
      {back == null ? (
        <Link to="/" search={keepScope} className={className}>{body}</Link>
      ) : (
        <a
          href={router.history.createHref(back)}
          onClick={(event) => {
            if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            void router.navigate({ href: back });
          }}
          className={className}
        >
          {body}
        </a>
      )}
    </div>
  );
}

export function SettingsNav({ back }: { back: string | null }) {
  const { hosts, now } = useConsolePeers();
  const search = useSearch({ from: "__root__" });
  const current = useRouterState({ select: (state) => /^\/settings\/([^/]+)/.exec(state.location.pathname)?.[1] ?? "general" });
  const navigate = useNavigate();
  const { marks } = useContext(SettingsMarksContext);
  const rows = switcherRows(hosts, now, new Map()).filter((row) => row.name != null);
  const project = search.project ?? rows[0]?.name ?? "";
  const held = settingsHost(hosts, project)?.status?.admission === "held";
  const attention = hostsNeedingAttention(hosts);
  const count = (tab: string) => marks?.counts[tab] ?? 0;
  return (
    <>
      <BackRow back={back} />
      <nav aria-label="Settings sections" className="flex flex-col pb-3">
        {GROUPS.map((group) => (
          <div key={group} role="group" aria-labelledby={`settings-group-${group}`} className="flex flex-col">
            {group === "Project" ? (
              <div className="flex items-center justify-between gap-2 px-4 pt-4 pb-1">
                <label id="settings-group-Project" htmlFor="settings-project" className={GROUP_LABEL}>Project</label>
                <select
                  id="settings-project"
                  value={project}
                  onChange={(event) => {
                    const project = event.target.value;
                    storeScope(project);
                    void navigate({ to: "/settings/$tab", params: { tab: current }, search: { project } });
                  }}
                  className="min-h-8 min-w-0 max-w-[60%] border border-rule bg-paper px-2 font-mono text-[12px] text-ink"
                >
                  {rows.map((row) => <option key={row.name} value={row.name!}>{row.label}{row.held ? " · held" : ""}</option>)}
                </select>
              </div>
            ) : (
              <p id={`settings-group-${group}`} className={`${GROUP_LABEL} px-4 pt-4 pb-1`}>{group}</p>
            )}
            {SETTINGS_TABS.filter((item) => item.group === group).map((item) => (
              <Link
                key={item.id}
                to="/settings/$tab"
                params={{ tab: item.id }}
                search={keepScope}
                aria-current={current === item.id ? "page" : undefined}
                className={`${SECTION_LINK} ${current === item.id ? "border-act bg-act-wash text-act" : "border-transparent text-ink-2 hover:bg-side-hover hover:text-ink"}`}
              >
                <span className="flex-1">{item.label}</span>
                {item.group === "Project" && count(item.id) > 0 && <span className="font-mono text-[11px] tracking-normal text-act">{count(item.id)}</span>}
                {item.id === "toml" && marks?.checkRefused && <span className="font-mono text-[11px] tracking-normal text-bad">1</span>}
                {item.id === "hold" && held && <span className="font-mono text-[11px] tracking-normal normal-case text-wait">held</span>}
                {item.id === "hosts" && attention > 0 && <span className="font-mono text-[12px] tracking-normal text-need">{attention}</span>}
              </Link>
            ))}
          </div>
        ))}
      </nav>
    </>
  );
}
