from __future__ import annotations

import httpx

from backend.sandbox_runner.contracts import RunnerExecutionRequest, RunnerExecutionResponse


class RunnerUnavailableError(RuntimeError):
    pass


class RunnerHttpClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if len(token) < 32:
            raise ValueError("sandbox runner token must be at least 32 characters")
        self.token = token
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"))

    async def execute(self, request: RunnerExecutionRequest) -> RunnerExecutionResponse:
        try:
            response = await self.client.post(
                "/internal/v1/execute",
                headers={"X-Runner-Token": self.token},
                json=request.model_dump(mode="json"),
                timeout=request.limits.timeout_seconds + 30,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RunnerUnavailableError("sandbox runner request failed") from exc
        return RunnerExecutionResponse.model_validate(response.json())

    async def healthcheck(self) -> bool:
        try:
            response = await self.client.get("/healthz", timeout=5)
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        return response.json().get("status") == "ok"

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()
