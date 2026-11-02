#Start point for program

#Tkinter is the library we are using for our UI.
import tkinter as tk

#The program starts with the login window hence it is imported from the correct file.
from login_window import LoginWindow


class ClientApplication:         #Creates and runs window
    

    def __init__(self):
        #Initialisation to get the login window.
        self.root = tk.Tk()
        LoginWindow(self.root)

    def run(self):                #Keeps the window open
        #Runs the event loop
        self.root.mainloop()


if __name__ == "__main__":
    ClientApplication().run()
