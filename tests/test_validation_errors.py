from typing import Self

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, model_validator

from app.api.errors import HIDDEN, validation_error_without_secrets
from app.main import app as main_app

PASSWORD = "s3cret-pass"


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


client = TestClient(demo)


def test_password_field_error_hides_value():
    response = client.post("/demo", json={"name": "alice", "password": "abc"})

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["loc"] == ["body", "password"]
    assert error["input"] == HIDDEN
    assert "abc" not in response.text


def test_model_error_hides_password_inside_body():
    # Ошибка валидатора модели кладёт в input всё тело — вместе с паролем
    response = client.post("/demo", json={"name": "forbidden", "password": PASSWORD})

    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert error["input"] == {"name": "forbidden", "password": HIDDEN}
    assert PASSWORD not in response.text


def test_other_fields_keep_input():
    response = client.post("/demo", json={"name": "ab"})

    [error] = response.json()["detail"]
    assert error["input"] == "ab"


def test_main_app_uses_handler():
    assert main_app.exception_handlers[RequestValidationError] is validation_error_without_secrets
