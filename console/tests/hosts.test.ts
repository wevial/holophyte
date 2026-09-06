import { expect, test } from "bun:test";
import { hostItems, mergeHosts, peerAddresses, visibleHosts, type PollResult } from "../src/lib/hosts";
import type { Status } from "../src/lib/types";
import { NO_ATTENTION, fixture, hostOf } from "./harness";

const working = await fixture<Status>("working.json");
const second = await fixture<Status>("idle_second_host.json");

const ok = (address: string, status: Status): PollResult => ({
  address,
  base: `http://${address}`,
  ok: true,
  status,
  attention: NO_ATTENTION,
});
const down = (address: string, error: string): PollResult => ({ address, base: `http://${address}`, ok: false, error });

test("/peers lists the origin first and each peer once; the origin's own address is never repeated", () => {
  const hosts = peerAddresses("http://writer:7710", { self: "writer:7710", peers: ["writer-2:7710", "writer:7710"] });
  expect(hosts).toEqual([
    { address: "writer:7710", base: "http://writer:7710" },
    { address: "writer-2:7710", base: "http://writer-2:7710" },
  ]);
  expect(peerAddresses("http://writer:7710", { daemons: ["writer-2:7710"] }).map((host) => host.address)).toEqual([
    "writer:7710",
    "writer-2:7710",
  ]);
  expect(peerAddresses("http://writer:7710", null)).toEqual([{ address: "writer:7710", base: "http://writer:7710" }]);
});

test("a timed-out peer keeps its last good status beside the error, and the next good answer clears it", () => {
  const first = mergeHosts([], [ok("writer:7710", working), ok("writer-2:7710", second)], 1_000);
  expect(first.map((host) => [host.address, host.project, host.error, host.seen_ms])).toEqual([
    ["writer:7710", "/srv/dev/writer", null, 1_000],
    ["writer-2:7710", "/srv/dev/writer-2", null, 1_000],
  ]);

  const then = mergeHosts(first, [ok("writer:7710", working), down("writer-2:7710", "http://writer-2:7710/status timed out")], 11_000);
  const [, lost] = then;
  expect(lost!.error).toBe("http://writer-2:7710/status timed out");
  expect(lost!.status).toEqual(second);
  expect(lost!.project).toBe("/srv/dev/writer-2");
  expect(lost!.seen_ms).toBe(1_000);
  expect(lost!.polled_ms).toBe(11_000);
  // The unreachable row is one critical item on the host's project, aged from the last good answer.
  const items = hostItems(lost!, 51_000);
  expect(items.length).toBe(1);
  expect(items[0]).toMatchObject({ kind: "unreachable", level: "critical", project: "/srv/dev/writer-2", last_seen_ms: 1_000 });

  const back = mergeHosts(then, [ok("writer:7710", working), ok("writer-2:7710", second)], 21_000);
  expect(back[1]!.error).toBeNull();
  expect(back[1]!.seen_ms).toBe(21_000);

  // A peer that has never answered is a record with no status at all.
  const never = mergeHosts(first, [ok("writer:7710", working), down("writer-3:7710", "connection refused")], 31_000);
  expect(never[1]).toMatchObject({ address: "writer-3:7710", status: null, project: null, seen_ms: null, error: "connection refused" });
});

test("visibleHosts narrows to the selected project's daemon and All projects restores the rest", () => {
  const hosts = [hostOf(working, NO_ATTENTION, "http://writer:7710"), hostOf(second, NO_ATTENTION, "http://writer-2:7710")];
  expect(visibleHosts(hosts, "/srv/dev/writer-2").map((host) => host.address)).toEqual(["writer-2:7710"]);
  expect(visibleHosts(hosts, "/srv/dev/elsewhere")).toEqual([]);
  expect(visibleHosts(hosts, "all")).toEqual(hosts);
});
