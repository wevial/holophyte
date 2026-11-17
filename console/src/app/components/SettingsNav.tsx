import { useContext, type ReactNode } from "react";
import { Link, useRouter, useRouterState, useSearch } from "@tanstack/react-router";
import { projectColours } from "../dayLog";
import { switcherRows } from "../projects";
import { keepScope, useConsolePeers } from "../scope";
import { hostsNeedingAttention, settingsHost, SETTINGS_TABS } from "../settings";
import { SettingsMarksContext } from "../settingsDraft";
import { ProjectSwitcher } from "./ProjectSwitcher";
import {
  AgentsIcon,
  AppearanceIcon,
  BackIcon,
  CaptureIcon,
  GeneralIcon,
  HoldIcon,
  HostsIcon,
  LogIcon,
  ReviewIcon,
  TomlIcon,
} from "./icons";

type TabId = (typeof SETTINGS_TABS)[number]["id"];

const TAB_ICONS: Record<TabId, () => ReactNode> = {
  appearance: AppearanceIcon,
  log: LogIcon,
  hosts: HostsIcon,
  general: GeneralIcon,
  review: ReviewIcon,
  agents: AgentsIcon,
  capture: CaptureIcon,
  hold: HoldIcon,
  toml: TomlIcon,
};

const NAV_ON = "bg-(image:--nav-on) text-ink";

function BackRow({ back }: { back: string | null }) {
  const router = useRouter();
  const className = "group mx-2 mb-2.5 flex h-[30px] items-center gap-2 rounded-[6px] px-2 text-[14px] font-semibold tracking-[-0.01em] text-ink hover:bg-hover";
  const body = <><span className="flex text-ink-3 group-hover:text-ink"><BackIcon /></span><span>Settings</span></>;
  return back == null ? (
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
  );
}

function Hairline() {
  return <hr className="mx-4 my-[9px] h-px border-0 bg-rule-2" />;
}

export function SettingsNav({ back }: { back: string | null }) {
  const { hosts, now } = useConsolePeers();
  const search = useSearch({ from: "__root__" });
  const current = useRouterState({ select: (state) => /^\/settings\/([^/]+)/.exec(state.location.pathname)?.[1] ?? "general" });
  const { marks } = useContext(SettingsMarksContext);
  const rows = switcherRows(hosts, now, new Map()).filter((row) => row.name != null);
  const project = search.project ?? rows[0]?.name ?? "";
  const held = settingsHost(hosts, project)?.status?.admission === "held";
  const attention = hostsNeedingAttention(hosts);
  const count = (tab: string) => marks?.counts[tab] ?? 0;
  const link = (item: (typeof SETTINGS_TABS)[number]) => {
    const Icon = TAB_ICONS[item.id];
    const on = current === item.id;
    return (
      <Link
        key={item.id}
        to="/settings/$tab"
        params={{ tab: item.id }}
        search={keepScope}
        aria-current={on ? "page" : undefined}
        className={`mx-2 flex h-row items-center gap-2.5 rounded-[6px] px-2 text-[13px] font-medium ${on ? NAV_ON : "text-ink-2 hover:bg-hover hover:text-ink"}`}
      >
        <span className={`flex ${on ? "text-focus" : "text-ink-3"}`}><Icon /></span>
        <span className="flex-1">{item.label}</span>
        {item.group === "Project" && count(item.id) > 0 && <span className="font-mono text-[12px] text-act">{count(item.id)}</span>}
        {item.id === "toml" && marks?.checkRefused && <span className="font-mono text-[12px] text-bad">1</span>}
        {item.id === "hold" && held && <span className="text-[11.5px] text-wait">held</span>}
        {item.id === "hosts" && attention > 0 && <span className="font-mono text-[12px] text-need">{attention}</span>}
      </Link>
    );
  };
  const group = (name: (typeof SETTINGS_TABS)[number]["group"]) => SETTINGS_TABS.filter((item) => item.group === name).map(link);
  return (
    <div className="flex flex-col pt-1 pb-3">
      <BackRow back={back} />
      <nav aria-label="Settings sections" className="flex flex-col gap-px">
        {group("You")}
        <Hairline />
        {group("Factory")}
        <Hairline />
        {project !== "" && <ProjectSwitcher rows={rows} colours={projectColours(hosts)} folded={false} selected={project} />}
        {group("Project")}
      </nav>
    </div>
  );
}
