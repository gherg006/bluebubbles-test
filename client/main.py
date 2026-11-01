#Start point for program.

import tkinter as tk                 #Imports standard python GUI library tkinter.

from login_window import LoginWindow      #Imports login window class from login_window.py.


class ClientApplication:         #Creates and runs application window.
    

    def __init__(self):           #The constructor method used to create the main window.
        self.root = tk.Tk()
        LoginWindow(self.root)     
        #Initialises the login window by passing in the root window into the class.

    def run(self):                
        #This starts the event loop for the application window.
        
        self.root.mainloop()        
        #Keeps the application window open until the user closes it.


if __name__ == "__main__":        
    #This is the main entry point for the program, ensuring it only runs when executed directly.
    ClientApplication().run() 
    #Creates an instance of the application and starts the GUI.
