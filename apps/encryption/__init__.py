"""Field-level encryption of personal data (ADR 0046).

``keys`` encrypts and decrypts text with the keys in
``FIELD_ENCRYPTION_KEYS`` (Fernet, newest first). ``fields`` holds the
model fields that store their values that way. The ``reencrypt_fields``
command rewrites every stored value with the newest key.
"""
