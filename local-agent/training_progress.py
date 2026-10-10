"""Evidence-only curriculum graduation, recalculated from persistent SQLite.

No manual scores, no invented success, no stage flags that can get stale.
Use the latest ten genuinely executed/resolved trials for each basic skill.
"""
import json
import math

BASIC_SKILLS = ("observe", "orient", "navigate", "mine", "collect")
WINDOW = 10
MIN_SUCCESSES = 9
MIN_SITES = 3
SITE_SEPARATION_BLOCKS = 8.0
LOOKBACK = 250


def _decoded_record(record):
    """Old proposal-only and unverifiable legacy records cannot graduate."""
    try:
        payload = json.loads(record.get("actions") or "{}")
        outcome = json.loads(record.get("outcome") or "{}")
        attempts = payload.get("experiments") or []
        executed = [
            a for a in attempts
            if isinstance(a, dict) and a.get("status") in {"completed", "failed", "progress"}
            and a.get("result") is not None
        ]
        if not executed or not isinstance(outcome, dict):
            return None
        if outcome.get("verified") is not bool(record.get("success")):
            return None
        start = payload.get("start")
        if not isinstance(start, list) or len(start) != 3:
            return None
        start = tuple(float(v) for v in start)
        if not all(math.isfinite(v) for v in start):
            return None
        # Never count an episode with rejected proposals but zero game actions.
        return {
            "success": bool(record["success"]),
            "start": start,
            "dimension": str(payload.get("dimension") or "unknown"),
            "reason": str(outcome.get("outcome") or "")[:180],
        }
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return None


def _distinct_success_sites(samples):
    distinct = []
    for sample in samples:
        if not sample["success"]:
            continue
        if all(
            sample["dimension"] != site["dimension"]
            or math.hypot(sample["start"][0] - site["start"][0],
                          sample["start"][2] - site["start"][2])
            >= SITE_SEPARATION_BLOCKS
            for site in distinct
        ):
            distinct.append(sample)
    return len(distinct)


def assess_training(memory, session_episodes=()):
    """Return JSON-serializable, audit-friendly graduation evidence.

    Never claim readiness from less than ten resolved, evidence-backed attempts
    per basic skill. A pending body command in this run blocks advancement.
    """
    skills = {}
    for name in BASIC_SKILLS:
        records = memory.recent_skill_trials("training." + name, LOOKBACK)
        genuine = [parsed for row in records
                   if (parsed := _decoded_record(row)) is not None]
        latest = genuine[:WINDOW]
        successes = sum(row["success"] for row in latest)
        sites = _distinct_success_sites(latest)
        session = [e for e in session_episodes if e.get("task") == name]
        unresolved = sum(e.get("status") == "pending" for e in session)
        unavailable = sum(e.get("status") == "unavailable" for e in session)
        rejected = sum(
            a.get("status") == "rejected"
            for e in session for a in e.get("experiments", [])
        )
        blockers = []
        for reason in [e.get("reason") for e in session
                       if e.get("status") in {"failed", "pending", "unavailable"}] + [
                           e["reason"] for e in latest if not e["success"]
                       ]:
            if reason and reason not in blockers:
                blockers.append(reason)
        requirements = {
            "resolved_trials": len(latest) >= WINDOW,
            "recent_successes": successes >= MIN_SUCCESSES,
            "distinct_success_sites": sites >= MIN_SITES,
            "no_pending_jobs": unresolved == 0,
        }
        skills[name] = {
            "resolved_trials": len(latest),
            "successes": successes,
            "failures": len(latest) - successes,
            "window": WINDOW,
            "success_rate": round(successes / len(latest), 3) if latest else None,
            "distinct_success_sites": sites,
            "required_sites": MIN_SITES,
            "min_site_separation_blocks": SITE_SEPARATION_BLOCKS,
            "rejected_proposals_this_session": rejected,
            "unavailable_this_session": unavailable,
            "pending_this_session": unresolved,
            "blockers": blockers[:5],
            "requirements": requirements,
            "graduated": all(requirements.values()),
        }
    pending_any = any(e.get("status") == "pending" for e in session_episodes)
    ready = all(s["graduated"] for s in skills.values()) and not pending_any
    return {
        "stage": 2 if ready else 1,
        "stage_name": "independent_wood_gathering" if ready else "basic_physical_skills",
        "stage_1_graduated": ready,
        "skills": skills,
        "graduated_count": sum(s["graduated"] for s in skills.values()),
        "required_skill_count": len(BASIC_SKILLS),
        "stage_2_unlocked": ready,
        "stage_2_objective": "Gather a naturally occurring log through real actions"
        if ready else None,
        "stage_3_unlocked": False,
        "graduation_policy": {
            "latest_resolved_trials": WINDOW,
            "required_successes": MIN_SUCCESSES,
            "minimum_separated_success_sites": MIN_SITES,
            "minimum_site_separation_blocks": SITE_SEPARATION_BLOCKS,
            "pending_jobs_block_advancement": True,
            "format_only_failures_count": False,
            "unavailable_tasks_count": False,
        },
    }
