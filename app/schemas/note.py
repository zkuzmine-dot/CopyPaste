from pydantic import BaseModel, ConfigDict


class NoteBase(BaseModel):
    text: str


class NoteCreate(NoteBase):
    pass


class NoteUpdate(BaseModel):
    text: str | None = None


class NoteRead(NoteBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
