import hashlib
import secrets


def generate_token() -> str:
    return secrets.token_urlsafe(32)


# Токен случайный и длинный, поэтому хватает SHA-256: медленный хэш (bcrypt) нужен для паролей
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
