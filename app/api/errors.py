from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

SECRET_FIELDS = frozenset({"password"})
HIDDEN = "**********"


def _hide_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: HIDDEN if k in SECRET_FIELDS else _hide_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_hide_secrets(item) for item in value]
    return value


async def validation_error_without_secrets(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Как стандартный ответ 422, но без паролей.

    FastAPI кладёт введённое значение в поле input — даже у SecretStr. А ошибка валидатора
    модели содержит в input всё тело запроса, вместе с паролем.
    """
    errors = []
    for error in exc.errors():
        error = dict(error)
        if SECRET_FIELDS.intersection(map(str, error.get("loc", ()))):
            error["input"] = HIDDEN
        elif "input" in error:
            error["input"] = _hide_secrets(error["input"])
        errors.append(error)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})
