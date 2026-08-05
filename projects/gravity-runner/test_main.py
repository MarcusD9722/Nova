from main import Player, PlatformManager

def test_player_collision():
    canvas_mock = None  # Mock canvas since we don't use it in logic tests
    player = Player(canvas_mock)
    platforms = [
        {'x': 100, 'y': 500, 'width': 100, 'height': 20, 'color': '#8B4513'},
        {'x': 250, 'y': 400, 'width': 100, 'height': 20, 'color': '#8B4513'}
    ]

    # Test collision from above
    player.x = 110
    player.y = 480
    player.vy = 5
    player.handle_vertical_collisions(platforms)
    assert player.y == 470, "Player should land on the platform"
    assert player.vy == 0, "Vertical velocity should be reset after landing"
    assert not player.is_jumping, "Player should not be jumping after landing"

    # Test no collision
    player.x = 50
    player.y = 480
    player.vy = 5
    player.handle_vertical_collisions(platforms)
    assert player.y == 485, "Player should not collide and continue falling"

def test_player_movement():
    canvas_mock = None
    player = Player(canvas_mock)

    # Test moving left
    player.move_left()
    assert player.vx == -7, "Player should move left with speed -7"

    # Test moving right
    player.move_right()
    assert player.vx == 7, "Player should move right with speed 7"

    # Test jumping
    player.is_jumping = False
    player.vy = 0
    player.jump()
    assert player.vy == -12, "Player should jump with force -12"
    assert player.is_jumping, "Player should be in jumping state"

def test_platform_generation():
    canvas_mock = None
    platform_manager = PlatformManager(canvas_mock)

    # Test initial platform creation
    platform_manager.reset()
    assert len(platform_manager.platforms) == 1, "There should be one initial platform"
    initial_platform = platform_manager.platforms[0]
    assert initial_platform['y'] == 550, "Initial platform should be at y=550"

    # Test platform generation logic
    platform_manager.generate_platforms()
    assert len(platform_manager.platforms) > 1, "Platforms should be generated"

if __name__ == "__main__":
    test_player_collision()
    test_player_movement()
    test_platform_generation()
    print("All tests passed.")
