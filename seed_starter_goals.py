r"""Seed the 6 starter monitoring goals into NEXUS (status='proposed').

All are read-only (risk=low, reversibility=reversible) and recurring (daily). The
`description` is the task prompt the agent runs on approval, written to use the
existing read-only native tools (unraid_status, channels_status,
homeassistant_status, adguard_status, unifi_status, hermes_status).

Run from the repo root:
    .\venv\Scripts\python.exe seed_starter_goals.py

Then open NEXUS -> Safety page and Approve the goals you want live. Re-running is
safe: identical titles/descriptions debounce as 'duplicate_active'.
"""

import asyncio

from backend.agents import goals

STARTER_GOALS = [
    {
        "title": "Unraid array capacity watch",
        "category": "storage",
        "cadence": "daily",
        "description": (
            "Check Unraid array storage using the unraid_status tool. Report current "
            "used and total capacity and the percent full. If usage is at or above 85%, "
            "identify the largest space consumers and propose specific cleanup candidates. "
            "Report only — never delete anything."
        ),
        "success_criteria": (
            "Array usage is below 85%, or a concrete cleanup proposal was produced when "
            "usage is at or above 85%."
        ),
    },
    {
        "title": "Channels DVR recording headroom",
        "category": "storage",
        "cadence": "daily",
        "description": (
            "Check Channels DVR storage using the channels_status tool. Report free vs "
            "total recording space. Warn if free space is low enough to threaten upcoming "
            "recordings."
        ),
        "success_criteria": "Channels DVR has sufficient free recording space; a warning was raised if low.",
    },
    {
        "title": "Integration uptime sweep",
        "category": "monitoring",
        "cadence": "daily",
        "description": (
            "Review the health of all homelab integrations using the status tools "
            "(homeassistant_status, unraid_status, unifi_status, adguard_status, "
            "channels_status, hermes_status). Report any integration that is currently "
            "down or has been flapping, and summarize overall availability."
        ),
        "success_criteria": "All integrations reachable; any outage or flapping integration is flagged.",
    },
    {
        "title": "Home Assistant unavailable-entity watch",
        "category": "monitoring",
        "cadence": "daily",
        "description": (
            "Check Home Assistant using the homeassistant_status tool for entities in an "
            "unavailable or unknown state. Report any new unavailable entities, ignoring "
            "known-noisy devices (phones, cast/echo/firestick, ipad). Keep it to genuinely "
            "new problems versus the normal baseline."
        ),
        "success_criteria": "No new unavailable Home Assistant entities beyond the known baseline.",
    },
    {
        "title": "AdGuard protection enabled",
        "category": "network",
        "cadence": "daily",
        "description": (
            "Check AdGuard using the adguard_status tool. Confirm DNS filtering/protection "
            "is enabled and report today's query and block counts. Alert if protection is "
            "disabled."
        ),
        "success_criteria": "AdGuard filtering/protection is enabled.",
    },
    {
        "title": "Channels DVR failed-recording check",
        "category": "media",
        "cadence": "daily",
        "description": (
            "Check Channels DVR for failed or skipped recordings using the channels_status "
            "tool, which reports a 'failed/skipped(24h)' count and titles. Report any failed "
            "or skipped recordings in the last day with the show/title so they can be "
            "re-scheduled. If the tool is unavailable, say so plainly — do not infer."
        ),
        "success_criteria": "channels_status reported zero failed/skipped recordings in the last 24 hours.",
    },
    {
        "title": "Proxmox pending-update check",
        "category": "maintenance",
        "cadence": "weekly",
        "description": (
            "Check for pending Proxmox (PVE) system updates using the proxmox_updates tool. "
            "Report how many apt packages are upgradable on the node and list the notable "
            "ones. If updates are pending, recommend scheduling a maintenance window to apply "
            "them — do not install anything yourself."
        ),
        "success_criteria": (
            "The pending Proxmox update count was retrieved and reported; if greater than "
            "zero, a maintenance-window recommendation was made."
        ),
    },
]


async def main() -> None:
    created, skipped = 0, 0
    for g in STARTER_GOALS:
        res = await goals.propose(
            g["title"],
            g["description"],
            actor="user",
            confidence=0.7,
            risk="low",
            reversibility="reversible",
            ttl_seconds=None,          # no expiry — persist until approved/rejected
            debounce_seconds=0,
            cadence=g["cadence"],
            category=g["category"],
            success_criteria=g["success_criteria"],
        )
        status = res.get("status")
        if status == "proposed":
            created += 1
        else:
            skipped += 1
        print(f"  [{status:<10}] {g['title']}  ({g['category']}/{g['cadence']})")
    print(f"\nDone: {created} proposed, {skipped} skipped (already existed).")
    print("Next: open NEXUS -> Safety and Approve the goals you want to run.")


if __name__ == "__main__":
    asyncio.run(main())
