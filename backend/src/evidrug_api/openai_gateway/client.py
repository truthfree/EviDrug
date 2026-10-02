"""Dacon의 OpenAI 호환 Responses API 클라이언트."""

from collections.abc import Mapping
from typing import Literal, TypedDict

from openai import AsyncOpenAI
from openai.types.responses import Response
from openai.types.responses.response_text_config_param import ResponseTextConfigParam
from pydantic import BaseModel, SecretStr

from evidrug_api.config import Settings
from evidrug_api.openai_gateway.models import DaconQuota, GeneratedText, ResponseUsage

DaconModel = Literal["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]


class StructuredTextOptions(TypedDict, total=False):
    text: ResponseTextConfigParam


class OpenAIConfigurationError(RuntimeError):
    """모델 호출에 필요한 안전한 설정이 빠졌을 때 발생한다."""


class DaconOpenAIClient:
    """SDK 세부사항을 숨기고 EviDrug이 사용할 텍스트 결과만 반환한다."""

    def __init__(self, sdk: AsyncOpenAI, default_model: DaconModel) -> None:
        self._sdk = sdk
        self._default_model = default_model

    async def generate_text(
        self,
        input_text: str,
        *,
        instructions: str | None = None,
        model: DaconModel | None = None,
        max_output_tokens: int | None = None,
        output_schema: type[BaseModel] | None = None,
    ) -> GeneratedText:
        """Responses API를 호출하고 본문, 토큰 사용량, 쿼터를 함께 반환한다."""

        if not input_text.strip():
            raise ValueError("input_text must not be empty")
        if max_output_tokens is not None and max_output_tokens < 1:
            raise ValueError("max_output_tokens must be at least 1")

        text_config: ResponseTextConfigParam = (
            {
                "format": {
                    "type": "json_schema",
                    "name": output_schema.__name__,
                    "schema": output_schema.model_json_schema(),
                    "strict": True,
                }
            }
            if output_schema
            else {"format": {"type": "text"}}
        )
        request_options: StructuredTextOptions = {"text": text_config} if output_schema else {}
        raw_response = await self._sdk.responses.with_raw_response.create(
            model=model or self._default_model,
            input=input_text,
            instructions=instructions,
            max_output_tokens=max_output_tokens,
            stream=False,
            **request_options,
        )
        response = raw_response.parse()

        return GeneratedText(
            response_id=response.id,
            model=response.model,
            text=response.output_text,
            usage=_read_usage(response),
            quota=_read_quota(raw_response.headers),
            status=response.status or "unknown",
            refused=any(
                item.type == "message"
                and any(content.type == "refusal" for content in item.content)
                for item in response.output
            ),
            incomplete_reason=(
                response.incomplete_details.reason if response.incomplete_details else None
            ),
        )

    async def close(self) -> None:
        """애플리케이션 종료 시 SDK의 HTTP 연결을 정리한다."""

        await self._sdk.close()


def build_dacon_openai_client(settings: Settings) -> DaconOpenAIClient:
    """검증된 애플리케이션 설정으로 Dacon용 SDK 클라이언트를 만든다."""

    return DaconOpenAIClient(
        sdk=build_dacon_openai_sdk(settings),
        default_model=settings.openai_model,
    )


def build_dacon_openai_sdk(settings: Settings) -> AsyncOpenAI:
    """호환성 검증과 runtime adapter가 공유할 저수준 SDK를 구성한다."""

    api_key = _required_secret(settings.openai_api_key)
    return AsyncOpenAI(
        base_url=settings.openai_base_url,
        api_key=api_key,
        default_headers={"api-key": api_key},
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )


def _required_secret(secret: SecretStr | None) -> str:
    if secret is None or not secret.get_secret_value().strip():
        raise OpenAIConfigurationError("EVIDRUG_OPENAI_API_KEY is not configured")
    return secret.get_secret_value()


def _read_usage(response: Response) -> ResponseUsage | None:
    if response.usage is None:
        return None
    return ResponseUsage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        total_tokens=response.usage.total_tokens,
    )


def _read_quota(headers: Mapping[str, str]) -> DaconQuota:
    return DaconQuota(
        remaining_quota_tokens=_optional_integer(headers.get("x-team-remaining-quota-tokens")),
        tokens_consumed=_optional_integer(headers.get("x-team-tokens-consumed")),
        remaining_tokens=_optional_integer(headers.get("x-team-remaining-tokens")),
        remaining_requests=_optional_integer(headers.get("x-team-remaining-requests")),
    )


def _optional_integer(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
