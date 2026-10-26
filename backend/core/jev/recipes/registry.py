"""Recipe registry for finding and matching workflow recipes."""

from typing import List, Optional
from recipes.base import WorkflowRecipe
from recipes.cams import CAMSMailbackRecipe
from recipes.kfintech import KFintechRecipe

ALL_RECIPES: List[WorkflowRecipe] = [
    CAMSMailbackRecipe(),
    KFintechRecipe(),
]


def find_recipe(goal: str, url: str = "") -> Optional[WorkflowRecipe]:
    """Find a matching workflow recipe for the given goal or current URL."""
    for recipe in ALL_RECIPES:
        if recipe.matches(goal, url):
            return recipe
    return None


def register_recipe(recipe: WorkflowRecipe):
    """Register a new workflow recipe."""
    ALL_RECIPES.append(recipe)
