"""Provider-neutral adapter API for the Project Job vertical slice."""

from .provider import LLMProvider, ProviderContractError
from .provider_config import ProviderConfigError
from .openai_compatible_provider import OpenAICompatibleProvider

__all__ = ["OpenAICompatibleProvider", "LLMProvider", "ProviderConfigError",
           "ProviderContractError"]
