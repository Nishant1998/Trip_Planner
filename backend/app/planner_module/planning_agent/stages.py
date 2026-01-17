"""Agent ownership and ordered planning milestones for revision 2."""
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentRole(StrEnum):
    PLANNING = "planning"
    REVIEW = "review_and_repair"
    PRESENTATION = "presentation"


class PlanningTask(StrEnum):
    SKELETON = "skeleton"
    JOURNEYS = "major_travel_and_stays"
    ACTIVITIES = "activity_allocation"
    SCHEDULE = "daily_routes_and_schedules"
    BUDGET = "budget_and_feasibility"
    HANDOFF = "reviewer_handoff"


TASK_ORDER = tuple(PlanningTask)


class AgentStage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    agent: AgentRole = AgentRole.PLANNING
    task: PlanningTask | None = PlanningTask.SKELETON
    correction: bool = False

    @model_validator(mode="after")
    def task_belongs_to_planner(self):
        if (self.agent == AgentRole.PLANNING) != (self.task is not None):
            raise ValueError("Only the Planning Agent has a planning task")
        if self.correction:
            raise ValueError("Planning correction stages are superseded by Review-and-Repair")
        return self

    @property
    def allows_tools(self) -> bool:
        return self.agent != AgentRole.PRESENTATION


class MilestoneReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task: PlanningTask
    plan_version: int = Field(ge=0)
    evidence_revision: int = Field(ge=0)
    draft_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    unresolved_codes: list[str] = Field(default_factory=list)


class AgentProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: AgentStage = Field(default_factory=AgentStage)
    milestones: list[MilestoneReceipt] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_checkpoint(self):
        completed = [receipt.task for receipt in self.milestones]
        if completed != list(TASK_ORDER[:len(completed)]):
            raise ValueError("Saved milestones must be an ordered prefix without duplicates")
        for previous, current in zip(self.milestones, self.milestones[1:]):
            if (current.plan_version < previous.plan_version
                    or current.evidence_revision < previous.evidence_revision):
                raise ValueError("Saved milestone versions must not regress")
        if self.stage.agent == AgentRole.PLANNING:
            if len(completed) >= len(TASK_ORDER) or self.stage.task != TASK_ORDER[len(completed)]:
                raise ValueError("Saved active task does not follow accepted milestones")
        elif len(completed) != len(TASK_ORDER):
            raise ValueError("Review and presentation require all planning milestones")
        return self

    def complete_milestone(self, receipt: MilestoneReceipt, *, checked: bool):
        """Advance only after the caller's phase-specific checks have run."""
        if not checked:
            raise ValueError("Milestone has not passed its structural checks")
        if self.stage.agent != AgentRole.PLANNING or receipt.task != self.stage.task:
            raise ValueError("Milestone does not match the active planning task")
        if self.milestones and (receipt.plan_version < self.milestones[-1].plan_version
                or receipt.evidence_revision < self.milestones[-1].evidence_revision):
            raise ValueError("Milestone cannot use an older plan or evidence revision")
        position = TASK_ORDER.index(receipt.task)
        next_stage = (AgentStage(task=TASK_ORDER[position + 1]) if position + 1 < len(TASK_ORDER)
                      else AgentStage(agent=AgentRole.REVIEW, task=None))
        self.milestones = [*self.milestones, receipt]
        self.stage = next_stage

    def begin_presentation(self, *, final_gate_checked: bool):
        if self.stage.agent != AgentRole.REVIEW or not final_gate_checked:
            raise ValueError("Presentation requires the final delivery gate after review")
        self.stage = AgentStage(agent=AgentRole.PRESENTATION, task=None)
