import json
import hashlib
import os
import queue
import tempfile
import threading
import tkinter as tk
import time
import uuid
from pathlib import Path
from tkinter import filedialog, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request

from transport import open_server


# Match the sharp blue layout from the reference.
BACKGROUND = "#d8e7fa"
PANEL = "#bdd9f1"
BUTTON = "#a8d4ed"
TEXT = "#1f3449"
WHITE = "#ffffff"
REFRESH_INTERVAL_MS = 1000
CONTACT_SORT_REFRESH_MS = 10000
UI_RESULT_INTERVAL_MS = 50
MESSAGE_PAGE_SIZE = 100
MESSAGE_BUBBLE_WIDTH = 390
MAXIMUM_FILE_BYTES = 2 * 1024 * 1024 * 1024
FILE_TRANSFER_TIMEOUT_SECONDS = 60 * 60


class MultipartFileBody:
    # Stream one attachment so progress can advance while urllib sends it.
    _chunk_size = 64 * 1024

    def __init__(self, recipient, filename, path, checksum, progress_callback):
        boundary = f"----BlueBubbles{uuid.uuid4().hex}"
        self.content_type = f"multipart/form-data; boundary={boundary}"
        self._prefix = self._headers(boundary, recipient, filename, checksum)
        self._suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        self._file = open(path, "rb")
        self._file_size = os.path.getsize(path)
        self.content_length = len(self._prefix) + self._file_size + len(self._suffix)
        self._progress_callback = progress_callback
        self._sent_file_bytes = 0

    @staticmethod
    def _headers(boundary, recipient, filename, checksum):
        encoded = bytearray()
        for name, value in (("recipient", recipient), ("checksum", checksum)):
            encoded.extend(f"--{boundary}\r\n".encode("ascii"))
            encoded.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        encoded.extend(f"--{boundary}\r\n".encode("ascii"))
        encoded.extend(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8")
        )
        encoded.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        return bytes(encoded)

    def read(self, size=-1):
        # HTTPConnection requests bounded reads; return only the next portion of the body.
        if size is None or size < 0:
            size = self._chunk_size
        parts = []
        remaining = size
        while remaining:
            if self._prefix:
                part, self._prefix = self._prefix[:remaining], self._prefix[remaining:]
            elif self._file is not None:
                part = self._file.read(min(remaining, self._chunk_size))
                if not part:
                    self._file.close()
                    self._file = None
                    continue
                self._sent_file_bytes += len(part)
                self._progress_callback(self._sent_file_bytes, self._file_size)
            elif self._suffix:
                part, self._suffix = self._suffix[:remaining], self._suffix[remaining:]
            else:
                break
            parts.append(part)
            remaining -= len(part)
        return b"".join(parts)

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None


class ChatWindow:
    # Show the main sharp-edged chat screen after login.
    def __init__(self, root, username, server_url):
        self.root = root
        self.username = username
        self.server_url = server_url
        self.users = []
        self.contacts = []
        self.contact_details = {}
        self._displayed_contacts = []
        self._context_contact = None
        self._sort_mode = "saved"
        self._contacts_request = None
        self._contacts_refresh_pending = False
        self._read_sent = {}
        self.message_text = tk.StringVar()
        self.send_status = tk.StringVar()
        self.search_text = tk.StringVar(value="Search")
        self.chat_title = tk.StringVar(value="Chat user")
        self.recipient = None
        self.current_messages = None
        self.attachment = None
        self._has_older = False
        self._has_newer = False
        self._conversation_generation = 0
        self._history_request = None
        self._poll_request = None
        self._send_in_flight = False
        self._attachment_check = None
        self._download_in_flight = False
        self._contact_delete_in_flight = False
        self._ui_results = queue.Queue()

        root.title("BlueBubbles")
        root.geometry("1080x650")
        root.minsize(980, 580)
        root.configure(bg=WHITE)
        self._build_window()
        self._load_users()
        self._load_contacts()
        self.root.after(UI_RESULT_INTERVAL_MS, self._drain_background)
        self._schedule_message_refresh()
        self.root.after(CONTACT_SORT_REFRESH_MS, self._refresh_contact_sort)

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
        tk.Label(
            chat,
            text=f"Logged in as: {self.username}",
            bg=BACKGROUND,
            fg=TEXT,
            font=("Arial", 9, "bold"),
        ).grid(
            row=0, column=0, sticky="w", pady=(0, 5)
        )
        message_area = tk.Frame(chat, bg=WHITE, bd=1, relief="solid")
        message_area.grid(row=1, column=0, sticky="nsew")
        message_area.grid_columnconfigure(0, weight=1)
        message_area.grid_rowconfigure(0, weight=1)
        self.message_canvas = tk.Canvas(message_area, bg=WHITE, highlightthickness=0)
        self.message_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(
            message_area,
            orient="vertical",
            command=self._scrollbar_scroll,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.message_canvas.configure(yscrollcommand=scrollbar.set)
        self.messages = tk.Frame(self.message_canvas, bg=WHITE)
        self.message_window = self.message_canvas.create_window(
            (0, 0), window=self.messages, anchor="nw"
        )
        self.messages.bind("<Configure>", self._update_message_scroll_region)
        self.message_canvas.bind("<Configure>", self._resize_message_frame)
        self.root.bind_all("<MouseWheel>", self._scroll_messages)
        self.root.bind_all("<Button-4>", self._scroll_messages)
        self.root.bind_all("<Button-5>", self._scroll_messages)
        self._show_empty_conversation()

        compose = tk.Frame(chat, bg=BACKGROUND, pady=8)
        compose.grid(row=2, column=0, sticky="ew")
        compose.grid_columnconfigure(0, weight=1)
        self.message_entry = tk.Entry(compose, textvariable=self.message_text, bg=WHITE, fg=TEXT, font=("Arial", 11), bd=1, relief="solid")
        self.message_entry.grid(row=0, column=0, sticky="ew", ipady=8)
        self.message_entry.bind("<Return>", self._send_from_enter)
        self.message_entry.bind("<KP_Enter>", self._send_from_enter)
        self.send_button = tk.Button(
            compose,
            text="Send",
            command=self.send_message,
            bg=BUTTON,
            fg=TEXT,
            font=("Arial", 10, "bold"),
            bd=1,
            relief="solid",
            width=8,
        )
        self.send_button.grid(
            row=0, column=1, padx=(8, 0)
        )
        self.attach_button = tk.Button(
            compose,
            text="Attach file",
            command=self.attach_file,
            bg=BUTTON,
            fg=TEXT,
            font=("Arial", 10, "bold"),
            bd=1,
            relief="solid",
        )
        self.attach_button.grid(row=0, column=2, padx=(8, 0))
        self.attachment_status = tk.StringVar()
        self.attachment_progress = tk.DoubleVar(value=0)
        self.attachment_frame = tk.Frame(compose, bg=BACKGROUND)
        self.attachment_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        self.attachment_frame.grid_columnconfigure(1, weight=1)
        tk.Label(
            self.attachment_frame,
            textvariable=self.attachment_status,
            bg=BACKGROUND,
            fg=TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Progressbar(
            self.attachment_frame,
            maximum=100,
            variable=self.attachment_progress,
            mode="determinate",
        ).grid(row=0, column=1, sticky="ew")
        self.remove_attachment_button = tk.Button(
            self.attachment_frame,
            text="Remove",
            command=self._clear_attachment,
            bg="#f5aaa4",
            relief="solid",
            bd=1,
        )
        self.remove_attachment_button.grid(row=0, column=2, padx=(8, 0))
        self.attachment_frame.grid_remove()
        tk.Label(
            compose,
            textvariable=self.send_status,
            bg=BACKGROUND,
            fg="#9a2d27",
            anchor="w",
        ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(5, 0))

    def _build_users(self, parent):
        # Display only the accounts the user has chosen to message.
        users = tk.Frame(parent, bg=BACKGROUND)
        users.grid(row=1, column=1, sticky="nsew")
        users.grid_columnconfigure(0, weight=1)
        users.grid_rowconfigure(2, weight=1)
        tk.Button(
            users,
            text="Add user",
            command=self._open_add_user_menu,
            bg=BUTTON,
            fg=TEXT,
            font=("Arial", 9, "bold"),
            bd=1,
            relief="solid",
            pady=5,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tk.Label(users, text="Messages", bg=PANEL, fg=TEXT, font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="ew")
        self.user_list = tk.Listbox(users, bg=WHITE, fg=TEXT, font=("Arial", 10), bd=1, relief="solid", selectbackground="#8bbde0", activestyle="none")
        self.user_list.grid(row=2, column=0, sticky="nsew")
        self.user_list.bind("<<ListboxSelect>>", self._select_user)
        self.user_list.bind("<Button-3>", self._open_contact_menu)
        self.contact_menu = tk.Menu(self.root, tearoff=0)
        self.contact_menu.add_command(label="Delete", command=self._delete_selected_contact)
        self.contact_menu.add_command(label="Pin (coming soon)", state="disabled")

    def _build_sorting(self, parent):
        # Keep the sorting actions in their own right-hand box.
        panel = tk.Frame(parent, bg=PANEL, bd=1, relief="solid", padx=8, pady=8)
        panel.grid(row=0, column=1, sticky="ns", padx=(24, 0))
        self.sort_buttons = {}
        for row, (label, mode) in enumerate((("Most Recent", "most_recent"), ("Alphabetical", "alphabetical"), ("Frequency", "frequency"), ("Date Added", "date_added"), ("New Messages", "new_messages"))):
            command = lambda selected=mode: self._set_sort_mode(selected)
            button = tk.Button(panel, text=label, command=command, bg=BUTTON, fg=TEXT, font=("Arial", 9), bd=1, relief="solid", width=16, pady=10)
            button.grid(
                row=row, column=0, pady=(0, 8), sticky="ew"
            )
            self.sort_buttons[mode] = button

    def _label(self, parent, text, row, column, **options):
        # Create a square-edged blue label used by the layout.
        tk.Label(parent, text=text, bg=BUTTON, fg=TEXT, bd=1, relief="solid", **options).grid(row=row, column=column, sticky="ew", pady=(0, 8))

    def _post_ui(self, callback, *args):
        # Worker threads only enqueue data; Tk widgets stay on the UI thread.
        self._ui_results.put((callback, args))

    def _drain_background(self):
        for _ in range(100):
            try:
                callback, args = self._ui_results.get_nowait()
            except queue.Empty:
                break
            callback(*args)
        self.root.after(UI_RESULT_INTERVAL_MS, self._drain_background)

    def _run_background(self, work, on_complete):
        def run():
            try:
                result = work()
                error = None
            except Exception as caught:
                result, error = None, caught
            self._post_ui(on_complete, result, error)

        threading.Thread(target=run, daemon=True).start()

    def _load_users(self):
        # Ask the server for usernames used by the add-user menu.
        def fetch():
            with open_server(f"{self.server_url}/users", timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("users", [])

        def loaded(users, error):
            self.users = users if error is None else []
            self._filter_users()

        self._run_background(fetch, loaded)

    def _load_contacts(self):
        # Fetch saved contacts and the server-side metrics used by the sort buttons.
        if self._contacts_request is not None:
            self._contacts_refresh_pending = True
            return
        request_token = object()
        self._contacts_request = request_token

        def fetch():
            with open_server(f"{self.server_url}/contacts", timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        def loaded(page, error):
            if self._contacts_request is not request_token:
                return
            self._contacts_request = None
            if error is None:
                self.contacts = page.get("contacts", [])
                self.contact_details = {
                    item["name"]: item for item in page.get("contact_details", [])
                }
                self._filter_users()
            if self._contacts_refresh_pending:
                self._contacts_refresh_pending = False
                self._load_contacts()

        self._run_background(fetch, loaded)

    def _refresh_contact_sort(self):
        if self._sort_mode in ("most_recent", "frequency", "new_messages"):
            self._load_contacts()
        self.root.after(CONTACT_SORT_REFRESH_MS, self._refresh_contact_sort)

    def _mark_read_through(self, contact, message_id):
        # Persist only messages actually reached in the open conversation.
        previous = self._read_sent.get(contact, 0)
        if message_id <= previous:
            return
        self._read_sent[contact] = message_id
        request = Request(
            f"{self.server_url}/contacts/read",
            data=json.dumps({"contact": contact, "through_id": message_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

        def mark():
            with open_server(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("success", False)

        def marked(success, error):
            if error is not None or not success:
                if self._read_sent.get(contact) == message_id:
                    self._read_sent[contact] = previous
            else:
                self._load_contacts()

        self._run_background(mark, marked)

    def _clear_search_hint(self, event):
        # Remove the search hint when the field receives focus.
        if self.search_text.get() == "Search":
            self.search_text.set("")

    def _filter_users(self, *args):
        # Search and sort contacts while keeping the current chat selected.
        query = self.search_text.get().lower()
        matches = self.contacts if query == "search" else [user for user in self.contacts if query in user.lower()]
        if self._sort_mode == "alphabetical":
            matches = sorted(matches, key=str.casefold)
        elif self._sort_mode == "most_recent":
            matches = sorted(matches, key=lambda user: (
                -self.contact_details.get(user, {}).get("latest_message_id", 0), user.casefold()
            ))
        elif self._sort_mode == "frequency":
            matches = sorted(matches, key=lambda user: (
                -self.contact_details.get(user, {}).get("message_count", 0),
                -self.contact_details.get(user, {}).get("latest_message_id", 0),
                user.casefold(),
            ))
        elif self._sort_mode == "date_added":
            matches = sorted(matches, key=lambda user: (
                -self.contact_details.get(user, {}).get("added_at", 0), user.casefold()
            ))
        elif self._sort_mode == "new_messages":
            matches = [user for user in matches if self.contact_details.get(user, {}).get("unread_count", 0) > 0]
            matches = sorted(matches, key=lambda user: (
                -self.contact_details[user]["unread_count"],
                -self.contact_details[user].get("latest_message_id", 0),
                user.casefold(),
            ))
        self._displayed_contacts = list(matches)
        self.user_list.delete(0, "end")
        for user in matches:
            unread = self.contact_details.get(user, {}).get("unread_count", 0)
            self.user_list.insert("end", f"{user} ({unread})" if unread else user)
        if self.recipient in matches:
            self.user_list.selection_set(matches.index(self.recipient))

    def _set_sort_mode(self, mode):
        self._sort_mode = mode
        for button_mode, button in self.sort_buttons.items():
            button.configure(relief="sunken" if button_mode == mode else "solid")
        self._filter_users()
        if mode in ("most_recent", "frequency", "new_messages"):
            self._load_contacts()

    def _select_user(self, event):
        # Change the current chat label when a user is selected.
        selected = self.user_list.curselection()
        if selected and selected[0] < len(self._displayed_contacts):
            recipient = self._displayed_contacts[selected[0]]
            if recipient != self.recipient:
                self._open_conversation(recipient)

    def _open_contact_menu(self, event):
        # Select the right-clicked contact before showing its actions.
        index = self.user_list.nearest(event.y)
        bounds = self.user_list.bbox(index)
        if (not bounds or index >= len(self._displayed_contacts)
                or not bounds[1] <= event.y <= bounds[1] + bounds[3]):
            return
        self._context_contact = self._displayed_contacts[index]
        self.user_list.selection_clear(0, "end")
        self.user_list.selection_set(index)
        try:
            self.contact_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.contact_menu.grab_release()

    def _delete_selected_contact(self):
        # Remove only the selected account from this user's saved chat list.
        if self._contact_delete_in_flight:
            return
        selected = self.user_list.curselection()
        if self._context_contact is None and (not selected or selected[0] >= len(self._displayed_contacts)):
            return
        contact = self._context_contact or self._displayed_contacts[selected[0]]
        self._context_contact = None
        data = json.dumps({"contact": contact}).encode("utf-8")
        request = Request(
            f"{self.server_url}/contacts",
            data=data,
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        self._contact_delete_in_flight = True

        def remove():
            with open_server(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("success", False)

        def removed(success, error):
            self._contact_delete_in_flight = False
            if error is not None or not success:
                self.send_status.set("Could not delete that contact.")
                return
            if contact in self.contacts:
                self.contacts.remove(contact)
            self.contact_details.pop(contact, None)
            self._filter_users()
            self._load_contacts()
            if self.recipient == contact:
                self._conversation_generation += 1
                self.recipient = None
                self.current_messages = None
                self._history_request = None
                self._poll_request = None
                self.chat_title.set("Chat user")
                self._clear_messages()
                self._show_empty_conversation()
                self._scroll_to_latest()

        self._run_background(remove, removed)

    def _open_add_user_menu(self):
        # Show registered accounts only when the user chooses to add a contact.
        menu = tk.Toplevel(self.root)
        menu.title("Add user")
        menu.resizable(False, False)
        menu.configure(bg=BACKGROUND, padx=12, pady=12)

        tk.Label(menu, text="Choose a user to message", bg=BACKGROUND, fg=TEXT).pack(anchor="w", pady=(0, 8))
        search_text = tk.StringVar()
        tk.Entry(menu, textvariable=search_text, bg=WHITE, fg=TEXT, bd=1, relief="solid", width=26).pack(pady=(0, 8))
        user_picker = tk.Listbox(menu, bg=WHITE, fg=TEXT, bd=1, relief="solid", width=26, height=10)
        user_picker.pack()

        def show_matching_users(*args):
            # Show only registered accounts that match the add-user search.
            query = search_text.get().lower()
            available = [
                user for user in self.users
                if user != self.username and user not in self.contacts and query in user.lower()
            ]
            user_picker.delete(0, "end")
            for user in available:
                user_picker.insert("end", user)

        search_text.trace_add("write", show_matching_users)
        show_matching_users()

        def add_selected_user():
            # Add the selected account to the main messages list.
            selected = user_picker.curselection()
            if not selected:
                return
            user = user_picker.get(selected[0])
            add_button.configure(state="disabled")

            def saved(success, error):
                if not menu.winfo_exists():
                    return
                if error is not None or not success:
                    add_button.configure(state="normal")
                    self.send_status.set("Could not save that user to your chats.")
                    return
                self.contacts.append(user)
                self._filter_users()
                self._open_conversation(user)
                self._load_contacts()
                menu.destroy()

            self._run_background(lambda: self._save_contact(user), saved)

        add_button = tk.Button(menu, text="Add", command=add_selected_user, bg=BUTTON, fg=TEXT, bd=1, relief="solid", padx=18)
        add_button.pack(pady=(10, 0))

    def _save_contact(self, contact):
        # Save an added user so the sidebar can be restored next time this user logs in.
        data = json.dumps({"contact": contact}).encode("utf-8")
        request = Request(
            f"{self.server_url}/contacts",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with open_server(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8")).get("success", False)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
            return False

    def _open_conversation(self, recipient):
        # Replace only this conversation's page when the selection changes.
        self._conversation_generation += 1
        self.recipient = recipient
        self.current_messages = None
        self._has_older = False
        self._has_newer = False
        self._history_request = None
        self._poll_request = None
        self.send_status.set("")
        self.chat_title.set(f"Chat user: {recipient}")
        self._clear_messages()
        self._add_message("BlueBubbles", "Loading messages...", "", True)
        self._load_page("latest")

    def _fetch_conversation(self, recipient, before_id=None, after_id=None):
        # The server sends at most 100 rows and a cursor for either direction.
        parameters = {"with": recipient}
        if before_id is not None:
            parameters["before_id"] = before_id
        if after_id is not None:
            parameters["after_id"] = after_id
        with open_server(f"{self.server_url}/messages?{urlencode(parameters)}", timeout=5) as response:
            page = json.loads(response.read().decode("utf-8"))
        messages = page.get("messages")
        if (
            not isinstance(messages, list)
            or not isinstance(page.get("has_more"), bool)
            or len(messages) > MESSAGE_PAGE_SIZE
            or any(not isinstance(message.get("id"), int) for message in messages)
        ):
            raise ValueError("The server does not support bounded message pages.")
        return page

    def _load_page(self, direction):
        if self._history_request is not None or not self.recipient:
            return
        if direction != "latest" and not self.current_messages:
            return
        recipient = self.recipient
        generation = self._conversation_generation
        cursor = None
        if direction == "older":
            cursor = self.current_messages[0]["id"]
        elif direction == "newer":
            cursor = self.current_messages[-1]["id"]
        request_token = object()
        self._history_request = request_token
        self._poll_request = None

        def fetch():
            return self._fetch_conversation(
                recipient,
                before_id=cursor if direction == "older" else None,
                after_id=cursor if direction == "newer" else None,
            )

        def loaded(page, error):
            if self._history_request is not request_token or generation != self._conversation_generation:
                return
            self._history_request = None
            if error is not None:
                self.send_status.set(
                    "Update the server before using message paging."
                    if isinstance(error, ValueError) else "Could not load messages."
                )
                return
            saved_messages = page["messages"]
            if direction == "older" and not saved_messages:
                self._has_older = False
                return
            if direction == "newer" and not saved_messages:
                self._has_newer = False
                return
            self.current_messages = saved_messages
            if direction == "latest":
                self._has_older, self._has_newer = page["has_more"], False
            elif direction == "older":
                self._has_older, self._has_newer = page["has_more"], True
            else:
                self._has_older, self._has_newer = True, page["has_more"]
            self._clear_messages()
            for message in saved_messages:
                self._add_saved_message(message)
            if not saved_messages:
                self._add_message(self.recipient, "No messages yet.", "", True)
            if direction == "newer":
                self._scroll_to_top()
            else:
                self._scroll_to_latest()
            if saved_messages and not self._has_newer:
                self._mark_read_through(recipient, saved_messages[-1]["id"])

        self._run_background(fetch, loaded)

    def _append_new_messages(self, new_messages, scroll_to_latest=False):
        if not new_messages or self.current_messages is None:
            return
        was_at_latest = self.message_canvas.yview()[1] >= 0.99
        existing = {message["id"] for message in self.current_messages}
        additions = [message for message in new_messages if message["id"] not in existing]
        if not additions:
            return
        additions.sort(key=lambda message: message["id"])
        if self.current_messages and additions[0]["id"] < self.current_messages[-1]["id"]:
            merged = {message["id"]: message for message in self.current_messages + additions}
            if len(merged) > MESSAGE_PAGE_SIZE:
                self._has_older = True
            self.current_messages = sorted(merged.values(), key=lambda message: message["id"])[-MESSAGE_PAGE_SIZE:]
            self._clear_messages()
            for message in self.current_messages:
                self._add_saved_message(message)
        else:
            if not self.current_messages:
                self._clear_messages()
            for message in additions:
                self._add_saved_message(message)
            self.current_messages.extend(additions)
            excess = len(self.current_messages) - MESSAGE_PAGE_SIZE
            if excess > 0:
                del self.current_messages[:excess]
                for child in self.messages.winfo_children()[:excess]:
                    child.destroy()
                self._has_older = True
        if scroll_to_latest or was_at_latest:
            self._scroll_to_latest()

    def _add_saved_message(self, message):
        self._add_message(
            message["sender"],
            message["content"],
            message["time"],
            message["sender"] != self.username,
            message.get("date", ""),
            message.get("file_id"),
            message.get("filename"),
        )

    def _schedule_message_refresh(self):
        # Poll for only rows newer than the last displayed message.
        self._poll_new_messages()
        self.root.after(REFRESH_INTERVAL_MS, self._schedule_message_refresh)

    def _poll_new_messages(self):
        if (
            not self.recipient or self.current_messages is None or self._has_newer
            or self._history_request is not None or self._poll_request is not None
            or self._send_in_flight
        ):
            return
        recipient = self.recipient
        generation = self._conversation_generation
        cursor = self.current_messages[-1]["id"] if self.current_messages else None
        request_token = object()
        self._poll_request = request_token

        def fetch():
            return self._fetch_conversation(recipient, after_id=cursor)

        def loaded(page, error):
            if self._poll_request is not request_token or generation != self._conversation_generation:
                return
            self._poll_request = None
            if error is not None:
                if isinstance(error, ValueError):
                    self.send_status.set("Update the server before using message paging.")
                return
            if cursor is None:
                if not page["messages"] and not self.current_messages:
                    return
                self.current_messages = page["messages"]
                self._has_older = page["has_more"]
                self._clear_messages()
                for message in self.current_messages:
                    self._add_saved_message(message)
                if not self.current_messages:
                    self._add_message(self.recipient, "No messages yet.", "", True)
                else:
                    self._scroll_to_latest()
                    self._mark_read_through(recipient, self.current_messages[-1]["id"])
            else:
                was_at_latest = self.message_canvas.yview()[1] >= 0.99
                self._append_new_messages(page["messages"])
                if page["messages"]:
                    if was_at_latest:
                        self._mark_read_through(recipient, self.current_messages[-1]["id"])
                    if self._sort_mode in ("most_recent", "frequency", "new_messages"):
                        self._load_contacts()
            if page["has_more"] and cursor is not None:
                self._poll_new_messages()

        self._run_background(fetch, loaded)

    def _clear_messages(self):
        # Remove all currently displayed message rows.
        for child in self.messages.winfo_children():
            child.destroy()

    def _update_message_scroll_region(self, event):
        # Keep the scrollbar sized to the complete conversation.
        self.message_canvas.configure(scrollregion=self.message_canvas.bbox("all"))

    def _resize_message_frame(self, event):
        # Keep the message rows as wide as the visible conversation area.
        self.message_canvas.itemconfigure(self.message_window, width=event.width)

    def _scroll_messages(self, event):
        # Scroll only when the pointer is over the conversation, not other controls.
        widget = event.widget
        is_message_widget = widget == self.message_canvas
        while not is_message_widget and widget is not None:
            is_message_widget = widget == self.messages
            widget = widget.master
        if not is_message_widget:
            return
        if getattr(event, "delta", 0):
            amount = -int(event.delta / 120)
        else:
            amount = -1 if event.num == 4 else 1
        self.message_canvas.yview_scroll(amount, "units")
        self.root.after_idle(self._maybe_load_history)
        return "break"

    def _scrollbar_scroll(self, *args):
        self.message_canvas.yview(*args)
        self.root.after_idle(self._maybe_load_history)

    def _maybe_load_history(self):
        if not self.current_messages or self._history_request is not None:
            return
        top, bottom = self.message_canvas.yview()
        if top <= 0.001 and self._has_older:
            self._load_page("older")
        elif bottom >= 0.999 and self._has_newer:
            self._load_page("newer")
        elif bottom >= 0.999 and self.current_messages:
            self._mark_read_through(self.recipient, self.current_messages[-1]["id"])

    def _scroll_to_latest(self):
        # Show the most recent message after sending or receiving a new one.
        self.root.update_idletasks()
        self.message_canvas.yview_moveto(1)

    def _scroll_to_top(self):
        # Start a newly opened conversation at its earliest saved message.
        self.root.update_idletasks()
        self.message_canvas.yview_moveto(0)

    def _show_empty_conversation(self):
        # Explain why the chat area is empty before an account is selected.
        self._add_message("BlueBubbles", "Choose an account from the users list to start chatting.", "", True)

    def _add_message(self, sender, text, time, incoming, sent_date="", file_id=None, filename=None):
        # Give each side a fixed-width column so message text starts consistently.
        block = tk.Frame(self.messages, bg=WHITE)
        block.pack(fill="x", padx=12, pady=(8, 0))
        bubble_color = WHITE
        bubble = tk.Frame(
            block, bg=bubble_color, bd=0, padx=10, pady=7,
        )
        bubble.pack(side="left" if incoming else "right")
        bubble.grid_columnconfigure(0, minsize=MESSAGE_BUBBLE_WIDTH)

        header = tk.Frame(bubble, bg=bubble_color)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header, text=sender, bg=bubble_color, fg=TEXT,
            font=("Arial", 9, "bold"),
        ).pack(side="left")
        timestamp = " ".join(value for value in (sent_date, time) if value)
        if timestamp:
            tk.Label(
                header, text=timestamp, bg=bubble_color, fg="#60788c",
                font=("Arial", 9),
            ).pack(side="right")
        if file_id and filename:
            tk.Button(
                bubble,
                text=filename,
                command=lambda: self._download_file(file_id, filename),
                bg=BUTTON,
                fg=TEXT,
                font=("Arial", 10, "underline"),
                bd=1,
                relief="solid",
                anchor="w",
                wraplength=MESSAGE_BUBBLE_WIDTH - 20,
            ).grid(row=1, column=0, sticky="ew", pady=(5, 0))
        else:
            tk.Label(
                bubble, text=text, bg=bubble_color, fg="#334f66",
                font=("Arial", 10), justify="left", anchor="w",
                wraplength=MESSAGE_BUBBLE_WIDTH,
            ).grid(row=1, column=0, sticky="ew", pady=(5, 0))
        return block

    def send_message(self):
        # Show text immediately, then complete both requests off the UI thread.
        text = self.message_text.get().strip()
        if self._send_in_flight or self._attachment_check is not None:
            return
        if not self.recipient:
            self.send_status.set("Choose a user before sending a message.")
            return
        if not text and self.attachment is None:
            self.send_status.set("Write a message or attach a file before sending.")
            self.message_entry.focus_set()
            return
        recipient = self.recipient
        generation = self._conversation_generation
        attachment = self.attachment
        self._send_in_flight = True
        self._set_send_controls(False)
        pending = None
        if text:
            if not self.current_messages:
                self._clear_messages()
            pending = self._add_message(self.username, text, "Sending...", False)
            self._scroll_to_latest()
        if text:
            self.message_text.set("")
        self.send_status.set("Sending...")
        if attachment is not None:
            self.attachment_progress.set(0)
            self.attachment_status.set(f"Uploading: {attachment['filename']} (0%)")

        def show_text_receipt(saved_message):
            if pending is not None and pending.winfo_exists():
                pending.destroy()
            if self.recipient == recipient and generation == self._conversation_generation:
                if self.current_messages is None or self._has_newer:
                    self._history_request = None
                    self.current_messages = []
                    self._has_newer = False
                    self._append_new_messages([saved_message], scroll_to_latest=True)
                    self._load_page("latest")
                else:
                    self._append_new_messages([saved_message], scroll_to_latest=True)

        def work():
            saved = []
            file_sent = False
            text_sent = False
            if text:
                response = self._send_text_message(recipient, text)
                if not response.get("success"):
                    return saved, file_sent, text_sent, response.get("message", "Message could not be sent.")
                text_sent = True
                if response.get("saved_message"):
                    saved.append(response["saved_message"])
                    if attachment is not None:
                        self._post_ui(show_text_receipt, response["saved_message"])
            if attachment is not None:
                response = self._send_attached_file(recipient, attachment)
                if not response.get("success"):
                    return saved, file_sent, text_sent, response.get("message", "The file could not be uploaded.")
                file_sent = True
                if response.get("saved_message"):
                    saved.append(response["saved_message"])
            return saved, file_sent, text_sent, ""

        def completed(result, error):
            self._send_in_flight = False
            self._set_send_controls(True)
            if pending is not None and pending.winfo_exists():
                pending.destroy()
            if error is not None:
                result = ([], False, False, "Could not reach the server. Try again.")
            saved, file_sent, text_sent, message = result
            if file_sent and self.attachment is attachment:
                self._clear_attachment()
            if text and not text_sent:
                self.message_text.set(text)
            if self.recipient == recipient and generation == self._conversation_generation:
                if (self.current_messages is None or self._has_newer) and saved:
                    # Show the receipt now, then reconcile the latest page.
                    self._history_request = None
                    self.current_messages = []
                    self._has_newer = False
                    self._append_new_messages(saved, scroll_to_latest=True)
                    self._load_page("latest")
                else:
                    self._append_new_messages(saved, scroll_to_latest=True)
                if not saved and not self.current_messages:
                    self._clear_messages()
                    self._add_message(self.recipient, "No messages yet.", "", True)
                self.send_status.set(message)
                self._poll_new_messages()

        self._run_background(work, completed)

    def _send_text_message(self, recipient, text):
        data = json.dumps(
            {"recipient": recipient, "content": text}
        ).encode("utf-8")
        request = Request(
            f"{self.server_url}/messages",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with open_server(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                return json.loads(error.read().decode("utf-8"))
            except json.JSONDecodeError:
                return {"success": False, "message": "Message could not be sent."}
        except (URLError, TimeoutError, json.JSONDecodeError):
            return {"success": False, "message": "Could not reach the server. Try again."}

    def attach_file(self):
        # Check the selected local file in the background; do not copy it.
        if not self.recipient:
            self.send_status.set("Choose a user before attaching a file.")
            return
        if self._send_in_flight or self._attachment_check is not None:
            return
        path = filedialog.askopenfilename(parent=self.root)
        if not path:
            return
        try:
            file_size = os.path.getsize(path)
        except OSError:
            self.send_status.set("Could not read that file.")
            return
        if file_size > MAXIMUM_FILE_BYTES:
            self.send_status.set("Files cannot be larger than 2 GB.")
            return
        check_token = object()
        self._attachment_check = check_token
        generation = self._conversation_generation
        self._set_send_controls(False)
        self.attachment_status.set(f"Checking: {os.path.basename(path)}")
        self.attachment_frame.grid()

        def checked(checksum, error):
            if self._attachment_check is not check_token:
                return
            self._attachment_check = None
            self._set_send_controls(True)
            if generation != self._conversation_generation:
                self._clear_attachment()
                return
            if error is not None:
                self._clear_attachment()
                self.send_status.set("Could not verify that file.")
                return
            self.attachment = {
                "path": path,
                "filename": os.path.basename(path),
                "size": file_size,
                "checksum": checksum,
            }
            self.attachment_progress.set(0)
            self.attachment_status.set(
                f"Attached: {self.attachment['filename']} ({self._file_size_label(file_size)}) — press Send"
            )
            self.send_status.set("")

        self._run_background(lambda: self._file_checksum(path), checked)

    def _send_attached_file(self, recipient, attachment):
        # This runs on a worker; progress is passed back to the UI queue.
        last_report = [0.0]

        def report(sent_bytes, total_bytes):
            now = time.monotonic()
            if sent_bytes < total_bytes and now - last_report[0] < 0.1:
                return
            last_report[0] = now
            self._post_ui(self._update_attachment_progress, sent_bytes, total_bytes, attachment)

        try:
            body = MultipartFileBody(
                recipient,
                attachment["filename"],
                attachment["path"],
                attachment["checksum"],
                report,
            )
        except OSError:
            return {"success": False, "message": "Could not read the attached file. Attach it again."}
        try:
            request = Request(
                f"{self.server_url}/files",
                data=body,
                headers={
                    "Content-Type": body.content_type,
                    "Content-Length": str(body.content_length),
                },
            )
            with open_server(request, timeout=FILE_TRANSFER_TIMEOUT_SECONDS) as response:
                response_body = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                return json.loads(error.read().decode("utf-8"))
            except json.JSONDecodeError:
                return {"success": False, "message": "The file could not be uploaded."}
        except (URLError, TimeoutError, json.JSONDecodeError):
            return {"success": False, "message": "Could not reach the server. Try again."}
        finally:
            body.close()
        if not response_body.get("success"):
            return response_body
        if response_body.get("checksum") != attachment["checksum"]:
            return {"success": False, "message": "The server did not confirm the file checksum."}
        return response_body

    def _update_attachment_progress(self, sent_bytes, total_bytes, attachment):
        if self.attachment is not attachment or not self._send_in_flight:
            return
        percentage = 100 if not total_bytes else sent_bytes * 100 / total_bytes
        self.attachment_progress.set(percentage)
        self.attachment_status.set(
            f"Uploading: {attachment['filename']} ({percentage:.0f}%)"
        )

    def _clear_attachment(self):
        self.attachment = None
        self.attachment_progress.set(0)
        self.attachment_status.set("")
        self.attachment_frame.grid_remove()

    def _set_send_controls(self, enabled):
        state = "normal" if enabled else "disabled"
        self.message_entry.configure(state=state)
        self.send_button.configure(state=state)
        self.attach_button.configure(state=state)
        self.remove_attachment_button.configure(state=state)

    @staticmethod
    def _file_size_label(size):
        if size >= 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024 * 1024):.1f} GB"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    @staticmethod
    def _file_checksum(path):
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _download_file(self, file_id, filename):
        # Received attachments stay on the server until this button is clicked.
        if self._download_in_flight:
            return
        target = filedialog.asksaveasfilename(parent=self.root, initialfile=filename)
        if not target:
            return
        self._download_in_flight = True
        self.send_status.set(f"Downloading {filename}...")

        def finished(_result, error):
            self._download_in_flight = False
            self.send_status.set(
                "The file could not be downloaded or verified." if error is not None else ""
            )

        self._run_background(lambda: self._download_to_path(file_id, target), finished)

    def _download_to_path(self, file_id, target):
        request = Request(f"{self.server_url}/files/{quote(file_id)}")
        temporary_path = None
        try:
            with open_server(request, timeout=FILE_TRANSFER_TIMEOUT_SECONDS) as response:
                checksum = response.headers.get("X-Content-SHA256", "").lower()
                if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
                    raise OSError("The server did not provide a valid file checksum.")
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix=".bluebubbles-download-", suffix=".tmp", dir=Path(target).parent
                )
                digest = hashlib.sha256()
                with os.fdopen(descriptor, "wb") as downloaded_file:
                    while chunk := response.read(1024 * 1024):
                        digest.update(chunk)
                        downloaded_file.write(chunk)
                if digest.hexdigest() != checksum:
                    raise OSError("Downloaded file checksum does not match.")
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path:
                Path(temporary_path).unlink(missing_ok=True)

    def _send_from_enter(self, event):
        # Send the message when Enter is pressed in the message field.
        self.send_message()
        return "break"
