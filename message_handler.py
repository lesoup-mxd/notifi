import os
import sqlite3
import time
from datetime import datetime
import json
import requests
import subprocess
import tempfile

from telethon import TelegramClient

# Telegram API credentials
API_ID = "YOUR_API_ID"
API_HASH = "YOUR_API_HASH"
SESSION_NAME = "my_account"
DB_DIR = "chat_databases"
DEBUG = True
MESSAGE_HISTORY_LIMIT = 120  # Keep this many messages per chat

# Add these constants at the top with your other constants
LLM_ENDPOINT = "http://localhost:8000/v1/chat/completions"
LLM_MODEL = "tiiuae/Falcon3-1B-Instruct"
LLM_TEMPERATURE = 0.0

def get_db_path(chat_id):
    """Returns the database file path for a given chat ID."""
    return os.path.join(DB_DIR, f"chat_{chat_id}.db")

def initialize_db(chat_id):
    """Creates a new database for the chat if it doesn't exist."""
    db_path = get_db_path(chat_id)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Add telegram_id to store actual Telegram message IDs
    # Add message_date to track when messages were sent on Telegram
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        message_date TIMESTAMP,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        sender TEXT,
        message TEXT
    )""")
    
    # Create table to track last sync info
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sync_info (
        last_sync_time TIMESTAMP,
        last_telegram_id INTEGER
    )""")
    
    # Initialize sync info if empty
    cursor.execute("SELECT COUNT(*) FROM sync_info")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO sync_info VALUES (?, ?)",
                      (datetime.now().timestamp(), 0))

    conn.commit()
    conn.close()

def store_message(chat_id, telegram_id, message_date, sender, message):
    """Stores a new message in the database."""
    db_path = get_db_path(chat_id)

    if not os.path.exists(db_path):
        initialize_db(chat_id)  # Create DB if missing

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if message already exists (avoid duplicates)
    cursor.execute("SELECT COUNT(*) FROM messages WHERE telegram_id = ?", (telegram_id,))
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO messages (telegram_id, message_date, sender, message) VALUES (?, ?, ?, ?)", 
            (telegram_id, message_date.timestamp(), sender, message)
        )
        conn.commit()
    
    conn.close()

def update_sync_info(chat_id, last_telegram_id):
    """Updates the sync information for a chat."""
    db_path = get_db_path(chat_id)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute(
        "UPDATE sync_info SET last_sync_time = ?, last_telegram_id = ?",
        (datetime.now().timestamp(), last_telegram_id)
    )
    
    conn.commit()
    conn.close()

def get_sync_info(chat_id):
    """Gets the last sync information for a chat."""
    db_path = get_db_path(chat_id)
    
    if not os.path.exists(db_path):
        initialize_db(chat_id)
        return 0
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT last_telegram_id FROM sync_info")
    result = cursor.fetchone()
    last_telegram_id = result[0] if result else 0
    
    conn.close()
    return last_telegram_id

def cleanup_old_messages(chat_id):
    """Deletes messages beyond the history limit."""
    db_path = get_db_path(chat_id)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Count total messages
    cursor.execute("SELECT COUNT(*) FROM messages")
    count = cursor.fetchone()[0]
    
    # Delete oldest messages if we have more than the limit
    if count > MESSAGE_HISTORY_LIMIT:
        to_delete = count - MESSAGE_HISTORY_LIMIT
        if DEBUG: print(f"Deleting {to_delete} old messages from chat {chat_id}")
        cursor.execute("""
            DELETE FROM messages 
            WHERE id IN (
                SELECT id FROM messages 
                ORDER BY message_date ASC 
                LIMIT ?
            )
        """, (to_delete,))
        conn.commit()
        
    conn.close()

def get_recent_messages(chat_id, limit=MESSAGE_HISTORY_LIMIT):
    """Gets the most recent messages from a specific chat."""
    db_path = get_db_path(chat_id)
    
    if not os.path.exists(db_path):
        return []
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get most recent messages first
    cursor.execute("""
        SELECT message_date, sender, message 
        FROM messages 
        ORDER BY message_date DESC
        LIMIT ?
    """, (limit,))
    
    messages = cursor.fetchall()
    conn.close()
    
    # Return in chronological order (oldest first)
    return list(reversed(messages))

# Modify fetch_messages to accept an existing client
async def fetch_messages(client=None):
    """Fetches messages from all active chats and stores them."""
    total_updates = 0
    total_chats = 0
    
    # Check if we need to create a client or use the existing one
    if client is None:
        # Convert API_ID to integer if it's from environment variable
        api_id = int(API_ID) if API_ID else API_ID
        
        if not api_id or not API_HASH:
            raise ValueError("API_ID and API_HASH must be set properly")
            
        # Create our own client since none was provided
        own_client = True
        client = TelegramClient(SESSION_NAME, api_id, API_HASH)
        await client.start()
    else:
        # Use the provided client
        own_client = False
    
    try:
        dialogs = await client.get_dialogs()  # Get all chats

        # Create the database directory if it doesn't exist
        if not os.path.exists(DB_DIR):
            os.makedirs(DB_DIR, exist_ok=True)

        for dialog in dialogs:
            total_chats += 1
            chat_id = dialog.id
            
            if not hasattr(dialog, 'title'):
                print("No title available, skipping")
                continue
            
            # Get last known message ID for this chat
            last_telegram_id = get_sync_info(chat_id)
            
            # Fetch new messages since last sync
            newest_telegram_id = last_telegram_id
            message_count = 0
            
            # Use reverse=True to get newest messages first
            async for message in client.iter_messages(chat_id, min_id=last_telegram_id, limit=MESSAGE_HISTORY_LIMIT):
                if message.text:
                    sender = message.sender_id or "Unknown"
                    store_message(chat_id, message.id, message.date, sender, message.text)
                    message_count += 1
                    # Keep track of the newest message ID
                    newest_telegram_id = max(newest_telegram_id, message.id)
            
            # Update sync information with newest message ID
            if newest_telegram_id > last_telegram_id:
                if DEBUG:
                    print(f"Updating sync info for chat {chat_id}: last_telegram_id = {newest_telegram_id}")
                update_sync_info(chat_id, newest_telegram_id)
                total_updates += 1
            
            # Clean up old messages to maintain history limit
            cleanup_old_messages(chat_id)
            
        if DEBUG:
            print(f"Total chats processed: {total_chats}")
        if DEBUG:
            print(f"Total updates made: {total_updates}")
            
    finally:
        # Only disconnect if we created our own client
        if own_client:
            await client.disconnect()

async def fetch_unread_messages(client=None):
    """Fetches only unread messages from all active chats and stores them."""
    total_updates = 0
    total_chats = 0
    
    # Check if we need to create a client or use the existing one
    if client is None:
        # Convert API_ID to integer if it's from environment variable
        api_id = int(API_ID) if API_ID else API_ID
        
        if not api_id or not API_HASH:
            raise ValueError("API_ID and API_HASH must be set properly")
            
        # Create our own client since none was provided
        own_client = True
        client = TelegramClient(SESSION_NAME, api_id, API_HASH)
        await client.start()
    else:
        # Use the provided client
        own_client = False
    
    try:
        dialogs = await client.get_dialogs()  # Get all chats

        # Create the database directory if it doesn't exist
        if not os.path.exists(DB_DIR):
            os.makedirs(DB_DIR, exist_ok=True)

        for dialog in dialogs:
            if dialog.unread_count == 0:
                continue  # Skip chats with no unread messages
                
            total_chats += 1
            chat_id = dialog.id
            
            if not hasattr(dialog, 'title'):
                print("No title available, skipping")
                continue
                
            print(f"Fetching {dialog.unread_count} unread messages from: {dialog.title} ({chat_id})")
            
            # Fetch only unread messages
            message_count = 0
            newest_telegram_id = get_sync_info(chat_id)
            
            # Get unread messages
            async for message in client.iter_messages(chat_id, limit=dialog.unread_count):
                if message.text:
                    sender = message.sender_id or "Unknown"
                    store_message(chat_id, message.id, message.date, sender, message.text)
                    message_count += 1
                    # Keep track of the newest message ID
                    newest_telegram_id = max(newest_telegram_id, message.id)
            
            # Update sync information with newest message ID
            if message_count > 0:
                update_sync_info(chat_id, newest_telegram_id)
                total_updates += 1
            
            # Mark messages as read
            await client.send_read_acknowledge(dialog)
            
        if DEBUG:
            print(f"Total chats with unread messages: {total_chats}")
            print(f"Total updates made: {total_updates}")
            
    finally:
        # Only disconnect if we created our own client
        if own_client:
            await client.disconnect()

async def get_unread_messages_for_chat(client, chat_id):
    """Gets only the unread messages for a specific chat."""
    dialog = None
    
    # First find the dialog for this chat_id
    async for d in client.iter_dialogs():
        if d.id == chat_id:
            dialog = d
            break
            
    if not dialog or dialog.unread_count == 0:
        return []
        
    print(f"Found {dialog.unread_count} unread messages in {dialog.title}")
    
    messages = []
    # Fetch unread messages
    async for message in client.iter_messages(chat_id, limit=dialog.unread_count):
        if message.text:
            sender = message.sender_id or "Unknown"
            # Instead of storing, just collect in memory
            messages.append((message.date.timestamp(), sender, message.text, message.id))
            
    # Return messages in chronological order (oldest first)
    return sorted(messages, key=lambda x: x[0])[:MESSAGE_HISTORY_LIMIT]
async def summarize_unread(client, chat_id):
    """Summarizes only the unread messages from a specific chat."""
    unread_messages = await get_unread_messages_for_chat(client, chat_id)
    
    
    if not unread_messages:
        print(f"No unread messages found for chat {chat_id}")
        return "No unread messages to summarize."
    
    # Format the chat history for context
    chat_context = "\n".join([
        f"[{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}] {sender}: {msg}" 
        for ts, sender, msg, _ in unread_messages
    ])
    
    # Prepare the full prompt
    prompt = f"Here are the unread messages that need summarizing:\n\n{chat_context}\n\n"
    prompt += "Please provide a concise summary of these unread messages, highlighting any important information or action items."
    
    print("\n--- Generating summary of unread messages ---\n")
    
    # Use the LLM to generate a summary
    summary = await process_with_llm_async(prompt)
    
    # Now mark messages as read
    await client.send_read_acknowledge(chat_id)
    
    return summary

async def generate_reply(client, chat_id):
    """Generates a reply based on recent messages and allows editing before sending."""
    # Get recent messages for context
    recent_messages = get_recent_messages(chat_id, limit=MESSAGE_HISTORY_LIMIT)
    
    if not recent_messages:
        return "No messages found to generate a reply for."
    
    # Format the chat history for context
    chat_context = "\n".join([
        f"[{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}] {sender}: {msg}" 
        for ts, sender, msg in recent_messages
    ])
    
    # Prepare the prompt for reply generation
    prompt = f"Here are the recent messages in this conversation:\n\n{chat_context}\n\n"
    prompt += "Please generate an appropriate reply to continue this conversation naturally."
    
    print("\n--- Generating suggested reply ---\n")
    
    # Use the LLM to generate a reply
    suggested_reply = await process_with_llm_async(prompt)
    
    print("\n--- Suggested reply ---\n")
    print(suggested_reply)
    print("\n--- You can now edit this reply. Press Ctrl+D when finished ---\n")
    
    # Save suggested reply to a temporary file
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="w+", delete=False) as tmp:
        tmp_path = tmp.name
        tmp.write(suggested_reply)
    
    try:
        # Open the editor for user to modify
        editor = os.environ.get("EDITOR", "nano")
        subprocess.run([editor, tmp_path])
        
        # Read back the edited reply
        with open(tmp_path, "r") as tmp:
            edited_reply = tmp.read().strip()
        
        # Confirm send
        print("\n--- Your edited reply ---\n")
        print(edited_reply)
        confirm = input("\nSend this reply? (y/n): ")
        
        if confirm.lower() == 'y':
            # Send the message
            await client.send_message(chat_id, edited_reply)
            print("Reply sent successfully!")
            return edited_reply
        else:
            print("Reply cancelled.")
            return None
    finally:
        # Clean up
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

async def process_with_llm_async(prompt):
    """Async version of process_with_llm that works with a direct prompt."""
    # Ensure prompt is never None
    if prompt is None:
        prompt = "Please analyze the recent messages."
    
    # Prepare the curl command for streaming
    curl_cmd = [
        "curl", "-sN", LLM_ENDPOINT,
        "-H", "Content-Type: application/json",
        "-d", json.dumps({
            "model": LLM_MODEL,
            "stream": True,
            "messages": [
                {"role": "system", "content": "You are an intelligent message analysis assistant that helps users understand their chat history.\nYou either summarize messages or generate replies.\n When analyzing messages:\n- Focus on factual content and key information\n- Note who said what when it's relevant\n- Identify any tasks, deadlines, or commitments mentioned\n- Highlight questions that were asked but not answered\n\nBe concise but thorough in your responses. Present information in an organized manner with clear sections when appropriate.\n\nWhen generating replies:\n- Generate a natural and appropriate response based on the context of the conversation.\n- Ensure the reply is relevant to the most recent messages and has no extra text.\n\n974218208 is the chat ID of the user you are assisting."},
                {"role": "user", "content": prompt}
            ],
            "temperature": LLM_TEMPERATURE
        })
    ]
    
    # Instead of returning the raw response, we'll process it line by line and collect the content
    collected_response = ""
    
    try:
        # Use Popen to stream the output in real-time
        process = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE, text=True)
        
        for line in process.stdout:
            line = line.strip()
            if not line or line == "data: [DONE]":
                continue
                
            if line.startswith("data:"):
                try:
                    # Extract just the JSON part after "data: "
                    json_data = json.loads(line[5:].strip())
                    
                    if 'choices' in json_data and json_data['choices']:
                        content = json_data['choices'][0].get('delta', {}).get('content', '')
                        if content:
                            # Print content directly without newlines to simulate streaming
                            print(content, end='', flush=True)
                            collected_response += content
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    print(f"Error parsing JSON: {str(e)}, line: {line}")
        
        # Add a final newline
        print("\n")
        return collected_response
        
    except Exception as e:
        error_msg = f"Error processing messages: {str(e)}"
        print(error_msg)
        return error_msg

async def summarize_all_unread(client):
    """Summarizes unread messages from all chats."""
    print("Checking all chats for unread messages...")
    
    # Get all dialogs with unread messages
    dialogs_with_unread = []
    async for dialog in client.iter_dialogs():
        if dialog.unread_count > 0:
            dialogs_with_unread.append(dialog)
    
    if not dialogs_with_unread:
        return "No unread messages in any chats."
    
    print(f"Found {len(dialogs_with_unread)} chats with unread messages.")
    
    # Generate summaries for each chat with unread messages
    all_summaries = []
    
    for dialog in dialogs_with_unread:
        chat_id = dialog.id
        chat_title = dialog.title if hasattr(dialog, 'title') else f"Chat {chat_id}"
        
        print(f"\nProcessing: {chat_title} ({dialog.unread_count} unread messages)")
        
        # Get unread messages for this chat
        unread_messages = await get_unread_messages_for_chat(client, chat_id)
        
        if not unread_messages:
            continue
        
        # Format the chat context
        chat_context = "\n".join([
            f"[{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}] {sender}: {msg}" 
            for ts, sender, msg, _ in unread_messages
        ])
        
        # Prepare the prompt for this chat
        prompt = f"Here are {len(unread_messages)} unread messages from '{chat_title}':\n\n{chat_context}\n\n"
        prompt += "Please provide a brief but informative summary of these messages, highlighting important points."
        
        print(f"Generating summary for '{chat_title}'...")
        
        # Get summary for this chat
        chat_summary = await process_with_llm_async(prompt)
        
        # Mark messages as read
        await client.send_read_acknowledge(chat_id)
        
        # Add to compiled summaries
        all_summaries.append(f"## {chat_title} ({dialog.unread_count} messages)\n\n{chat_summary}\n")
    
    # Combine all summaries
    return "\n\n".join(all_summaries)

# Example function to use in your main script
async def analyze_chat(chat_id, query=None):
    """Fetches messages and analyzes them with the LLM."""
    # Pass the existing client instead of creating a new one
    #await fetch_messages(client)
    # Then process with LLM
    result = await process_with_llm_async(query)

if __name__ == "__main__":
    import asyncio
    asyncio.run(fetch_messages())
