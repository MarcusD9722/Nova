import tkinter as tk
from tkinter import messagebox
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
        self.x += self.vx
        self.handle_horizontal_collisions(platforms)
        
        # Move vertically
        self.y += self.vy
        self.handle_vertical_collisions(platforms)
        
        # Check if player has fallen below the canvas
        if self.y > CANVAS_HEIGHT:
            return True  # Signal game over
        
        return False

    def handle_horizontal_collisions(self, platforms):
        for platform in platforms:
            if self.is_colliding(platform):
                if self.vx > 0:
                    self.x = platform['x'] - self.width
                elif self.vx < 0:
                    self.x = platform['x'] + platform['width']

    def handle_vertical_collisions(self, platforms):
        for platform in platforms:
            if self.is_colliding(platform):
                if self.y + self.height - self.vy <= platform['y']:
                    self.y = platform['y'] - self.height
                    self.vy = 0
                    self.is_jumping = False

    def is_colliding(self, platform):
        return (self.x < platform['x'] + platform['width'] and
                self.x + self.width > platform['x'] and
                self.y < platform['y'] + platform['height'] and
                self.y + self.height > platform['y'])

    def jump(self):
        if not self.is_jumping and abs(self.vy) < GRAVITY * 2:
            self.vy = JUMP_FORCE
            self.is_jumping = True

    def move_left(self):
        self.vx = -PLAYER_SPEED

    def move_right(self):
        self.vx = PLAYER_SPEED

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
        self.create_initial_platform()

    def create_initial_platform(self):
        start_x = CANVAS_WIDTH // 4 - PLATFORM_WIDTH_MIN // 2
        self.platforms.append({
            'x': start_x,
            'y': CANVAS_HEIGHT - 150 + 30,  # Adjusted to be directly under the player
            'width': PLATFORM_WIDTH_MAX,
            'height': 20,
            'color': '#8B4513'
        })

    def generate_platforms(self):
        if self.game_over:
            return

        last_platform = self.platforms[-1]
        current_x = last_platform['x'] + last_platform['width']

        while current_x < CANVAS_WIDTH - 50:
            gap = random.randint(30, PLATFORM_GAP_MAX)
            if current_x > CANVAS_WIDTH * 2.5 and len(self.platforms) >= 10:
                break

            new_width = random.randint(PLATFORM_WIDTH_MIN, PLATFORM_WIDTH_MAX)
            new_y = last_platform['y'] + random.randint(-80, -30)

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

        self.ensure_final_platform()

    def ensure_final_platform(self):
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
        self.root.minsize(CANVAS_WIDTH, CANVAS_HEIGHT)
        
        self.canvas = tk.Canvas(self.root, width=CANVAS_WIDTH, height=CANVAS_HEIGHT, bg='#2C3E50')
        self.canvas.pack()
        
        self.player = Player(self.canvas)
        self.platform_manager = PlatformManager(self.canvas)
        
        self.bind_controls()
        
        self.running = False
        self.last_time = time.time()
        
        self.show_start_menu()

    def bind_controls(self):
        self.root.bind('<Left>', lambda event: self.on_key_press('left'))
        self.root.bind('<Right>', lambda event: self.on_key_press('right'))
        self.root.bind('<Up>', lambda event: self.on_key_press('jump'))
        self.root.bind('<space>', lambda event: self.on_key_press('jump'))
        self.root.bind('<a>', lambda event: self.on_key_press('left'))
        self.root.bind('<d>', lambda event: self.on_key_press('right'))
        self.root.bind('<w>', lambda event: self.on_key_press('jump'))

    def on_key_press(self, key):
        if not self.running or self.platform_manager.game_over:
            return

        if key == 'left':
            self.player.move_left()
        elif key == 'right':
            self.player.move_right()
        elif key == 'jump':
            self.player.jump()

    def show_start_menu(self):
        self.canvas.delete("all")
        self.canvas.create_text(CANVAS_WIDTH // 2, CANVAS_HEIGHT // 3, text="Gravity Runner", font=("Arial", 48), fill="white")
        start_button = tk.Button(self.root, text="Start Game", command=self.start_game)
        self.canvas.create_window(CANVAS_WIDTH // 2, CANVAS_HEIGHT // 2, window=start_button)

    def start_game(self):
        self.canvas.delete("all")
        self.countdown(3)

    def countdown(self, count):
        if count > 0:
            self.canvas.delete("all")
            self.canvas.create_text(CANVAS_WIDTH // 2, CANVAS_HEIGHT // 2, text=str(count), font=("Arial", 48), fill="white")
            self.root.after(1000, self.countdown, count - 1)
        else:
            self.running = True
            self.game_loop()

    def game_loop(self):
        if not self.running:
            return

        self.canvas.delete("all")
        self.platform_manager.draw()
        self.canvas.create_rectangle(
            self.player.x, self.player.y, 
            self.player.x + self.player.width, self.player.y + self.player.height, 
            fill=self.player.color, outline=''
        )

        game_over = self.player.update(self.platform_manager.platforms)

        if game_over:
            self.running = False
            self.show_game_over()
        else:
            self.root.after(16, self.game_loop)

    def show_game_over(self):
        self.canvas.delete("all")
        self.canvas.create_text(CANVAS_WIDTH // 2, CANVAS_HEIGHT // 3, text="Game Over", font=("Arial", 48), fill="white")
        restart_button = tk.Button(self.root, text="Restart Game", command=self.start_game)
        self.canvas.create_window(CANVAS_WIDTH // 2, CANVAS_HEIGHT // 2, window=restart_button)

if __name__ == "__main__":
    game = GameWindow()
    game.root.mainloop()
