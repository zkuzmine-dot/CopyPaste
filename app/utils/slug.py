import secrets

SLUG_LENGTH = 8

# Без похожих символов: 0/o, 1/l/i. Только нижний регистр, без "-" и "_"
SLUG_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def generate_slug() -> str:
    return "".join(secrets.choice(SLUG_ALPHABET) for _ in range(SLUG_LENGTH))
