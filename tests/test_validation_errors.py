from typing import Self

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, model_validator

from app.api.errors import HIDDEN, validation_error_without_secrets
from app.main import app as main_app

PASSWORD = "s3cret-pass"

pytestmark = pytest.mark.anyio


class Body(BaseModel):
    name: str = Field(min_length=3)
    password: str | None = Field(default=None, min_length=4)

    @model_validator(mode="after")
    def not_forbidden(self) -> Self:
        if self.name == "forbidden":
            raise ValueError("name is forbidden")
        return self


demo = FastAPI()
demo.add_exception_handler(RequestValidationError, validation_error_without_secrets)


@demo.post("/demo")
def endpoint(body: Body) -> dict:
    return {}


@pytest.fixture
async def client():
    transport = httpx2.ASGITransport(app=demo)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_password_field_error_hides_value(client):
    response = await client.post("/demo", json={"name": "alice", "password": "abc"})

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "password"]
    assert error["input"] == HIDDEN
    assert "abc" not in response.text


async def test_model_error_hides_password_inside_body(client):
    # Ошибка валидатора модели кладёт в input всё тело — вместе с паролем
    response = await client.post("/demo", json={"name": "forbidden", "password": PASSWORD})

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["input"] == {"name": "forbidden", "password": HIDDEN}
    assert PASSWORD not in response.text


async def test_other_fields_keep_input(client):
    response = await client.post("/demo", json={"name": "ab"})

    [error] = response.json()["detail"]
    assert error["input"] == "ab"


async def test_main_app_uses_handler():
    assert main_app.exception_handlers[RequestValidationError] is validation_error_without_secrets
