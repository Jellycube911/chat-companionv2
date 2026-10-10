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
    return {
        "query": search_query,
        "goals": goals,
        "memories": memories,
        "skills": skills,
        "trials": store.recent_trials(trial_limit),
        "episodes": store.recent_episodes(episode_limit),
    }
