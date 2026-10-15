import os
from fastapi import FastAPI
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ["DATABASE_URL"]

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

app = FastAPI()

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0")