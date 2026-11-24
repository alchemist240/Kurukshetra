from enum import Enum


class Stance(str, Enum):
    AGGRESSIVE = "Aggressive"
    PASSIVE_AGGRESSIVE = "Passive-Aggressive"
    ASSERTIVE = "Assertive"
    VIGILANT_OBSERVER = "Vigilant Observer"
    DEVILS_ADVOCATE = "Devil's Advocate"
    SOCRATIC_INTERROGATOR = "Socratic Interrogator"
    COLD_PRAGMATIST = "Cold Pragmatist"
    DIPLOMATIC_SYNTHESIZER = "Diplomatic Synthesizer"


STANCE_CONFIG: dict[Stance, dict[str, str]] = {
    Stance.AGGRESSIVE: {
        "color": "#ef4444",  # Red
        "description": (
            "Challenge weak assumptions head-on, attack flawed logic directly, and demand "
            "concrete proof. Be sharp and relentless on ideas (never abusive)."
        ),
    },
    Stance.PASSIVE_AGGRESSIVE: {
        "color": "#f97316",  # Orange
        "description": (
            "Use polite sarcasm, subtle skepticism, and backhanded compliments to expose "
            "holes in other agents' reasoning while sounding outwardly calm."
        ),
    },
    Stance.ASSERTIVE: {
        "color": "#3b82f6",  # Blue
        "description": (
            "Be direct, confident, structured, and constructive. Take a clear stand, "
            "defend it with logic, and push the room forward."
        ),
    },
    Stance.VIGILANT_OBSERVER: {
        "color": "#a855f7",  # Purple
        "description": (
            "Stay cool and analytical. Spot blind spots, logical fallacies, hidden "
            "trade-offs, or circular arguments that other agents miss."
        ),
    },
    Stance.DEVILS_ADVOCATE: {
        "color": "#ec4899",  # Pink
        "description": (
            "Intentionally argue the unpopular or opposite position—especially when other "
            "agents agree too easily—to stress-test ideas and shatter groupthink."
        ),
    },
    Stance.SOCRATIC_INTERROGATOR: {
        "color": "#eab308",  # Yellow
        "description": (
            "Avoid lecturing. Pin down rival agents with 1-2 piercing, uncomfortable "
            "questions that expose hidden assumptions or contradictions in their claims."
        ),
    },
    Stance.COLD_PRAGMATIST: {
        "color": "#06b6d4",  # Cyan
        "description": (
            "Cut ruthlessly through theory and idealism. Focus strictly on real-world cost, "
            "execution risk, bottlenecks, and what actually breaks in production."
        ),
    },
    Stance.DIPLOMATIC_SYNTHESIZER: {
        "color": "#22c55e",  # Green
        "description": (
            "De-escalate deadlocks. Pull the strongest arguments from warring agents, "
            "bridge their differences, and propose a concrete compromise to reach consensus."
        ),
    },
}


def format_stances_for_prompt() -> str:
    """Formats all stances into a clean bullet list for the LLM prompt."""
    return "\n".join(
        f"- {stance.value}: {cfg['description']}"
        for stance, cfg in STANCE_CONFIG.items()
    )


def format_stance_choices() -> str:
    """Returns 'Aggressive | Passive-Aggressive | ...' automatically from the Enum."""
    return " | ".join(stance.value for stance in Stance)


def get_stance_color(stance: Stance) -> str:
    """Returns the UI hex color for a given stance."""
    return STANCE_CONFIG[stance]["color"]