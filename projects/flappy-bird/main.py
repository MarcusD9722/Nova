import tkinter as tk
from tkinter import messagebox, simpledialog
import random
import time
import sys

# --- Error Handling for Tkinter Import ---
try:
    import tkinter as tk
except ImportError:
    print("Error: The 'tkinter' module is not available. Please install it using:")
    if sys.platform == "win32":
        print("  pip install tk")
    elif sys.platform == "darwin":
        print("  brew install python-tk (or ensure X11 is installed)")
    else:
        print("  sudo apt-get install python3-tk")
    sys.exit(1)

class FlappyBirdGame:
    def __init__(self, root):
        self.root = root
        self.root.title("Flappy Bird with Countdown")
        
        # Game settings
        self.width = 400
        self.height = 600
        self.bird_size = 30
        self.pipe_width = 50
        self.gap_height = 150
        self.gravity = 0.5
        self.jump_strength = -8
        
        # Initialize canvas with a background color
        self.canvas = tk.Canvas(root, width=self.width, height=self.height, bg='sky blue', highlightthickness=0)
        self.canvas.pack()
        
        # Game state variables
        self.bird_y = 300  # Start in the middle vertically
        self.bird_velocity = 0
        self.pipes = []
        self.score = 0
        self.game_over = False
        self.is_running = False  # Explicit flag to control game loop state
        self.start_time = None
        self.elapsed_seconds = 0.0
        
        # UI Elements for Timer, Countdown, and Game Over Frame
        self.timer_label_id = None
        self.countdown_label_id = None
        self.game_over_frame = tk.Frame(root)
        
        # Bind events: Space to jump, 'r' or Enter to restart if game over
        root.bind('<space>', lambda event: self.jump())
        root.bind('<KeyRelease-r>', lambda event: self.restart_game())
        root.bind('<Return>', lambda event: self.restart_game())

    def reset_game(self):
        """Resets all game variables and UI for a new playthrough."""
        # Reset Timer
        self.start_time = time.time()
        
        # Clear existing UI elements if they exist (Timer)
        if self.timer_label_id:
            try:
                self.canvas.delete(self.timer_label_id)
            except tk.TclError:
                pass
        
        # Hide Game Over Frame if visible and destroy widgets inside it
        for widget in list(self.game_over_frame.winfo_children()):
            widget.destroy()

    def start_countdown(self):
        """Starts the 3-second countdown before gameplay begins."""
        count = 3
        self.countdown_label_id = self.canvas.create_text(200, 150, text=str(count), font=('Arial', 60, 'bold'), fill='white')
        
        def countdown_step():
            if self.countdown_label_id and count >= 1:
                count -= 1
                try:
                    self.canvas.itemconfig(self.countdown_label_id, text=str(count))
                except tk.TclError:
                    pass
                
                # Schedule next step after 1 second (1000ms)
                self.root.after(1000, countdown_step)
            else:
                # Countdown finished, start the game loop
                try:
                    self.canvas.delete(self.countdown_label_id)
                except tk.TclError:
                    pass
                
                # Ensure we are in a valid state to start physics
                if not self.game_over and self.bird_y is not None:
                    self.start_time = time.time()
                    self.is_running = True  # Explicitly enable the loop
                    self.update()

    def jump(self):
        """Applies upward velocity to the bird."""
        # Ensure game has started (countdown finished) before allowing jumps
        if not self.game_over and self.bird_y is not None: 
            self.bird_velocity = self.jump_strength

    def spawn_pipe(self):
        """Generates a new pipe pair with random vertical positioning."""
        min_y = 50
        max_y = self.height - self.gap_height - 50
        
        if max_y <= min_y:
            return

        pipe_top_height = random.randint(min_y, max_y)
        
        self.pipes.append({
            'x': self.width,
            'top_height': pipe_top_height,
            'bottom_y': pipe_top_height + self.gap_height,
            'passed': False
        })

    def update(self):
        """Main game loop logic."""
        
        # If the game is not running (e.g., during countdown or paused), do nothing
        if not self.is_running:
            return
        
        # Update Timer Logic
        current_time = time.time()
        elapsed = current_time - self.start_time
        self.elapsed_seconds = max(0.0, int(elapsed))
        
        # Move bird (apply gravity)
        self.bird_velocity += self.gravity
        self.bird_y += self.bird_velocity
        
        # Generate new pipes periodically based on time to ensure consistent spacing logic
        frame_count = int(time.time() * 60) % 120 
        
        if len(self.pipes) == 0:
            self.spawn_pipe()

        # More robust spawning check: spawn if the last pipe is far enough left OR it's the first pipe
        if len(self.pipes) > 0 and self.pipes[-1]['x'] < self.width - 350:
            self.spawn_pipe()

        # Move and manage pipes
        for pipe in self.pipes[:]:
            pipe['x'] -= 3
            
            # --- Improved Collision Detection (AABB with padding) ---
            bird_left = 50 
            bird_right = 80 
            bird_top = self.bird_y - 2 
            bird_bottom = (self.bird_y + self.bird_size) + 2
            
            pipe_left = pipe['x']
            pipe_right = pipe['x'] + self.pipe_width
            top_pipe_bottom = pipe['top_height']
            bottom_pipe_top = pipe['bottom_y']

            # Check horizontal overlap first (optimization and logic clarity)
            if bird_right > pipe_left and bird_left < pipe_right:
                # Check vertical collision with either top or bottom pipe
                if (bird_top < top_pipe_bottom) or (bird_bottom > bottom_pipe_top):
                    self.game_over = True
            
            # Check score update (when bird passes the right side of the pipe)
            if not pipe['passed'] and pipe['x'] + self.pipe_width <= 0:
                self.score += 1
                pipe['passed'] = True
        
        # Remove off-screen pipes (keep a small buffer for collision safety)
        self.pipes = [pipe for pipe in self.pipes if pipe['x'] > -self.pipe_width]
        
        # Check floor/ceiling collision with padding
        bird_bottom_edge = self.bird_y + self.bird_size
        if (bird_bottom_edge >= self.height or 
            self.bird_y <= 0):
            self.game_over = True
        
        # Draw everything
        self.draw()

        # If the game just ended, show the rest
        if self.game_over:
            self.show_game_over_screen()

    def draw(self):
        """Draws all game elements on the canvas."""
        # Clear canvas
        self.canvas.delete("all")
        
        # Draw Bird (Yellow Square)
        bird_left = 50
        bird_right = 80
        bird_top = self.bird_y - 2
        bird_bottom = (self.bird_y + self.bird_size) + 2
        
        self.canvas.create_rectangle(bird_left, bird_top, bird_right, bird_bottom, fill='yellow', outline='black')

        # Draw Pipes (Green Rectangles)
        for pipe in self.pipes:
            x = pipe['x']
            
            # Top Pipe
            top_y = 0
            bottom_y = pipe['top_height']
            self.canvas.create_rectangle(x, top_y, x + self.pipe_width, bottom_y, fill='green', outline='black')
            
            # Bottom Pipe
            top_y = pipe['bottom_y']
            bottom_y = self.height
            self.canvas.create_rectangle(x, top_y, x + self.pipe_width, bottom_y, fill='green', outline='black')

        # Draw Score (if game is running)
        if not self.game_over:
            score_text = f"Score: {self.score}"
            timer_text = f"{self.elapsed_seconds}s"
            
            self.timer_label_id = self.canvas.create_text(20, 10, text=timer_text, font=('Arial', 16), fill='black')
            self.canvas.create_text(self.width // 2 - len(score_text) * 5, 30, text=score_text, font=('Arial', 24, 'bold'), fill='white')

    def show_game_over_screen(self):
        """Displays the game over screen with score and restart option."""
        # Stop the game loop immediately to prevent further updates during UI rendering
        self.is_running = False
        
        # Create a frame for the Game Over message centered on canvas
        go_frame = tk.Frame(self.canvas, bg='red', width=self.width, height=200)
        go_frame.place(x=0, y=(self.height - 200)//2)

        # Add labels to the game over frame
        score_label = tk.Label(go_frame, text=f"Game Over! Score: {self.score}", font=('Arial', 16), bg='red')
        score_label.pack(pady=50)
        
        restart_label = tk.Label(go_frame, text="Press 'R' or Enter to Restart", font=('Arial', 12), fg='white', bg='red')
        restart_label.pack()

    def restart_game(self):
        """Restarts the game from the beginning."""
        # Reset all state variables
        self.bird_y = 300
        self.bird_velocity = 0
        self.pipes = []
        self.score = 0
        self.game_over = False
        self.is_running = False
        
        # Clear canvas completely to remove any old UI elements (countdown, game over screen)
        self.canvas.delete("all")

        if self.timer_label_id:
            try:
                self.canvas.delete(self.timer_label_id)
            except tk.TclError:
                pass

        # Reset Timer and start countdown sequence
        self.start_time = time.time()
        
        # Start the 3-second countdown
        self.start_countdown()


# Main Application Entry Point
if __name__ == "__main__":
    root = tk.Tk()
    
    # Set window size to match game dimensions
    root.geometry(f"{game.game_width}x{game.game_height}")
    
    game = FlappyBirdGame(root)
    
    # Start the countdown immediately when app launches
    game.start_countdown()
    
    # Run the Tkinter event loop
    root.mainloop()
