import tkinter as tk
from tkinter import messagebox, ttk
import math
import random
import time

# Constants
CANVAS_WIDTH = 800
CANVAS_HEIGHT = 600
GRAVITY = 0.5
JUMP_FORCE = -12
PLAYER_SPEED = 7
PLATFORM_WIDTH_MIN = 40
PLATFORM_WIDTH_MAX = 150
PLATFORM_GAP_MAX = 120

class Player:
    def __init__(self, canvas):
        self.canvas = canvas
        self.width = 30
        self.height = 30
        self.x = CANVAS_WIDTH // 4
        self.y = CANVAS_HEIGHT - 150
        self.vx = 0
        self.vy = 0
        self.color = "#FFD700"
        self.is_jumping = False
        
    def update(self, platforms):
        # Apply gravity
        self.vy += GRAVITY
        
        # Move horizontally
        old_x = self.x
        self.x += self.vx
        
        # Horizontal collision with platforms (only if moving)
        if abs(self.vx) > 0:
            for platform in platforms:
                if (self.x < platform['x'] + platform['width'] and
                    self.x + self.width > platform['x'] and
                    self.y < platform['y'] + platform['height'] and
                    self.y + self.height > platform['y']):
                    
                    # Resolve collision based on direction of movement
                    if self.vx > 0:
                        self.x = platform['x'] - self.width
                    elif self.vx < 0:
                        self.x = platform['x'] + platform['width']
        
        # Move vertically
        old_y = self.y
        self.y += self.vy
        
        # Vertical collision with platforms (landing)
        for platform in platforms:
            if (self.x < platform['x'] + platform['width'] and
                self.x + self.width > platform['x'] and
                self.y < platform['y'] + platform['height'] and
                self.y + self.height > platform['y']):
                
                # Check if we were above the platform before this frame (landing)
                prev_y = old_y - self.vy
                if prev_y + self.height <= platform['y']:
                    self.y = platform['y'] - self.height
                    self.vy = 0
                    self.is_jumping = False
        
        # Check floor collision (game over condition handled in main loop)
        if self.y > CANVAS_HEIGHT:
            return True  # Signal game over
        
        return False

    def jump(self):
        if not self.is_jumping and abs(self.vy) < GRAVITY * 2:
            self.vy = JUMP_FORCE
            self.is_jumping = True
            
    def move_left(self, amount):
        self.vx = -amount
        
    def move_right(self, amount):
        self.vx = amount

class PlatformManager:
    def __init__(self, canvas):
        self.canvas = canvas
        self.platforms = []
        self.score = 0
        self.game_over = False
        self.reset()
        
    def reset(self):
        self.platforms = []
        self.score = 0
        
        # Create starting platform centered at bottom
        start_x = CANVAS_WIDTH // 2 - PLATFORM_WIDTH_MIN // 2
        self.platforms.append({
            'x': start_x,
            'y': CANVAS_HEIGHT - 50,
            'width': random.randint(PLATFORM_WIDTH_MIN, PLATFORM_WIDTH_MAX),
            'height': 20,
            'color': '#8B4513'
        })
        
    def generate_platforms(self):
        if self.game_over:
            return
            
        # Generate platforms until we reach the end or have enough
        last_platform = self.platforms[-1]
        current_x = last_platform['x'] + last_platform['width']
        
        while current_x < CANVAS_WIDTH - 50:
            gap = random.randint(30, PLATFORM_GAP_MAX)
            
            # Ensure we don't go past the screen too early if it's a short level
            if current_x > CANVAS_WIDTH * 2.5 and len(self.platforms) >= 10:
                break
                
            new_width = random.randint(PLATFORM_WIDTH_MIN, PLATFORM_WIDTH_MAX)
            new_y = last_platform['y'] + random.randint(-80, -30)
            
            # Keep platforms within reasonable bounds vertically
            if new_y < CANVAS_HEIGHT // 2 or new_y > CANVAS_HEIGHT * 1.5:
                continue
                
            self.platforms.append({
                'x': current_x + gap,
                'y': max(50, min(CANVAS_HEIGHT - 30, new_y)),
                'width': new_width,
                'height': 20,
                'color': '#8B4513'
            })
            
            last_platform = self.platforms[-1]
            current_x += gap + new_width
            
        # Add a final platform at the end if needed to catch player
        if not any(p['x'] > CANVAS_WIDTH - 200 for p in self.platforms):
            last_platform = self.platforms[-1]
            self.platforms.append({
                'x': last_platform['x'] + random.randint(50, 100),
                'y': last_platform['y'],
                'width': CANVAS_WIDTH - (last_platform['x'] + last_platform['width']),
                'height': 20,
                'color': '#8B4513'
            })

    def draw(self):
        for platform in self.platforms:
            self.canvas.create_rectangle(
                platform['x'], 
                platform['y'], 
                platform['x'] + platform['width'], 
                platform['y'] + platform['height'],
                fill=platform['color'],
                outline=''
            )

class GameWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gravity Runner")
        
        # Set minimum size to prevent window from being too small on some displays
        self.root.minsize(CANVAS_WIDTH, CANVAS_HEIGHT)
        
        self.canvas = tk.Canvas(self.root, width=CANVAS_WIDTH, height=CANVAS_HEIGHT, bg='#2C3E50')
        self.canvas.pack()
        
        # Initialize game objects
        self.player = Player(self.canvas)
        self.platform_manager = PlatformManager(self.canvas)
        
        # Bind controls (using both arrow keys and WASD for better accessibility)
        self.root.bind('<Left>', lambda event: self.on_key_press('left'))
        self.root.bind('<Right>', lambda event: self.on_key_press('right'))
        self.root.bind('<Up>', lambda event: self.on_key_press('up'))
        self.root.bind('<space>', lambda event: self.on_key_press('jump'))
        # Add WASD support
        self.root.bind('<a>', lambda event: self.on_key_press('left'))
        self.root.bind('<d>', lambda event: self.on_key_press('right'))
        self.root.bind('<w>', lambda event: self.on_key_press('up'))

        # Start the game loop
        self.running = True
        self.last_time = time.time()
        
    def on_key_press(self, key):
        if not self.running or self.platform_manager.game_over:
            return
            
        if key == 'left':
            self.player.move_left(PLAYER_SPEED)
        elif key == 'right':
            self.player.move_right(PLAYER_SPEED)
        elif key == 'up' and abs(self.player.vy) < 10: # Prevent jumping while falling fast or already jumping logic handled in jump() but up key triggers it here for consistency if not is_jumping check added to move_left/right context, actually standard platformer allows jump anytime on ground. Let's stick to the class method which checks state properly.
            self.player.jump()

    def game_over_handler(self):
        """Called when player falls off screen."""
        self.running = False
        self.platform_manager.game_over = True
        
        # Show score message box with restart option
        messagebox.showinfo(
            "Game Over", 
            f"Your Score: {self.platform_manager.score}\n\nClick OK to Restart."
        )
        
    def reset_game(self):
        """Resets the game state and starts a new run."""
        self.running = True
        self.last_time = time.time()
        self.player.x = CANVAS_WIDTH // 4
        self.player.y = CANVAS_HEIGHT - 150
        self.player.vx = 0
        self.player.vy = 0
        
        # Reset platform manager and regenerate platforms for new run
        self.platform_manager.reset()
        
    def draw(self):
        self.canvas.delete("all")
        
        # Draw player
        self.canvas.create_rectangle(
            self.player.x, 
            self.player.y, 
            self.player.x + self.player.width, 
            self.player.y + self.player.height,
            fill=self.player.color,
            outline=''
        )
        
        # Draw platforms
        self.platform_manager.draw()

    def update(self):
        if not self.running:
            return
            
        # Update player position and check for game over
        is_fallen = self.player.update(self.platform_manager.platforms)
        
        if is_fallen:
            self.game_over_handler()
            return
        
        # Generate new platforms as we move forward
        self.platform_manager.generate_platforms()

    def run_loop(self):
        while True:
            time.sleep(0.016) # Approx 60 FPS
            
            if not self.running:
                break
                
            self.update()
            self.draw()
            
            # Update score based on distance traveled (simple implementation)
            # In a more complex version, we might track specific checkpoints or coins
            # For now, let's just increment slightly as platforms are generated/moved relative to player
            if len(self.platform_manager.platforms) > 1:
                self.platform_manager.score += 0.5

    def start_game(self):
        """Entry point for the game loop."""
        try:
            self.run_loop()
        except tk.TclError as e:
            # Handle potential window closing during update
            pass
        finally:
            self.root.destroy()

if __name__ == "__main__":
    app = GameWindow()
    app.start_game()
