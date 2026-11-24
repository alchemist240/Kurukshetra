from backend.agents.base import ArenaAgent
from backend.agents.stances import get_stance_color

UNDECIDED_COLOR = "#64748b"  # Neutral slate gray before an agent picks a stance


class AgentRegistry:
    """Live directory of all agents in Kurukshetra (enables peer discovery)."""

    def __init__(self):
        self._agents: dict[str, ArenaAgent] = {}

    def register(self, agent: ArenaAgent) -> None:
        self._agents[agent.agent_id] = agent

    def get(self, agent_id: str) -> ArenaAgent | None:
        return self._agents.get(agent_id)

    def all_agents(self) -> list[ArenaAgent]:
        return list(self._agents.values())

    def get_selected(self, agent_ids: list[str] | None = None) -> list[ArenaAgent]:
        """Returns either specific agents chosen for a debate, or all registered agents."""
        if not agent_ids:
            return self.all_agents()
        return [self._agents[aid] for aid in agent_ids if aid in self._agents]

    def reset_stances(self, agent_ids: list[str] | None = None) -> None:
        """Clears leftover stances so a new wake-up or debate starts with a clean slate."""
        for agent in self.get_selected(agent_ids):
            agent.current_stance = None

    def get_peer_roster(self, agent_ids: list[str] | None = None) -> list[dict[str, str]]:
        """Snapshot of who is in the arena and their current stance (used by prompts & UI)."""
        return [
            {
                "agent_id": a.agent_id,
                "name": a.name,
                "provider": a.provider,
                "model": a.model,
                "stance": a.current_stance.value if a.current_stance else "Undecided",
                "stance_color": get_stance_color(a.current_stance) if a.current_stance else UNDECIDED_COLOR,
            }
            for a in self.get_selected(agent_ids)
        ]