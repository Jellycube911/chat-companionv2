"""Shared, bounded SQLite context retrieval for conversation and gameplay.

One persistent memory store supplies all turns; no extra database is created.
"""


def load_context(store, *, query="", goals=None, memory_limit=6,
                 skill_limit=5, trial_limit=5, episode_limit=5):
    """Return bounded memories, goals, skills and recent experience evidence."""
    if goals is None:
        goals = store.list_goals("active", 4)
    goals = list(goals)
    parts = [" ".join(str(query or "").split())]
    parts.extend(
        f"{goal.get('title', '')} {goal.get('description', '')}"
        for goal in goals[:3]
    )
    search_query = " ".join(part for part in parts if part).strip()[:900]
    memories = store.recall(search_query, limit=memory_limit) if search_query else []
    skills = (
        store.find_learned_skills(search_query, limit=skill_limit)
        if search_query else []
    )
    # Historical confidence counters include legacy mislabelled successes.
    # Only offer skills supported by at least two completed, evidence-backed
    # attempts. Untested teacher ideas stay in memory as hypotheses.
    verified_skills = []
    for skill in skills:
        trials = store.recent_skill_trials(skill["intent"], 80)
        verified = sum(t["success"] and t.get("evidence_status") == "verified"
                       for t in trials)
        if verified >= 2:
            verified_skills.append({**skill, "verified_successes": verified})

    return {
        "query": search_query,
        "goals": goals,
        "memories": memories,
        "skills": verified_skills,
        "trials": store.recent_trials_digest(trial_limit),
        "episodes": store.recent_episodes(episode_limit),
    }
