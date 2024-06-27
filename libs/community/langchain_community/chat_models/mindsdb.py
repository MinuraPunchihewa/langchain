"""MindsDB Endpoint chat wrapper. Relies heavily on ChatOpenAI as the Minds Endpoint is OpenAI API compatible."""

import os
import logging
import requests
from typing import (
    Any,
    Callable,
    Dict,
    Literal,
    Optional,
    Sequence,
    Set,
    Text,
    Type,
    Union,
)

from langchain_community.utils.openai import is_openai_v1
from langchain_community.chat_models.anyscale import ChatAnyscale

from langchain_core.tools import BaseTool
from langchain_core.runnables import Runnable
from langchain_core.messages import BaseMessage
from langchain_core.language_models import LanguageModelInput
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.utils import convert_to_secret_str, get_from_dict_or_env
from langchain_core.pydantic_v1 import BaseModel, Field, SecretStr, root_validator

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://llm.mdb.ai"
DEFAULT_MODEL = "gpt-3.5-turbo"


class ChatAIMind(ChatAnyscale):
    """
    `Minds Endpoint` chat large language models from MindsDB.

    See https://docs.mdb.ai/ for information about MindsDB and the MindsDB Endpoint.

    To use this chat model, you should have the ``openai`` python package installed, and the environment variable ``MINDSDB_API_KEY`` set with your API key.
    Alternatively, you can use the mindsdb_api_key keyword argument.

    Any parameters that are valid to be passed to the `openai.create` call can be passed in, even if not explicitly saved on this class.

    However, whether or not the parameters will take effect depends on that provider + model combination.
    See https://docs.litellm.ai/docs/completion/input for more information.

    Example:
        .. code-block:: python

            from langchain_community.chat_models import ChatAIMind
            chat = ChatAIMind(model="llama-3-70b")
    """
    @property
    def _llm_type(self) -> Text:
        """Return type of chat model."""
        return "ai-mind-chat"

    @property
    def lc_secrets(self) -> Dict[Text, Text]:
        return {"mindsdb_api_key": "MINDSDB_API_KEY"}

    @classmethod
    def is_lc_serializable(cls) -> bool:
        return True

    mindsdb_api_key: SecretStr = Field(default=None)
    mindsdb_api_base: str = Field(default=DEFAULT_API_BASE)
    model_name: str = Field(default=DEFAULT_MODEL, alias="model")

    @staticmethod
    def get_available_models(
        mindsdb_api_key: Optional[Text] = None,
        mindsdb_api_base: str = DEFAULT_API_BASE,
    ) -> Set[Text]:
        """
        Get a list of models supported by the Minds Endpoint API.
        """
        try:
            mindsdb_api_key = mindsdb_api_key or os.environ["MINDSDB_API_KEY"]
        except KeyError as e:
            raise ValueError(
                "MindsDB API key must be passed as keyword argument or "
                "set in environment variable MINDSDB_API_KEY.",
            ) from e

        models_url = f"{mindsdb_api_base}/models"
        models_response = requests.get(
            models_url,
            headers={
                "Authorization": f"Bearer {mindsdb_api_key}",
            },
        )

        if models_response.status_code != 200:
            raise ValueError(
                f"Error getting models from {models_url}: "
                f"{models_response.status_code}",
            )

        return {model["id"] for model in models_response.json()["data"] if 'embedding' not in model["id"]}

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """
        Validate that the Minds Endpoint API credentials are provided and create an OpenAI client.
        Further, validate that the chosen model is supported by the MindsDB API.
        """
        # Validate that the API key and base URL are available.
        values["mindsdb_api_key"] = convert_to_secret_str(
            get_from_dict_or_env(
                values,
                "mindsdb_api_key",
                "MINDSDB_API_KEY",
            )
        )
        values["mindsdb_api_base"] = get_from_dict_or_env(
            values,
            "mindsdb_api_base",
            "MINDSDB_API_BASE",
            default=DEFAULT_API_BASE,
        )

        # Validate that the `openai` package can be imported.
        try:
            import openai

        except ImportError as e:
            raise ImportError(
                "Could not import openai python package. "
                "Please install it with `pip install openai`.",
            ) from e

        # Set the client based on the version of the `openai` package that is being used.
        try:
            if is_openai_v1():
                client_params = {
                    "api_key": values["mindsdb_api_key"].get_secret_value(),
                    "base_url": values["mindsdb_api_base"],
                }
                if not values.get("client"):
                    values["client"] = openai.OpenAI(**client_params).chat.completions
                if not values.get("async_client"):
                    values["async_client"] = openai.AsyncOpenAI(
                        **client_params
                    ).chat.completions
            else:
                values["openai_api_base"] = values["mindsdb_api_base"]
                values["openai_api_key"] = values["mindsdb_api_key"].get_secret_value()
                values["client"] = openai.ChatCompletion
        except AttributeError as exc:
            raise ValueError(
                "`openai` has no `ChatCompletion` attribute, this is likely "
                "due to an old version of the openai package. Try upgrading it "
                "with `pip install --upgrade openai`.",
            ) from exc

        # Validate that the chosen model provided is supported.
        if "model_name" not in values.keys():
            values["model_name"] = DEFAULT_MODEL

        model_name = values["model_name"]
        available_models = cls.get_available_models(
            values["mindsdb_api_key"].get_secret_value(),
            values["mindsdb_api_base"],
        )

        if model_name not in available_models:
            raise ValueError(
                f"Model name {model_name} not found in available models: "
                f"{available_models}.",
            )

        values["available_models"] = available_models

        return values

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], Type[BaseModel], Callable, BaseTool]],
        *,
        tool_choice: Optional[
            Union[dict, str, Literal["auto", "none", "required", "any"], bool]
        ] = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, BaseMessage]:
        """Bind tool-like objects to this chat model.

        Assumes model is compatible with OpenAI tool-calling API.

        Args:
            tools: A list of tool definitions to bind to this chat model.
                Can be  a dictionary, pydantic model, callable, or BaseTool. Pydantic
                models, callables, and BaseTools will be automatically converted to
                their schema dictionary representation.
            tool_choice: Which tool to require the model to call.
                Options are:
                name of the tool (str): calls corresponding tool;
                "auto": automatically selects a tool (including no tool);
                "none": does not call a tool;
                "any" or "required": force at least one tool to be called;
                True: forces tool call (requires `tools` be length 1);
                False: no effect;

                or a dict of the form:
                {"type": "function", "function": {"name": <<tool_name>>}}.
            **kwargs: Any additional parameters to pass to the
                :class:`~langchain.runnable.Runnable` constructor.
        """

        if self.model_name != "gpt-3.5-turbo":
            logger.warning(
                """Tool calling is only supported for the gpt-3.5-turbo model currently.
                Please use the gpt-3.5-turbo model to bind tools."""
            )

            return

        formatted_tools = [convert_to_openai_tool(tool) for tool in tools]
        if tool_choice:
            if isinstance(tool_choice, str):
                # tool_choice is a tool/function name
                if tool_choice not in ("auto", "none", "any", "required"):
                    tool_choice = {
                        "type": "function",
                        "function": {"name": tool_choice},
                    }
                # 'any' is not natively supported by OpenAI API.
                # We support 'any' since other models use this instead of 'required'.
                if tool_choice == "any":
                    tool_choice = "required"
            elif isinstance(tool_choice, bool):
                if len(tools) > 1:
                    raise ValueError(
                        "tool_choice=True can only be used when a single tool is "
                        f"passed in, received {len(tools)} tools."
                    )
                tool_choice = {
                    "type": "function",
                    "function": {"name": formatted_tools[0]["function"]["name"]},
                }
            elif isinstance(tool_choice, dict):
                tool_names = [
                    formatted_tool["function"]["name"]
                    for formatted_tool in formatted_tools
                ]
                if not any(
                    tool_name == tool_choice["function"]["name"]
                    for tool_name in tool_names
                ):
                    raise ValueError(
                        f"Tool choice {tool_choice} was specified, but the only "
                        f"provided tools were {tool_names}."
                    )
            else:
                raise ValueError(
                    f"Unrecognized tool_choice type. Expected str, bool or dict. "
                    f"Received: {tool_choice}"
                )
            kwargs["tool_choice"] = tool_choice
        return super().bind(tools=formatted_tools, **kwargs)
