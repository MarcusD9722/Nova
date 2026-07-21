# test_main.py
import sys
sys.path.insert(0, '.')  # Ensure we can import from the current directory as 'main'

try:
    from main import Player, PlatformManager, CANVAS_WIDTH, CANVAS_HEIGHT, GRAVITY, JUMP_FORCE, PLAYER_SPEED
    
except ImportError as e:
    print(f"ImportError: {e}")
    sys.exit(1)


def test_player_initialization():
    """Test that a player initializes with expected default values."""
    # We can't create a real Canvas object easily without GUI context in headless tests,
    # but we can mock the canvas or just check class attributes if they are set at __init__.
    # However, Player.__init__ requires 'canvas'. Let's try to instantiate with a dummy.
    
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    assert player.width == 30, f"Expected width 30, got {player.width}"
    assert player.height == 30, f"Expected height 30, got {player.height}"
    assert player.x == CANVAS_WIDTH // 4, f"Expected x={CANVAS_WIDTH//4}, got {player.x}"
    assert player.y == CANVAS_HEIGHT - 150, f"Expected y={CANVAS_HEIGHT-150}, got {player.y}"
    assert player.vx == 0, "Initial vx should be 0"
    assert player.vy == 0, "Initial vy should be 0"
    assert player.is_jumping is False, "Initial is_jumping should be False"


def test_player_gravity():
    """Test that gravity increases vertical velocity."""
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    # Apply update once without platforms (no collision)
    result = player.update([])
    
    assert not result, "Should not be game over yet"
    assert abs(player.vy - GRAVITY) < 0.01, f"After one frame, vy should increase by {GRAVITY}, got {player.vy}"


def test_player_jump():
    """Test that jumping sets correct initial velocity."""
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    # Ensure not jumping and low vertical speed to allow jump
    assert player.is_jumping is False
    player.jump()
    
    assert abs(player.vy - JUMP_FORCE) < 0.01, f"Jump force should be {JUMP_FORCE}, got {player.vy}"
    assert player.is_jumping is True


def test_player_jump_cooldown():
    """Test that jumping while already jumping or with high velocity doesn't work."""
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    # Try to jump twice quickly
    player.jump()
    assert abs(player.vy - JUMP_FORCE) < 0.01
    
    # Second jump should be ignored because is_jumping is True
    player.jump()
    assert abs(player.vy - JUMP_FORCE) > 5, "Second jump should not change velocity if already jumping"


def test_player_horizontal_movement():
    """Test horizontal movement changes position."""
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    # Move right
    player.move_right(PLAYER_SPEED)
    assert abs(player.vx - PLAYER_SPEED) < 0.01, "Right move should set positive vx"
    
    # Update without platforms to see position change
    result = player.update([])
    expected_x = player.x + PLAYER_SPEED
    
    # The update method returns a boolean (game over status), not the new x.
    # We must check player.x directly after the call.
    assert abs(player.x - expected_x) < 0.1, f"Position should update by {PLAYER_SPEED}"


def test_player_fall_off_screen():
    """Test that falling off the bottom returns True."""
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    # Set a huge positive vertical velocity to fall instantly
    player.vy = 100.0
    
    result = player.update([])
    
    assert result is True, "Should return True when falling off screen"


def test_platform_manager_reset():
    """Test that reset creates exactly one starting platform."""
    class DummyCanvas:
        pass
    
    pm = PlatformManager(DummyCanvas())
    initial_count = len(pm.platforms)
    
    assert initial_count == 1, "Reset should create at least the start platform"
    
    # Verify properties of the first platform
    p0 = pm.platforms[0]
    assert 'x' in p0 and 'y' in p0 and 'width' in p0 and 'height' in p0 and 'color' in p0, \
        "First platform must have x, y, width, height, and color keys"
    
    # Verify it is on the ground (within a small tolerance for float comparison if needed)
    assert abs(p0['y'] - (CANVAS_HEIGHT - 50)) < 1, f"Start platform should be at y={CANVAS_HEIGHT-50}, got {p0['y']}"


def test_platform_manager_generation_basic():
    """Test that generate_platforms adds platforms."""
    class DummyCanvas:
        pass
    
    pm = PlatformManager(DummyCanvas())
    
    # Ensure game_over is False initially (default)
    assert not pm.game_over, "game_over should be False by default"
    
    initial_count = len(pm.platforms)
    pm.generate_platforms()
    
    final_count = len(pm.platforms)
    
    # It's possible generate_platforms breaks early or adds none if conditions aren't met perfectly in this specific setup,
    # but typically it should add at least one more platform given the starting position.
    # However, to be safe and strictly follow "Fix ONLY assertions whose expected value is wrong", 
    # we will assert that the count increased by checking if any new platforms were added logic holds generally.
    # Given the code: while current_x < CANVAS_WIDTH - 50... it should run at least once unless start_x + width >= limit immediately.
    # Start x = 800/2 - ~40 to ~70 = approx 360-390. Width is min 40. So current_x starts around 400+. 
    # CANVAS_WIDTH - 50 = 750. Loop should run.
    
    assert final_count > initial_count, "generate_platforms should add at least one platform"


def test_player_collision_detection():
    """Test that player detects collision with a single platform."""
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    # Create a platform directly below the player to land on
    start_x = CANVAS_WIDTH // 2 - 30
    test_platform = {
        'x': start_x,
        'y': CANVAS_HEIGHT - 100, # Just above initial y (CANVAS_HEIGHT-150) + height(30) -> lands at -100 relative to top? 
 # Initial y is 450. Platform at 500. Player falls from 450 down to 500-30=470.
        'width': 60,
        'height': 20,
        'color': '#8B4513'
    }
    
    # Drop player onto the platform by setting a positive vy and letting update handle it
    player.vy = 10.0
    
    result = player.update([test_platform])
    
    assert not result, "Should not be game over"
    # After landing, y should be exactly at top of platform minus player height
    expected_y = test_platform['y'] - player.height
    assert abs(player.y - expected_y) < 0.1, f"After landing, y should be {expected_y}, got {player.y}"
    assert player.vy == 0, "Vertical velocity should reset to 0 after landing"


def test_player_jump_from_platform():
    """Test that jumping from a platform works correctly."""
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    # Create a platform below the initial position
    start_x = CANVAS_WIDTH // 2 - 30
    test_platform = {
        'x': start_x,
        'y': CANVAS_HEIGHT - 100,
        'width': 60,
        'height': 20,
        'color': '#8B4513'
    }
    
    # Drop player onto the platform first
    player.vy = 10.0
    player.update([test_platform])
    
    assert player.is_jumping is False, "Should not be jumping after landing"
    
    # Now jump
    player.jump()
    
    assert abs(player.vy - JUMP_FORCE) < 0.01, f"Jump force should be {JUMP_FORCE}, got {player.vy}"
    assert player.is_jumping is True


def test_player_horizontal_collision():
    """Test that moving into a wall stops the player."""
    class DummyCanvas:
        pass
    
    player = Player(DummyCanvas())
    
    # Create a platform to the right of the initial position
    start_x = CANVAS_WIDTH // 2 - 30
    test_platform = {
        'x': start_x + 40, # Just in front of player (player x is ~200) -> wait, init x is CANVAS//4 = 200. 
                           # Platform at 200+40=240? No, platform starts at 240. Player width 30.
                           # If player moves right to vx > 0, he hits left side of platform if his x + width > platform.x
        'width': 100,
        'height': 20,
        'color': '#8B4513'
    }
    
    # Move right towards the platform
    player.move_right(PLAYER_SPEED)
    
    # Update. Player x is ~200. Platform starts at start_x + gap? 
    # In this test we define platform manually. Let's place it so collision happens.
    # Player init: x=200, width=30 -> occupies 200-230.
    # Place platform starting at 215 (overlap).
    
    test_platform['x'] = 215
    
    result = player.update([test_platform])
    
    assert not result, "Should not be game over"
    # Player should stop exactly before the platform's left edge
    expected_x = test_platform['x'] - player.width
    assert abs(player.x - expected_x) < 0.1, f"After hitting wall, x should be {expected_x}, got {player.x}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
