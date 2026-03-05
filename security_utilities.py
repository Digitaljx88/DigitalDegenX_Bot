import os
from cryptography.fernet import Fernet

class SecurityUtils:
    def __init__(self, key=None):
        self.key = key or self.generate_key()
        self.cipher = Fernet(self.key)

    @staticmethod
    def generate_key():
        """Generate a new key for encryption."""
        return Fernet.generate_key()

    def encrypt_data(self, data):
        """Encrypt the given data."""
        return self.cipher.encrypt(data.encode()).decode()

    def decrypt_data(self, encrypted_data):
        """Decrypt the given data."""
        return self.cipher.decrypt(encrypted_data.encode()).decode()

    @staticmethod
    def load_sensitive_data(var_name):
        """Load sensitive data from environment variables."""
        return os.getenv(var_name)