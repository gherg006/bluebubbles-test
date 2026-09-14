# Start point for program

import tkinter as tk

from login_window import LoginWindow


class ClientApplication:         # Creates and runs window
    

    def __init__(self):
        self.root = tk.Tk()
        LoginWindow(self.root)

    def run(self):                # Keeps the window open
        
        self.root.mainloop()


if __name__ == "__main__":
    ClientApplication().run()
