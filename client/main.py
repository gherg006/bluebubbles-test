"""Starting point for the BlueBubbles client."""

import tkinter as tk

from login_window import LoginWindow


class ClientApplication:
    """Creates and runs the client window."""

    def __init__(self):
        self.root = tk.Tk()
        LoginWindow(self.root)

    def run(self):
        """Keep the window open until the user closes it."""
        self.root.mainloop()


if __name__ == "__main__":
    ClientApplication().run()
