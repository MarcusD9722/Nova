import tkinter as tk
from tkinter import messagebox, simpledialog
import random
import time
import sys

# --- Error Handling for Tkinter Import ---
try:
    from tkinter import ttk
except ImportError:
    print("Error: The 'tkinter' module is not available.")
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
        self.pipe_speed = 4
        
        # Initialize canvas with a background color
        self.canvas = tk.Canvas(root, width=self.width, height=self.height, bg='sky blue', highlightthickness=0)
        self.canvas.pack()
        
        # Game state variables
        self.bird_y = None  
        self.bird_velocity = 0
        self.pipes = []
        self.score = 0
        self.game_over = False
        self.is_running = False 
        self.start_time = None
        
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
            # `nonlocal` is required: assigning to `count` below would otherwise
            # make it local to this function, and reading it would raise
            # UnboundLocalError. Tkinter swallows exceptions raised inside
            # `after` callbacks, so that failure showed up as a silent freeze.
            nonlocal count
            count -= 1
            if self.countdown_label_id and count >= 1:
                try:
                    self.canvas.itemconfig(self.countdown_label_id, text=str(count))
                except tk.TclError:
                    pass

                # Schedule next step after 1 second (1000ms)
                self.root.after(1000, countdown_step)
            else:
                # Countdown finished, start the game loop
                try:
                    if self.countdown_label_id:
                        self.canvas.delete(self.countdown_label_id)
                        self.countdown_label_id = None
                except tk.TclError:
                    pass
                
                # Ensure we are in a valid state to start physics
                if not self.game_over and self.bird_y is not None:
                    self.start_time = time.time()
                    self.is_running = True  # Explicitly enable the loop
                    self.update_game_loop()

        # Actually START the countdown. Without this the callback above was only
        # ever referenced from inside itself, so the first tick never fired and
        # the game sat on "3" forever — the freeze that was being reported.
        self.root.after(1000, countdown_step)

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

    def update_game_loop(self):
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
            pipe['x'] -= self.pipe_speed
            
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

    def draw(self):
        """Draws the current state."""
        
        # Clear canvas
        self.canvas.delete("all")
        
        # Draw Timer if game is running and not over
        if self.is_running and not self.game_over:
            try:
                text = f"{self.elapsed_seconds}s"
                self.timer_label_id = self.canvas.create_text(20, 15, text=text, font=('Arial', 30), fill='black')
            except tk.TclError:
                pass

        # Draw Bird (Yellow Square)
        if self.bird_y is not None and not self.game_over:
            try:
                bird_rect = [50, self.bird_y - 15, 80, self.bird_y + 15]
                self.canvas.create_rectangle(bird_rect[0], bird_rect[1], bird_rect[2], bird_rect[3], fill='yellow', outline='black')
            except tk.TclError:
                pass

        # Draw Pipes (Green Rectangles)
        for pipe in self.pipes:
            try:
                top_pipe = [pipe['x'], 0, pipe['x'] + self.pipe_width, pipe['top_height']]
                bottom_pipe = [pipe['x'], pipe['bottom_y'], pipe['x'] + self.pipe_width, self.height]
                
                # Draw Top Pipe
                self.canvas.create_rectangle(top_pipe[0], top_pipe[1], top_pipe[2], top_pipe[3], fill='green', outline='black')
                # Draw Bottom Pipe
                self.canvas.create_rectangle(bottom_pipe[0], bottom_pipe[1], bottom_pipe[2], bottom_pipe[3], fill='green', outline='black')
            except tk.TclError:
                pass

        # Draw Score if game is running and not over
        if self.is_running and not self.game_over:
            try:
                score_text = f"Score: {self.score}"
                self.canvas.create_text(20, 350, text=score_text, font=('Arial', 20), fill='black')
            except tk.TclError:
                pass

        # Draw Game Over Screen if game is over
        if self.game_over:
            try:
                # Create the frame for game over details
                self.game_over_frame.pack(fill=tk.BOTH, expand=True)
                
                # Label for "Game Over"
                go_label = tk.Label(self.game_over_frame, text="GAME OVER", font=('Arial', 40), fg='red')
                go_label.pack(pady=20)

                # Score Display
                score_label = tk.Label(self.game_over_frame, text=f"Score: {self.score}", font=('Arial', 30))
                score_label.pack()

                # Restart Button (or instruction to press R/Enter)
                restart_btn = tk.Button(
                    self.game_over_frame, 
                    text="Restart Game", 
                    command=self.restart_game,
                    bg='green',
                    fg='white',
                    font=('Arial', 16),
                    padx=20,
                    pady=10
                )
                restart_btn.pack(pady=20)

            except tk.TclError:
                pass

    def start_game(self):
        """Initializes the bird and starts the countdown."""
        # Reset state for a fresh game attempt (but keep score if desired, here we reset everything)
        self.reset_game()
        
        # Initialize Bird Position
        self.bird_y = 300
        
        # Start Countdown
        self.start_countdown()

    def restart_game(self):
        """Restarts the game from the beginning."""
        # Reset state completely
        self.game_over = False
        self.is_running = False
        self.score = 0
        self.bird_y = None
        
        # Clear pipes and reset timer logic
        self.pipes = []
        
        # Destroy any existing UI elements related to the previous game loop (Timer)
        if self.timer_label_id:
            try:
                self.canvas.delete(self.timer_label_id)
                self.timer_label_id = None
            except tk.TclError:
                pass
        
        # Clear Game Over Frame if it exists
        for widget in list(self.game_over_frame.winfo_children()):
            widget.destroy()

        # Start a new game instance (which triggers the countdown)
        self.start_game()

def main():
    root = tk.Tk()
    
    # Create an instance of our game class and start it
    game = FlappyBirdGame(root)
    game.start_game()  # This initializes bird_y and starts the countdown
    
    # Run the Tkinter event loop
    try:
        root.mainloop()
    except tk.TclError as e:
        print(f"Tkinter Error during mainloop: {e}")

if __name__ == "__main__":
    main()
