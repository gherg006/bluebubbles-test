# The simple login and registration screen shown by the client.

import json
import os
import tkinter as tk
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from chat_window import ChatWindow


# Change this environment variable if the server uses a different address.
SERVER_URL = os.getenv("BLUEBUBBLES_SERVER_URL", "http://192.168.0.150:5000")


class LoginWindow:                      # Displays login or register then sends creds to auth server

    def __init__(self, root):
        self.root = root
        self.root.title("BlueBubbles - Log in")
        self.root.resizable(False, False)

        self.username = tk.StringVar()
        self.password = tk.StringVar()
        self.status = tk.StringVar()

        self.form = ttk.Frame(root, padding=24)
        self.form.grid()
        self._build_form()

    def _build_form(self):
        #Create the two fields and the login/register buttons.
        ttk.Label(self.form, text="Log in", font=("Arial", 16, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 16)
        )
        ttk.Label(self.form, text="Username").grid(row=1, column=0, sticky="w")
        ttk.Entry(self.form, textvariable=self.username, width=28).grid(
            row=2, column=0, columnspan=2, pady=(0, 10)
        )
        ttk.Label(self.form, text="Password").grid(row=3, column=0, sticky="w")
        ttk.Entry(self.form, textvariable=self.password, show="*", width=28).grid(
            row=4, column=0, columnspan=2, pady=(0, 14)
        )
        ttk.Button(self.form, text="Log in", command=self.login).grid(row=5, column=0)
        ttk.Button(self.form, text="Register", command=self.register).grid(
            row=5, column=1
        )
        ttk.Label(self.form, textvariable=self.status).grid(
            row=6, column=0, columnspan=2, pady=(12, 0)
        )

    def login(self):                    # Asks server to check account details
        
        success, message = self._send_request("/login")
        if success:
            self._show_temporary_chat()
        else:
            self.status.set(message)

    def register(self):                 # Client request to server to save new account
        username = self.username.get().strip()
        if not username.isalnum() or not 2 <= len(username) <= 20:
            self.status.set("Username must be 2 to 20 letters or numbers.")
            return

        success, message = self._send_request("/register")
        if success:
            self.password.set("")
            self.status.set("Account created. You can now log in.")
        else:
            self.status.set(message)

    def _send_request(self, path):         # Sends form data in json format
        
        if not self.username.get().strip() or not self.password.get():
            return False, "Enter a username and password."

        data = json.dumps(
            {"username": self.username.get().strip(), "password": self.password.get()}
        ).encode("utf-8")
        request = Request(
            f"{SERVER_URL}{path}", data=data, headers={"Content-Type": "application/json"}
        )

        try:
            with urlopen(request, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
                return body["success"], body["message"]
        except HTTPError as error:
            body = json.loads(error.read().decode("utf-8"))
            return False, body.get("message", "Request failed.")
        except (URLError, TimeoutError, json.JSONDecodeError):
            return False, "Could not reach the server."

    def _show_temporary_chat(self):
        # Replace the login form with the main chat interface.
        self.form.destroy()
        ChatWindow(self.root, self.username.get(), SERVER_URL)
