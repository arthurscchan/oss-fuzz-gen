"""The abstract base class for stages in fuzzing pipeline."""
import argparse
from abc import ABC, abstractmethod
from typing import Optional

from agent.base_agent import BaseAgent
from results import Result


class BaseStage(ABC):
  """The abstract base class for stages in fuzzing pipeline."""

  def __init__(self,
               args: argparse.Namespace,
               agents: Optional[list[BaseAgent]] = None) -> None:
    self.args = args
    self.agents: list[BaseAgent] = agents or []

  def add_agent(self, agent: BaseAgent) -> 'BaseStage':
    """Adds an agent for the stage."""
    agent.args = agent.args or self.args
    self.agents.append(agent)
    return self

  def get_agent(self, agent_name: str) -> BaseAgent:
    """Finds the agent by its name."""
    for agent in self.agents:
      if agent.name == agent_name:
        return agent
    raise RuntimeError(f'Agent {agent_name} is undefined')

  @abstractmethod
  def execute(self, result_history: list[Result]) -> Result:
    """Executes the stage-specific actions using agents."""
