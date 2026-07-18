import tkinter as tk
from tkinter import messagebox, ttk


class CalculatorLogic:
    """Handles all calculation logic for the calculator."""

    @staticmethod
    def calculate_expression(expression):
        try:
            # Replace visual operators with Python evaluation operators
            expression = expression.replace('×', '*').replace('÷', '/').replace('%', '/')
            
            if not expression.strip():
                return ""
                
            result = eval(expression, {"__builtins__": {}}, {})
            
            # Handle division by zero or other errors that might slip through
            if isinstance(result, complex):
                raise ValueError("Invalid operation")

            # Format the result to avoid long decimals unless necessary
            if isinstance(result, float):
                if result.is_integer():
                    return str(int(result))
                
                # Limit decimal places for display but keep precision reasonable
                formatted_result = f"{result:.10f}".rstrip('0').rstrip('.')
                return formatted_result
            
            return str(result)

        except ZeroDivisionError:
            raise ValueError("Cannot divide by zero")
        except SyntaxError:
            raise ValueError("Invalid expression syntax")
        except Exception as e:
            raise ValueError(f"Calculation error: {str(e)}")


class QuickCalcApp:
    def __init__(self, root):
        self.root = root
        self.current_expression = ""
        
        # Configure style for a modern look
        self.style = ttk.Style()
        try:
            self.theme_name = "clam"  # Fallback theme if others aren't available
            self.style.theme_use(self.theme_name)
        except tk.TclError:
            pass

        self.setup_ui()

    def setup_ui(self):
        """Sets up the user interface components."""
        
        # Main container with padding
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0)

        # Display area (Entry widget)
        self.display_var = tk.StringVar()
        display_entry = ttk.Entry(main_frame, textvariable=self.display_var, font=("Arial", 24), justify='right', state='readonly')
        display_entry.grid(row=0, column=0, columnspan=5, sticky="ew")

        # Button frame
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=1, column=0, pady=(10, 0))

        # Define buttons layout and properties
        self.buttons_config = [
            ("C", "clear"),
            ("(", "parenthesis_left"),
            (")", "parenthesis_right"),
            ("/", "operator"),
            
            ("7", "number"),
            ("8", "number"),
            ("9", "number"),
            ("×", "operator"),

            ("4", "number"),
            ("5", "number"),
            ("6", "number"),
            ("-", "operator"),

            ("1", "number"),
            ("2", "number"),
            ("3", "number"),
            ("+", "operator"),

            ("0", "number"),
            (".", "decimal"),
            ("=", "equals"),
        ]

        # Create buttons dynamically
        for text, command_type in self.buttons_config:
            btn = ttk.Button(button_frame, text=text)
            
            if command_type == 'clear':
                btn.config(command=self.clear_display)
                btn.configure(style='TButton')
            elif command_type == 'equals':
                btn.config(command=self.calculate_result)
                # Make equals button stand out slightly by spanning or color (optional, keeping simple here)
                btn.configure(style='TButton') 
            else:
                btn.config(command=lambda t=text: self.append_to_display(t))

            # Grid placement logic to arrange buttons in 4 columns
            col = 0
            row_idx = 1
            
            if command_type == 'clear':
                col = 0
                row_idx = 2
            elif text == '=':
                col = 3
                row_idx = 5 # Last row for equals usually, or we can do a grid layout manually. 
                           # Let's stick to the list order and adjust columns dynamically based on index if needed,
                           # but simpler is just iterating through rows of buttons.
            
            # Re-implementing button placement logic for better control:
            pass

        # Clear previous attempt at dynamic generation and do a structured grid layout
        
        self.buttons = []
        
        # Row 1: C (0), ( (1), ) (2), ÷ (3) -> Actually let's make it standard calculator layout
        # Standard Layout:
        # [C] [(] [)] [/]
        # [7] [8] [9] [*]
        # [4] [5] [6] [-]
        # [1] [2] [3] [+]
        # [0] [.]=

        row = 0
        
        # Helper to create button at specific grid coordinates
        def make_btn(text, cmd_type):
            btn_text = text
            
            if cmd_type == 'clear':
                command = self.clear_display
            elif cmd_type == 'equals':
                command = self.calculate_result
            else:
                command = lambda t=text: self.append_to_display(t)

            return ttk.Button(button_frame, text=btn_text, command=command)

        # Row 1
        btns_row_1 = [make_btn("C", "clear"), make_btn("(", "parenthesis_left"), make_btn(")", "parenthesis_right"), make_btn("/", "operator")]
        
        for i, btn in enumerate(btns_row_1):
            btn.grid(row=row, column=i, padx=5, pady=5)

        # Row 2
        btns_row_2 = [make_btn("7", "number"), make_btn("8", "number"), make_btn("9", "number"), make_btn("×", "operator")]
        for i, btn in enumerate(btns_row_2):
            btn.grid(row=row+1, column=i, padx=5, pady=5)

        # Row 3
        btns_row_3 = [make_btn("4", "number"), make_btn("5", "number"), make_btn("6", "number"), make_btn("-", "operator")]
        for i, btn in enumerate(btns_row_3):
            btn.grid(row=row+2, column=i, padx=5, pady=5)

        # Row 4
        btns_row_4 = [make_btn("1", "number"), make_btn("2", "number"), make_btn("3", "number"), make_btn("+", "operator")]
        for i, btn in enumerate(btns_row_4):
            btn.grid(row=row+3, column=i, padx=5, pady=5)

        # Row 5 (0 spans two columns usually, or just placed left with dot next to it and equals on right)
        # Let's do: [0] [.]= where = is separate? Or standard grid.
        # Standard: [0] [.] [=] but we need 4 cols. 
        # Col 0: 0, Col 1: ., Col 2: (empty or spacer), Col 3: = ? No, usually = spans bottom right.
        
        btn_0 = make_btn("0", "number")
        btn_dot = make_btn(".", "decimal")
        btn_eq = make_btn("=", "equals")

        # Place 0 and dot in first two columns of last row
        btn_0.grid(row=row+4, column=0, padx=5, pady=5)
        btn_dot.grid(row=row+4, column=1, padx=5, pady=5)
        
        # Make equals button span the remaining width or just put it in col 2 and leave col 3 empty? 
        # Better: Put = in col 2 and make it wider? Or use grid_columnspan.
        btn_eq.grid(row=row+4, column=2, padx=5, pady=5)

    def append_to_display(self, char):
        """Appends a character to the current expression string."""
        # Prevent multiple decimals in one number segment (basic validation)
        if char == '.' and self.current_expression:
            parts = self.current_expression.split('.')
            if len(parts[1]) > 0:
                return
        
        try:
            CalculatorLogic.calculate_expression(self.current_expression + char) # Check validity before adding? 
            # Actually, eval might fail on partial expressions like "5++", but we can catch it.
            self.display_var.set(self.current_expression + char)
            self.update_display()
        except ValueError as e:
            if str(e) == "Invalid expression syntax":
                pass # Ignore syntax errors during typing (e.g., 5+), let user finish the operation
            else:
                messagebox.showerror("Error", f"Calculation Error\n{str(e)}")

    def clear_display(self):
        """Clears the current display and resets state."""
        self.current_expression = ""
        self.display_var.set("")
        
    def calculate_result(self):
        """Calculates the result of the expression in the display."""
        try:
            if not self.current_expression:
                return
                
            # Check for division by zero before calculating to give a friendly message
            # We can't easily check without evaluating, so we catch it.
            
            result = CalculatorLogic.calculate_expression(self.current_expression)
            
            self.display_var.set(result)
            self.current_expression = "" # Reset after calculation
            
        except ValueError as e:
            if "Cannot divide by zero" in str(e):
                messagebox.showerror("Error", "Cannot divide by zero")
            else:
                messagebox.showerror("Error", f"Invalid Expression\n{str(e)}")

    def update_display(self):
        """Updates the display variable with current expression."""
        self.display_var.set(self.current_expression)


def main():
    root = tk.Tk()
    
    # Window configuration
    root.title("QuickCalc")
    root.geometry("320x450")
    root.resizable(False, False)

    app = QuickCalcApp(root)
    
    # Run the application event loop
    try:
        root.mainloop()
    except Exception as e:
        print(f"Application error: {e}")


if __name__ == "__main__":
    main()
