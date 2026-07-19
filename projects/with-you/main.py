import random


def get_random_number(min_val: int, max_val: int) -> int:
    """Generates a random integer within the specified range."""
    return random.randint(min_val, max_val)


def validate_guess(guess_str: str, min_val: int, max_val: int) -> tuple[bool, int]:
    """
    Validates if the input string is a valid number within the game bounds.
    
    Returns:
        A tuple of (is_valid, parsed_value). If invalid, value is -1.
    """
    try:
        guess = int(guess_str)
        if min_val <= guess <= max_val:
            return True, guess
        else:
            return False, -1
    except ValueError:
        return False, -1


def get_hint(current_guess: int, target_number: int) -> str:
    """Determines the hint based on how close the guess is to the target."""
    if current_guess == target_number:
        return "Correct!"
    
    diff = abs(target_number - current_guess)
    
    if diff <= 2:
        return f"Very close! (Difference of {diff})"
    elif diff <= 5:
        return f"Getting warmer... (Difference of {diff})"
    else:
        return "Keep trying!"


def play_game():
    """Main game loop logic."""
    print("Welcome to the Number Guessing Game!")
    
    # Configuration
    min_val = 1
    max_val = 100
    max_attempts = 7
    
    target_number = get_random_number(min_val, max_val)
    attempts = 0
    game_active = True
    
    print(f"I'm thinking of a number between {min_val} and {max_val}.")
    
    while game_active:
        user_input = input("Enter your guess: ").strip()
        
        is_valid, parsed_guess = validate_guess(user_input, min_val, max_val)
        
        if not is_valid:
            print(f"Invalid input. Please enter an integer between {min_val} and {max_val}.")
            continue
        
        attempts += 1
        
        # Check for win condition
        if parsed_guess == target_number:
            hint = get_hint(parsed_guess, target_number)
            print(hint)
            print(f"Congratulations! You guessed the number in {attempts} attempt(s).")
            game_active = False
            
        else:
            hint = get_hint(parsed_guess, target_number)
            remaining = max_attempts - attempts + 1
            if remaining > 0:
                print(hint)
                print(f"You have {remaining} attempt(s) left.")
            else:
                print("Out of attempts!")

    # Game Over state (Loss)
    if not game_active and parsed_guess != target_number:
        print(f"Game Over! The number was {target_number}.")


if __name__ == "__main__":
    play_game()
