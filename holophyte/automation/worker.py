"""An act firing's worker: take the lease, recheck, rerun main's failed jobs once."""
import os
import time

import store
import store.automations
from holophyte.automation.automation_config import (
    ACT,
    main_watch_config,
    unapproved,
)
from holophyte.automation.gate import inhibitor
from holophyte.automation.main_watch import REVISION, workflow_run_attempt
from holophyte.automation.policy import refusal, report_only, report_only_reason
from holophyte.babysit.check_fix import rerun_failed_jobs
from holophyte.config.project import Project
from holophyte.host.registry import Host, HostError, home
from holophyte.loop.gates import InfraFailure
from holophyte.loop.runs import open_store
from holophyte.pr import github
from store.enums import FiringState

CANCELED = FiringState.CANCELED.value
REFUSED = FiringState.REFUSED.value
CONFIRMED = FiringState.CONFIRMED.value


def now_ms():
    return int(time.time() * 1000)


def config_change(target, firing):
    try:
        watch = main_watch_config(Project.locate(target.path, adopt=False))
    except (ValueError, SystemExit) as bad:
        return f"the config no longer reads: {bad}"
    if watch.mode != ACT:
        return f"[automations.{firing.automation}] mode is now {watch.mode}"
    stale = unapproved(watch, REVISION)
    if stale is not None:
        return stale
    if watch.revision != firing.revision:
        return (f"[automations.{firing.automation}] revision is now"
                f" {watch.revision}, not rule revision {firing.revision}")
    return None


def report_only_listing(target):
    try:
        listed = report_only(Host.locate(home()))
    except HostError as bad:
        return f"the host config no longer reads: {bad}"
    pull = github._origin_pull(target)
    return None if pull is None else report_only_reason(pull, listed)


def standing_refusal(conn, target, firing):
    stopped = (inhibitor(home(), conn, firing.automation)
               or config_change(target, firing)
               or report_only_listing(target))
    if stopped is not None:
        return CANCELED, stopped
    refused = refusal(firing.automation, firing.detail.get("action"))
    return None if refused is None else (REFUSED, refused)


class Worker:
    def __init__(self, conn, target, firing):
        self.conn, self.target, self.firing = conn, target, firing

    def settle(self, state, reason, detail=None):
        store.automations.settle_firing(
            self.conn, self.firing.id, self.firing.workerPid, state, reason,
            now=now_ms(), detail=detail)
        print(f"[holo2] firing {self.firing.id} {state}: {reason}", flush=True)
        return 0

    def settled_if_refused(self, refused=None):
        with store.transaction(self.conn):
            found = standing_refusal(self.conn, self.target, self.firing) or (
                None if refused is None else (REFUSED, refused))
            if found is not None:
                self.settle(*found)
            return found is not None

    def still_leased(self):
        current = store.automations.firing(self.conn, self.firing.id)
        return (current is not None
                and current.state == store.automations.LEASED
                and current.workerPid == self.firing.workerPid)

    def attempt_refusal(self, pull, run, recorded):
        if pull is None:
            return "the project has no GitHub origin"
        try:
            attempt = workflow_run_attempt(self.target, pull, run)
        except InfraFailure as bad:
            return f"GitHub refused the read: {bad}"
        if attempt > recorded:
            return (f"workflow run {run} is at attempt {attempt}, past"
                    f" attempt {recorded} seen at detection; someone reran"
                    " it already")
        return None

    def run(self):
        if self.settled_if_refused():
            return 0
        pull = github._origin_pull(self.target)
        detail = self.firing.detail
        run, recorded = detail["workflow_run_id"], detail["run_attempt"]
        refused = self.attempt_refusal(pull, run, recorded)
        with store.transaction(self.conn):
            if self.settled_if_refused(refused):
                return 0
            if not self.still_leased():
                print(f"[holo2] firing {self.firing.id} was settled before"
                      " its rerun; no rerun sent", flush=True)
                return 0
            try:
                rerun_failed_jobs(self.target, pull, run)
            except InfraFailure as bad:
                return self.settle(REFUSED, f"GitHub refused the rerun: {bad}")
            return self.settle(
                CONFIRMED, f"reran the failed jobs of workflow run {run} at"
                           f" {detail['sha']}, attempt {recorded + 1}",
                {**detail, "rerun_attempt": recorded + 1})


def automation_firing_mode(target, firing_id):
    conn = open_store(target)
    try:
        firing = store.automations.take_firing(conn, firing_id, os.getpid(),
                                               now=now_ms())
        if firing is None:
            print(f"[holo2] firing {firing_id} is not leased to a waiting"
                  " worker; nothing to do", flush=True)
            return 0
        return Worker(conn, target, firing).run()
    finally:
        conn.close()
