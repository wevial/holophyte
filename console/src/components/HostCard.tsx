import type { ProjectChoice } from "../lib/attention";
import { age } from "../lib/format";
import { daemonsUp, hostTone, type HostGroup } from "../lib/hosts";
import { HostRow } from "./HostRow";

export { KEY_GLYPH } from "./HostRow";

/** One host's card in the rail footer: the host label over a row per
 *  daemon, with how long the daemons have been up in the foot. The card
 *  takes the bad border when any of its daemons does. */
export function HostCard({
  group,
  project,
  onProject,
}: {
  group: HostGroup;
  project: ProjectChoice;
  onProject: (project: ProjectChoice) => void;
}) {
  const bad = group.hosts.some((host) => hostTone(host) === "bad");
  const up = daemonsUp(group.hosts);
  const foot = up == null ? null : `${group.hosts.length === 1 ? "daemon" : "daemons"} up ${age(up)}`;
  return (
    <div data-host-label={group.label} className={`rounded-card bg-rail-card p-2 ${bad ? "border border-bad/50" : ""}`}>
      <div className="px-1 text-[11px] font-semibold text-rail-sub">{group.label}</div>
      <div className="mt-1 flex flex-col">
        {group.hosts.map((host) => (
          <HostRow
            key={host.address}
            host={host}
            selected={host.project != null && project === host.project}
            onClick={() => host.project != null && onProject(host.project)}
          />
        ))}
      </div>
      {foot && (
        <div data-foot className="mt-1 px-1 font-mono text-[10px] text-rail-faint">
          {foot}
        </div>
      )}
    </div>
  );
}
