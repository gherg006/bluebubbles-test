import json
import tkinter as tk
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


# Match the sharp blue layout from the reference.
BACKGROUND = "#d8e7fa"
PANEL = "#bdd9f1"
BUTTON = "#a8d4ed"
TEXT = "#1f3449"
WHITE = "#ffffff"


class ChatWindow:
    # Show the main sharp-edged chat screen after login.
    def __init__(self, root, username, server_url):
        self.root = root
        self.username = username
        self.server_url = server_url
        self.users = []
        self.message_text = tk.StringVar()
        self.search_text = tk.StringVar(value="Search")
        self.chat_title = tk.StringVar(value="Chat user")

        root.title("BlueBubbles")
        root.geometry("1080x650")
        root.minsize(980, 580)
        root.configure(bg=WHITE)
        self._build_window()
        self._load_users()

    def _build_window(self):
        # Keep the app box and separate sort box in the same arrangement as the image.
        page = tk.Frame(self.root, bg=WHITE, padx=26, pady=20)
        page.pack(fill="both", expand=True)
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(0, weight=1)

        app = tk.Frame(page, bg=BACKGROUND, bd=1, relief="solid", padx=8, pady=8)
        app.grid(row=0, column=0, sticky="nsew")
        app.grid_columnconfigure(0, weight=4)
        app.grid_columnconfigure(1, weight=1)
        app.grid_rowconfigure(1, weight=1)
        self._build_header(app)
        self._build_chat(app)
        self._build_users(app)
        self._build_sorting(page)

    def _build_header(self, parent):
        # Add the logo area, exit button, chat title, and search field.
        header = tk.Frame(parent, bg=BACKGROUND)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        header.grid_columnconfigure(2, weight=1)
        self._label(header, "BlueBubbles", 0, 0, width=14, font=("Arial", 10, "bold"), padx=8, pady=11)
        tk.Button(header, text="Exit", command=self.root.destroy, bg="#f5aaa4", relief="solid", bd=1).grid(
            row=0, column=1, padx=(12, 8)
        )
        tk.Label(header, textvariable=self.chat_title, bg=BUTTON, fg=TEXT, relief="solid", bd=1).grid(
            row=0, column=2, sticky="ew", padx=(0, 8), ipady=5
        )
        search = tk.Entry(header, textvariable=self.search_text, bg=BUTTON, fg=TEXT, relief="solid", bd=1, width=17)
        search.grid(row=0, column=3, ipady=5)
        search.bind("<FocusIn>", self._clear_search_hint)
        self.search_text.trace_add("write", self._filter_users)

    def _build_chat(self, parent):
        # Build the large left conversation panel and message entry bar.
        chat = tk.Frame(parent, bg=BACKGROUND)
        chat.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        chat.grid_columnconfigure(0, weight=1)
        chat.grid_rowconfigure(1, weight=1)
        tk.Label(chat, text="User1        min:hr   dd/mm/yyyy", bg=BACKGROUND, fg=TEXT, font=("Arial", 9, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        self.messages = tk.Frame(chat, bg=WHITE, bd=1, relief="solid")
        self.messages.grid(row=1, column=0, sticky="nsew")
        self.messages.grid_columnconfigure(0, weight=1)
        self._add_message("User1", "Choose an account from the users list to start chatting.", "", True)

        compose = tk.Frame(chat, bg=BACKGROUND, pady=8)
        compose.grid(row=2, column=0, sticky="ew")
        compose.grid_columnconfigure(0, weight=1)
        entry = tk.Entry(compose, textvariable=self.message_text, bg=WHITE, fg=TEXT, font=("Arial", 11), bd=1, relief="solid")
        entry.grid(row=0, column=0, sticky="ew", ipady=8)
        entry.bind("<Return>", lambda event: self.send_message())
        tk.Button(compose, text="+", command=self.send_message, bg=WHITE, fg="#6e8393", font=("Arial", 20), bd=1, relief="solid", width=2).grid(
            row=0, column=1, padx=(8, 0)
        )

    def _build_users(self, parent):
        # Display every registered account in the narrow users column.
        users = tk.Frame(parent, bg=BACKGROUND)
        users.grid(row=1, column=1, sticky="nsew")
        users.grid_columnconfigure(0, weight=1)
        users.grid_rowconfigure(2, weight=1)
        self._label(users, "Filter\n(dropdown)", 0, 0, pady=5, font=("Arial", 9))
        tk.Label(users, text="Users", bg=PANEL, fg=TEXT, font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="ew")
        self.user_list = tk.Listbox(users, bg=WHITE, fg=TEXT, font=("Arial", 10), bd=1, relief="solid", selectbackground="#8bbde0", activestyle="none")
        self.user_list.grid(row=2, column=0, sticky="nsew")
        self.user_list.bind("<<ListboxSelect>>", self._select_user)

    def _build_sorting(self, parent):
        # Keep the sorting actions in their own right-hand box.
        panel = tk.Frame(parent, bg=PANEL, bd=1, relief="solid", padx=8, pady=8)
        panel.grid(row=0, column=1, sticky="ns", padx=(24, 0))
        for row, (label, command) in enumerate((("Most Recent", self._keep_order), ("Alphabetical", self._sort_alphabetical), ("Frequency", self._keep_order), ("Date Added", self._keep_order), ("New Messages", self._keep_order))):
            tk.Button(panel, text=label, command=command, bg=BUTTON, fg=TEXT, font=("Arial", 9), bd=1, relief="solid", width=16, pady=10).grid(
                row=row, column=0, pady=(0, 8), sticky="ew"
            )

    def _label(self, parent, text, row, column, **options):
        # Create a square-edged blue label used by the layout.
        tk.Label(parent, text=text, bg=BUTTON, fg=TEXT, bd=1, relief="solid", **options).grid(row=row, column=column, sticky="ew", pady=(0, 8))

    def _load_users(self):
        # Ask the server for all registered usernames.
        try:
            with urlopen(f"{self.server_url}/users", timeout=5) as response:
                self.users = json.loads(response.read().decode("utf-8")).get("users", [])
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            self.users = []
        self._filter_users()

    def _clear_search_hint(self, event):
        # Remove the search hint when the field receives focus.
        if self.search_text.get() == "Search":
            self.search_text.set("")

    def _filter_users(self, *args):
        # Refresh the list with account names that match the search term.
        query = self.search_text.get().lower()
        matches = self.users if query == "search" else [user for user in self.users if query in user.lower()]
        self.user_list.delete(0, "end")
        for user in matches:
            self.user_list.insert("end", user)

    def _sort_alphabetical(self):
        # Sort the loaded usernames alphabetically.
        self.users.sort(key=str.lower)
        self._filter_users()

    def _keep_order(self):
        # Preserve database order until messages are stored by the server.
        self._filter_users()

    def _select_user(self, event):
        # Change the current chat label when a user is selected.
        selected = self.user_list.curselection()
        if selected:
            self.chat_title.set(f"Chat user: {self.user_list.get(selected[0])}")

    def _add_message(self, sender, text, time, incoming):
        # Add a plain message line to the conversation area.
        row = self.messages.grid_size()[1]
        block = tk.Frame(self.messages, bg=WHITE)
        block.grid(row=row, column=0, sticky="w" if incoming else "e", padx=10, pady=(10, 0))
        tk.Label(block, text=f"{sender}    {time}".strip(), bg=WHITE, fg=TEXT, font=("Arial", 9, "bold")).pack(anchor="w")
        tk.Label(block, text=text, bg=WHITE, fg="#526b7e", font=("Arial", 10), justify="left", wraplength=430).pack(anchor="w", pady=(3, 0))

    def send_message(self):
        # Show the new message locally until chat storage is added.
        text = self.message_text.get().strip()
        if text:
            self._add_message(self.username, text, datetime.now().strftime("%H:%M"), False)
            self.message_text.set("")
