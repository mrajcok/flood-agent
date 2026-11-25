"""Model choices: which models can be used here, with their Efforts, and the default model. Built once at startup, so
the page's Model Choice dropdown, the starting Drafts of new Cells and typing Cells all agree."""

from dataclasses import dataclass

from flood_agent.cell import ModelEfforts
from flood_agent.llm import ModelSpec

NO_MODEL = "no-model"
"""The Model Choice id for Fake Mode: a prompt sent with it is answered by the scripted stand-in."""


@dataclass(frozen=True)
class ModelChoices:
    models: tuple[ModelSpec, ...]  # the curated list
    has_api_key: bool  # without one, only NO_MODEL can be used
    default_model: str  # for a Run or a Cell that names none: NO_MODEL without an API key

    @classmethod
    def build(cls, models: tuple[ModelSpec, ...], configured_model: str, has_api_key: bool) -> "ModelChoices":
        """The configured model is the default, or NO_MODEL when there is no API key."""
        return cls(models, has_api_key, configured_model if has_api_key else NO_MODEL)

    def spec_of(self, model_id: str) -> ModelSpec | None:
        """The curated model with this id; None for an id outside the list (NO_MODEL included)."""
        return next((m for m in self.models if m.id == model_id), None)

    def window_of(self, model_id: str) -> int:
        """The context window of a curated model; 0 for an id outside the list (NO_MODEL: no context window)."""
        spec = self.spec_of(model_id)
        return spec.context_window if spec else 0

    def is_known(self, model_id: str) -> bool:
        """Whether the id is in the curated list or is NO_MODEL."""
        return model_id == NO_MODEL or self.spec_of(model_id) is not None

    def can_use(self, model_id: str) -> bool:
        """Whether a model can be used here: NO_MODEL always; a curated model only with an API key."""
        return model_id == NO_MODEL or (self.has_api_key and self.spec_of(model_id) is not None)

    def efforts_of(self, model_id: str) -> ModelEfforts | None:
        """A curated model's Efforts, whether or not it can be used here; None outside the list."""
        spec = self.spec_of(model_id)
        return ModelEfforts(tuple(spec.efforts), spec.default_effort) if spec else None
