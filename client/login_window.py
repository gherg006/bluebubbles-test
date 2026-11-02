#The login and registration screen shown by the client.

import json
import tkinter as tk
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request
#All of our external libraries are imported here.
#Then from each of the libraries we import the specific classes and functions we need to use in our program.
#This is done to avoid having to write the full library name each time we want to use a class or function from it.
#The os was removed for requests as we are now handling those requests through a different method.

#These are imported from files in the project, and are not external libraries.
from chat_window import ChatWindow
from transport import connection_error_message, open_server, server_url


#Change this environment variable if the server uses a different HTTPS address (in testing it will never).
#Used to be hard coded here but is now a method.
SERVER_URL = server_url()


class LoginWindow:                      #Displays login or register then sends creds to auth server.

    def __init__(self, root):
        self.root = root
        self.root.title("BlueBubbles - Log in")
        self.root.resizable(False, False)

        #The variables that the user inputs into (username and password).
        self.username = tk.StringVar()
        self.password = tk.StringVar()
        #The status whether the user is logged in or not.
        self.status = tk.StringVar()

        #Builds the window then calls the helper method for the actual form.
        self.form = ttk.Frame(root, padding=24)
        self.form.grid()
        self._build_form()

    def _build_form(self):
        #Creates the whole login window.
        ttk.Label(self.form, text="Log in", font=("Arial", 16, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 16)
        )
        #Username Box
        ttk.Label(self.form, text="Username").grid(row=1, column=0, sticky="w")
        ttk.Entry(self.form, textvariable=self.username, width=28).grid(
            row=2, column=0, columnspan=2, pady=(0, 10)
        )
        #Password Box
        ttk.Label(self.form, text="Password").grid(row=3, column=0, sticky="w")
        ttk.Entry(self.form, textvariable=self.password, show="*", width=28).grid(
            row=4, column=0, columnspan=2, pady=(0, 14)
        )
        #Creates the login and register buttons.
        ttk.Button(self.form, text="Log in", command=self.login).grid(row=5, column=0)
        ttk.Button(self.form, text="Register", command=self.register).grid(
            row=5, column=1
        )
        ttk.Label(self.form, textvariable=self.status).grid(
            row=6, column=0, columnspan=2, pady=(12, 0)
        )

    def login(self):                    #Asks server to check account details.
        #Calls the login endpoint to validate credentials
        success, message = self._send_request("/login")
        if success:
            #Shows the chat window
            #Name is depreciated as now it shows the actual chat window.
            self._show_temporary_chat()
        else:
            #If the login fails then the correct error message is shown.
            self.status.set(message)

    def register(self):                 #Client request to server to save new account.
        #The username is passed in and stripped, then assigned to the username variable.
        username = self.username.get().strip()
        if not username.isalnum() or not 2 <= len(username) <= 20:
            self.status.set("Username must be 2 to 20 letters or numbers.")
            return
        #The request is sent to the register endpoint of the server.
        success, message = self._send_request("/register")

        #Basic checks are now done to ensure that the request was recieved and the account was actually created.
        if success:
            self.password.set("")
            self.status.set("Account created. You can now log in.")
            #Once this is all done, the user will be prompted to log in and use their account.
        else:
            self.status.set(message)

    def _send_request(self, path):         #Sends form data in json format. This is a helper function.
        #Ensures that the request is not empty.
        #This means that the server isn't getting empty requests.
        if not self.username.get().strip() or not self.password.get():
            return False, "Enter a username and password."

        #The data is put into a variable using the json format so that it is ready to be sent.
        data = json.dumps(
            {"username": self.username.get().strip(), "password": self.password.get()}
        ).encode("utf-8")
        #This is the other part of the request, where it actually builds the packet.
        #It holds the server URL, endpoint, data as well as any headers that are needed.
        request = Request(
            f"{SERVER_URL}{path}", data=data, headers={"Content-Type": "application/json"}
        )

        #Standard exception handling
        #Made exceptions more specific for iteration 2 to make troubleshooting easier.
        try:
            with open_server(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
                return body["success"], body["message"]
        except HTTPError as error:
            body = json.loads(error.read().decode("utf-8"))
            return False, body.get("message", "Request failed.")
        except (URLError, TimeoutError) as error:
            return False, connection_error_message(error)
        except json.JSONDecodeError:
            return False, "The server returned an invalid response."

    def _show_temporary_chat(self):     #Still used simply has the depreciated name in the program.
        # Replace the login form with the main chat interface.
        self.form.destroy()
        ChatWindow(self.root, self.username.get(), SERVER_URL)
