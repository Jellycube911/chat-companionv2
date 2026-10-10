"""Data-driven crafting knowledge from the connected Minecraft server.

No hard-coded axes, pickaxes, or mod recipes. The local RecipeManager (including
datapacks) is the source of truth. The Python brain only decides which discovered
recipe to try and which missing intermediate material to make first.
"""
from collections import Counter
import re


def output_for_goal(title):
    text = str(title or "").strip().lower()
    match = re.fullmatch(
        r"craft an? (wooden|stone|iron|golden|diamond|netherite) pickaxe", text
    )
    if match:
        return "minecraft:" + match.group(1) + "_pickaxe"
    if text.startswith("craft minecraft:"):
        return text.removeprefix("craft ").strip()
    return None


def item_counts(inventory):
    counts = Counter()
    for row in inventory or []:
        if row.get("item"):
            counts[str(row["item"])] += int(row.get("count") or 0)
    return counts


def recipe_grid(recipe, inventory):
    """Select item alternatives by actual inventory and preserve empty slots."""
    try:
        width, height = int(recipe["width"]), int(recipe["height"])
    except (KeyError, TypeError, ValueError):
        return None
    options = recipe.get("ingredients")
    if width not in (1, 2, 3) or height not in (1, 2, 3):
        return None
    if not isinstance(options, list) or len(options) != width * height:
        return None
    counts = item_counts(inventory)
    used = Counter()
    grid = []
    for alternatives in options:
        if not isinstance(alternatives, list):
            return None
        choices = [str(item) for item in alternatives if isinstance(item, str) and item]
        if not choices:
            grid.append("")
            continue
        # Prefer a material already owned; distribute between equivalent
        # materials only if the selected item's remaining count is exhausted.
        selected = max(
            choices,
            key=lambda item: (counts[item] - used[item] > 0,
                              counts[item] - used[item],
                              item.startswith("minecraft:"))
        )
        grid.append(selected)
        used[selected] += 1
    if not any(grid):
        return None
    missing = Counter({
        item: count - counts[item]
        for item, count in used.items() if count > counts[item]
    })
    return {"width": width, "height": height, "grid": grid,
            "missing": dict(missing), "missing_total": sum(missing.values())}


def choose_recipe(recipes, inventory, output):
    choices = []
    for recipe in recipes or []:
        if recipe.get("output") != output:
            continue
        grid = recipe_grid(recipe, inventory)
        if grid is None:
            continue
        choices.append((grid["missing_total"], len(grid["grid"]),
                        recipe.get("recipe_id", ""), recipe, grid))
    if not choices:
        return None
    _, _, _, recipe, grid = min(choices, key=lambda row: row[:3])
    return recipe, grid


def craft_action(recipe, grid, goal):
    return {
        "action": "craft",
        "intent": "craft " + str(recipe["output"]).split(":")[-1],
        "hypothesis": "try the recipe reported by the world's active recipe registry",
        "width": grid["width"], "height": grid["height"],
        "grid": grid["grid"], "times": 1,
        "expected_output": recipe["output"],
        "recipe_id": recipe["recipe_id"],
        "goal_id": goal.get("id"),
        "goal_reason": "craft a required item for " + str(goal.get("title") or "the goal"),
    }


def decide_recipe(goal, inventory, lookup, depth=3):
    """Return a craft step and chain evidence, or a precise unmet prerequisite.

    lookup(output) -> server recipe records. Recursion is bounded and cycle-safe.
    No resources are consumed by this decision; execution stays in the bridge.
    """
    target = output_for_goal(goal.get("title"))
    if not target:
        return None, "not_a_crafting_goal", []
    trace = []
    visited = set()

    def solve(output, level):
        if output in visited:
            return None, "recipe_cycle"
        if level > depth:
            return None, "dependency_depth_exceeded"
        visited.add(output)
        try:
            data = lookup(output)
            if not isinstance(data, dict) or data.get("ok") is False:
                return None, "recipe_service_unavailable"
            found = choose_recipe(data.get("recipes"), inventory, output)
            if not found:
                return None, "no_loaded_crafting_recipe:" + output
            recipe, grid = found
            trace.append({"output": output, "recipe": recipe.get("recipe_id"),
                          "missing": grid["missing"]})
            if not grid["missing"]:
                return craft_action(recipe, grid, goal), "ready"
            if level == depth:
                return None, "missing_materials:" + repr(grid["missing"])
            # Try crafting a missing ingredient first. Inventory must be read
            # again after the physical step before the next action is chosen.
            for item in grid["missing"]:
                # A tag slot may accept oak, jungle, modded planks, etc.
                # Never assume the first choice is the only material possible.
                alternatives = [item]
                for index, selected in enumerate(grid["grid"]):
                    if selected == item and index < len(recipe["ingredients"]):
                        alternatives.extend(recipe["ingredients"][index][:16])
                for alternative in dict.fromkeys(alternatives):
                    if alternative == output:
                        continue
                    action, _reason = solve(alternative, level + 1)
                    if action is not None:
                        return action, "intermediate:" + alternative
            return None, "missing_materials:" + repr(grid["missing"])
        finally:
            visited.remove(output)

    action, reason = solve(target, 0)
    return action, reason, trace
