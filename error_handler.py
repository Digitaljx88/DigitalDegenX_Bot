import time
import requests

class ApiError(Exception):
    """Custom exception for API errors."""
    pass

class RateLimitExceededError(Exception):
    """Custom exception for rate limit exceeded errors."""
    pass

class ErrorHandler:
    """Handles API calls and rate limiting."""

    def __init__(self, api_url, rate_limit=60):  # rate_limit in seconds
        self.api_url = api_url
        self.rate_limit = rate_limit
        self.last_api_call_time = 0

    def safe_api_call(self, endpoint, params=None):
        """Performs a safe API call with error handling and rate limiting."""
        current_time = time.time()
        if current_time - self.last_api_call_time < self.rate_limit:
            raise RateLimitExceededError(f"Rate limit exceeded. Please wait for {self.rate_limit} seconds.")

        response = requests.get(f"{self.api_url}/{endpoint}", params=params)
        if response.status_code != 200:
            raise ApiError(f"Error {response.status_code}: {response.text}")

        self.last_api_call_time = current_time
        return response.json()  

# Usage example:
# handler = ErrorHandler('https://api.example.com')
# try:
#     data = handler.safe_api_call('data_endpoint')
# except (ApiError, RateLimitExceededError) as e:
#     print(e)