import tkinter as tk
from tkinter import messagebox, simpledialog
import random
import time

# Game constants
WINDOW_SIZE = 400
GRID_SIZE = 20
SPEED = 150  # milliseconds per frame


class SnakeGame:
    def __init__(self, root):
        self.root = root
        self.root.title("Cobra")
        
        # Colors
        self.bg_color = "#0f3d0f"
        self.snake_color = "#4caf50"
        self.food_color = "#ffeb3b"
        self.text_color = "white"
        
        # Game state variables
        self.canvas = tk.Canvas(root, width=WINDOW_SIZE, height=WINDOW_SIZE, bg=self.bg_color)
        self.canvas.pack()
        
        self.snake = []
        self.food = ()
        self.direction = 'Right'  # Initial direction
        self.next_direction = 'Right'
        self.game_over = False
        
        # Bind keyboard events (bind to root window, not canvas, for better reliability)
        self.root.bind('<Key>', self.on_key_press)
        
        # Start the game loop immediately after initialization is complete
        self.reset_game()
        root.after(SPEED, self.update)

    def reset_game(self):
        """Initialize or reset the game state."""
        self.snake = [(10 * GRID_SIZE, 10 * GRID_SIZE)]
        self.direction = 'Right'
        self.next_direction = 'Right'
        self.game_over = False
        
        # Place initial food
        self.place_food()

    def place_food(self):
        """Place food at a random position not occupied by the snake."""
        while True:
            x = random.randint(0, (WINDOW_SIZE // GRID_SIZE) - 1) * GRID_SIZE
            y = random.randint(0, (WINDOW_SIZE // GRID_SIZE) - 1) * GRID_SIZE
            if self.is_position_free((x, y)):
                self.food = (x, y)
                break

    def is_position_free(self, position):
        """Check if a position is not occupied by the snake."""
        return position not in self.snake

    def on_key_press(self, event):
        """Handle keyboard input to change direction or quit game."""
        key = event.keysym
        
        # Handle Quit (Escape)
        if key == 'escape':
            self.quit_game()
            return
            
        # Prevent reversing direction directly (e.g., can't go Down immediately after going Up)
        if key == 'Up' and self.direction != 'Down':
            self.next_direction = 'Up'
        elif key == 'Down' and self.direction != 'Up':
            self.next_direction = 'Down'
        elif key == 'Left' and self.direction != 'Right':
            self.next_direction = 'Left'
        elif key == 'Right' and self.direction != 'Left':
            self.next_direction = 'Right'

    def quit_game(self):
        """Handle game quitting."""
        if messagebox.askyesno("Quit", "Are you sure you want to quit?"):
            self.game_over = True
            # Stop the loop by not scheduling next update and clearing canvas eventually handled in mainloop exit or explicit stop
            self.root.after_cancel(self.update_id)

    def update(self):
        """Main game loop."""
        if self.game_over:
            return
        
        # Update direction from input buffer (prevents 180-degree turns in one frame)
        self.direction = self.next_direction
        
        # Calculate new head position based on current direction
        head_x, head_y = self.snake[0]
        
        if self.direction == 'Up':
            new_head = (head_x, head_y - GRID_SIZE)
        elif self.direction == 'Down':
            new_head = (head_x, head_y + GRID_SIZE)
        elif self.direction == 'Left':
            new_head = (head_x - GRID_SIZE, head_y)
        elif self.direction == 'Right':
            new_head = (head_x + GRID_SIZE, head_y)
        
        # Check wall collision
        if not (0 <= new_head[0] < WINDOW_SIZE and 0 <= new_head[1] < WINDOW_SIZE):
            self.game_over = True
            return
        
        # Check self-collision
        if new_head in self.snake:
            self.game_over = True
            return
        
        # Move snake: add new head
        self.snake.insert(0, new_head)
        
        # Check food collision
        if new_head == self.food:
            self.place_food()
            # Don't pop the tail when eating, so snake grows
        else:
            self.snake.pop()  # Remove tail if no food eaten
        
        # Draw everything immediately after state update
        self.draw()
        
        # Schedule next update only if game is not over (though update handles this check)
        self.update_id = self.root.after(SPEED, self.update)

    def draw(self):
        """Render the game state."""
        self.canvas.delete("all")
        
        # Draw snake segments
        for i, segment in enumerate(self.snake):
            color = "#81c784" if i == 0 else self.snake_color
            x1 = segment[0]
            y1 = segment[1]
            x2 = x1 + GRID_SIZE - 1
            y2 = y1 + GRID_SIZE - 1
            
            # Make head slightly different color and rounder (oval)
            if i == 0:
                self.canvas.create_oval(x1+5, y1+5, x2-5, y2-5, fill=color)
            else:
                self.canvas.create_rectangle(x1, y1, x2, y2, fill=color)
        
        # Draw food (yellow oval with orange outline)
        fx, fy = self.food
        self.canvas.create_oval(fx+3, fy+3, fx+GRID_SIZE-3, fy+GRID_SIZE-3, 
                               fill=self.food_color, outline="orange")


def main():
    root = tk.Tk()
    
    # Create the game instance FIRST before scheduling any callbacks that might use it.
    # This ensures 'game' exists and is fully initialized when show_start_message runs.
    game = SnakeGame(root)
    
    def show_start_message():
        if not hasattr(game, 'message_shown'):
            messagebox.showinfo("Cobra", "Use Arrow Keys to Move\nPress ESC to Quit")
            game.message_shown = True
    
    # Schedule the message display 1 second after window creation
    root.after(1000, show_start_message)
    
    # Run Tkinter's main event loop. 
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        if not game.game_over and hasattr(game, 'message_shown'):
            messagebox.showinfo("Cobra", "Game Stopped")


if __name__ == "__main__":
    main()
