# Input Validator Utilities

"""
This module provides comprehensive input validation utilities for various data types.
"""

import re


def validate_email(email):
    """
    Validates an email address based on a standard regex pattern.
    """
    pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
    return re.match(pattern, email) is not None


def validate_phone_number(phone):
    """
    Validates a phone number based on a basic regex pattern that allows digits,
    optional leading '+', spaces, and dashes. Adjust as necessary for specific formats.
    """
    pattern = r'^\+?\d[\d -]{7,15}\d$'
    return re.match(pattern, phone) is not None


def validate_username(username):
    """
    Validates a username: between 3 to 16 characters, only letters and digits allowed.
    """
    pattern = r'^[a-zA-Z0-9]{3,16}$'
    return re.match(pattern, username) is not None


def validate_password(password):
    """
    Validates a password: at least 8 characters, at least one uppercase letter,
    one lowercase letter, one number, and one special character.
    """
    pattern = r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&])[A-Za-z\d@$!%*?&]{8,}$'
    return re.match(pattern, password) is not None


def validate_date(date_string):
    """
    Validates a date string in YYYY-MM-DD format.
    """
    pattern = r'^(\d{4})-(\d{2})-(\d{2})$'
    if re.match(pattern, date_string):
        year, month, day = map(int, date_string.split('-'))
        try:
            datetime.date(year, month, day)
            return True
        except ValueError:
            return False
    return False
