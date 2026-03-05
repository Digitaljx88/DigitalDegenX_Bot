import logging

# Set up logging
logging.basicConfig(level=logging.INFO, filename='bot.log', filemode='w',
                    format='%(asctime)s - %(levelname)s - %(message)s')

def validate_input(user_input):
    """Validate user input to ensure it meets the requirements."""
    if not isinstance(user_input, str):
        logging.error("Invalid input type. Expected a string.")
        raise ValueError("Input must be a string.")
    return user_input.strip()

def main():
    try:
        # Example input from users
        user_input = input("Enter command: ")
        validated_input = validate_input(user_input)
        
        logging.info(f"Received input: {validated_input}")
        
        # Process the input (placeholder for actual logic)
        
    except Exception as e:
        logging.exception("An error occurred")
        print("An error occurred. Please check the log file for more details.")

if __name__ == "__main__":
    main()