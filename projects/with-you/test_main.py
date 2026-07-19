from main import get_random_number, validate_guess, get_hint


def test_get_random_number_range():
    """Test that random number generation stays within bounds."""
    # Test multiple times to ensure consistency (though randomness makes full coverage hard)
    for _ in range(10):
        num = get_random_number(5, 20)
        assert 5 <= num <= 20, f"Generated {num} outside [5, 20]"


def test_validate_guess_valid_input():
    """Test valid integer inputs within range."""
    is_valid, value = validate_guess("42", 1, 100)
    assert is_valid == True and value == 42

    is_valid, value = validate_guess("5", 1, 5)
    assert is_valid == True and value == 5


def test_validate_guess_invalid_input():
    """Test invalid inputs (non-integer or out of range)."""
    # Non-numeric string
    is_valid, _ = validate_guess("abc", 1, 100)
    assert is_valid == False

    # Out of range low
    is_valid, value = validate_guess("0", 1, 100)
    assert is_valid == False and value == -1

    # Out of range high
    is_valid, value = validate_guess("101", 1, 100)
    assert is_valid == False and value == -1


def test_validate_guess_boundary_values():
    """Test exact boundary values."""
    is_valid, _ = validate_guess("1", 1, 100)
    assert is_valid == True

    is_valid, _ = validate_guess("100", 1, 100)
    assert is_valid == True


def test_get_hint_correct():
    """Test hint when guess matches target."""
    hint = get_hint(50, 50)
    assert hint == "Correct!"


def test_get_hint_very_close():
    """Test hint for very close guesses (diff <= 2)."""
    # Difference of 1
    hint = get_hint(49, 50)
    assert "Very close" in hint and "(Difference of 1)" in hint

    # Difference of 2
    hint = get_hint(48, 50)
    assert "Very close" in hint and "(Difference of 2)" in hint


def test_get_hint_warming():
    """Test hint for getting warmer (diff between 3 and 5)."""
    # Difference of 3
    hint = get_hint(47, 50)
    assert "Getting warmer" in hint and "(Difference of 3)" in hint

    # Difference of 5
    hint = get_hint(45, 50)
    assert "Getting warmer" in hint and "(Difference of 5)" in hint


def test_get_hint_keep_trying():
    """Test hint for far away guesses (diff > 5)."""
    hint = get_hint(10, 50)
    assert hint == "Keep trying!"

    # Difference exactly 6
    hint = get_hint(44, 50)
    assert hint == "Keep trying!"


if __name__ == "__main__":
    test_get_random_number_range()
    test_validate_guess_valid_input()
    test_validate_guess_invalid_input()
    test_validate_guess_boundary_values()
    test_get_hint_correct()
    test_get_hint_very_close()
    test_get_hint_warming()
    test_get_hint_keep_trying()
